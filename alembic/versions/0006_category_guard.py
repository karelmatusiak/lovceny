"""Kategorie musí sedět s druhem v názvu. Jinak přesun, nebo skrytí.

Revision ID: 0006_category_guard
Revises: 0005_auto_resolve
Create Date: 2026-11-10

Platí pro všechny zveřejněné produkty, ne jen pro dávku z 0005.
Id, URL a historie cen zůstávají. Sporná nabídka (vysavač na hodinkách)
dostane withheld.
"""
revision = "0006_category_guard"
down_revision = "0005_auto_resolve"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session, selectinload
    from alembic import op
    from lovceny.categories import BY_SLUG
    from lovceny.category_guard import guarded_category, offer_type_conflict
    from lovceny.matching import label_from_url
    from lovceny.migrate_log import log_counts
    from lovceny.models import Product
    session = Session(bind=op.get_bind())
    products = list(session.scalars(
        select(Product).options(selectinload(Product.offers)).where(Product.retired_at.is_(None))
    ).all())
    published = [
        p for p in products
        if not p.catalog_hidden and not p.needs_review and p.category_slug in BY_SLUG
    ]
    before: dict[str, int] = {}
    for product in published:
        before[product.category_slug] = before.get(product.category_slug, 0) + 1
    kept = moved = hidden = withheld = 0
    for product in published:
        action, target = guarded_category(product.name or "", product.category_slug)
        if action == "move" and target in BY_SLUG:
            product.category_slug = target
            product.category = BY_SLUG[target].name
            moved += 1
        elif action == "hide":
            product.needs_review = True
            hidden += 1
        else:
            kept += 1
        final_name = product.name or ""
        for offer in list(product.offers):
            if getattr(offer, "withheld", False):
                continue
            if offer_type_conflict(final_name, label_from_url(offer.url or "")):
                offer.withheld = True
                withheld += 1
    after: dict[str, int] = {}
    for product in published:
        if product.needs_review or product.category_slug not in BY_SLUG:
            continue
        after[product.category_slug] = after.get(product.category_slug, 0) + 1
    fields = {
        "checked": len(published),
        "kept": kept,
        "moved": moved,
        "hidden": hidden,
        "offers_withheld": withheld,
    }
    for slug in sorted(set(before) | set(after)):
        fields[f"before_{slug}"] = before.get(slug, 0)
        fields[f"after_{slug}"] = after.get(slug, 0)
    log_counts("0006_category_guard", **fields)
    session.flush()


def downgrade() -> None:
    raise RuntimeError(
        "Kontrola kategorie se sama nevrací. Přesun je category_slug, skrytí je needs_review, "
        "sporná nabídka má withheld. Historie cen se nemaže."
    )
