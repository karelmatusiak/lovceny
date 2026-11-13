"""v6: Ověř slevu – validace URL (SSRF), šetrné stažení stránky, obecný parser, kontrola 30 dní, stránka."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from lovceny import verify
from lovceny.models import Product, Offer, PricePoint
from lovceny.shops import SHOPS, search
from lovceny.shops.search import SearchHit
from lovceny.shops.scrape import parse_microdata, parse_generic

PAGE = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@type":"Product",
"name":"Sluchátka Testovací X1 černá","gtin13":"","image":"https://www.mironet.cz/img/x1.jpg",
"offers":{"@type":"Offer","price":"1990","priceCurrency":"CZK","availability":"https://schema.org/InStock",
"priceSpecification":[{"@type":"UnitPriceSpecification","priceType":"https://schema.org/StrikethroughPrice","price":"2990"}]}}
</script></head><body></body></html>"""


def no_fetch(*a, **k):
    raise AssertionError("stránka obchodu se nesmí stahovat")


@pytest.mark.parametrize("bad", ["", "ftp://www.alza.cz/x", "javascript:alert(1)", "http://localhost/x", "http://127.0.0.1/x",
                                 "http://[::1]/x", "http://10.0.0.5/x", "http://169.254.169.254/latest/meta-data",
                                 "https://user:pass@www.alza.cz/x", "https://www.alza.cz:8443/x", "https://intranet/x",
                                 "https://router.local/x", "https://www.alza.cz/" + "a" * 2100, "nic"])
def test_clean_url_rejects(bad):
    with pytest.raises(verify.BadUrl):
        verify.clean_url(bad)


def test_clean_url_accepts_and_normalizes():
    assert verify.clean_url("www.alza.cz/iphone-d123.htm#x") == "https://www.alza.cz/iphone-d123.htm"
    assert verify.clean_url("http://www.mironet.cz/x/") == "https://www.mironet.cz/x"


def test_ip_checks(monkeypatch):
    assert not verify._ip_public("169.254.169.254") and not verify._ip_public("::ffff:127.0.0.1")
    assert not verify._ip_public("192.168.1.1") and not verify._ip_public("fd00::1")
    assert verify._ip_public("8.8.8.8")
    monkeypatch.setattr(verify.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0)), (0, 0, 0, "", ("10.0.0.1", 0))])
    assert not verify.resolves_public("rebind.example.cz")  # jeden privátní záznam stačí k odmítnutí
    monkeypatch.setattr(verify.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))])
    assert verify.resolves_public("www.example.cz")
    monkeypatch.setattr(verify.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("198.18.0.7", 0))])
    monkeypatch.delenv("LOVCENY_DNS_FAKEIP_OK", raising=False)
    assert not verify.resolves_public("www.example.cz")
    monkeypatch.setenv("LOVCENY_DNS_FAKEIP_OK", "1")  # jen vývoj za fake-IP proxy
    assert verify.resolves_public("www.example.cz")


def _transport(routes):
    def h(req: httpx.Request):
        fn = routes.get(req.url.path)
        return fn(req) if fn else httpx.Response(404)
    return httpx.MockTransport(h)


@pytest.fixture(autouse=True)
def _fresh_robots():
    search._robots.clear()
    verify._last.clear()
    yield
    search._robots.clear()


DOM = ["mironet.cz"]
OKR = {"/robots.txt": lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /kosik\n")}


def test_safe_fetch_ok():
    t = _transport({**OKR, "/p/x1": lambda r: httpx.Response(200, html=PAGE)})
    html, final = verify.safe_fetch("https://www.mironet.cz/p/x1", DOM, resolver=lambda h: True, transport=t)
    assert "Testovací X1" in html and final.endswith("/p/x1")


def test_safe_fetch_respects_robots():
    t = _transport({"/robots.txt": lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /p/\n"),
                    "/p/x1": lambda r: httpx.Response(200, html=PAGE)})
    with pytest.raises(verify.FetchError) as e:
        verify.safe_fetch("https://www.mironet.cz/p/x1", DOM, resolver=lambda h: True, transport=t)
    assert e.value.kind == "robots"


def test_safe_fetch_redirect_outside_shop_and_private_dns():
    t = _transport({**OKR, "/p/x1": lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})})
    with pytest.raises(verify.FetchError) as e:
        verify.safe_fetch("https://www.mironet.cz/p/x1", DOM, resolver=lambda h: True, transport=t)
    assert e.value.kind == "redirect"
    with pytest.raises(verify.FetchError):
        verify.safe_fetch("https://www.mironet.cz/p/x1", DOM, resolver=lambda h: False, transport=t)


def test_safe_fetch_limits_and_blocks(monkeypatch):
    monkeypatch.setattr(verify, "MAX_BYTES", 1000)
    t = _transport({**OKR, "/big": lambda r: httpx.Response(200, html="x" * 5000),
                    "/cf": lambda r: httpx.Response(403, text="cf-chl"),
                    "/pdf": lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})})
    for path, kind in (("/big", "too_big"), ("/cf", "blocked"), ("/pdf", "error")):
        with pytest.raises(verify.FetchError) as e:
            verify.safe_fetch("https://www.mironet.cz" + path, DOM, resolver=lambda h: True, transport=t)
        assert e.value.kind == kind, path


def test_microdata_parser():
    html = """<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Pračka Test &amp; Co 8 kg</h1>
    <img itemprop="image" src="https://x.cz/a.jpg"><div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <meta itemprop="price" content="8 490,00"><link itemprop="availability" href="https://schema.org/InStock"></div></div>"""
    r = parse_microdata(html)
    assert r.price == 8490 and r.name == "Pračka Test & Co 8 kg" and r.in_stock is True and r.image == "https://x.cz/a.jpg"
    assert parse_generic(html).price == 8490
    assert parse_generic(PAGE).price == 1990 and parse_generic(PAGE).original_price == 2990
    assert parse_generic("<html></html>") is None


@pytest.mark.parametrize("url,want", [
    ("https://www.alza.cz/iphone-16-128gb-cerna-d12345678.htm", "iphone 16 128gb cerna"),
    ("https://www.datart.cz/mobilni-telefon-apple-iphone-16-128gb-black.html", "mobilni telefon apple iphone 16 128gb black"),
    ("https://www.neznamy-obchod.cz/produkt/samsung-galaxy-s25-ultra/12345", "samsung galaxy s25 ultra"),
    ("https://www.obchod.cz/", ""),
])
def test_name_from_url(url, want):
    assert verify.name_from_url(url) == want


def _hits(q):
    return [SearchHit("mironet", "Apple iPhone 16 128GB černá", 21990, "https://www.mironet.cz/iphone-16-a"),
            SearchHit("mironet", "Apple iPhone 16 128GB černá (rozbaleno)", 20990, "https://www.mironet.cz/iphone-16-b")]


def test_unknown_shop_no_fetch_live_search_by_slug(client_db, monkeypatch):
    client, db, make = client_db
    seen = []
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: seen.append(q) or _hits(q))
    r = verify.verify(db, "https://www.neznamy-obchod.cz/apple-iphone-16-128gb-cerna-p998877", fetch=no_fetch)
    assert r.status == "unknown_shop" and "nesledujeme" in r.message
    assert seen == ["apple iphone 16 128gb cerna"] and r.lowest_elsewhere.price == 20990
    assert not r.can_track and r.check is None


@pytest.mark.parametrize("url,status", [("https://www.czc.cz/iphone-16/123/produkt", "blocked_shop"),
                                        ("https://www.alza.cz/iphone-16-128gb-cerna-d12345678.htm", "feed")])
def test_blocked_and_feed_shops_never_fetched(client_db, url, status):
    client, db, make = client_db
    r = verify.verify(db, url, fetch=no_fetch, live=False)
    assert r.status == status and r.message
    assert r.can_track == (status == "feed")


def test_supported_shop_fetch_and_track(client_db):
    client, db, make = client_db
    r = verify.verify(db, "https://www.mironet.cz/sluchatka-x1-nove/", fetch=lambda u, d: (PAGE, u), live=False)
    assert r.status == "ok" and r.price == 1990 and r.original_price == 2990 and r.name.startswith("Sluchátka")
    assert r.can_track and r.product is None and r.query == "Sluchátka Testovací X1 černá"
    r2 = verify.verify(db, "https://www.mironet.cz/robots-zakazano", live=False,
                       fetch=lambda u, d: (_ for _ in ()).throw(verify.FetchError("robots", "robots.txt obchodu čtení této stránky nedovoluje")))
    assert r2.status == "robots" and "robots.txt" in r2.message and not r2.can_track


def _tracked(db, url, prices_by_day_ago: dict, demo=False):
    p = Product(name="Kontrolní sluchátka K30", slug="kontrolni-sluchatka-k30")
    o = Offer(product=p, shop="mironet", url=url, status="ok", price=list(prices_by_day_ago.values())[-1], in_stock=True)
    now = datetime.now(timezone.utc)
    for ago, pr in prices_by_day_ago.items():
        o.points.append(PricePoint(price=pr, captured_at=now - timedelta(days=ago), is_demo=demo))
    db.add(p)
    db.commit()
    return p


def test_check30_flag_for_tracked_product(client_db, monkeypatch):
    client, db, make = client_db
    url = f"https://www.mironet.cz/kontrola-30-{datetime.now().timestamp()}"
    hist = {ago: (1500 if ago in (0, 1) else (900 if ago == 10 else 1000)) for ago in range(40, -1, -1)}
    p = _tracked(db, url, hist)
    page = PAGE.replace('"price":"1990"', '"price":"1450"').replace('"price":"2990"', '"price":"2000"')
    r = verify.verify(db, url, fetch=lambda u, d: (page, u), live=False)
    assert r.product.id == p.id and r.check.enough and r.check.min30 == 900 and r.check.flag
    assert r.verdict.code == "fake"
    monkeypatch.setattr(verify, "safe_fetch", lambda u, d, **k: (page, u))
    html = client.get("/over-slevu", params={"url": url}).text
    assert "Za 30 dní bylo levněji" in html and "900\xa0Kč" in html and "2\xa0000\xa0Kč" in html
    assert f"/produkt/{p.id}-" in html and "falešn" not in html.lower()
    assert 'name="robots" content="noindex' in html


def test_check30_needs_week_of_data(client_db):
    client, db, make = client_db
    url = f"https://www.mironet.cz/kratka-{datetime.now().timestamp()}"
    _tracked(db, url, {3: 1000, 2: 1000, 1: 990})
    r = verify.verify(db, url, fetch=lambda u, d: (PAGE, u), live=False)
    assert r.check is not None and not r.check.enough
    url2 = f"https://www.mironet.cz/demo-{datetime.now().timestamp()}"
    _tracked(db, url2, {ago: 1000 for ago in range(30, 0, -1)}, demo=True)
    assert not verify.verify(db, url2, fetch=lambda u, d: (PAGE, u), live=False).check.enough  # demo se nepočítá


def test_page_form_errors_share_and_rate_limit(client_db, monkeypatch):
    client, db, make = client_db
    html = client.get("/over-slevu").text
    assert 'action="/over-slevu"' in html and 'type="url"' in html and 'rel="canonical"' in html
    r = client.get("/over-slevu", params={"url": "http://127.0.0.1/admin"})
    assert r.status_code == 422 and "IP adresu" in r.text
    monkeypatch.setattr(verify, "safe_fetch", lambda u, d, **k: (PAGE, u))
    u = "https://www.mironet.cz/sdileni-x1"
    html = client.get("/over-slevu", params={"url": u}).text
    assert "Sledovat tento produkt" in html and "1\xa0990\xa0Kč" in html and "−33 % podle obchodu" in html
    assert "/over-slevu?url=https%3A%2F%2Fwww.mironet.cz%2Fsdileni-x1" in html  # sdílitelná adresa výsledku
    assert "/over-slevu/jinde?url=" in html
    monkeypatch.setattr(verify, "PER_IP_HOUR", 2)
    verify._rl.clear()
    codes = [client.get("/over-slevu", params={"url": f"https://www.mironet.cz/rl-{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    assert client.get("/over-slevu", params={"url": u}).status_code == 200  # z cache bez limitu


def test_elsewhere_fragment_and_nojs(client_db, monkeypatch):
    client, db, make = client_db
    monkeypatch.setattr(SHOPS["mironet"], "searcher", _hits)
    url = "https://www.neznamy-obchod.cz/apple-iphone-16-128gb-cerna-p998877"
    html = client.get("/over-slevu/jinde", params={"url": url}).text
    assert "Nabídky v dalších obchodech" in html and "20\xa0990\xa0Kč" in html and "nejnižší cena" in html
    assert html.index("20\xa0990") < html.index("21\xa0990")
    assert "Prohledat další obchody" in client.get("/over-slevu", params={"url": url}).text
    assert "20\xa0990" in client.get("/over-slevu", params={"url": url, "jinde": 1}).text


def test_home_entry_point():
    from fastapi.testclient import TestClient
    from lovceny.main import app
    html = TestClient(app).get("/").text
    assert "Ověř slevu z našeho katalogu" in html and html.count('action="/over-slevu"') == 1
    assert "jakéhokoli" not in html
    assert 'href="/over-slevu"' in html and "nebo ověř slevu z odkazu" in html
    assert 'href="/#hledat"' in html and "Prověřit cenu" not in html and ">Hledat<" in html
    assert 'class="btn btn-verify" href="/over-slevu"' not in html  # removed from nav CTA


def test_pretty_name():
    assert verify.pretty_name("samsung galaxy s25 ultra 256gb") == "Samsung Galaxy S25 Ultra 256GB"
    assert verify.pretty_name("iPhone 16 5000mah 120hz") == "iPhone 16 5000mAh 120Hz"
