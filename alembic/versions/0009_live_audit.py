"""Schová použité nabídky, špatné kódy modelu a příslušenství z auditu.

Revision ID: 0009_live_audit
Revises: 0008_audit_offers
Create Date: 2026-11-12

Pravidlo je deterministické. Nejisté se neschovává do fronty. Historie cen
se nemaže. U TESLA se místo ceny se slevovým kódem vrátí běžná cena.
"""
revision = "0009_live_audit"
down_revision = "0008_audit_offers"
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
        "Úklid živého auditu se sám nevrací. Schovaná nabídka má withheld, "
        "použitý produkt a příslušenství mají catalog_hidden. Historie cen zůstává."
    )
