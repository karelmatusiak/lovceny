"""Idempotentní úklid katalogu: cizí nabídky, duplicity, kategorie.

Nemaze historii cen. Sloučený produkt dostane 301 na přeživšího (redirect_product_id).
Příslušenství zmizí z výpisu kategorie (category_slug = NULL), řádek zůstane.
Spuštění: start aplikace, fetch_prices.py a `python seed.py --repair-catalog`.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .categories import BY_SLUG, category_keywords_hit, classify, is_accessory_text
from .matching import label_from_url, primary_brand, same_model
from .models import BudgetHit, Offer, Product, PushSubscription, Watch, now
from .shops.feed import names_match, offer_conflicts_product
from .util import slugify

log = logging.getLogger("lovceny.catalog")


def _desired_category(name: str, current: str | None) -> str | None:
    if is_accessory_text(name or ""):
        return None
    guessed = classify(name or "")
    if current and guessed and guessed != current and not category_keywords_hit(name or "", current):
        return guessed
    return current


def _survivor_key(p: Product) -> tuple:
    shops = {o.shop for o in p.offers}
    non_allegro = sum(1 for s in shops if s != "allegro")
    return (len(shops), non_allegro, -p.id)


def _move_dependents(db: Session, loser: Product, survivor: Product) -> None:
    for offer in list(loser.offers):
        offer.product_id = survivor.id
    for watch in db.scalars(select(Watch).where(Watch.product_id == loser.id)):
        watch.product_id = survivor.id
    for sub in db.scalars(select(PushSubscription).where(PushSubscription.product_id == loser.id)):
        sub.product_id = survivor.id
    for hit in db.scalars(select(BudgetHit).where(BudgetHit.product_id == loser.id)):
        taken = db.scalar(select(BudgetHit.id).where(
            BudgetHit.budget_id == hit.budget_id, BudgetHit.product_id == survivor.id))
        if taken:
            db.delete(hit)
        else:
            hit.product_id = survivor.id


def _relocate_offer(db: Session, offer: Offer, label: str, products: list[Product]) -> str:
    """Přesuň nabídku na produkt, kterému sedí název. Jinak založ nový a historii nech u nabídky."""
    target = None
    for p in products:
        if p.retired_at is not None or p.id == offer.product_id:
            continue
        if names_match(p.name or "", label) and not offer_conflicts_product(p.name or "", offer.url, label):
            target = p
            break
        cat = p.category_slug
        if cat and same_model(p.name or "", cat, label, cat) and not offer_conflicts_product(p.name or "", offer.url, label):
            target = p
            break
    if target is not None:
        offer.product_id = target.id
        return "reattached"
    cat = classify(label)
    prod = Product(
        name=label[:300],
        slug=slugify(label)[:200],
        category_slug=cat,
        category=BY_SLUG[cat].name if cat and cat in BY_SLUG else None,
    )
    db.add(prod)
    db.flush()
    products.append(prod)
    offer.product_id = prod.id
    return "split"


def repair_catalog(db: Session, dry_run: bool = False) -> dict:
    """Vrátí počty. dry_run nic nezapíše (rollback). Opakovaný běh je no-op."""
    stats = {
        "dry_run": bool(dry_run),
        "accessories_cleared": 0,
        "recategorized": 0,
        "offers_detached": 0,
        "offers_reattached": 0,
        "offers_split": 0,
        "products_merged": 0,
        "empty_retired": 0,
        "samples": [],
    }
    products = list(db.scalars(select(Product)).all())
    active = [p for p in products if p.retired_at is None]

    for p in active:
        new_slug = _desired_category(p.name or "", p.category_slug)
        if new_slug == p.category_slug:
            continue
        if new_slug is None and is_accessory_text(p.name or ""):
            stats["accessories_cleared"] += 1
            kind = "accessory"
        else:
            stats["recategorized"] += 1
            kind = "move"
        if len(stats["samples"]) < 30:
            stats["samples"].append(f"{kind} #{p.id} {p.category_slug}->{new_slug} {(p.name or '')[:80]}")
        if not dry_run:
            p.category_slug = new_slug
            p.category = BY_SLUG[new_slug].name if new_slug and new_slug in BY_SLUG else None

    if not dry_run:
        db.flush()

    detached_hosts: set[int] = set()
    for p in active:
        if p.retired_at is not None:
            continue
        for offer in list(p.offers):
            label = label_from_url(offer.url)
            if len(label) < 8 or not offer_conflicts_product(p.name or "", offer.url, label):
                continue
            stats["offers_detached"] += 1
            detached_hosts.add(p.id)
            if len(stats["samples"]) < 30:
                stats["samples"].append(f"detach #{offer.id} from #{p.id} {label[:80]}")
            if dry_run:
                continue
            how = _relocate_offer(db, offer, label, products)
            stats["offers_split" if how == "split" else "offers_reattached"] += 1
    if not dry_run:
        db.flush()
        for pid in detached_hosts:
            n_offers = db.scalar(select(func.count()).select_from(Offer).where(Offer.product_id == pid)) or 0
            if n_offers:
                continue
            host = db.get(Product, pid)
            if host is None or host.retired_at is not None:
                continue
            host.retired_at = now()
            stats["empty_retired"] += 1
            if len(stats["samples"]) < 30:
                stats["samples"].append(f"retire-empty #{pid} {(host.name or '')[:80]}")

    # Sloučení duplicit mezi tím, co zůstalo aktivní. Jen stejná kategorie a značka.
    fresh = [p for p in products if p.retired_at is None]
    if not dry_run:
        fresh = list(db.scalars(select(Product).where(Product.retired_at.is_(None))).all())
    buckets: dict[tuple, list[Product]] = {}
    for p in fresh:
        brand = primary_brand(p.name or "")
        if not brand or not p.category_slug:
            continue
        buckets.setdefault((p.category_slug, brand), []).append(p)
    groups: list[list[Product]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for p in bucket:
            placed = False
            for group in groups:
                if group[0].category_slug != p.category_slug:
                    continue
                if same_model(p.name or "", p.category_slug, group[0].name or "", group[0].category_slug):
                    group.append(p)
                    placed = True
                    break
            if not placed:
                groups.append([p])

    for group in groups:
        if len(group) < 2:
            continue
        ranked = sorted(group, key=_survivor_key, reverse=True)
        survivor = ranked[0]
        for loser in ranked[1:]:
            if loser.id == survivor.id or loser.retired_at is not None:
                continue
            if not same_model(survivor.name or "", survivor.category_slug, loser.name or "", loser.category_slug):
                continue
            stats["products_merged"] += 1
            if len(stats["samples"]) < 30:
                stats["samples"].append(
                    f"merge #{loser.id} -> #{survivor.id} {(loser.name or '')[:60]}"
                )
            if dry_run:
                continue
            _move_dependents(db, loser, survivor)
            db.flush()
            loser.redirect_product_id = survivor.id
            loser.retired_at = now()
            # EAN necháváme. apply_feed u retired řádku s redirect_product_id
            # pokračuje na přeživšího a novou nabídku připojí tam.

    if dry_run:
        db.rollback()
    else:
        db.commit()
    log.info(
        "Úklid katalogu: accessory=%s recat=%s detach=%s split=%s reattach=%s merge=%s empty=%s",
        stats["accessories_cleared"], stats["recategorized"], stats["offers_detached"],
        stats["offers_split"], stats["offers_reattached"], stats["products_merged"],
        stats["empty_retired"],
    )
    for line in stats["samples"]:
        log.info("  %s", line)
    return stats
