"""Allegro-only produkty bez známé značky se vyřadí. URL jde 301 na kategorii.

Známá značka je KNOWN_BRANDS v matching.py: kanonické značky, které už
v názvech poznáváme (apple, samsung, bosch, …). Produkt, který má nabídku
i z jiného obchodu, se nesahe. Historie cen zůstane u nabídek.

Nespouští se při startu. Jednou to zapíše migrace 0003.
Náhled: python -m lovceny.allegro_narrow
Zápis:  python -m lovceny.allegro_narrow --apply
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .matching import KNOWN_BRANDS, primary_brand
from .models import Product, now


def is_known_brand(name: str) -> bool:
    """Značka z KNOWN_BRANDS, ne zmínka platformy v názvu jiného výrobce.

    „Google TV“ na televizi Kruger není značka Google. „by Bose“ na Baseusu
    není Bose. „PS5“ na SteelSeries není Sony.
    """
    from .matching import _fold
    brand = primary_brand(name or "")
    if brand not in KNOWN_BRANDS:
        return False
    folded = _fold(name or "")
    if brand == "google" and "pixel" not in folded and ("google tv" in folded or "android tv" in folded):
        return False
    if brand == "bose" and "baseus" in folded:
        return False
    if brand == "sony" and "steelseries" in folded:
        return False
    return True


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
    return {
        "dry_run": dry_run,
        "allegro_only": len(groups["keep"]) + len(groups["retire"]),
        "keep": len(groups["keep"]),
        "retire": len(groups["retire"]),
        "known_brands": sorted(KNOWN_BRANDS),
        "keep_rows": [_row(p) for p in groups["keep"]],
        "retire_rows": [_row(p) for p in groups["retire"]],
    }


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
