from pathlib import Path

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, Product
from lovceny.service import offers_for_query, record_price
from lovceny.shops import SHOPS, search
from lovceny.shops.robots import Robots
from lovceny.shops.search import SearchHit, parse_mironet, live_search

FIX = Path(__file__).parent / "fixtures"

PLANEO_ROBOTS = "User-agent: *\nDisallow: *filter=*\nDisallow: /vyhledavani*\n"
XZONE_ROBOTS = "User-agent: AhrefsBot\nDisallow: /\n\nUser-agent: *\nDisallow: /*?*term=\nDisallow: /*&term=\n"


def test_robots_wildcards():
    r = Robots(PLANEO_ROBOTS)
    assert not r.can_fetch("LovcenyBot", "https://www.planeo.cz/vyhledavani$a1013-search?q=garmin")
    assert not r.can_fetch("LovcenyBot", "https://www.planeo.cz/mobily?filter=1")
    assert r.can_fetch("LovcenyBot", "https://www.planeo.cz/mobily/iphone-15")
    x = Robots(XZONE_ROBOTS)
    assert not x.can_fetch("LovcenyBot", "https://www.xzone.cz/katalog.php?term=garmin")
    assert x.can_fetch("LovcenyBot", "https://www.xzone.cz/katalog.php")
    assert not x.can_fetch("AhrefsBot", "https://www.xzone.cz/")
    assert Robots("User-agent: *\nDisallow: /a\nAllow: /a/b$\n").can_fetch("X", "https://h/a/b")


def test_parse_mironet_listing_and_brand_page():
    hits = parse_mironet((FIX / "mironet_iphone.html").read_text("utf8"))
    assert len(hits) >= 3
    h = hits[0]
    assert "iPhone" in h.name and h.price > 1000 and h.url.startswith("https://www.mironet.cz/") and "+dp" in h.url
    assert h.image and h.image.startswith("https://")
    g = parse_mironet((FIX / "mironet_garmin.html").read_text("utf8"))
    assert g and all("Garmin" in x.name for x in g)


def test_live_search_sorts_caches_and_isolates_failures():
    search.clear_cache()
    calls = []

    class S:
        def __init__(self, slug, fn):
            self.slug, self.searcher = slug, fn

    def ok(q):
        calls.append(q)
        return [SearchHit("mironet", "Garmin B", 500, "u2"), SearchHit("mironet", "Garmin A", 300, "u1")]

    def bad(q):
        raise search.SearchBlocked("robots")

    def slow(q):
        import time
        time.sleep(2)
        return []

    res = live_search("garmin", shops=[S("mironet", ok), S("planeo", bad), S("x", slow)], timeout=0.5)
    assert [h.price for h in res.hits] == [300, 500]
    st = {s.shop: s.status for s in res.shops}
    assert st == {"mironet": "ok", "planeo": "robots", "x": "timeout"}


def test_live_search_cache(monkeypatch):
    search.clear_cache()
    n = []
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: n.append(1) or [SearchHit("mironet", "Garmin X", 1, "u")])
    assert not live_search("Garmin").cached
    assert live_search("garmin ").cached and len(n) == 1


def test_planeo_search_is_not_attempted():
    assert SHOPS["planeo"].searcher is None
    assert all(SHOPS[s].searcher is None for s in ["alza", "datart", "czc", "mall", "smarty", "tsbohemia",
                                                    "electroworld", "kasa"])


def test_feed_offers_merged_with_live(monkeypatch):
    init_db()
    db = SessionLocal()
    p = Product(name="Garmin Venu feedtest", slug="garmin-venu-feedtest")
    o = Offer(product=p, shop="alza", url="https://www.alza.cz/garmin-venu-feedtest.htm")
    db.add(p)
    db.flush()
    record_price(db, o, 7000, None, True, "feed")
    db.commit()
    search.clear_cache()
    monkeypatch.setattr(SHOPS["mironet"], "searcher",
                        lambda q: [SearchHit("mironet", "Garmin Venu feedtest M", 6500, "https://www.mironet.cz/a+dp1/")])
    offers, st = offers_for_query(db, "garmin venu feedtest")
    assert [(h.shop, h.price) for h, _ in offers] == [("mironet", 6500), ("alza", 7000)]
    assert offers[1][1] is not None  # alza nabídka už je sledovaná
    db.close()


def test_search_page_and_live_fragment(monkeypatch):
    from lovceny.main import app
    search.clear_cache()
    monkeypatch.setattr(SHOPS["mironet"], "searcher",
                        lambda q: [SearchHit("mironet", "Garmin Fenix 8", 19990, "https://www.mironet.cz/garmin-fenix-8+dp9/", None)])
    c = TestClient(app)
    r = c.get("/hledat?q=garmin")
    assert r.status_code == 200 and "Hledáme „garmin“ v obchodech" in r.text and "/hledat/zive" in r.text
    r = c.get("/hledat/zive?q=garmin")
    assert "Garmin Fenix 8" in r.text and "Mironet" in r.text and "Hlídat" in r.text and "19" in r.text
    r = c.get("/hledat?q=garmin&zive=1")
    assert "Garmin Fenix 8" in r.text
    search.clear_cache()
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: [])
    r = c.get("/hledat/zive?q=neexistujiciprodukt")
    assert "zatím nikde nenacházíme" in r.text


def test_track_button_uses_add_by_url(monkeypatch):
    from lovceny import main
    called = {}

    def fake(db, url):
        called["url"] = url
        p = Product(name="X", slug="x")
        p.id = 1
        return p, ""
    monkeypatch.setattr(main, "add_by_url", fake)
    r = TestClient(main.app).post("/sledovat", data={"url": "https://www.mironet.cz/x+dp1/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/produkt/1-x"
    assert called["url"] == "https://www.mironet.cz/x+dp1/"
