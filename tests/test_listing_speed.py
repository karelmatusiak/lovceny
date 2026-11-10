"""/slevy a /black-friday: verdikt se počítá jednou, další request nenačítá price_points."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lovceny import blackfriday as bf
from lovceny import deals
from lovceny.models import PricePoint
from lovceny.verdict import compute

ROOT = Path(__file__).resolve().parents[1]


def test_lite_points_match_orm_verdict(client_db):
    _, db, make = client_db
    p = make([2000, 2100, 1900, 2000, 1800, 2000, 1950, 1700], name="Lite verdikt", original=2200)
    orm_pts, _ = deals._points_for(p, False)
    v_orm = compute(orm_pts, float(p.offers[0].price), float(p.offers[0].original_price))
    lite = deals.load_points_for_offers(db, [p.offers[0].id])[p.offers[0].id]
    v_lite = compute(lite, float(p.offers[0].price), float(p.offers[0].original_price))
    assert v_lite.code == v_orm.code
    assert v_lite.text == v_orm.text
    assert v_lite.min90 == v_orm.min90 and v_lite.median90 == v_orm.median90
    assert v_lite.ref_min30 == v_orm.ref_min30


def _snap(rows):
    return [(r.product.id, r.kind, r.is_lowest, r.verdict.text, r.days, r.min30, r.claimed_pct, round(r.real_pct, 4))
            for r in rows]


def test_black_friday_cache_skips_points_until_new_price(client_db, monkeypatch):
    client, db, make = client_db
    p = make([20000] * 40 + [16000], name="BF cache televize", category="televize", original=20000)
    p.offers[0].last_checked = datetime.now(timezone.utc)
    db.commit()
    bf.clear_cache()
    calls = {"n": 0}
    real = deals.load_points_for_offers

    def wrap(db, ids):
        calls["n"] += 1
        return real(db, ids)

    monkeypatch.setattr(deals, "load_points_for_offers", wrap)
    first = bf.rows(db)
    second = bf.rows(db)
    assert calls["n"] == 1
    assert _snap(first) == _snap(second)
    assert first and first[0].product.id == p.id
    db.add(PricePoint(offer=p.offers[0], price=16000, captured_at=datetime.now(timezone.utc), source="scrape"))
    db.commit()
    third = bf.rows(db)
    assert calls["n"] == 2
    assert _snap(third) == _snap(first)
    html = client.get("/black-friday").text
    assert "BF cache televize" in html
    assert calls["n"] == 2  # stránka použije stejný snímek


def test_deals_cache_skips_points_until_stamp_changes(client_db, monkeypatch):
    _, db, make = client_db
    make([2000] * 8 + [1500], name="Deal cache telefon")
    deals.clear_cache()
    calls = {"n": 0}
    real = deals.product_rows

    def wrap(db, **kw):
        calls["n"] += 1
        return real(db, **kw)

    monkeypatch.setattr(deals, "product_rows", wrap)
    a = deals.real_deals(db, top=40)
    b = deals.real_deals(db, top=3)
    assert calls["n"] == 1
    assert [r.product.name for r in a["buys"]] == [r.product.name for r in b["buys"]]
    assert a["buys"][0].verdict.code == "buy" and a["buys"][0].verdict.text
    p = a["buys"][0].product
    db.add(PricePoint(offer=p.offers[0], price=1500, captured_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                      source="scrape"))
    db.commit()
    deals.real_deals(db, top=40)
    assert calls["n"] == 2


def test_slevy_skips_card_scan_when_buys_exist(client_db, monkeypatch):
    import lovceny.main as m
    client, db, make = client_db
    make([2000] * 10 + [1500], name="Sleva telefon rychla", category="telefony")
    calls = {"n": 0}
    real = m.priced_product_cards

    def wrap(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(m, "priced_product_cards", wrap)
    html = client.get("/slevy").text
    assert calls["n"] == 0
    assert "Sleva telefon rychla" in html
    assert "ušetříš" in html or "u minima" in html


def test_fetch_clears_listing_caches_and_migration_is_additive():
    src = (ROOT / "fetch_prices.py").read_text()
    assert "deals.clear_cache()" in src and "blackfriday.clear_cache()" in src
    sql = (ROOT / "migrations/008_listing_indexes.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS ix_pp_offer_time" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_offers_last_checked" in sql
    assert "DROP " not in sql.upper()
