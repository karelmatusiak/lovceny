"""Allegro-only produkty bez známé značky se vyřadí. URL jde 301 na kategorii.

Známá značka je KNOWN_BRANDS v matching.py. Guess, Larma a Libeica se dál
poznají při párování, ale Allegro-only produkt neudrží. Produkt s nabídkou
z jiného obchodu se nesahe. Historie cen zůstane u nabídek.

Nespouští se při startu. Jednou to zapíše migrace 0003.
Náhled: python -m lovceny.allegro_narrow
Zápis:  python -m lovceny.allegro_narrow --apply
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .matching import KNOWN_BRANDS, _fold, brands, primary_brand
from .migrate_log import log_counts
from .models import Product, now


def manufacturer_brands(name: str) -> set[str]:
    """Značky výrobce. Platforma v cizím názvu se nepočítá.

    „Google TV“ bez Pixelu není Google. „by Bose“ vedle jiné značky není Bose.
    „PS5“ u SteelSeries není Sony.
    """
    found = set(brands(name or ""))
    folded = _fold(name or "")
    if "pixel" not in folded and ("google tv" in folded or "android tv" in folded):
        found.discard("google")
    if "by bose" in folded and len(found) > 1:
        found.discard("bose")
    if "steelseries" in folded:
        found.discard("sony")
    # „pro PC PS4 PS5 Xbox“ je seznam platforem, ne konzole Sony ani Microsoft.
    if (re.search(r"\bps\s*[45]\b", folded) and re.search(r"\bxbox\b", folded)
            and "sony" not in folded and "playstation" not in folded and "microsoft" not in folded):
        found.discard("sony")
        found.discard("microsoft")
    return found


def is_known_brand(name: str) -> bool:
    return bool(manufacturer_brands(name) & KNOWN_BRANDS)


def allegro_only(product: Product) -> bool:
    shops = {o.shop for o in product.offers}
    return shops == {"allegro"}


def plan_allegro(products: list[Product]) -> dict[str, list[Product]]:
    keep, retire = [], []
    for product in products:
        if product.retired_at is not None or product.catalog_hidden:
            continue
        if not allegro_only(product):
            continue
        if is_known_brand(product.name or ""):
            keep.append(product)
        else:
            retire.append(product)
    keep.sort(key=lambda p: p.id)
    retire.sort(key=lambda p: p.id)
    return {"keep": keep, "retire": retire}


def _row(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "category": product.category_slug,
        "brand": primary_brand(product.name or ""),
    }


def apply_allegro_narrow(db: Session, *, dry_run: bool = True) -> dict:
    products = list(db.scalars(select(Product).options(selectinload(Product.offers))).all())
    groups = plan_allegro(products)
    if not dry_run:
        for product in groups["retire"]:
            if product.retired_at is not None:
                continue
            product.redirect_product_id = None
            product.retired_at = now()
        db.flush()
    stats = {
        "dry_run": dry_run,
        "allegro_only": len(groups["keep"]) + len(groups["retire"]),
        "keep": len(groups["keep"]),
        "retire": len(groups["retire"]),
        "known_brands": sorted(KNOWN_BRANDS),
        "keep_rows": [_row(p) for p in groups["keep"]],
        "retire_rows": [_row(p) for p in groups["retire"]],
    }
    log_counts("allegro_narrow", dry_run=int(dry_run), keep=stats["keep"], retire=stats["retire"])
    return stats


def main(argv=None) -> int:
    import argparse
    from .db import SessionLocal, init_db
    ap = argparse.ArgumentParser(description="Náhled nebo vyřazení Allegro produktů bez známé značky.")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    init_db()
    db = SessionLocal()
    try:
        stats = apply_allegro_narrow(db, dry_run=not args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
