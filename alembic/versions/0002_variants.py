"""Varianta modelu a jednorázové sloučení barev a konfigurací.

Revision ID: 0002_variants
Revises: 0001_baseline
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_variants"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    names = set(insp.get_table_names())
    if "variants" not in names:
        op.create_table(
            "variants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_product_id", sa.Integer(), nullable=True),
            sa.Column("signature", sa.String(200), nullable=False),
            sa.Column("label", sa.String(200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("source_product_id", name="ux_variant_source"),
            sa.UniqueConstraint("product_id", "signature", name="ux_variant_signature"),
        )
        op.create_index("ix_variants_product_id", "variants", ["product_id"])
    offer_cols = {c["name"] for c in insp.get_columns("offers")} if "offers" in names else set()
    if "variant_id" not in offer_cols:
        op.add_column("offers", sa.Column("variant_id", sa.Integer(), nullable=True))
        op.create_index("ix_offers_variant_id", "offers", ["variant_id"])
    from lovceny.variant_merge import apply_variant_merges
    from sqlalchemy.orm import Session
    session = Session(bind=bind)
    apply_variant_merges(session, dry_run=False)
    session.flush()


def downgrade() -> None:
    raise RuntimeError(
        "Sloučení variant se nevrací automaticky. Obnova je revert kódu a ruční "
        "vrácení redirect_product_id. Historie cen se nemaže."
    )
