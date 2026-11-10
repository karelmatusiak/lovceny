"""Vyřadí Allegro-only produkty, jejichž značka není v KNOWN_BRANDS.

Revision ID: 0003_allegro_brands
Revises: 0002_variants
Create Date: 2026-11-10

Řádky se nemažou. retired_at bez redirect_product_id znamená 301 na kategorii.
Nabídky a price_points zůstávají.
"""
revision = "0003_allegro_brands"
down_revision = "0002_variants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    from sqlalchemy.orm import Session
    from lovceny.allegro_narrow import apply_allegro_narrow
    from lovceny.migrate_log import log_counts
    session = Session(bind=op.get_bind())
    stats = apply_allegro_narrow(session, dry_run=False)
    log_counts("0003_allegro_brands", keep=stats["keep"], retire=stats["retire"])
    session.flush()


def downgrade() -> None:
    raise RuntimeError(
        "Vyřazení se samo nevrací. Obnova je smazat retired_at u dotčených id. "
        "Historie cen se nemaže."
    )
