"""Vrátí katalog do stavu po fázi 1.

Noční import vyřazené produkty znovu zveřejnil (smazal retired_at) a u skrytých
zavolal pravidlo výpisu. Tahle obnova:

- sloučený produkt s redirect_product_id znovu vyřadí
- Allegro-only bez známé značky znovu vyřadí
- příslušenství a kategorii v rozporu s názvem schová (needs_review)
- nabídku jiného druhu (vysavač na hodinkách) nechá v historii a označí withheld

Id, slug, URL a price_points se nemažou.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .allegro_narrow import apply_allegro_narrow
from .categories import BY_SLUG, is_accessory_text
from .category_guard import guarded_category, offer_type_conflict
from .matching import label_from_url
from .migrate_log import log_counts
from .models import Product, now


def _public(products: list[Product]) -> list[Product]:
    return [
        p for p in products
        if p.retired_at is None and not p.catalog_hidden and not p.needs_review and p.category_slug in BY_SLUG
    ]


def _counts(products: list[Product]) -> dict[str, int]:
    out: dict[str, int] = {}
    for product in _public(products):
        out[product.category_slug] = out.get(product.category_slug, 0) + 1
    return out


def restore_catalog(session: Session) -> dict:
    products = list(session.scalars(
        select(Product).options(selectinload(Product.offers))
    ).all())
    before = _counts(products)
    public_before = {p.id for p in _public(products)}
    sample: list[dict] = []

    def remember(product: Product, reason: str) -> None:
        if product.id not in public_before or len(sample) >= 24:
            return
        sample.append({
            "id": product.id,
            "name": (product.name or "")[:80],
            "category": product.category_slug,
            "reason": reason,
        })

    merged = 0
    for product in products:
        if product.retired_at is None and product.redirect_product_id:
            product.retired_at = now()
            merged += 1
            remember(product, "sloučený produkt")

    narrow = apply_allegro_narrow(session, dry_run=False)
    retired_ids = {row["id"] for row in narrow.get("retire_rows") or []}
    for product in products:
        if product.id in retired_ids:
            remember(product, "allegro bez známé značky")

    moved = hidden_guard = hidden_accessory = hidden_open = withheld = 0
    for product in list(_public(products)):
        if is_accessory_text(product.name or ""):
            product.needs_review = True
            hidden_accessory += 1
            remember(product, "příslušenství")
            continue
        if product.category_slug not in BY_SLUG:
            product.needs_review = True
            hidden_open += 1
            remember(product, "bez kategorie")
            continue
        action, target = guarded_category(product.name or "", product.category_slug)
        if action == "move" and target in BY_SLUG:
            product.category_slug = target
            product.category = BY_SLUG[target].name
            moved += 1
        elif action == "hide":
            product.needs_review = True
            hidden_guard += 1
            remember(product, "kategorie nesedí s názvem")
        final_name = product.name or ""
        for offer in list(product.offers):
            if getattr(offer, "withheld", False):
                continue
            if offer_type_conflict(final_name, label_from_url(offer.url or "")):
                offer.withheld = True
                withheld += 1
    after = _counts(products)
    fields = {
        "before_total": sum(before.values()),
        "after_total": sum(after.values()),
        "merged_again": merged,
        "allegro_retired": narrow.get("retire", 0),
        "allegro_kept": narrow.get("keep", 0),
        "moved": moved,
        "hidden_category": hidden_guard,
        "hidden_accessory": hidden_accessory,
        "hidden_uncategorized": hidden_open,
        "offers_withheld": withheld,
    }
    for slug in sorted(set(before) | set(after)):
        fields[f"before_{slug}"] = before.get(slug, 0)
        fields[f"after_{slug}"] = after.get(slug, 0)
    log_counts("0007_restore_catalog", **fields)
    if sample:
        shown = "; ".join(f"#{row['id']} {row['name']} ({row['reason']})" for row in sample[:12])
        log_counts("0007_restore_catalog_sample", text=shown)
    session.flush()
    return {**fields, "sample": sample, "before": before, "after": after}
