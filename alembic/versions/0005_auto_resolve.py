"""Fronta se uzavře pravidly. Potvrzený kód výrobce sloučí i needs_review.

Revision ID: 0005_auto_resolve
Revises: 0004_part_codes
Create Date: 2026-11-10

Shoda podle názvu se nepřipojuje. Do výpisu jde jen jistá kategorie s ověřenou
nabídkou. Odpojení se přesune jen podle GTIN nebo kódu, jinak se nabídka schová.
Varianta se sloučí jen když model_family souhlasí. MTP43SX/A se sloučí do
MTP03SX/A i když má needs_review.
"""
import sqlalchemy as sa

revision = "0005_auto_resolve"
down_revision = "0004_part_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "offers" in set(insp.get_table_names()):
        cols = {c["name"] for c in insp.get_columns("offers")}
        if "withheld" not in cols:
            op.add_column(
                "offers",
                sa.Column("withheld", sa.Boolean(), nullable=False, server_default=sa.false()),
            )
    from sqlalchemy.orm import Session
    from lovceny.auto_resolve import resolve_pending, review_listings
    from lovceny.migrate_log import log_counts
    from lovceny.part_codes import apply_part_code_merges
    from lovceny.variant_merge import apply_variant_merges
    session = Session(bind=bind)
    queue = resolve_pending(session, dry_run=False)
    parts = apply_part_code_merges(session, dry_run=False)
    variants = apply_variant_merges(session, dry_run=False)
    listings = review_listings(session)
    log_counts(
        "0005_auto_resolve",
        published=queue["published"],
        hidden=queue["hidden"],
        merged=queue["merged"] + parts["retired"] + variants["retired"],
        detached=queue["detached"],
        separate=queue["separate"],
        listing_published=listings["published"],
        listing_hidden=listings["hidden"],
        part_ids=",".join("-".join(str(i) for i in group) for group in parts["ids"]) or "-",
        variant_groups=variants["groups"],
        variant_retired=variants["retired"],
        **{f"{name}_{key}": bucket[key] for name, bucket in queue["buckets"].items() for key in bucket},
    )
    session.flush()


def downgrade() -> None:
    raise RuntimeError(
        "Automatické uzavření fronty se samo nevrací. Skrytý produkt má needs_review, "
        "skrytá nabídka má withheld. Historie cen se nemaže."
    )
