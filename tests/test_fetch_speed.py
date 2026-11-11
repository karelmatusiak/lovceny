"""Stažení cen nesmí na každou položku feedu sahat do databáze ani projet celý katalog."""
import time

from sqlalchemy import event

from lovceny.models import Product, ShopRunStat
from lovceny.monitor import FetchStats
from lovceny.service import apply_feed
from lovceny.shops.feed import FeedItem
from lovceny.triage import ensure_mappings
from lovceny.util import slugify


def test_price_import_query_count_stays_flat(client_db):
    _client, db, _make = client_db
    ensure_mappings(db)
    db.commit()
    for i in range(50):
        db.add(Product(
            name=f"Sencor SVC {2000 + i} vysavač",
            slug=slugify(f"svc-speed-{i}"),
            category_slug="vysavace",
            category="Vysavače",
        ))
    db.commit()
    items = [
        FeedItem(
            item_id=f"ses-{i}",
            name=f"Sencor SES {7000 + i} espresso kávovar",
            url=f"https://www.sencor.cz/ses-speed-{i}",
            price=4500,
            ean=None,
            category="Heureka.cz | Bílé zboží | Malé spotřebiče | Kávovary",
            in_stock=True,
        )
        for i in range(50)
    ]
    queries: list[str] = []

    def before(_conn, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", before)
    started = time.perf_counter()
    stats = apply_feed(db, "sencor", items, create_missing=False)
    elapsed = time.perf_counter() - started
    event.remove(db.get_bind(), "before_cursor_execute", before)
    assert stats["attached"] == 0 and stats["created"] == 0
    # Příprava mapování a jeden zápis na konci. Ne select na každou z 50 položek.
    assert len(queries) < 15, len(queries)
    # 50×50 průchod brand_and_mpn trval přes 2 s. Index to srazí pod sekundu.
    assert elapsed < 1.0, elapsed


def test_health_checkpoint_keeps_one_row_and_refreshes_the_run(client_db):
    _client, db, _make = client_db
    stats = FetchStats(run_id="fetch-checkpoint")
    stats.add("4home", "ok", 200)
    stats.save(db)
    stats.add("4home", "ok", 200)
    stats.add("istyle", "errors", 500, "timeout")
    stats.save(db)
    rows = db.query(ShopRunStat).filter_by(run_id="fetch-checkpoint").all()
    assert {row.shop for row in rows} == {"4home", "istyle"}
    home = next(row for row in rows if row.shop == "4home")
    assert home.ok == 2 and home.run_at is not None
