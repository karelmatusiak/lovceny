from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny import main
from lovceny.db import SessionLocal, init_db
from lovceny.main import app, parse_ids
from lovceny.models import Offer, PricePoint, Product
from lovceny.service import record_price


def _product(db, name, price, shop="planeo", days=10):
    p = Product(name=name, slug=name.lower().replace(" ", "-"))
    o = Offer(product=p, shop=shop, url=f"https://www.{shop}.cz/{p.slug}.html")
    db.add(p)
    db.flush()
    now = datetime.now(timezone.utc)
    for d in range(days, 0, -1):
        db.add(PricePoint(offer=o, price=price + d * 10, captured_at=now - timedelta(days=d), is_demo=True, source="demo"))
    record_price(db, o, price, None, True, "scrape")
    db.commit()
    return p


def test_parse_ids():
    assert parse_ids("3,1,3, x,7,8,9") == [3, 1, 7, 8]
    assert parse_ids("") == []


def test_compare_page_side_by_side():
    init_db()
    db = SessionLocal()
    a = _product(db, "Porovnani Pracka A", 9990)
    b = _product(db, "Porovnani Pracka B", 7490, shop="mironet")
    r = TestClient(app).get(f"/porovnani?ids={a.id},{b.id},999999")
    assert r.status_code == 200
    h = r.text
    assert h.count('class="card cmp-col"') == 2  # neexistující id se ignoruje
    assert "Porovnani Pracka A" in h and "Porovnani Pracka B" in h
    assert "9\xa0990\xa0Kč" in h and "v obchodě Mironet" in h
    assert "Minimum 90 dní" in h and "Maximum 90 dní" in h and "Parametry doplníme brzy" in h
    assert f'data-cmp-remove="{a.id}"' in h and f'/produkt/{a.id}-' in h
    assert 'class="verdict v-' in h and "verdict-reason" in h
    assert f'data-ids="{a.id},{b.id}"' in h and '"label": "Porovnani Pracka A"' in h  # jedna čára na produkt
    db.close()


def test_compare_empty_and_single():
    c = TestClient(app)
    assert "Porovnání je prázdné" in c.get("/porovnani").text
    init_db()
    db = SessionLocal()
    a = _product(db, "Porovnani Single", 100)
    assert "Přidej ještě aspoň jeden produkt" in c.get(f"/porovnani?ids={a.id}").text
    db.close()


def test_cards_and_detail_have_compare_button():
    init_db()
    db = SessionLocal()
    a = _product(db, "Porovnani Karta", 500)
    c = TestClient(app)
    assert f'data-cmp-id="{a.id}"' in c.get("/hledat?q=Porovnani+Karta").text
    d = c.get(f"/produkt/{a.id}-{a.slug}").text
    assert f'data-cmp-id="{a.id}"' in d and 'id="cmp-bar"' in d and "/static/compare.js" in d
    db.close()


def test_live_compare_tracks_then_adds(monkeypatch):
    p = Product(name="Live", slug="live")
    p.id = 42
    monkeypatch.setattr(main, "add_by_url", lambda db, url: (p, ""))
    r = TestClient(app).post("/sledovat", data={"url": "https://www.mironet.cz/x+dp1/", "compare": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/porovnani?pridat=42"


def test_live_results_offer_compare(monkeypatch):
    from lovceny.shops import SHOPS
    from lovceny.shops.search import SearchHit
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: [SearchHit("mironet", "Zbrusu nova pracka", 1, "https://www.mironet.cz/z+dp7/")])
    h = TestClient(app).get("/hledat/zive?q=zbrusu nova pracka").text
    assert 'name="compare" value="1"' in h and "Porovnat" in h


def test_compare_js_served():
    js = TestClient(app).get("/static/compare.js").text
    assert "lovceny_porovnani" in js and "MAX = 4" in js
