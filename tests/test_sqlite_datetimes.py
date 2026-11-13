"""SQLite vrací část časů bez zóny a část s posunem. Po načtení jsou všechny UTC."""
from datetime import datetime, timezone

from sqlalchemy import select, text

from lovceny.db import SessionLocal, as_utc, init_db
from lovceny.honesty import _compute
from lovceny.models import Offer, Product


def test_as_utc_accepts_naive_aware_and_text():
    naive = datetime(2026, 11, 13, 10, 0, 0)
    aware = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    assert as_utc(None) is None
    assert as_utc(naive).tzinfo is timezone.utc
    assert as_utc(aware) == aware
    assert as_utc("2026-11-13T18:00:00+00:00").tzinfo is not None
    assert as_utc("2026-11-13 08:00:00").tzinfo is timezone.utc


def test_mixed_sqlite_timestamps_can_be_sorted():
    init_db()
    db = SessionLocal()
    try:
        first = Product(name="Samsung Galaxy A55", slug="a55-dt")
        second = Product(name="Apple iPhone 16", slug="ip16-dt")
        db.add_all([first, second])
        db.flush()
        early = Offer(product=first, shop="alza", url="https://www.alza.cz/a55-dt.htm", price=8000, in_stock=True, status="ok")
        late = Offer(product=second, shop="datart", url="https://www.datart.cz/ip16-dt.htm", price=19000, in_stock=True, status="ok")
        db.add_all([early, late])
        db.commit()
        db.execute(text("UPDATE offers SET last_checked = '2026-11-01 08:00:00' WHERE id = :id"), {"id": early.id})
        db.execute(text("UPDATE offers SET last_checked = '2026-11-13T18:00:00+00:00' WHERE id = :id"), {"id": late.id})
        db.commit()
        db.expire_all()
        checks = [row.last_checked for row in db.scalars(select(Offer).order_by(Offer.id))]
        assert all(item.tzinfo is not None for item in checks)
        latest = max(checks)
        assert latest == datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)
        listing = _compute(db, datetime.now(timezone.utc), False)
        assert listing.latest_check is None or listing.latest_check.tzinfo is not None
    finally:
        db.close()
