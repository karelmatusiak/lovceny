"""/slevy skóruje produkty s týdnem cen a ignoruje cizí nabídku."""
from datetime import datetime, timedelta, timezone

from lovceny import deals
from lovceny.models import Offer, PricePoint


def test_older_measured_discount_is_not_crowded_out(client_db):
    _client, db, make = client_db
    real = make([2000] * 10 + [1500], name="Starší kávovar De'Longhi se slevou", category="kavovary")
    real.created_at = datetime.now(timezone.utc) - timedelta(days=60)
    db.commit()
    for i in range(4):
        make([9000, 9000], name=f"Nový kávovar bez historie {i}", category="kavovary")
    rows = deals.product_rows(db, allow_demo=True, limit_products=1)
    assert [r.product.id for r in rows if r.verdict.code == "buy"] == [real.id]


def test_vacuum_offer_on_a_watch_is_not_a_discount(client_db):
    _client, db, make = client_db
    watch = make(
        [6000] * 10 + [5748], name="Xiaomi Watch 2 Pro LTE", category="chytre-hodinky", shop="xm",
    )
    watch.offers[0].url = "https://xm.cz/xiaomi-watch-2-pro-lte"
    vacuum = Offer(
        product=watch, shop="allegro",
        url="https://allegro.cz/nabidka/vysavac-hepa-899w-2-5l",
        price=1523, in_stock=True, status="ok",
    )
    now = datetime.now(timezone.utc)
    for day in range(10):
        vacuum.points.append(PricePoint(price=1523, captured_at=now - timedelta(days=9 - day)))
    db.add(vacuum)
    db.commit()
    deals.clear_cache()
    rows = [r for r in deals.product_rows(db, allow_demo=True) if r.product.id == watch.id]
    assert rows and rows[0].best.shop == "xm"
    assert rows[0].savings_pct < 15
    assert rows[0].verdict.code != "fake"
