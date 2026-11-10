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
    """Označí nezařazené a beznázvové produkty a vloží je do fronty. Opakovaný běh je no-op."""
    from .triage import enqueue
    approved = set(db.scalars(
        select(TriageDecision.product_id).where(
            TriageDecision.kind == "confirm_listing",
            TriageDecision.status == "approved",
            TriageDecision.product_id.isnot(None),
        )
    ).all())
    products = list(db.scalars(
        select(Product)
        .where(Product.retired_at.is_(None))
        .options(selectinload(Product.offers))
    ).all())
    flagged = 0
    queued = 0
    for product in products:
        if product.id in approved:
            continue
        if product.catalog_hidden and product.category_slug:
            continue
        missing_category = not product.category_slug
        from_url = name_is_from_url(product)
        if not missing_category and not from_url:
            continue
        if not product.needs_review:
            product.needs_review = True
            flagged += 1
        why = []
        if missing_category:
            why.append("nemá kategorii")
        if from_url:
            why.append("název je složený z adresy stránky")
        reason = "Výpis čeká na schválení: " + " a ".join(why) + "."
        if enqueue(
            db, kind="confirm_listing", fingerprint=f"confirm_listing:{product.id}",
            summary=f"Schválit „{(product.name or '')[:80]}“ do výpisů",
            reason=reason, product_id=product.id, proposed_category=product.category_slug,
        ):
            queued += 1
    db.commit()
    return {"flagged": flagged, "queued": queued, "checked": len(products)}


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
