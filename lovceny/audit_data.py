"""Jednorázový úklid z auditu: špatně připojené nabídky a vyloučené značky.

Pravidla jsou stejná jako při importu. Žádný model, žádný odhad z ceny.
Historie cen zůstává. Sporná nabídka dostane withheld.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .category_guard import category_against_offer, offer_type_conflict
from .exclusions import bury_excluded, is_excluded_brand
from .matching import label_from_url, url_model_conflict
from .migrate_log import log_counts
from .models import Product


def detach_mismatched_offers(session: Session) -> dict:
    """Schová nabídku, jejíž adresa patří k jinému modelu nebo druhu."""
    products = list(session.scalars(
        select(Product).options(selectinload(Product.offers))
    ).all())
    withheld = 0
    touched: set[int] = set()
    for product in products:
        for offer in list(product.offers or []):
            if getattr(offer, "withheld", False):
                continue
            url = offer.url or ""
            if not url.strip():
                continue
            label = label_from_url(url)
            if (
                url_model_conflict(product.name or "", label)
                or offer_type_conflict(product.name or "", label)
                or category_against_offer(product.category_slug, label)
            ):
                offer.withheld = True
                withheld += 1
                touched.add(product.id)
    fields = {
        "offers_withheld": withheld,
        "products_with_detached_offers": len(touched),
    }
    log_counts("0008_detach_offers", **fields)
    session.flush()
    return fields


def hide_excluded_brands(session: Session) -> dict:
    """Gigabyte a Karl Lagerfeld zmizí z katalogu. Id a historie cen zůstávají."""
    products = list(session.scalars(select(Product)).all())
    matched = public = 0
    for product in products:
        if not is_excluded_brand(product.name or ""):
            continue
        matched += 1
        was_public = product.retired_at is None and not product.catalog_hidden
        if bury_excluded(product) or was_public:
            if was_public:
                public += 1
    fields = {"excluded_matched": matched, "excluded_removed_from_public": public}
    log_counts("0008_excluded_brands", **fields)
    session.flush()
    return fields
