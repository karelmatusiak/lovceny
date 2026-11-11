"""Vrátí vyřazené a nejisté produkty mimo veřejný katalog.

Revision ID: 0007_restore_catalog
Revises: 0006_category_guard
Create Date: 2026-11-11

Noční import smazal retired_at a znovu zveřejnil produkty, které fáze 1
vyřadila nebo schovala. Id, URL a historie cen zůstávají.
"""
revision = "0007_restore_catalog"
down_revision = "0006_category_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.orm import Session
    from alembic import op
    from lovceny.catalog_restore import restore_catalog
    session = Session(bind=op.get_bind())
    restore_catalog(session)


def downgrade() -> None:
    raise RuntimeError(
        "Obnova katalogu se sama nevrací. Vyřazení je retired_at, skrytí je needs_review, "
        "sporná nabídka má withheld. Id, URL a historie cen se nemažou."
    )
