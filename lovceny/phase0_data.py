"""Jednorázové opravy dat z auditu 10. 11. 2026. Idempotentní, nespouští úklid katalogu.

Odpojí pět nabídek, které visí na cizím produktu. Historie cen zůstane u nabídky,
id a adresa původního produktu taky. Nový řádek pro odpojenou nabídku se do
výpisů nedostane, dokud ho někdo neschválí.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .matching import label_from_url
from .models import Product
from .util import slugify

# (id produktu, nápověda v názvu produktu, co musí být v adrese cizí nabídky)
# iPhone 15 se pozná zvlášť, ať se nestrhne iPhone 16e.
_RULES: tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...] = (
    (328, ("pixel", "10"), ("motorola", "signature")),
    (420, ("electrolux",), ("larma",)),
    (452, ("jabra",), ("guess",)),
    (2, ("iphone", "16"), ("iphone", "15")),
)


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _offer_text(offer) -> str:
    url = offer.url or ""
    return _fold(f"{url} {label_from_url(url)}")


def _is_foreign(text: str, needles: tuple[str, ...]) -> bool:
    if needles == ("iphone", "15"):
        return bool(re.search(r"\biphone 15\b", text)) and "iphone 16" not in text
    return all(part in text for part in needles)


def _product_matches(name: str, hint: tuple[str, ...]) -> bool:
    folded = _fold(name)
    return all(part in folded for part in hint)


def detach_known_bad_offers(db: Session, *, apply: bool = False) -> dict:
    """Najde cizí nabídky na známých produktech. Bez apply jen spočítá a vypíše."""
    stats = {"dry_run": not apply, "detached": 0, "samples": [], "offer_ids": []}
    products = {
        p.id: p for p in db.scalars(
            select(Product).where(Product.id.in_([rule[0] for rule in _RULES]))
            .options(selectinload(Product.offers))
        ).all()
    }
    for product_id, hint, needles in _RULES:
        product = products.get(product_id)
        if product is None or not _product_matches(product.name or "", hint):
            continue
        for offer in list(product.offers):
            text = _offer_text(offer)
            if not _is_foreign(text, needles):
                continue
            stats["detached"] += 1
            stats["offer_ids"].append(offer.id)
            line = f"detach offer #{offer.id} from #{product.id} {label_from_url(offer.url)[:80]}"
            stats["samples"].append(line)
            if apply:
                _move_off(db, offer, label_from_url(offer.url) or f"nabidka {offer.id}")
    if apply:
        db.commit()
    else:
        db.rollback()
    return stats


def _move_off(db: Session, offer, label: str) -> None:
    """Přesune nabídku na vlastní skrytý řádek. Ceny jedou s ní, původní produkt zůstane."""
    name = (label or "Nabídka").strip()[:300]
    held = Product(
        name=name,
        slug=(slugify(name) or f"nabidka-{offer.id}")[:200],
        category_slug=None,
        category=None,
        needs_review=True,
    )
    db.add(held)
    db.flush()
    offer.product_id = held.id


def apply_phase0_data(db: Session) -> dict:
    """Nasazení: odpojí známé cizí nabídky a zařadí nezařazené do fronty. Opakovaně no-op."""
    detached = detach_known_bad_offers(db, apply=True)
    from .listing_review import queue_listing_review
    review = queue_listing_review(db)
    return {"detached": detached, "review": review}
