"""Kompaktní snapshot: jeden cenový bod na nabídku a pražský den, a žádný soubor = žádný Neon."""
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, PricePoint, Product
from lovceny.snapshot import LAST_POINT_PER_PRAGUE_DAY, ready_snapshot, write_snapshot


def test_snapshot_keeps_the_last_price_of_each_prague_day(tmp_path):
    init_db()
    db = SessionLocal()
    try:
        product = Product(name="Samsung Galaxy A55", slug="samsung-galaxy-a55")
        offer = Offer(
            product=product, shop="alza", url="https://www.alza.cz/samsung-galaxy-a55.htm",
            price=8400, in_stock=True,
        )
        db.add(product)
        db.flush()
        db.add(PricePoint(offer=offer, price=9000, captured_at=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc), source="feed"))
        db.add(PricePoint(offer=offer, price=8500, captured_at=datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc), source="feed"))
        # 23:30 UTC je v zimě už další pražský den.
        db.add(PricePoint(offer=offer, price=8400, captured_at=datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc), source="feed"))
        db.commit()
        path = tmp_path / "lovceny.sqlite"
        counts = write_snapshot(db, path)
        assert counts["products"] == 1
        assert counts["offers"] == 1
        assert counts["price_points"] == 2
        dest = Session(create_engine(f"sqlite:///{path}"))
        try:
            rows = dest.scalars(select(PricePoint).order_by(PricePoint.captured_at)).all()
            assert [float(row.price) for row in rows] == [8500.0, 8400.0]
            assert dest.scalars(select(Product)).one().name == "Samsung Galaxy A55"
        finally:
            dest.close()
    finally:
        db.close()


def test_missing_snapshot_is_not_ready(tmp_path):
    assert ready_snapshot(tmp_path / "lovceny.sqlite") is False
    empty = tmp_path / "empty.sqlite"
    empty.write_bytes(b"")
    assert ready_snapshot(empty) is False
    assert "DISTINCT ON" in LAST_POINT_PER_PRAGUE_DAY
    assert "Europe/Prague" in LAST_POINT_PER_PRAGUE_DAY
