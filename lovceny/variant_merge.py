"""Jedna stránka na model. Barva, úložiště a RAM jsou varianty, ne další produkty.

Kanonická stránka je aktivní produkt s nejnižším id ve skupině. Ostatní id zůstávají
a jejich URL jde 301 na kanonickou stránku. Nabídky a historie cen se nepřepisují,
jen se přenesou na kanonický produkt a dostanou variantu.

Samotný import to nespouští. Jednou to zapíše migrace 0002 při nasazení.
Náhled: python -m lovceny.variant_merge
Zápis mimo migraci: python -m lovceny.variant_merge --apply
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .matching import model_family
from .models import Offer, Product, Variant, now
from .specs import extract


def option_label(name: str) -> str:
    raw = extract(name or "")
    parts = []
    if raw.get("storage"):
        parts.append(str(raw["storage"]))
    if raw.get("ram"):
        parts.append(f"RAM {raw['ram']}")
    if raw.get("color"):
        parts.append(str(raw["color"]))
    return ", ".join(parts) or "Základní"


def option_signature(name: str) -> str:
    return option_label(name).casefold()


def plan_variant_merges(products: list[Product]) -> list[list[Product]]:
    """Skupiny aktivních produktů, které jsou jeden model. Jednotlivce nevrací."""
    buckets: dict[tuple, list[Product]] = {}
    for product in products:
        if product.retired_at is not None or product.catalog_hidden or product.needs_review:
            continue
        key = model_family(product.name or "", product.category_slug)
        if key is None:
            continue
        buckets.setdefault(key, []).append(product)
    groups = []
    for members in buckets.values():
        alive = [p for p in members if p.retired_at is None]
        if len(alive) >= 2:
            groups.append(sorted(alive, key=lambda p: p.id))
    groups.sort(key=lambda g: g[0].id)
    return groups


def _summary(groups: list[list[Product]], *, applied: bool) -> dict:
    samples = []
    for group in groups[:40]:
        canonical = group[0]
        others = ", ".join(f"#{p.id} {option_label(p.name or '')}" for p in group[1:])
        samples.append(f"#{canonical.id} {canonical.name} ← {others}")
    return {
        "dry_run": not applied,
        "groups": len(groups),
        "products": sum(len(g) for g in groups),
        "retired": sum(len(g) - 1 for g in groups),
        "samples": samples,
    }


def apply_variant_merges(db: Session, *, dry_run: bool = True) -> dict:
    products = list(db.scalars(
        select(Product).options(selectinload(Product.offers))
    ).all())
    groups = plan_variant_merges(products)
    if dry_run:
        return _summary(groups, applied=False)
    from .catalog_repair import _move_dependents
    for group in groups:
        canonical = group[0]
        for product in group:
            label = option_label(product.name or "")
            signature = option_signature(product.name or "")[:200]
            variant = db.scalar(select(Variant).where(Variant.source_product_id == product.id))
            if variant is None:
                variant = db.scalar(select(Variant).where(
                    Variant.product_id == canonical.id, Variant.signature == signature,
                ))
            if variant is None:
                variant = Variant(
                    product_id=canonical.id, source_product_id=product.id,
                    signature=signature, label=label[:200], created_at=now(),
                )
                db.add(variant)
                db.flush()
            elif variant.product_id != canonical.id:
                variant.product_id = canonical.id
            for offer in list(product.offers):
                offer.product_id = canonical.id
                offer.variant_id = variant.id
            if product.id == canonical.id:
                continue
            _move_dependents(db, product, canonical)
            for offer in list(db.scalars(select(Offer).where(Offer.product_id == canonical.id,
                                                             Offer.variant_id == variant.id))):
                offer.variant_id = variant.id
            product.redirect_product_id = canonical.id
            product.retired_at = now()
    db.flush()
    out = _summary(groups, applied=True)
    out["applied"] = True
    return out


def main(argv=None) -> int:
    import argparse
    from .db import SessionLocal, init_db
    ap = argparse.ArgumentParser(description="Náhled nebo zápis sloučení variant modelu.")
    ap.add_argument("--apply", action="store_true", help="zapsat změny (bez toho jen náhled)")
    args = ap.parse_args(argv)
    init_db()
    db = SessionLocal()
    try:
        stats = apply_variant_merges(db, dry_run=not args.apply)
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
