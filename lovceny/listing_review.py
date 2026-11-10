"""Produkty bez kategorie a s názvem z adresy stránky nepatří do výpisů,
dokud je někdo neschválí v /admin/trideni. Stránka produktu zůstane dostupná.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .categories import BY_SLUG
from .models import Product, TriageDecision


def name_is_from_url(product) -> bool:
    """Název produktu je pořád jen text složený z adresy některé jeho nabídky."""
    from .service import _name_from_url
    name = (getattr(product, "name", None) or "").strip().casefold()
    if not name:
        return False
    for offer in getattr(product, "offers", None) or []:
        derived = _name_from_url(getattr(offer, "url", None) or "").strip().casefold()
        if derived and derived == name:
            return True
    return False


def listing_withheld(product) -> bool:
    return bool(getattr(product, "needs_review", False))


def queue_listing_review(db: Session) -> dict:
    """Publikuje produkt jen při jisté kategorii a ověřené nabídce. Jinak ho schová.

    Fronta na to nečeká. Opakovaný běh už viditelné produkty nemění.
    """
    from .auto_resolve import review_listings
    stats = review_listings(db)
    db.commit()
    stats["checked"] = stats["published"] + stats["hidden"]
    return stats


def apply_confirm_listing(db: Session, decision) -> tuple[bool, str]:
    product = db.get(Product, decision.product_id) if decision.product_id else None
    if product is None:
        return True, "produkt už není"
    target = decision.proposed_category
    if target in BY_SLUG:
        product.category_slug = target
        product.category = BY_SLUG[target].name
    product.needs_review = False
    product.catalog_hidden = False
    return True, "schváleno do výpisů"
