"""AB-COM a Ellex: detail obchodu doplní fotku, další stažení ji uloží, /img pipeline ji umí."""
from pathlib import Path

from lovceny import images
from lovceny.shops import SHOPS
from lovceny.shops.scrape import og_image, parse_product
from lovceny.shops.search import abcom_detail_image, parse_abcom_detail, parse_ellexgk_detail

FIX = Path(__file__).parent / "fixtures"
ABCOM_IMG = "https://www.ab-com.cz/content/images/product/default/2003681.jpg"
ELLEX_IMG = "https://sony-center.s3.cdn-upgates.com/_cache/f/7/f7521d0d6d86e6b830b9621adda3ca9b-ps5slim1tb-05-jpg.jpg"


def test_abcom_detail_image_ignores_placeholder_and_related():
    page = (FIX / "abcom_detail.html").read_text(encoding="utf-8")
    assert abcom_detail_image(page) == ABCOM_IMG
    # obecný parser cenu vidí, fotku ne (src je 1×1 gif, ne data-original)
    bare = parse_product(page)
    assert bare is not None and bare.price == 14490
    assert not bare.image or bare.image.startswith("data:")
    res = parse_abcom_detail(page)
    assert res.price == 14490 and res.image == ABCOM_IMG
    assert SHOPS["abcom"].page_parser(page).image == ABCOM_IMG


def test_ellex_detail_uses_og_image_not_shop_logo():
    page = (FIX / "ellexgk_detail.html").read_text(encoding="utf-8")
    assert og_image(page) == ELLEX_IMG
    assert "logo" not in ELLEX_IMG
    bare = parse_product(page)
    assert bare.price == 16090 and not bare.image
    res = parse_ellexgk_detail(page)
    assert res.price == 16090 and res.image == ELLEX_IMG
    assert SHOPS["ellexgk"].page_parser(page).image == ELLEX_IMG


def test_shop_images_go_through_wsrv_not_blocked():
    for url in (ABCOM_IMG, ELLEX_IMG):
        assert images.is_allowed(url)
        assert not images.wsrv_blocked(url)
        attrs = images.srcset(url, 60)
        assert attrs["src"].startswith("https://wsrv.nl/?url=")
        assert attrs["fallback"].startswith("/img/")
        assert url.split("/", 3)[2] in attrs["orig"]


def test_update_offer_stores_image_without_changing_identity(client_db, monkeypatch):
    from lovceny import service
    client, db, make = client_db
    html = (FIX / "abcom_detail.html").read_text(encoding="utf-8")

    class Fake:
        def get(self, url):
            return html

    monkeypatch.setattr(service, "client", lambda: Fake())
    p = make([10000], name="AEG pračka LTR6A41460", shop="abcom", category="pracky")
    p.slug = "aeg-pracka-ltr6a41460"
    p.image_url = None
    o = p.offers[0]
    o.shop = "abcom"
    o.url = "https://www.ab-com.cz/aeg-pracka-ltr6a41460/"
    db.commit()
    assert service.update_offer(db, o) == "ok"
    db.commit()
    db.refresh(p)
    assert p.image_url == ABCOM_IMG
    assert p.slug == "aeg-pracka-ltr6a41460" and p.name == "AEG pračka LTR6A41460"
    assert float(o.price) == 14490
    html_page = client.get(f"/produkt/{p.id}-{p.slug}").text
    assert "wsrv.nl" in html_page and 'sizes="120px"' in html_page
    assert "img-missing" in html_page  # skrytý fallback, kdyby wsrv selhal
    assert 'aria-label="Fotka produktu chybí"' in html_page


def test_product_page_missing_image_uses_framed_placeholder(client_db):
    client, _, make = client_db
    p = make([500], name="Bez fotky detail", category="pracky")
    p.image_url = None
    html = client.get(f"/produkt/{p.id}-{p.slug}").text
    assert 'class="p-img"' in html and 'aria-label="Fotka produktu chybí"' in html
    slot = html.split('class="p-img"', 1)[1].split("</div>", 1)[0]
    assert "<img " not in slot and "img-missing" in slot
    css = (Path(__file__).resolve().parents[1] / "lovceny/static/app.css").read_text(encoding="utf-8")
    assert ".product .p-head .p-img{width:120px;height:120px;" in css
    assert ".product .p-head .img-missing svg{width:42px;height:42px}" in css
