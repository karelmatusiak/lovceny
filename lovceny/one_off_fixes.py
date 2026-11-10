"""Jednorázové opravy, které už v katalogu proběhly a nepatří do obecného úklidu.

Motorola Edge a Gigabyte A16 omylem v televizích, Surface Pro schovaný pod
adresou Xboxu. Spouští se jen ručně (`seed.py --one-off-fixes`, bez `--apply`
jen náhled). Start webu ani stahování cen je nespouští.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .categories import BY_SLUG, _fold
from .models import Product, now


def _legacy_category(name: str, current: str | None) -> str | None:
    folded = _fold(name or "")
    if current == "televize" and re.search(r"\bmotorola\b", folded) and re.search(r"\bedge\b", folded):
        return "telefony"
    if current == "televize" and re.search(r"\bgigabyte\b", folded) and re.search(r"\ba16\b", folded):
        return "notebooky"
    return None


def apply_legacy_product_fixes(db: Session, *, apply: bool = False) -> dict:
    stats = {"dry_run": not apply, "recategorized": 0, "reattached": 0, "samples": []}
    products = list(db.scalars(select(Product).options(selectinload(Product.offers))).all())
    for product in products:
        if product.retired_at is not None:
            continue
        target = _legacy_category(product.name or "", product.category_slug)
        if not target or target == product.category_slug:
            continue
        stats["recategorized"] += 1
        stats["samples"].append(
            f"category #{product.id} {product.category_slug}->{target} {(product.name or '')[:80]}"
        )
        if apply:
            product.category_slug = target
            product.category = BY_SLUG[target].name if target in BY_SLUG else product.category

    hosts = [p for p in products if "surface pro" in _fold(p.name or "")]
    for product in products:
        for offer in list(product.offers):
            if "microsoft-xbox-series-s-1tb" not in (offer.url or "").lower():
                continue
            host = next((item for item in hosts if item.id != product.id), None)
            if host is None or offer.product_id == host.id:
                continue
            stats["reattached"] += 1
            stats["samples"].append(f"reattach #{offer.id} -> #{host.id} {(host.name or '')[:60]}")
            if apply:
                offer.product_id = host.id
                if host.retired_at is not None and not host.redirect_product_id:
                    host.retired_at = None
                if product.id != host.id and product.retired_at is None:
                    product.redirect_product_id = host.id
                    product.retired_at = now()
    if apply:
        db.commit()
    else:
        db.rollback()
    return stats
