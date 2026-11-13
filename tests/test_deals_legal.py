from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.deals import real_deals
from lovceny.main import app
from lovceny.models import Offer, PricePoint, Product


def _mk(db, name, history, current, original=None, demo=False):
    """history: fn(day)->price for 30 days back; current: dnešní cena."""
    from sqlalchemy import select
    old = db.scalar(select(Product).where(Product.name == name))
    if old:
        return old
    p = Product(name=name, slug=name.lower().replace(" ", "-"), category_slug="telefony", category="Mobilní telefony")
    o = Offer(product=p, shop="mironet", url=f"https://www.mironet.cz/{p.slug}", price=current,
              original_price=original, status="ok")
    db.add(p)
    db.flush()
    now = datetime.now(timezone.utc)
    for d in range(30, 0, -1):
        db.add(PricePoint(offer=o, price=history(d), captured_at=now - timedelta(days=d), is_demo=demo,
                          source="demo" if demo else "scrape"))
    db.add(PricePoint(offer=o, price=current, captured_at=now, is_demo=False, source="scrape"))
    db.commit()
    return p


def _seed():
    init_db()
    db = SessionLocal()
    big = _mk(db, "Samsung Galaxy A55 Sleva Velka", lambda d: 20000, 15000)     # −25 %, −5000 Kč
    small = _mk(db, "Samsung Galaxy A35 Sleva Mala", lambda d: 1000, 800)          # −20 %, −200 Kč
    fake = _mk(db, "Apple iPhone 16 Sleva Levneji", lambda d: 1000, 990, original=1500)
    demo = _mk(db, "Samsung Galaxy A15 Sleva Demo", lambda d: 50000, 10000, demo=True)
    wait = _mk(db, "Xiaomi Redmi Note Sleva Pockej", lambda d: 800, 1000)
    db.close()
    return big, small, fake, demo, wait


def test_ranking_sorted_by_real_savings_and_excludes_demo(monkeypatch):
    monkeypatch.delenv("LOVCENY_INCLUDE_DEMO", raising=False)
    big, small, fake, demo, wait = _seed()
    with SessionLocal() as db:
        r = real_deals(db)
    names = [x.product.name for x in r["buys"]]
    assert names.index("Samsung Galaxy A55 Sleva Velka") < names.index("Samsung Galaxy A35 Sleva Mala")
    assert "Samsung Galaxy A15 Sleva Demo" not in names and "Xiaomi Redmi Note Sleva Pockej" not in names
    top = next(x for x in r["buys"] if x.product.name == "Samsung Galaxy A55 Sleva Velka")
    assert round(top.savings_czk) == 5000 and round(top.savings_pct) == 25
    assert "Apple iPhone 16 Sleva Levneji" in [x.product.name for x in r["fakes"]]


def test_demo_included_only_with_flag_and_labelled(monkeypatch):
    _seed()
    monkeypatch.setenv("LOVCENY_INCLUDE_DEMO", "1")
    h = TestClient(app).get("/slevy").text
    assert "Sleva Demo" in h and "DEMO" in h and "započítává" in h


def test_slevy_page_and_home_teaser():
    _seed()
    c = TestClient(app)
    h = c.get("/slevy").text
    assert "<title>Skutečné slevy – Lovceny</title>" in h and 'og:title" content="Skutečné slevy' in h
    assert "Za 30 dní bylo levněji" in h and "§ 12a" in h and "Nejnižší cena za posledních 30 dní byla" in h and "falešn" not in h.lower()
    assert "−25&nbsp;%" in h and "ušetříš 5\xa0000\xa0Kč" in h
    home = c.get("/").text
    assert "Slevy dne" not in home
    assert 'href="/slevy"' in home and "Jak vyděláváme" in home
    assert "Nejvíc nabízené modely" in home


def test_slevy_empty_state(monkeypatch):
    import lovceny.main as m
    monkeypatch.setattr(m, "real_deals", lambda db, **k: {"buys": [], "fakes": [], "include_demo": False})
    h = TestClient(app).get("/slevy").text
    assert "Dnes tu žádná skutečná sleva není" in h and "žádnou takovou slevu" in h


def test_legal_pages_linked_and_accurate():
    c = TestClient(app)
    p = c.get("/ochrana-osobnich-udaju")
    k = c.get("/cookies")
    assert p.status_code == k.status_code == 200
    for s in ["Karel Matusiak", "info@lovceny.cz", "IČO 30210453", "U Hřiště 267/16", "Gemini", "Frankfurt", "ÚOOÚ", "double opt-in",
              "čl. 6 odst. 1 písm. a"]:
        assert s in p.text, s
    assert "lovceny_porovnani" in k.text and "nezobrazujeme cookie lištu" in k.text
    home = c.get("/").text
    assert 'href="/ochrana-osobnich-udaju"' in home and 'href="/cookies"' in home
    assert "set-cookie" not in {h.lower() for h in p.headers}
