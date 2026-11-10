"""Druh v názvu musí sedět s kategorií z mapování. Sleva na homepage nesmí stát na cizí nabídce."""
from datetime import datetime, timedelta, timezone

from lovceny.category_guard import guarded_category, offer_type_conflict, storage_conflict
from lovceny.hero import _badge_for, clear_cache, hero_showcase
from lovceny.models import Offer, PricePoint
from lovceny.verdict import MIN_DAYS, Verdict


def test_watch_leaves_vacuums_and_bundle_leaves_tvs():
    assert guarded_category("Xiaomi Watch 2 Pro LTE barva Silver 47002", "vysavace") == (
        "move", "chytre-hodinky",
    )
    assert guarded_category("EVOLVEO bundle XSleep + BoneMax(šedé)", "televize") == (
        "move", "sluchatka",
    )
    assert guarded_category("Bosch WGE03200BY pračka", "pracky") == ("keep", "pracky")
    assert guarded_category("Sencor SVC 3001 Orca", "vysavace") == ("keep", "vysavace")
    assert guarded_category("JBL Tune 770NC modrá", "sluchatka") == ("keep", "sluchatka")
    assert guarded_category("JBL Quantum 910X Wireless for Xbox barva Black", "sluchatka") == (
        "keep", "sluchatka",
    )
    assert guarded_category("3mk sklo pro iPhone 15", "telefony") == ("hide", None)


def test_vacuum_offer_on_a_watch_is_not_a_discount(client_db):
    client, db, make = client_db
    watch = make(
        [5748] * 8,
        name="Xiaomi Watch 2 Pro LTE barva Silver 47002",
        category="vysavace",
        shop="xm",
    )
    watch.image_url = "https://img.example/watch.jpg"
    vacuum = Offer(
        product=watch, shop="allegro",
        url="https://allegro.cz/nabidka/vysavac-hepa-899w-2-5l",
        price=1523, in_stock=True, status="ok",
    )
    now = datetime.now(timezone.utc)
    for day in range(8):
        vacuum.points.append(PricePoint(price=1523, captured_at=now - timedelta(days=day)))
    db.add(vacuum)
    db.commit()
    assert offer_type_conflict(watch.name, "vysavac hepa 899w")
    clear_cache()
    picks = {h.product.id: h for h in hero_showcase(db, limit=12)}
    assert watch.id not in picks
    page = client.get(f"/produkt/{watch.id}")
    assert "Koupit za 1\u00a0523" not in page.text
    assert "Koupit za 5\u00a0748" in page.text


def test_storage_mismatch_suppresses_the_percent_badge():
    assert storage_conflict(
        "Apple iPhone 18 Pro Max 2 TB černý",
        "https://allegro.cz/nabidka/apple-iphone-18-pro-max-512gb-cerny",
    )
    assert not storage_conflict("Apple iPhone 18 Pro Max 2 TB černý", "iphone-18-pro-max-2tb")
    v = Verdict(code="buy", label="Kupuj", text="", current=1200, min90=1000, median90=2000, max90=2000)
    assert _badge_for(1200, v, hist_n=2) is None
    assert _badge_for(1200, v, hist_n=MIN_DAYS).startswith("−")
