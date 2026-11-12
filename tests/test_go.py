"""První hop /go/<id> a úklid prázdných parametrů, které utekly z feedu."""
import json
from urllib.parse import parse_qs, unquote, urlparse

from lovceny.go import (
    GO_FUNCTION_JS,
    RESOLVE_AFFILIATE,
    fallback_page,
    merge_routes,
    safe_http_url,
    write_go_assets,
)
from lovceny.shops import get_shop
from lovceny.shops.feed import strip_junk_query

SVC = "https://www.bscom.cz/tycovy-vysavac-sencor-svc-8618gd-clean-go-svc8618gd_d736595/?GO="
KM = "https://www.bscom.cz/televizor-kruger-matz-km0240fhd-s6-smart-tv-40-06438415_d2519623/?+MATZ+KM0240FHD-S6+SMART+TV+40%22="
F8 = "https://www.bscom.cz/sluchatka-bluetooth-kruger-matz-f8a-black-05560909_d2519393/?+MATZ+F8A+Black="
VID = "https://www.bscom.cz/bezdratova-sluchatka-apple-airpods-max-2-mhwk4zm-a_d2549058?vid=2546520"
SVC_CLEAN = "https://www.bscom.cz/tycovy-vysavac-sencor-svc-8618gd-clean-go-svc8618gd_d736595/"
KM_CLEAN = "https://www.bscom.cz/televizor-kruger-matz-km0240fhd-s6-smart-tv-40-06438415_d2519623/"
F8_CLEAN = "https://www.bscom.cz/sluchatka-bluetooth-kruger-matz-f8a-black-05560909_d2519393/"


def _dest(url: str) -> str:
    return unquote(parse_qs(urlparse(url).query)["desturl"][0])


def test_strip_junk_query_keeps_real_params():
    assert strip_junk_query(SVC) == SVC_CLEAN
    assert strip_junk_query(KM) == KM_CLEAN
    assert strip_junk_query(F8) == F8_CLEAN
    assert strip_junk_query(VID) == VID
    assert strip_junk_query(VID + "&GO=") == VID
    assert strip_junk_query("https://shop.example/p?ref=") == "https://shop.example/p?ref="
    valued = "https://www.bscom.cz/x/?+MATZ+F8A+Black=1"
    assert strip_junk_query(valued) == valued
    assert strip_junk_query("https://www.bscom.cz/x") == "https://www.bscom.cz/x"


def test_affiliate_wrap_uses_cleaned_dest(monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    shop = get_shop("bscom")
    wrapped = shop.affiliate_link(SVC)
    assert _dest(wrapped) == SVC_CLEAN
    assert "GO=" not in _dest(wrapped)
    assert _dest(shop.affiliate_link(VID)) == VID
    assert _dest(shop.affiliate_link(KM)) == KM_CLEAN
    assert _dest(shop.affiliate_link(F8)) == F8_CLEAN
    # parametr s hodnotou u jiného obchodu se obalem nemaže
    monkeypatch.setenv("ALZA_AFFILIATE_ID", "testovaci")
    alza = get_shop("alza").affiliate_link("https://www.alza.cz/x.htm?a=1")
    assert alza == "https://www.alza.cz/x.htm?a=1&idp=testovaci"


def test_go_redirect_headers_fallback_and_oos(client_db, monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    client, db, make = client_db
    product = make([2490], name="Sencor SVC 8618GD tyčový", shop="bscom", category="vysavace")
    offer = product.offers[0]
    offer.url = SVC
    offer.in_stock = True
    db.commit()

    html = client.get(f"/produkt/{product.id}").text
    assert f'href="/go/{offer.id}"' in html
    assert 'rel="nofollow sponsored noopener"' in html
    assert 'target="_blank"' in html
    assert "ehub.cz" not in html
    assert "btn-buy" in html

    hop = client.get(f"/go/{offer.id}?next=https://evil.example/phish", follow_redirects=False)
    assert hop.status_code == 302
    assert hop.headers["cache-control"] == "no-store"
    assert "noindex" in hop.headers["x-robots-tag"]
    assert "nofollow" in hop.headers["x-robots-tag"]
    location = hop.headers["location"]
    assert location.startswith("https://ehub.cz/system/scripts/click.php?")
    assert "a_aid=667205fc" in location and "a_bid=f4a2088a" in location
    assert "evil.example" not in location
    assert _dest(location) == SVC_CLEAN

    vid = make([12990], name="AirPods Max 2", shop="bscom", category="sluchatka")
    vid.offers[0].url = VID
    db.commit()
    vid_hop = client.get(f"/go/{vid.offers[0].id}", follow_redirects=False)
    assert _dest(vid_hop.headers["location"]) == VID

    assert client.get("/go/999999", follow_redirects=False).status_code == 404
    offer.url = "javascript:alert(1)"
    db.commit()
    blocked = client.get(f"/go/{offer.id}", follow_redirects=False)
    assert blocked.status_code == 404
    assert "noindex" in blocked.headers["x-robots-tag"]
    assert safe_http_url("javascript:alert(1)") is None
    assert safe_http_url("https://user:pass@shop.example/x") is None

    offer.url = SVC
    offer.in_stock = False
    db.commit()
    oos = client.get(f"/produkt/{product.id}").text
    assert "btn-buy" not in oos and "Koupit za" not in oos
    assert 'class="btn btn-shop"' not in oos
    assert "Odkaz do obchodu" in oos and f'href="/go/{offer.id}"' in oos

    assert "Disallow: /go/" in client.get("/robots.txt").text
    assert "/go/" not in client.get("/sitemap.xml").text
    js = client.get("/sw.js").text
    assert "startsWith('/go/')" in js


def test_function_does_not_fetch_the_affiliate_url():
    assert RESOLVE_AFFILIATE is False
    assert "RESOLVE_AFFILIATE = false" in GO_FUNCTION_JS
    assert "status: 302" in GO_FUNCTION_JS
    assert '"Cache-Control": "no-store"' in GO_FUNCTION_JS
    assert "noindex, nofollow" in GO_FUNCTION_JS
    assert "click.php?" not in GO_FUNCTION_JS
    assert "fetch(target" not in GO_FUNCTION_JS
    assert "fetch(row" not in GO_FUNCTION_JS
    assert "ehub.cz" not in GO_FUNCTION_JS


def test_fallback_page_carries_plain_desturl():
    affiliate = "https://ehub.cz/system/scripts/click.php?a_aid=1&a_bid=2&desturl=https%3A%2F%2Fwww.bscom.cz%2Fp"
    plain = "https://www.bscom.cz/p"
    html = fallback_page(4, affiliate, plain, sponsored=True)
    assert 'name="robots" content="noindex, nofollow"' in html
    assert "https://www.bscom.cz/p" in html
    assert "stránku produktu" in html
    assert "nofollow sponsored noopener" in html
    assert "&amp;" in html  # query v href je escapované
    same = fallback_page(5, plain, plain, sponsored=False)
    assert "stránku produktu" not in same
    assert 'rel="nofollow noopener"' in same


def test_write_go_assets_and_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)

    class Offer:
        id = 7
        shop = "bscom"
        url = KM
        withheld = False

    class Product:
        offers = [Offer()]

    stats = write_go_assets(tmp_path, [Product()])
    assert stats["offers"] == 1 and stats["pages"] == 1
    data = json.loads((tmp_path / "data" / "go.json").read_text())
    assert data["7"]["f"] == KM_CLEAN
    assert "ehub.cz/system/scripts/click.php" in data["7"]["u"]
    assert _dest(data["7"]["u"]) == KM_CLEAN
    page = (tmp_path / "go" / "7.html").read_text()
    assert "noindex" in page and KM_CLEAN in page
    assert (tmp_path / "functions" / "go" / "[[path]].js").read_text() == GO_FUNCTION_JS
    (tmp_path / "_routes.json").write_text(
        json.dumps({"version": 1, "include": ["/produkt/*"], "exclude": []}),
        encoding="utf-8",
    )
    merge_routes(tmp_path, ["/go/*"])
    assert json.loads((tmp_path / "_routes.json").read_text())["include"] == ["/produkt/*", "/go/*"]
