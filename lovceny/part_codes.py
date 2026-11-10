"""Kódy výrobce, u kterých je model ověřený mimo název sousedního produktu.

MTP03SX/A je iPhone 15 128 GB černý (GTIN 195949036033).
MTP43SX/A je iPhone 15 128 GB modrý (GTIN 195949036576).
Stejná řada, jiná barva: varianta jedné stránky. Jiná řada (15 Pro) sem nepatří.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .migrate_log import log_counts
from .models import Product

# Kód bez regionální přípony /A. Hodnoty jsou z katalogu výrobce, ne z odhadu názvu.
PARTS: dict[str, dict] = {
    "mtp03sx": {
        "line": "iphone-15",
        "storage": "128 GB",
        "color": "černá",
        "gtin": "195949036033",
    },
    "mtp43sx": {
        "line": "iphone-15",
        "storage": "128 GB",
        "color": "modrá",
        "gtin": "195949036576",
    },
}

_CODE_RE = re.compile(r"\b([a-z]{3}\d{2}[a-z]{2,4})\b", re.I)
_GTIN_INDEX = {row["gtin"]: (code, row) for code, row in PARTS.items()}


def _fold_codes(text: str) -> str:
    from .matching import _fold
    return _fold(text or "")


def codes_in(text: str) -> set[str]:
    return {m.group(1).lower() for m in _CODE_RE.finditer(_fold_codes(text)) if m.group(1).lower() in PARTS}


def identify_product(product: Product) -> tuple[str, dict] | None:
    """Řada modelu, pokud ji určí kód výrobce nebo jeho GTIN. Jinak None."""
    blob = " ".join([
        product.name or "",
        product.ean or "",
        *[f"{o.url or ''} {o.gtin or ''}" for o in product.offers],
    ])
    for code in codes_in(blob):
        return PARTS[code]["line"], PARTS[code]
    digits = re.sub(r"\D", "", product.ean or "")
    for offer in product.offers:
        digits += " " + re.sub(r"\D", "", offer.gtin or "")
    for gtin, (code, row) in _GTIN_INDEX.items():
        if gtin and gtin in digits:
            return row["line"], row
    return None


def part_label(row: dict) -> str:
    return ", ".join(part for part in (row.get("storage"), row.get("color")) if part)


def plan_part_code_groups(products: list[Product]) -> list[list[tuple[Product, dict]]]:
    buckets: dict[str, list[tuple[Product, dict]]] = {}
    for product in products:
        if product.retired_at is not None or product.catalog_hidden:
            continue
        found = identify_product(product)
        if found is None:
            continue
        line, row = found
        buckets.setdefault(line, []).append((product, row))
    groups = []
    for members in buckets.values():
        by_id = {}
        for product, row in members:
            by_id.setdefault(product.id, (product, row))
        if len(by_id) >= 2:
            groups.append(sorted(by_id.values(), key=lambda item: item[0].id))
    groups.sort(key=lambda group: group[0][0].id)
    return groups


def apply_part_code_merges(db: Session, *, dry_run: bool = True) -> dict:
    from .variant_merge import merge_variant_group
    products = list(db.scalars(select(Product).options(selectinload(Product.offers))).all())
    groups = plan_part_code_groups(products)
    ids = [[item[0].id for item in group] for group in groups]
    if not dry_run:
        for group in groups:
            ordered = [item[0] for item in group]
            labels = {item[0].id: part_label(item[1]) for item in group}
            merge_variant_group(db, ordered, labels=labels)
    stats = {
        "dry_run": dry_run,
        "groups": len(groups),
        "products": sum(len(group) for group in ids),
        "retired": sum(len(group) - 1 for group in ids),
        "ids": ids,
    }
    log_counts(
        "part_codes",
        dry_run=int(dry_run),
        groups=stats["groups"],
        retired=stats["retired"],
        ids=",".join("-".join(str(i) for i in group) for group in ids) or "-",
    )
    return stats
