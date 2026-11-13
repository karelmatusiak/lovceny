"""Stažení cen: stejná cena nezaloží bod a katalog se nenačítá znovu pro každý obchod."""
from sqlalchemy import event

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, PricePoint, Product
from lovceny.service import apply_feed, record_price
from tests.test_feed_create import item


def _offer(db) -> Offer:
    product = Product(name="Samsung Galaxy A55", slug="samsung-galaxy-a55")
    offer = Offer(product=product, shop="alza", url="https://www.alza.cz/samsung-galaxy-a55.htm")
    db.add(product)
    db.flush()
    return offer


def test_unchanged_price_and_stock_does_not_insert_a_point():
    init_db()
    db = SessionLocal()
    try:
        offer = _offer(db)
        record_price(db, offer, 8990, 9990, True, "feed")
        record_price(db, offer, 8990, 9990, True, "feed")
        db.flush()
        assert db.query(PricePoint).count() == 1
        assert float(offer.price) == 8990
        assert offer.last_checked is not None
    finally:
        db.close()


def test_changed_price_or_stock_inserts_a_point():
    init_db()
    db = SessionLocal()
    try:
        offer = _offer(db)
        record_price(db, offer, 8990, None, True, "feed")
        record_price(db, offer, 8490, None, True, "feed")
        record_price(db, offer, 8490, None, False, "feed")
        db.flush()
        assert db.query(PricePoint).count() == 3
        assert offer.in_stock is False
    finally:
        db.close()


def test_second_shop_does_not_select_the_whole_catalog_again():
    init_db()
    db = SessionLocal()
    statements: list[str] = []
    listening = False

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        apply_feed(db, "alza", [item()], create_identified=True)
        event.listen(db.get_bind(), "before_cursor_execute", capture)
        listening = True
        stats = apply_feed(db, "datart", [item(
            url="https://www.datart.cz/samsung-galaxy-a55.htm", item_id="D2",
        )], create_identified=True)
    finally:
        if listening:
            event.remove(db.get_bind(), "before_cursor_execute", capture)
        db.close()
    full_catalog = [
        sql for sql in statements
        if "FROM products" in sql and "WHERE" not in sql.upper() and "COUNT" not in sql.upper()
    ]
    assert full_catalog == []
    assert stats["created"] == 0
    assert stats["attached"] + stats.get("second_offer", 0) >= 1
