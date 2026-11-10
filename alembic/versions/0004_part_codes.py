"""Kód výrobce sloučí jen ověřenou řadu. Zbytek blízkých názvů jde do fronty.

Revision ID: 0004_part_codes
Revises: 0003_allegro_brands
Create Date: 2026-11-10

Automaticky jen tam, kde kód nebo GTIN z tabulky part_codes.py určí stejnou řadu.
MTP03SX/A (iPhone 15 128 GB černý) a MTP43SX/A (iPhone 15 128 GB modrý) jsou jedna
řada. AirPods, MacBook a PS5 Slim bez společného kódu se nesloučí, jen navrhnou.
"""
revision = "0004_part_codes"
down_revision = "0003_allegro_brands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    from sqlalchemy.orm import Session
    from lovceny.migrate_log import log_counts
    from lovceny.part_codes import apply_part_code_merges
    from lovceny.variant_merge import enqueue_variant_proposals
    session = Session(bind=op.get_bind())
    merged = apply_part_code_merges(session, dry_run=False)
    queued = enqueue_variant_proposals(session)
    log_counts(
        "0004_part_codes",
        merged_groups=merged["groups"],
        retired=merged["retired"],
        queued=queued["queued"],
        merge_ids=",".join("-".join(str(i) for i in group) for group in merged["ids"]) or "-",
    )
    session.flush()


def downgrade() -> None:
    raise RuntimeError(
        "Sloučení podle kódu výrobce se samo nevrací. Návrh ve frontě lze zamítnout. "
        "Historie cen se nemaže."
    )
