"""Denní SQLite snímek katalogu. Statický web čte tenhle soubor a na Neon už nesahá.

Historie cen je jeden poslední bod na nabídku a pražský den. Produkty, varianty,
nabídky a týdenní přehledy se kopírují celé. Ostatní tabulky (hlídání, analytika,
pošta) do veřejného webu nepatří.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from .models import Offer, PricePoint, Product, Variant, WeeklyDigest

PRAGUE = ZoneInfo("Europe/Prague")

# Postgres vrátí poslední bod dne po síti. SQLite (testy) seskupí stejné pravidlo v Pythonu.
LAST_POINT_PER_PRAGUE_DAY = """
SELECT DISTINCT ON (offer_id, (captured_at AT TIME ZONE 'Europe/Prague')::date)
       offer_id, price, original_price, in_stock, captured_at, is_demo, source
FROM price_points
ORDER BY offer_id,
         (captured_at AT TIME ZONE 'Europe/Prague')::date,
         captured_at DESC
"""

COPY_MODELS = (Product, Variant, Offer, WeeklyDigest)


def prague_day(value: datetime):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PRAGUE).date()


def ready_snapshot(path: Path) -> bool:
    """Chybějící nebo prázdný soubor znamená: web se negeneruje a Neon se nevolá."""
    return path.is_file() and path.stat().st_size > 0


def aggregated_points(db: Session) -> list[dict]:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return [dict(row) for row in db.execute(text(LAST_POINT_PER_PRAGUE_DAY)).mappings()]
    best: dict[tuple, PricePoint] = {}
    for point in db.scalars(select(PricePoint)).all():
        key = (point.offer_id, prague_day(point.captured_at))
        prev = best.get(key)
        if prev is None or point.captured_at > prev.captured_at:
            best[key] = point
    return [
        {
            "offer_id": point.offer_id,
            "price": point.price,
            "original_price": point.original_price,
            "in_stock": point.in_stock,
            "captured_at": point.captured_at,
            "is_demo": point.is_demo,
            "source": point.source,
        }
        for point in best.values()
    ]


def _clone(model, row):
    data = {column.name: getattr(row, column.name) for column in model.__table__.columns}
    return model(**data)


def write_snapshot(source: Session, path: Path) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    engine = create_engine(f"sqlite:///{path}")
    from .db import Base
    Base.metadata.create_all(engine)
    dest = Session(engine)
    counts: dict[str, int] = {}
    try:
        for model in COPY_MODELS:
            rows = list(source.scalars(select(model)).all())
            dest.add_all(_clone(model, row) for row in rows)
            counts[model.__tablename__] = len(rows)
        points = aggregated_points(source)
        for row in points:
            dest.add(PricePoint(
                offer_id=row["offer_id"],
                price=row["price"],
                original_price=row["original_price"],
                in_stock=row["in_stock"],
                captured_at=row["captured_at"],
                is_demo=bool(row["is_demo"]),
                source=row["source"] or "feed",
            ))
        dest.commit()
        counts["price_points"] = len(points)
        return counts
    finally:
        dest.close()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zapíše kompaktní SQLite snímek katalogu.")
    parser.add_argument("--out", default="snapshot/lovceny.sqlite")
    args = parser.parse_args(argv)
    from .db import SessionLocal
    db = SessionLocal()
    try:
        counts = write_snapshot(db, Path(args.out))
    finally:
        db.close()
    print(
        "Snapshot: "
        + ", ".join(f"{name} {count}" for name, count in counts.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
