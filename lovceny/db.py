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
                ("products", "redirect_product_id", "INTEGER")]


def _migrate():
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    for table, col, ddl in _ADD_COLUMNS:
        if table in insp.get_table_names() and col not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
