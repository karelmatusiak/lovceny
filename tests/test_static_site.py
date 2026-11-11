"""Statický generátor: stejné URL a pravidla jako živý web, bez databáze pro návštěvníka."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from lovceny.categories import is_accessory_text
from lovceny.main import app, tpl
from lovceny.models import Offer, PricePoint, Product
from lovceny.seo import robots_txt, sitemap_xml
from lovceny.staticsite import _Redirects, build, norm_offer_url, rewrite_img_proxy, rewrite_pagination, write_redirects
from lovceny.util import slugify


def test_preview_build_does_not_migrate(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("lovceny.db.init_db", lambda: calls.append("migrate"))
    monkeypatch.setattr(
        "lovceny.staticsite.build",
        lambda *args, **kwargs: {
            "pages": 1, "files": 1, "bytes": 1, "seconds": 0, "products": 0,
            "search_index_bytes": 0, "redirect_mode": "file", "affiliate": [],
            "url_changes": ["x"],
        },
    )
    from lovceny.staticsite import main
    assert main(["--out", str(tmp_path / "dist"), "--report", str(tmp_path / "report.json")]) == 0
    assert calls == []
    assert main(["--migrate", "--out", str(tmp_path / "dist2"), "--report", str(tmp_path / "report2.json")]) == 0
    assert calls == ["migrate"]


def test_rewrites_keep_pagination_and_original_photos():
    html = '<a href="https://lovceny.cz/kategorie/telefony?strana=2">2</a>'
    assert rewrite_pagination(html) == '<a href="https://lovceny.cz/kategorie/telefony/strana/2">2</a>'
    proxied = '/img/320.webp?u=https%3A%2F%2Fdata.planeo.cz%2Ffoto.jpg'
    assert rewrite_img_proxy(proxied) == "https://data.planeo.cz/foto.jpg"
    assert norm_offer_url("https://www.alza.cz/iphone?utm_source=x&idp=1") == "alza.cz/iphone?idp=1"


def test_redirect_overflow_uses_a_pages_function(tmp_path):
    book = _Redirects()
    book.add("/kontakt", "/o-nas#kontakt", 302)
    book.add("/pes/*", "/bez-serveru", 301, dynamic=True)
    for i in range(4):
        book.gone[str(i)] = "/kategorie/telefony"
        book.gone[f"{i}-stary"] = "/kategorie/telefony"
    mode = write_redirects(tmp_path, book, static_limit=3)
    assert mode == "function"
    text = (tmp_path / "_redirects").read_text()
    assert "/kontakt /o-nas#kontakt 302" in text
    assert "/produkt/0 " not in text
    script = (tmp_path / "functions" / "produkt" / "[[path]].js").read_text()
    assert '"0":"/kategorie/telefony"' in script or '"0": "/kategorie/telefony"' in script
    routes = json.loads((tmp_path / "_routes.json").read_text())
    assert routes["include"] == ["/produkt/*"]


def _offer(product, shop, url, price, *, stock=True, when=None, days=8):
    now = when or datetime.now(timezone.utc)
    offer = Offer(
        product=product, shop=shop, url=url, price=price, in_stock=stock,
        status="ok", last_checked=now, last_ok_at=now,
    )
    for day in range(days):
        offer.points.append(PricePoint(
            price=price, captured_at=now - timedelta(days=days - 1 - day), is_demo=False,
        ))
    return offer


def test_build_matches_live_urls_and_hides_unsafe_offers(client_db, tmp_path, monkeypatch):
    client, db, _make = client_db
    monkeypatch.setenv("ALZA_AFFILIATE_ID", "testovaci")
    now = datetime.now(timezone.utc)
    good = Product(
        name="Samsung Galaxy A55 128GB", slug=slugify("Samsung Galaxy A55 128GB"),
        category_slug="telefony", category="Mobilní telefony", ean="8806095498765",
        image_url="https://data.planeo.cz/foto/a55.jpg",
    )
    thin = Product(
        name="Samsung Galaxy A35 tenká historie", slug="samsung-galaxy-a35-tenka",
        category_slug="telefony", category="Mobilní telefony",
    )
    oos = Product(
        name="Samsung Galaxy A15 není skladem", slug="samsung-galaxy-a15-oos",
        category_slug="telefony", category="Mobilní telefony",
    )
    stale = Product(
        name="Samsung Galaxy A25 neověřená cena", slug="samsung-galaxy-a25-neoverena",
        category_slug="telefony", category="Mobilní telefony",
    )
    accessory_name = "Samsung Galaxy A55 silikonový kryt"
    assert is_accessory_text(accessory_name)
    accessory = Product(
        name=accessory_name, slug="samsung-galaxy-a55-kryt",
        category_slug="telefony", category="Mobilní telefony",
    )
    laptop = Product(
        name="Gigabyte GAMING A16 PRO notebook 16GB", slug="gigabyte-a16-pro",
        category_slug="notebooky", category="Notebooky",
    )
    gpu = Product(
        name="Gigabyte GeForce RTX 4070", slug="gigabyte-rtx-4070",
        category_slug="notebooky", category="Notebooky",
        retired_at=now,
    )
    perfume = Product(
        name="Karl Lagerfeld parfém", slug="karl-lagerfeld-parfem",
        category_slug="telefony", category="Mobilní telefony",
        retired_at=now,
    )
    hidden = Product(
        name="Skrytý iPhone 16", slug="skryty-iphone-16",
        category_slug="telefony", category="Mobilní telefony", catalog_hidden=True,
    )
    review = Product(
        name="Nejistý iPhone 16e", slug="nejisty-iphone-16e",
        category_slug="telefony", category="Mobilní telefony", needs_review=True,
    )
    merged = Product(
        name="Samsung Galaxy A55 staré URL", slug="samsung-galaxy-a55-stare",
        category_slug="telefony", category="Mobilní telefony", retired_at=now,
    )
    db.add_all([good, thin, oos, stale, accessory, laptop, gpu, perfume, hidden, review, merged])
    db.flush()
    merged.redirect_product_id = good.id
    db.add_all([
        _offer(good, "alza", "https://www.alza.cz/samsung-galaxy-a55", 8990),
        _offer(thin, "alza", "https://www.alza.cz/samsung-galaxy-a35", 7990, days=2),
        _offer(oos, "alza", "https://www.alza.cz/samsung-galaxy-a15", 4990, stock=False),
        _offer(stale, "alza", "https://www.alza.cz/samsung-galaxy-a25", 6990, when=now - timedelta(days=10)),
        _offer(accessory, "alza", "https://www.alza.cz/kryt-a55", 299),
        _offer(laptop, "alza", "https://www.alza.cz/gigabyte-a16", 24990),
        _offer(review, "alza", "https://www.alza.cz/iphone-16e", 19990),
    ])
    extra = []
    for i in range(30):
        item = Product(
            name=f"Samsung Galaxy A54 varianta {i}", slug=f"samsung-galaxy-a54-{i}",
            category_slug="telefony", category="Mobilní telefony",
        )
        extra.append(item)
    db.add_all(extra)
    db.flush()
    for i, item in enumerate(extra):
        db.add(_offer(item, "mironet", f"https://www.mironet.cz/a54-{i}", 20000 + i))
    db.commit()

    out = tmp_path / "site"
    report = build(out)
    assert tpl.env.globals["STATIC_SITE"] is False
    assert report["pages"] > 40
    assert report["files"] > report["pages"]
    assert report["bytes"] > 50_000
    assert report["seconds"] > 0
    assert report["seconds"] < 120
    assert report["redirect_mode"] == "file"
    alza = next(row for row in report["affiliate"] if row["shop"] == "Alza")
    assert alza["wrapper"] == "ALZA_AFFILIATE_ID" and alza["wrapped"] >= 1 and alza["plain"] == 0
    assert report["search_index_bytes"] < 200_000
    assert report["url_changes"]
    assert any("Hlídání ceny" in line for line in report["not_published"])

    good_path = out / "produkt" / f"{good.id}-{good.slug}" / "index.html"
    good_html = good_path.read_text()
    assert "btn-buy" in good_html
    assert "nofollow sponsored" in good_html
    assert "idp=testovaci" in good_html
    assert "<svg" in good_html
    assert "https://data.planeo.cz/foto/a55.jpg" in good_html
    assert "/img/" not in good_html.replace("/static/img/", "")
    assert 'action="/produkt/' not in good_html

    thin_html = (out / "produkt" / f"{thin.id}-{thin.slug}" / "index.html").read_text()
    assert "v-none" in thin_html and "v-buy" not in thin_html
    oos_html = (out / "produkt" / f"{oos.id}-{oos.slug}" / "index.html").read_text()
    assert "btn-buy" not in oos_html and "Momentálně není skladem" in oos_html
    stale_html = (out / "produkt" / f"{stale.id}-{stale.slug}" / "index.html").read_text()
    assert "btn-buy" not in stale_html and "Cena neověřena" in stale_html

    phones = (out / "kategorie" / "telefony" / "index.html").read_text()
    assert "Galaxy A55 128GB" in phones or "Galaxy A55" in phones
    assert "silikonový kryt" not in phones
    assert "Lagerfeld" not in phones
    assert "GeForce" not in phones
    assert "?strana=" not in phones
    assert f"/kategorie/telefony/strana/2" in phones
    page2 = (out / "kategorie" / "telefony" / "strana" / "2" / "index.html").read_text()
    assert "A54" in page2
    notebooks = (out / "kategorie" / "notebooky" / "index.html").read_text()
    assert "A16" in notebooks and "RTX 4070" not in notebooks

    index = json.loads((out / "data" / "katalog.json").read_text())
    names = {row["n"] for row in index}
    assert "Samsung Galaxy A55 128GB" in names
    assert "Karl Lagerfeld parfém" not in names
    assert "Gigabyte GeForce RTX 4070" not in names
    assert "Nejistý iPhone 16e" not in names
    assert "Skrytý iPhone 16" not in names
    kryt = next(row for row in index if row["n"] == accessory_name)
    assert kryt["a"] == 1 and "h" in kryt and "nofollow sponsored" in kryt["r"]
    urls = json.loads((out / "data" / "url.json").read_text())
    assert urls[norm_offer_url("https://www.alza.cz/samsung-galaxy-a55")] == f"/produkt/{good.id}-{good.slug}"

    assert not (out / "admin").exists()
    assert not (out / "produkt" / f"{hidden.id}-{hidden.slug}").exists()
    assert not (out / "produkt" / f"{perfume.id}-{perfume.slug}").exists()
    review_html = (out / "produkt" / f"{review.id}-{review.slug}" / "index.html").read_text()
    assert "noindex" in review_html and "btn-buy" not in review_html

    sitemap = (out / "sitemap.xml").read_text()
    assert sitemap == sitemap_xml(db)
    assert f"/produkt/{good.id}-" in sitemap
    assert f"/produkt/{perfume.id}-" not in sitemap
    assert f"/produkt/{review.id}-" not in sitemap
    assert (out / "robots.txt").read_text() == robots_txt()
    assert (out / "hledat" / "index.html").read_text().count("katalog.js") == 1
    assert "/hledat/zive" not in (out / "hledat" / "index.html").read_text()
    assert (out / "stav" / "index.html").exists()
    assert (out / "slevy" / "index.html").exists()
    assert (out / "black-friday" / "index.html").exists()
    assert (out / "bez-serveru" / "index.html").exists()
    assert "mailto:info@lovceny.cz" in (out / "o-nas" / "index.html").read_text()
    assert 'action="/kontakt"' not in (out / "o-nas" / "index.html").read_text()
    assert (out / "404.html").exists()
    assert (out / "sw.js").exists()

    redirects = (out / "_redirects").read_text()
    live = TestClient(app)
    for path in ("/kontakt", "/metodika", "/bot", "/pro-e-shopy", "/podminky-uzivani",
                 "/porovnani", "/prehled", "/rozsireni", "/hlidat-rozpocet",
                 f"/produkt/{perfume.id}-{perfume.slug}", f"/produkt/{perfume.id}",
                 f"/produkt/{merged.id}-{merged.slug}"):
        response = live.get(path, follow_redirects=False)
        assert response.status_code in (301, 302), path
        assert f"{path} {response.headers['location']} {response.status_code}" in redirects
    bare = live.get(f"/produkt/{good.id}")
    assert bare.status_code == 200
    assert f"/produkt/{good.id} /produkt/{good.id}-{good.slug} 301" in redirects

    assert f"/produkt/{merged.id}-{merged.slug} /produkt/{good.id}-{good.slug} 301" in redirects
    assert "/pes/* /bez-serveru 301" in redirects
    assert "/hlidani/* /bez-serveru 301" in redirects
