"""Úklid po živém auditu. Nejisté se schová, do fronty se nic neřadí.

Náhled: python -m lovceny.live_audit
Zápis:  python -m lovceny.live_audit --apply

Historie cen se nemaže. Sporná nabídka dostane withheld, použitý nebo
příslušenství-produkt zmizí z katalogu (catalog_hidden, bez needs_review).
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .categories import is_accessory_text, is_non_new
from .matching import label_from_url, model_codes_conflict
from .migrate_log import log_counts
from .models import Product


def _hide_product(product) -> bool:
    changed = False
    if not getattr(product, "catalog_hidden", False):
        product.catalog_hidden = True
        changed = True
    if getattr(product, "needs_review", False):
        product.needs_review = False
        changed = True
    return changed


def _withhold(offer) -> bool:
    if getattr(offer, "withheld", False):
        return False
    offer.withheld = True
    return True


def _offer_non_new(offer) -> bool:
    return is_non_new(
        label_from_url(offer.url or ""),
        shop_category=getattr(offer, "shop_category", None),
    )


def apply_live_audit(session: Session, *, dry_run: bool = True) -> dict:
    products = list(session.scalars(
        select(Product).options(selectinload(Product.offers))
    ).all())
    stats = {
        "products_hidden_used": 0,
        "offers_withheld_used": 0,
        "offers_withheld_model": 0,
        "products_hidden_accessory": 0,
        "offers_withheld_accessory": 0,
        "tesla_prices_restored": 0,
    }
    for product in products:
        name = product.name or ""
        used_product = is_non_new(name, shop_category=product.category)
        accessory_product = is_accessory_text(name, product.category)
        if used_product and (product.catalog_hidden is not True or product.needs_review):
            stats["products_hidden_used"] += 1
            if not dry_run:
                _hide_product(product)
        elif accessory_product and (product.catalog_hidden is not True or product.needs_review):
            stats["products_hidden_accessory"] += 1
            if not dry_run:
                _hide_product(product)
        for offer in list(product.offers or []):
            if offer.shop == "tesla" and offer.original_price and offer.price:
                regular = float(offer.original_price)
                current = float(offer.price)
                if regular > current:
                    stats["tesla_prices_restored"] += 1
                    if not dry_run:
                        offer.price = regular
                        offer.original_price = None
            if _offer_non_new(offer):
                if not offer.withheld:
                    stats["offers_withheld_used"] += 1
                    if not dry_run:
                        _withhold(offer)
                continue
            label = label_from_url(offer.url or "")
            if is_accessory_text(label) and not accessory_product:
                if not offer.withheld:
                    stats["offers_withheld_accessory"] += 1
                    if not dry_run:
                        _withhold(offer)
                continue
            if (
                not offer.withheld
                and (
                    model_codes_conflict(name, offer.url or "")
                    or model_codes_conflict(name, label)
                )
            ):
                stats["offers_withheld_model"] += 1
                if not dry_run:
                    _withhold(offer)
    log_counts("live_audit", dry_run=int(dry_run), **stats)
    if not dry_run:
        session.flush()
    return stats


def main() -> None:
    import argparse

    from .db import SessionLocal, init_db

    parser = argparse.ArgumentParser(description="Náhled nebo zápis úklidu z živého auditu.")
    parser.add_argument("--apply", action="store_true", help="zapsat změny (jinak jen počty)")
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        stats = apply_live_audit(db, dry_run=not args.apply)
        if args.apply:
            db.commit()
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
