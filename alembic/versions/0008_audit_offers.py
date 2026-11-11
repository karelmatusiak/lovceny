"""Odpojí špatně spárované nabídky a schová Gigabyte a Karl Lagerfeld.

Revision ID: 0008_audit_offers
Revises: 0007_restore_catalog
Create Date: 2026-11-11

Pravidlo je deterministické: liší-li se rozlišující token modelu, modelový
kód nebo druh výrobku, nabídka dostane withheld. Historie cen se nemaže.
Gigabyte a Karl Lagerfeld dostanou catalog_hidden a retired_at.
"""
revision = "0008_audit_offers"
down_revision = "0007_restore_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.orm import Session
    from alembic import op
    from lovceny.audit_data import detach_mismatched_offers, hide_excluded_brands
    session = Session(bind=op.get_bind())
    detach_mismatched_offers(session)
    hide_excluded_brands(session)


def downgrade() -> None:
    raise RuntimeError(
        "Úklid auditu se sám nevrací. Sporná nabídka má withheld, vyloučená značka "
        "má catalog_hidden a retired_at. Id, URL a historie cen se nemažou."
    )
