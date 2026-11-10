import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///./lovceny.db")
    # Neon/Render give postgres:// or postgresql:// – use psycopg3 driver
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _url()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    _migrate()


# Jednoduché doplnění sloupců do existujících tabulek (create_all je nepřidá). Idempotentní.
_ADD_COLUMNS = [("watches", "notified_price", "NUMERIC(12, 2)"),
                ("products", "category_slug", "VARCHAR(40)"),
                ("products", "retired_at", "TIMESTAMP WITH TIME ZONE"),
                ("products", "redirect_product_id", "INTEGER"),
                ("products", "catalog_hidden", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("products", "needs_review", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("offers", "shop_category", "TEXT"),
                ("offers", "gtin", "VARCHAR(14)")]


# Indexy, které create_all na existující tabulku nepřidá. Idempotentní (SQLite i Postgres).
_ADD_INDEXES = [
    ("price_points", "CREATE INDEX IF NOT EXISTS ix_pp_offer_time ON price_points (offer_id, captured_at)"),
    ("offers", "CREATE INDEX IF NOT EXISTS ix_offers_last_checked ON offers (last_checked)"),
    ("offers", "CREATE INDEX IF NOT EXISTS ix_offers_gtin ON offers (gtin)"),
    ("triage_decisions", "CREATE INDEX IF NOT EXISTS ix_triage_decisions_status ON triage_decisions (status)"),
]


def _migrate():
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table, col, ddl in _ADD_COLUMNS:
        if table in insp.get_table_names() and col not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
    names = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, sql in _ADD_INDEXES:
            if table in names:
                conn.execute(text(sql))
