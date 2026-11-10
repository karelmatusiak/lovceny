"""Baseline schématu. Na existující databázi jen doplní chybějící sloupce a indexy.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-10
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from lovceny.db import ensure_current_schema
    ensure_current_schema(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Baseline se nemaže. Návrat je revert kódu, tabulky a historie cen zůstávají.")
