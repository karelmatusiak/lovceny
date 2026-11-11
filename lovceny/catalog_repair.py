"""Idempotentní úklid katalogu: cizí nabídky, duplicity, kategorie.

Sloučení, odpojení a změna kategorie se zapíšou hned, když je signál jistý
(kód výrobce, GTIN, nebo kategorie z pravidel). Shoda jen podle názvu nabídku
schová. Historie cen se nemaže, sloučený produkt dostane 301 na nejnižší id,
příslušenství zmizí z výpisu a řádek zůstane.
Spuštění jen ručně: `python seed.py --repair-catalog` (náhled) a `--apply`,
nebo akce v /admin. Start webu ani stahování cen ho nespouští.
Zámek brání dvěma ručním běhům najednou.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .categories import BY_SLUG, _fold, is_accessory_text
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
    # Konkrétní řádky (Motorola Edge, Gigabyte A16) jsou v one_off_fixes.py.
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
    """Dřív tu byl Surface Pro pod adresou Xboxu. Ta oprava je v one_off_fixes.py."""
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
    prod = Product(
        name=label[:300],
        slug=slugify(label)[:200],
        category_slug=None,
        needs_review=True,
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
        "gtin_blocked": 0,
        "queued": 0,
        "samples": [],
        "audit": [],
    }
    from .gtin import gtins_conflict
    from .matching import model_family
    products = list(db.scalars(select(Product)).all())
    # Návrat podle slugu v URL je shoda názvu. Bez GTIN nebo kódu výrobce se neprovede.

    active = [p for p in products if p.retired_at is None and not p.catalog_hidden]

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
        if dry_run:
            continue
        if kind == "accessory":
            p.category_slug = None
            p.category = None
            p.needs_review = True
        elif new_slug in BY_SLUG:
            p.category_slug = new_slug
            p.category = BY_SLUG[new_slug].name

    for p in active:
        if p.retired_at is not None:
            continue
        for offer in list(p.offers):
            if getattr(offer, "withheld", False):
                continue
            label = label_from_url(offer.url)
            if len(label) < 8 or not strong_listing_mismatch(p.name or "", label):
                continue
            stats["offers_detached"] += 1
            stats["audit"].append({
                "id": p.id, "title": p.name, "category": p.category_slug,
                "reason": f"detach-strong {label[:80]}",
            })
            if len(stats["samples"]) < 30:
                stats["samples"].append(f"detach #{offer.id} from #{p.id} {label[:80]}")
            if dry_run:
                continue
            from .auto_resolve import settle_disputed_offer
            settle_disputed_offer(db, offer, label=label, products=products)

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
    fresh = [p for p in products if p.retired_at is None and not p.catalog_hidden]
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
            if model_family(survivor.name or "", survivor.category_slug) != model_family(
                loser.name or "", loser.category_slug
            ):
                continue
            if model_family(survivor.name or "", survivor.category_slug) is None:
                continue
            if gtins_conflict(survivor, loser):
                stats["gtin_blocked"] += 1
                if len(stats["samples"]) < 30:
                    stats["samples"].append(
                        f"gtin-block #{loser.id} /= #{survivor.id} {(loser.name or '')[:60]}"
                    )
                continue
            stats["products_merged"] += 1
            if len(stats["samples"]) < 30:
                stats["samples"].append(
                    f"merge #{loser.id} -> #{survivor.id} {(loser.name or '')[:60]}"
                )
            if dry_run:
                continue
            from .variant_merge import merge_variant_group
            merge_variant_group(db, [survivor, loser])

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


def execute_decision(db: Session, decision) -> tuple[bool, str]:
    """Provede schválené rozhodnutí. Slug, URL a historii cen nemaže."""
    kind = decision.kind
    if kind in ("recategorize", "fallback_category", "unmapped_category"):
        return _apply_category(db, decision)
    if kind == "title_match":
        return _apply_title(db, decision)
    if kind == "variant_merge":
        from .variant_merge import apply_variant_proposal
        return apply_variant_proposal(db, decision)
    if kind == "merge":
        return _apply_merge(db, decision)
    if kind == "detach":
        return _apply_detach(db, decision)
    if kind == "model_mismatch":
        return True, "nepřipojeno, jiný model"
    if kind == "confirm_listing":
        from .listing_review import apply_confirm_listing
        return apply_confirm_listing(db, decision)
    if kind == "reattach":
        return _apply_reattach(db, decision)
    if kind == "retire":
        return _apply_retire(db, decision)
    return False, "neznámý typ rozhodnutí"


def _payload(decision) -> dict:
    if not decision.payload:
        return {}
    try:
        data = json.loads(decision.payload)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _apply_category(db: Session, decision) -> tuple[bool, str]:
    product = db.get(Product, decision.product_id)
    if product is None:
        return True, "produkt už neexistuje"
    from .exclusions import bury_excluded, is_excluded_brand
    if is_excluded_brand(product.name or ""):
        bury_excluded(product)
        return True, "značka je mimo katalog"
    target = decision.proposed_category
    if target in BY_SLUG:
        product.category_slug = target
        product.category = BY_SLUG[target].name
        product.catalog_hidden = False
        return True, f"kategorie {BY_SLUG[target].name}"
    product.category_slug = None
    product.category = None
    if decision.kind in ("unmapped_category", "fallback_category") and target in ("accessory", "ignore", None, ""):
        product.catalog_hidden = True
    return True, "kategorie zrušena"


def _apply_title(db: Session, decision) -> tuple[bool, str]:
    from .service import record_price
    product = db.get(Product, decision.product_id)
    if product is None or product.retired_at is not None:
        return False, "produkt už není aktivní"
    data = _payload(decision)
    url = data.get("url") or ""
    if not url:
        return False, "chybí URL nabídky"
    existing = db.scalar(select(Offer).where(Offer.url == url))
    if existing is not None and existing.product_id != product.id:
        return False, "URL už patří jinému produktu"
    if existing is None:
        existing = Offer(
            product=product, shop=data.get("shop") or decision.shop or "", url=url,
            external_id=data.get("item_id"), status="pending",
        )
        db.add(existing)
        db.flush()
    if data.get("shop_category"):
        existing.shop_category = data["shop_category"]
    if data.get("gtin"):
        existing.gtin = data["gtin"]
    record_price(db, existing, data.get("price"), data.get("original_price"), data.get("in_stock"), "feed")
    return True, f"nabídka připojena k produktu #{product.id}"


def _apply_merge(db: Session, decision) -> tuple[bool, str]:
    from .gtin import gtins_conflict
    survivor = db.get(Product, decision.product_id)
    loser = db.get(Product, decision.other_product_id)
    if survivor is None or loser is None or survivor.id == loser.id:
        return True, "už sloučeno"
    if survivor.retired_at is not None or loser.retired_at is not None:
        return True, "už není aktivní"
    if gtins_conflict(survivor, loser):
        return False, "různá GTIN, sloučení je zablokované"
    _move_dependents(db, loser, survivor)
    db.flush()
    loser.redirect_product_id = survivor.id
    loser.retired_at = now()
    return True, f"#{loser.id} sloučen do #{survivor.id}"


def _apply_detach(db: Session, decision) -> tuple[bool, str]:
    offer = db.get(Offer, decision.offer_id)
    if offer is None:
        return True, "nabídka už není"
    data = _payload(decision)
    label = data.get("label") or label_from_url(offer.url)
    origin_id = offer.product_id
    products = list(db.scalars(select(Product)).all())
    _relocate_offer(db, offer, label, products)
    origin = db.get(Product, origin_id)
    clear = data.get("clear_ean")
    if origin is not None and clear and origin.ean == clear:
        origin.ean = None
    if origin is not None and origin.id != offer.product_id:
        n_offers = db.scalar(select(func.count()).select_from(Offer).where(Offer.product_id == origin.id)) or 0
        if n_offers == 0 and origin.retired_at is None and not origin.redirect_product_id:
            origin.retired_at = now()
    return True, "nabídka odpojena"


def _apply_reattach(db: Session, decision) -> tuple[bool, str]:
    offer = db.get(Offer, decision.offer_id)
    host = db.get(Product, decision.product_id)
    shell = db.get(Product, decision.other_product_id)
    if offer is None or host is None or shell is None:
        return True, "už neplatí"
    offer.product_id = host.id
    if host.retired_at is not None and not host.redirect_product_id:
        host.retired_at = None
    if shell.id != host.id:
        shell.redirect_product_id = host.id
        shell.retired_at = now()
    return True, "nabídka vrácena"


def _apply_retire(db: Session, decision) -> tuple[bool, str]:
    product = db.get(Product, decision.product_id)
    if product is None or product.retired_at is not None:
        return True, "už vyřazeno"
    product.retired_at = now()
    return True, "vyřazeno"


def repair_catalog_locked(db: Session, dry_run: bool = False) -> dict:
    """Jeden ruční běh najednou. Druhý se přeskočí."""
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
