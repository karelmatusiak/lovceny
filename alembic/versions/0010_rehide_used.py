"""Znovu schová použité produkty, které stažení cen po 0009 vrátilo do výpisu.

Revision ID: 0010_rehide_used
Revises: 0009_live_audit
Create Date: 2026-11-12

0009 je už v alembic_version, takže se sama neopakuje. Stejná pravidla
z live_audit se pustí ještě jednou. Historie cen se nemaže.
"""
revision = "0010_rehide_used"
down_revision = "0009_live_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.orm import Session
    from alembic import op
    from lovceny.live_audit import apply_live_audit
    session = Session(bind=op.get_bind())
    apply_live_audit(session, dry_run=False)


def downgrade() -> None:
    raise RuntimeError(
        "Schování použitých produktů se samo nevrací. catalog_hidden a withheld zůstávají."
    )
