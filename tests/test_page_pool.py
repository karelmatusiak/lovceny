"""Homepage, /kategorie a /slevy nesmí na katalogu držet spojení ani dělat dotaz na produkt."""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, insert

from lovceny.db import engine
from lovceny.models import Offer, PricePoint, Product


def _track():
    state = {"out": 0, "max": 0, "queries": 0}

    def checkout(*_a):
        state["out"] += 1
        state["max"] = max(state["max"], state["out"])

    def checkin(*_a):
        state["out"] = max(0, state["out"] - 1)

    def before(*_a):
        state["queries"] += 1

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    event.listen(engine, "before_cursor_execute", before)
    state["stop"] = lambda: (
        event.remove(engine, "checkout", checkout),
        event.remove(engine, "checkin", checkin),
        event.remove(engine, "before_cursor_execute", before),
    )
    return state


def test_catalog_pages_use_few_queries_and_release_the_connection(client_db):
    client, db, _make = client_db
    now = datetime.now(timezone.utc)
    products = []
    for i in range(650):
        cat = "telefony" if i < 400 else "sluchatka"
        products.append(Product(
            name=f"Samsung Galaxy A55 {i}",
            slug=f"samsung-galaxy-a55-{i}",
            category_slug=cat,
            category="Mobilní telefony" if cat == "telefony" else "Sluchátka",
            image_url="https://www.alza.cz/foto.jpg",
        ))
    db.add_all(products)
    db.flush()
    offers = [
        Offer(
            product_id=p.id, shop="alza", url=f"https://www.alza.cz/samsung-galaxy-a55-{i}",
            price=1500 if i == 0 else 10000, status="ok", in_stock=True,
            last_checked=now, last_ok_at=now,
        )
        for i, p in enumerate(products)
    ]
    db.add_all(offers)
    db.flush()
    rows = []
    sale_id = offers[0].id
    for offer in offers:
        for day in range(90):
            dropped = offer.id == sale_id and day == 89
            rows.append({
                "offer_id": offer.id,
                "price": 1500 if dropped else (2000 if offer.id == sale_id else 10000),
                "captured_at": now - timedelta(days=89 - day),
                "is_demo": False,
                "source": "feed",
            })
    for i in range(0, len(rows), 5000):
        db.execute(insert(PricePoint), rows[i:i + 5000])
    db.commit()
    db.close()

    timings = {}
    state = _track()
    try:
        for path in ("/healthz", "/", "/kategorie", "/kategorie/telefony", "/slevy"):
            state["queries"] = 0
            state["max"] = state["out"]
            t0 = time.perf_counter()
            response = client.get(path)
            timings[path] = time.perf_counter() - t0
            assert response.status_code == 200, path
            assert state["queries"] < 40, (path, state["queries"])
            assert state["max"] <= 2, (path, state["max"])
            assert timings[path] < 3, (path, round(timings[path], 3), state["queries"])
        assert state["out"] == 0
    finally:
        state["stop"]()
    text = client.get("/slevy").text
    assert "Samsung Galaxy A55 0" in text
