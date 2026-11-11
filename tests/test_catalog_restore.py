"""Import nesmí vracet vyřazené produkty a obnova je znovu schová."""
from datetime import datetime, timedelta, timezone

from lovceny.catalog_restore import restore_catalog
from lovceny.models import Offer, PricePoint, Product
from lovceny.service import apply_feed
from lovceny.shops.feed import FeedItem
from lovceny.triage import ensure_mappings

WATCH = "Elektronika > Telefony a příslušenství > Chytré hodinky a příslušenství > Chytré hodinky"


def test_feed_does_not_resurrect_or_publish_unknown_allegro(client_db):
    _client, db, _make = client_db
    ensure_mappings(db)
    now = datetime.now(timezone.utc)
    retired = Product(
        name="Chytré hodinky Zeblaze Vibe 8", slug="zeblaze-vibe-8",
        category_slug="chytre-hodinky", category="Chytré hodinky", retired_at=now,
    )
    hidden = Product(
        name="Chytré hodinky Cubot U3", slug="cubot-u3",
        category_slug="chytre-hodinky", category="Chytré hodinky", needs_review=True,
    )
    db.add_all([retired, hidden])
    db.flush()
    db.add(Offer(
        product=retired, shop="allegro", url="https://allegro.cz/nabidka/zeblaze-vibe-8",
        price=900, status="ok", in_stock=True,
    ))
    db.add(Offer(
        product=hidden, shop="allegro", url="https://allegro.cz/nabidka/cubot-u3",
        price=800, status="ok", in_stock=True,
    ))
    db.commit()
    again = FeedItem(
        "z1", "Chytré hodinky Zeblaze Vibe 8", "https://allegro.cz/nabidka/zeblaze-vibe-8",
        890, category=WATCH, in_stock=True,
    )
    fresh = FeedItem(
        "c1", "Chytré hodinky Cubot U3", "https://allegro.cz/nabidka/cubot-u3",
        790, category=WATCH, in_stock=True,
    )
    created = FeedItem(
        "n1", "Chytré hodinky MK-Tech pulzoměr", "https://allegro.cz/nabidka/mk-tech-hodinky",
        700, category=WATCH, in_stock=True,
    )
    stats = apply_feed(db, "allegro", [again, fresh, created], create_missing=True, max_create=5)
    db.expire_all()
    assert stats["created"] == 0
    assert db.get(Product, retired.id).retired_at is not None
    brought = db.get(Product, hidden.id)
    assert brought.needs_review or brought.retired_at is not None
    assert db.query(Product).filter(Product.name.contains("MK-Tech")).count() == 0


def test_restore_rehires_unknown_allegro_and_keeps_ids(client_db):
    _client, db, make = client_db
    keep = make([5000], name="Samsung Galaxy S25 FE 128GB černá", category="telefony", shop="allegro")
    drop = make([900], name="Chytré hodinky Zeblaze Vibe 8", category="chytre-hodinky", shop="allegro")
    mixed = make([8000], name="Cubot Kingkong Mini", category="telefony", shop="allegro")
    db.add(Offer(product=mixed, shop="alza", url="https://www.alza.cz/cubot-kingkong", price=8100,
                 status="ok", in_stock=True))
    merged = make([1000], name="Apple iPhone 15 128GB modrá", category="telefony", shop="istyle")
    merged.redirect_product_id = keep.id
    point_id = drop.offers[0].points[0].id
    slug = drop.slug
    db.commit()
    before_id = drop.id
    stats = restore_catalog(db)
    db.commit()
    db.expire_all()
    assert db.get(Product, keep.id).retired_at is None
    assert db.get(Product, mixed.id).retired_at is None
    retired = db.get(Product, before_id)
    assert retired.retired_at is not None and retired.slug == slug
    assert db.get(PricePoint, point_id) is not None
    assert db.get(Product, merged.id).retired_at is not None
    assert stats["before_total"] > stats["after_total"]
    assert stats["before"]["chytre-hodinky"] == 1
    assert stats["after"].get("chytre-hodinky", 0) == 0
    assert any(row["id"] == before_id and "značky" in row["reason"] for row in stats["sample"])
