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
    """Dožene schéma na hlavu Alembicu. Na aktuální databázi je to jeden SELECT.

    drop_all v testech smaže tabulky modelů a alembic_version nechá. Proto
    se schéma doplní i když je verze už na hlavě a tabulky chybí.
    """
    from . import models  # noqa: F401
    if _revision() != _head():
        _upgrade()
    if not _core_tables_present():
        ensure_current_schema(engine)


def _alembic_config():
    from pathlib import Path
    from alembic.config import Config
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


def _head() -> str | None:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def _core_tables_present() -> bool:
    from sqlalchemy import inspect
    names = set(inspect(engine).get_table_names())
    return "products" in names and "offers" in names and "price_points" in names


def _revision() -> str | None:
    from alembic.runtime.migration import MigrationContext
    with engine.connect() as conn:
        heads = MigrationContext.configure(conn).get_current_heads()
    if not heads:
        return None
    return heads[0]


def _upgrade() -> None:
    from alembic import command
    command.upgrade(_alembic_config(), "head")


def ensure_current_schema(bind=None) -> None:
    """Vytvoří chybějící tabulky, sloupce a indexy. Existující data nemění.

    Volá ji migrace 0001 a init_db, když tabulky chybí. create_all na hotové
    tabulce sloupce nepřidá, proto jsou tu stejné doplňky jako dřív při startu.
    """
    from sqlalchemy import inspect, text
    from . import models  # noqa: F401
    target = engine if bind is None else bind
    if target.__class__.__name__ == "Engine":
        with target.begin() as conn:
            _apply_schema(conn)
        return
    _apply_schema(target)


def _apply_schema(target) -> None:
    from sqlalchemy import inspect, text
    Base.metadata.create_all(target)
    insp = inspect(target)
    names = set(insp.get_table_names())
    for table, col, ddl in _ADD_COLUMNS:
        if table in names and col not in {c["name"] for c in insp.get_columns(table)}:
            target.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
    names = set(inspect(target).get_table_names())
    for table, sql in _ADD_INDEXES:
        if table in names:
            target.execute(text(sql))


# Jednoduché doplnění sloupců do existujících tabulek (create_all je nepřidá). Idempotentní.
_ADD_COLUMNS = [("watches", "notified_price", "NUMERIC(12, 2)"),
                ("products", "category_slug", "VARCHAR(40)"),
                ("products", "retired_at", "TIMESTAMP WITH TIME ZONE"),
                ("products", "redirect_product_id", "INTEGER"),
                ("products", "catalog_hidden", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("products", "needs_review", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("offers", "shop_category", "TEXT"),
                ("offers", "gtin", "VARCHAR(14)"),
                ("offers", "last_ok_at", "TIMESTAMP WITH TIME ZONE"),
                ("offers", "due_at", "TIMESTAMP WITH TIME ZONE"),
                ("offers", "due_prev", "TIMESTAMP WITH TIME ZONE"),
                ("offers", "withheld", "BOOLEAN NOT NULL DEFAULT FALSE")]


# Indexy, které create_all na existující tabulku nepřidá. Idempotentní (SQLite i Postgres).
_ADD_INDEXES = [
    ("price_points", "CREATE INDEX IF NOT EXISTS ix_pp_offer_time ON price_points (offer_id, captured_at)"),
    ("offers", "CREATE INDEX IF NOT EXISTS ix_offers_last_checked ON offers (last_checked)"),
    ("offers", "CREATE INDEX IF NOT EXISTS ix_offers_gtin ON offers (gtin)"),
    ("triage_decisions", "CREATE INDEX IF NOT EXISTS ix_triage_decisions_status ON triage_decisions (status)"),
]


