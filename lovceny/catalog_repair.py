"""Idempotentní úklid katalogu: cizí nabídky, duplicity, kategorie.

Nemaze historii cen. Sloučený produkt dostane 301 na přeživšího (redirect_product_id).
Příslušenství zmizí z výpisu kategorie (category_slug = NULL), řádek zůstane.
Spuštění: vlákno po startu webu (port se váže hned), fetch_prices.py a
`python seed.py --repair-catalog`. Zámek brání dvěma běhům najednou.
"""
from __future__ import annotations

import logging
import re
import threading

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .categories import BY_SLUG, _fold, classify, is_accessory_text
from .matching import (
    label_from_url, listing_overlap, primary_brand, same_model, strong_listing_mismatch,
)
from .models import BudgetHit, Offer, Product, PushSubscription, Watch, now
from .shops.feed import names_match
from .util import slugify

log = logging.getLogger("lovceny.catalog")
_LOCK = threading.Lock()
_PG_LOCK = 867530901


def _desired_category(name: str, current: str | None) -> str | None:
    """Jen jistá pravidla. classify() (6.67\" → televize, Xbox v názvu sluchátek) sem nepatří."""
    n = _fold(name or "")
    if is_accessory_text(name or ""):
        return None
    if re.search(r"\bsurface (pro|laptop|go)\b", n):
        return "notebooky"
    # Vrátit řádky, které starý odhad kategorie přesunul špatně.
    if current == "televize" and re.search(r"\bmotorola\b", n) and re.search(r"\bedge\b", n):
        return "telefony"
    if current == "televize" and re.search(r"\bgigabyte\b", n) and re.search(r"\ba16\b", n):
        return "notebooky"
    if current == "televize" and re.search(r"\b(notebook|laptop)\b", n):
        return "notebooky"
    if current == "konzole" and re.search(r"\b(quantum|sluchat|headphone|headset)\b", n):
        return "sluchatka"
    return current


def _redirect_category(name: str) -> str | None:
    """Kam 301, když je řádek vyřazený a kategorii jsme mezitím smazali."""
    n = _fold(name or "")
    if re.search(r"\b(dualsense|dualshock|joy-?con)\b", n):
        return "konzole"
    return None


def _explicit_restore(url: str) -> str | None:
    """Slug obchodu, který nejmenuje výrobek (Svět počítačů u Surface Pro)."""
    u = (url or "").lower()
    if "microsoft-xbox-series-s-1tb" in u:
        return "surface pro"
    return None


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
        if strong_listing_mismatch(p.name or "", label):
            continue
        if names_match(p.name or "", label) or listing_overlap(p.name or "", label):
            target = p
            break
        cat = p.category_slug
        if cat and same_model(p.name or "", cat, label, cat):
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


def _is_split_shell(p: Product) -> bool:
    if p.retired_at is not None or len(p.offers) != 1:
        return False
    offer = p.offers[0]
    return (p.name or "") == label_from_url(offer.url)[:300]


def _host_for_shell(shell: Product, products: list[Product]) -> Product | None:
    offer = shell.offers[0]
    label = label_from_url(offer.url)
    hint = _explicit_restore(offer.url)
    best = None
    best_score = -1
    for p in products:
        if p.id == shell.id or p.redirect_product_id or _is_split_shell(p):
            continue
        name = p.name or ""
        if hint:
            if hint not in _fold(name):
                continue
            score = 100
        elif strong_listing_mismatch(name, label) or not listing_overlap(name, label):
            continue
        else:
            score = 10
        if p.retired_at is not None and not p.offers:
            score += 5
        if score > best_score:
            best, best_score = p, score
    return best


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
        "restored": 0,
        "redirect_fixed": 0,
        "samples": [],
        "audit": [],
    }
    products = list(db.scalars(select(Product)).all())

    # Vrátit nabídky, které minulý běh odtrhl jen kvůli slugu (8/128 → 8128gb, barva, „pro“).
    for shell in list(products):
        if not _is_split_shell(shell):
            continue
        host = _host_for_shell(shell, products)
        if host is None:
            continue
        offer = shell.offers[0]
        stats["restored"] += 1
        stats["audit"].append({
            "id": host.id, "title": host.name, "category": host.category_slug,
            "reason": f"reattach-slug offer {offer.id}",
        })
        if len(stats["samples"]) < 40:
            stats["samples"].append(f"restore #{offer.id} -> #{host.id} {(host.name or '')[:60]}")
        if dry_run:
            continue
        offer.product_id = host.id
        if host.retired_at is not None and not host.redirect_product_id:
            host.retired_at = None
        db.flush()
        shell.redirect_product_id = host.id
        shell.retired_at = now()

    if not dry_run:
        db.flush()
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
            if len(label) < 8 or not strong_listing_mismatch(p.name or "", label):
                continue
            stats["offers_detached"] += 1
            stats["audit"].append({
                "id": p.id, "title": p.name, "category": p.category_slug,
                "reason": f"detach-strong {label[:80]}",
            })
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

    # Vyřazený ovladač bez kategorie by 301 na /. Patří na stránku své kategorie.
    for p in products:
        if p.retired_at is None or p.category_slug or p.redirect_product_id:
            continue
        cat = _redirect_category(p.name or "")
        if not cat:
            continue
        stats["redirect_fixed"] += 1
        stats["audit"].append({
            "id": p.id, "title": p.name, "category": cat, "reason": "retired-accessory-redirect",
        })
        if not dry_run:
            p.category_slug = cat
            p.category = BY_SLUG[cat].name if cat in BY_SLUG else p.category

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
        "Úklid katalogu: accessory=%s recat=%s detach=%s split=%s reattach=%s "
        "restore=%s redirect=%s merge=%s empty=%s",
        stats["accessories_cleared"], stats["recategorized"], stats["offers_detached"],
        stats["offers_split"], stats["offers_reattached"], stats["restored"],
        stats["redirect_fixed"], stats["products_merged"], stats["empty_retired"],
    )
    for line in stats["samples"]:
        log.info("  %s", line)
    return stats


def repair_catalog_locked(db: Session, dry_run: bool = False) -> dict:
    """Jeden běh najednou. Druhý (vlákno webu a fetch_prices) se přeskočí."""
    if not _LOCK.acquire(blocking=False):
        log.info("Úklid katalogu už běží, přeskakuji")
        return {"skipped": "locked", "dry_run": bool(dry_run)}
    locked_pg = False
    try:
        bind = db.get_bind()
        if bind is not None and bind.dialect.name == "postgresql":
            locked_pg = bool(db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _PG_LOCK}).scalar())
            if not locked_pg:
                log.info("Úklid katalogu drží jiný proces, přeskakuji")
                return {"skipped": "locked", "dry_run": bool(dry_run)}
        return repair_catalog(db, dry_run=dry_run)
    finally:
        if locked_pg:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _PG_LOCK})
                db.commit()
            except Exception:
                log.exception("Nepodařilo se odemknout úklid katalogu")
        _LOCK.release()
