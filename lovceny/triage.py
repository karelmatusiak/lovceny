"""Deterministické třídění: mapování kategorií obchodu a záznam rozhodnutí.

Kategorii určuje jen tabulka mapování. Co v ní není, se na web nedostane.
Stejné platné GTIN se připojí samo. /admin/trideni je jen záznam, neschvaluje se.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .categories import (
    BY_SLUG,
    CATALOG_RULE_SHOPS,
    FEED_CATEGORY_WHITELIST,
    MIN_PRICE_CZK,
    _ALLEGRO_SUFFIXES,
    _path_segments,
    _stem_at_word,
    category_url_conflict,
    classify_feed_item,
    is_accessory_text,
    jbl_catalog_category,
)
from .gtin import gtins_conflict, normalize_gtin
from .models import ShopCategoryMapping, TriageDecision, TriageLog, now

# Obchody, které ve feedu posílají cestu kategorie (Heureka, Google, Dognet/eHUB).
FEED_SHOPS: tuple[str, ...] = (
    "alza", "datart",
    "istyle", "sencor", "xm", "evolveo", "pocitace24", "smarty",
    "electronicstar", "electrolux", "4home", "dyson", "jbl",
    "allegro",
)
# Heureka / Google / Dognet cesty, kde „příslušenství“ v rodiči není list.
_LEAF_ONLY_SHOPS = frozenset({"allegro"})

# Stemy listu / úseku → accessory. „kabel“ nesedí na „bezkabelové“ (_stem_at_word).
_ACCESSORY_STEMS: tuple[tuple[str, str], ...] = (
    ("prislusenstvi", "Příslušenství"),
    ("filtry", "Filtry"),
    ("filtr", "Filtr"),
    ("obaly", "Obaly"),
    ("obal", "Obal"),
    ("kabely", "Kabely"),
    ("kabel", "Kabel"),
    ("pouzdra", "Pouzdra"),
    ("kryty", "Kryty"),
    ("folie", "Fólie"),
    ("skla", "Skla"),
    ("brasny", "Brašny"),
    ("batohy", "Batohy"),
    ("nahradni", "Náhradní díly"),
    ("nabijeck", "Nabíječky"),
    ("adapter", "Adaptéry"),
    ("baterie", "Baterie"),
    ("powerbank", "Powerbanky"),
    ("sacky", "Sáčky"),
)

_KIND_ORDER = {
    "reattach": 10,
    "recategorize": 20,
    "fallback_category": 30,
    "unmapped_category": 30,
    "detach": 40,
    "model_mismatch": 45,
    "confirm_listing": 48,
    "title_match": 50,
    "variant_merge": 55,
    "merge": 60,
    "retire": 70,
}

CATEGORY_KINDS = frozenset({"unmapped_category", "fallback_category", "recategorize"})


def path_key(raw: str | None) -> str:
    return " | ".join(_path_segments(raw))


def category_label(slug: str | None) -> str:
    if slug and slug in BY_SLUG:
        return BY_SLUG[slug].name
    return {"accessory": "příslušenství", "ignore": "ignorovat", None: "nezařazeno", "": "nezařazeno"}.get(
        slug, slug or "nezařazeno")


def _segment_has_hint(segment: str, hint: str) -> bool:
    """Stejně jako whitelist: prefix úseku, a u vysavačů i celé slovo uprostřed úseku."""
    if segment == hint or segment.startswith(hint):
        return True
    if hint in ("vysavac", "vysavace"):
        return re.search(rf"(^| ){re.escape(hint)}( |$)", segment) is not None
    return False


def _blocked(target: str | None, key: str) -> bool:
    if target == "lednice" and re.search(r"vinotek|zrani|napoj|minibar|autochlad", key):
        return True
    if target == "vysavace" and re.search(r"listi|jezerkov|prumysl", key):
        return True
    return False


def _plan() -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple] = set()

    def add(shop: str, mode: str, key: str, label: str, target: str, leaf_only: bool = False):
        key = (key or "").strip()
        if not key or not shop:
            return
        ident = (shop, mode, key)
        if ident in seen:
            return
        seen.add(ident)
        rows.append({
            "shop": shop, "match_mode": mode, "path_key": key[:300], "path_label": label[:300],
            "target": target, "leaf_only": leaf_only, "source": "seed",
        })

    for shop in FEED_SHOPS:
        leaf = shop in _LEAF_ONLY_SHOPS
        for stem, label in _ACCESSORY_STEMS:
            add(shop, "stem", stem, label, "accessory", leaf_only=leaf)
        for slug, hints in FEED_CATEGORY_WHITELIST.items():
            for hint in hints:
                key = path_key(hint)
                add(shop, "segment", key, hint, slug)
    for suffix, slug in _ALLEGRO_SUFFIXES:
        key = path_key(suffix.replace(">", "|"))
        add("allegro", "suffix", key, suffix, slug)
    return rows


def ensure_mappings(db: Session) -> int:
    """Doplní chybějící seed. Existující řádky (včetně úprav z adminu) nepřepisuje."""
    have = {
        (shop, mode, key)
        for shop, mode, key in db.execute(
            select(ShopCategoryMapping.shop, ShopCategoryMapping.match_mode, ShopCategoryMapping.path_key)
        ).all()
    }
    for row in _plan():
        ident = (row["shop"], row["match_mode"], row["path_key"])
        if ident in have:
            continue
        db.add(ShopCategoryMapping(**row))
        have.add(ident)
    db.flush()
    return int(db.scalar(
        select(func.count()).select_from(ShopCategoryMapping).where(ShopCategoryMapping.source == "seed")
    ) or 0)


def lookup_mapping(db: Session, shop: str, raw: str | None, rows: list[ShopCategoryMapping] | None = None) -> str | None:
    """Nejkonkrétnější pravidlo: exact, pak příslušenství, pak sufix cesty, pak úsek."""
    key = path_key(raw)
    if not key or not shop:
        return None
    if rows is None:
        rows = list(db.scalars(select(ShopCategoryMapping).where(ShopCategoryMapping.shop == shop)).all())
    exact = [r for r in rows if r.match_mode == "exact" and r.path_key == key]
    if exact:
        exact.sort(key=lambda r: r.source == "admin", reverse=True)
        target = exact[0].target
        return None if _blocked(target, key) else target
    segs = key.split(" | ")
    leaf = segs[-1]
    for row in rows:
        if row.match_mode != "stem" or row.target != "accessory":
            continue
        scope = [leaf] if row.leaf_only else segs
        if any(_stem_at_word(" " + seg + " ", row.path_key) for seg in scope):
            return "accessory"
    best = None
    best_len = -1
    for row in rows:
        if row.match_mode != "suffix":
            continue
        if key == row.path_key or key.endswith(" | " + row.path_key):
            if len(row.path_key) > best_len and not _blocked(row.target, key):
                best, best_len = row, len(row.path_key)
    if best is not None:
        return best.target
    best = None
    best_len = -1
    for row in rows:
        if row.match_mode != "segment":
            continue
        hint = row.path_key
        if any(_segment_has_hint(seg, hint) for seg in segs):
            if len(hint) > best_len and not _blocked(row.target, key):
                best, best_len = row, len(hint)
    return best.target if best is not None else None


class Resolved:
    def __init__(self, action: str, slug: str | None = None):
        self.action = action  # skip | accessory | high | low | unmapped
        self.slug = slug


def resolve_category(db: Session, shop: str, name: str, shop_category: str | None,
                     price: float | None, url: str | None = None,
                     rows: list[ShopCategoryMapping] | None = None) -> Resolved:
    """Jediné automatické rozhodnutí o kategorii: tabulka mapování.

    JBL feed nemá cestu kategorie. Známé řady sluchátek dostanou cestu „Sluchátka“
    a dál rozhodne stejná tabulka. Obecný odhad z názvu se nepoužívá.
    Nové feedy (Dognet/eHUB 2026-11) rozhoduje classify_feed_item: co pravidlo
    nezařadí, se nezaloží a nečeká ve frontě.
    """
    if shop in CATALOG_RULE_SHOPS:
        slug, reason = classify_feed_item(shop, name, shop_category, price, url)
        if reason == "accessory":
            return Resolved("accessory")
        if reason == "ok" and slug:
            return Resolved("high", slug)
        return Resolved("skip")
    if is_accessory_text(name, shop_category):
        return Resolved("accessory")
    raw = shop_category
    if shop == "jbl" and not (raw or "").strip():
        if jbl_catalog_category(name) == "sluchatka":
            raw = "Sluchátka"
    mapped = lookup_mapping(db, shop, raw, rows=rows)
    if mapped == "accessory":
        return Resolved("accessory")
    if mapped == "ignore":
        return Resolved("skip")
    if mapped in BY_SLUG:
        if category_url_conflict(mapped, name, url):
            return Resolved("skip")
        if price is None or price <= 0 or price < MIN_PRICE_CZK.get(mapped, 0):
            return Resolved("skip")
        return Resolved("high", mapped)
    if (raw or "").strip() and price is not None and price >= 500:
        return Resolved("unmapped")
    return Resolved("skip")


def enqueue(db: Session, *, kind: str, fingerprint: str, summary: str, reason: str,
            shop: str | None = None, shop_category: str | None = None,
            product_id: int | None = None, offer_id: int | None = None,
            other_product_id: int | None = None, proposed_category: str | None = None,
            payload: dict | None = None) -> bool:
    """True = nový pending řádek. Stejný otisk (i zamítnutý) se znovu nezakládá."""
    fp = fingerprint[:200]
    hit = db.scalar(select(TriageDecision.id).where(TriageDecision.fingerprint == fp))
    if hit:
        return False
    db.add(TriageDecision(
        fingerprint=fp, status="pending", kind=kind, shop=shop,
        shop_category=(shop_category or None),
        product_id=product_id, offer_id=offer_id, other_product_id=other_product_id,
        proposed_category=proposed_category, summary=summary[:300], reason=reason[:2000],
        payload=json.dumps(payload, ensure_ascii=False) if payload else None,
        created_at=now(),
    ))
    db.flush()
    return True


def log_decision(db: Session, decision: TriageDecision | None, actor: str, action: str, detail: str) -> None:
    db.add(TriageLog(
        decision_id=decision.id if decision is not None else None,
        actor=(actor or "admin")[:80], action=action[:32], detail=(detail or "")[:2000],
        created_at=now(),
    ))


def approve(db: Session, decision: TriageDecision, actor: str, *, category: str | None = None) -> tuple[bool, str]:
    if decision.status != "pending":
        return False, "Rozhodnutí už není otevřené."
    if category:
        if category not in BY_SLUG and category not in ("accessory", "ignore"):
            return False, "Neznámá kategorie."
        decision.proposed_category = category
    from .catalog_repair import execute_decision
    ok, detail = execute_decision(db, decision)
    if not ok:
        log_decision(db, decision, actor, "approve_failed", detail)
        return False, detail
    decision.status = "approved"
    log_decision(db, decision, actor, "approve", detail or decision.summary)
    return True, detail


def reject(db: Session, decision: TriageDecision, actor: str) -> None:
    if decision.status != "pending":
        return
    decision.status = "rejected"
    log_decision(db, decision, actor, "reject", decision.summary)


def approve_pending(db: Session, actor: str = "test") -> int:
    rows = list(db.scalars(select(TriageDecision).where(TriageDecision.status == "pending")).all())
    rows.sort(key=lambda d: (_KIND_ORDER.get(d.kind, 100), d.id))
    n = 0
    for row in rows:
        ok, _detail = approve(db, row, actor)
        if ok:
            n += 1
    db.commit()
    return n


def map_shop_category(db: Session, shop: str, shop_category: str, target: str, actor: str) -> int:
    """Zapamatuje exact mapování a schválí čekající položky se stejnou cestou."""
    if target not in BY_SLUG and target not in ("accessory", "ignore"):
        return 0
    key = path_key(shop_category)
    if not key or not shop:
        return 0
    row = db.scalar(select(ShopCategoryMapping).where(
        ShopCategoryMapping.shop == shop,
        ShopCategoryMapping.match_mode == "exact",
        ShopCategoryMapping.path_key == key,
    ))
    if row is None:
        db.add(ShopCategoryMapping(
            shop=shop, match_mode="exact", path_key=key[:300], path_label=(shop_category or key)[:300],
            target=target, leaf_only=False, source="admin",
        ))
    else:
        row.target = target
        row.source = "admin"
        row.path_label = (shop_category or key)[:300]
    db.flush()
    pending = list(db.scalars(select(TriageDecision).where(
        TriageDecision.status == "pending",
        TriageDecision.shop == shop,
        TriageDecision.kind.in_(tuple(CATEGORY_KINDS)),
    )).all())
    applied = 0
    for decision in pending:
        if path_key(decision.shop_category or "") != key:
            continue
        ok, _detail = approve(db, decision, actor, category=target)
        if ok:
            applied += 1
    log_decision(db, None, actor, "map", f"{shop}: {shop_category} → {category_label(target)} ({applied})")
    return applied


def queue_category(db: Session, *, kind: str, product_id: int, shop: str | None,
                   shop_category: str | None, proposed: str | None, summary: str, reason: str) -> bool:
    slug = proposed or "none"
    return enqueue(
        db, kind=kind, fingerprint=f"{kind}:{product_id}:{slug}",
        summary=summary, reason=reason, shop=shop, shop_category=shop_category,
        product_id=product_id, proposed_category=proposed,
    )


def queue_model_mismatch(db: Session, *, shop: str, product_id: int, item, clean_url: str) -> bool:
    """Nabídka, jejíž adresa nebo název patří k jinému modelu, se nepřipojí. Čeká ve frontě."""
    return enqueue(
        db, kind="model_mismatch", fingerprint=f"mismatch:{shop}:{clean_url}"[:200],
        summary=f"Nepřipojovat „{(item.name or '')[:80]}“ k produktu #{product_id}",
        reason=("Adresa nebo název nabídky patří k jinému modelu nebo značce. "
                "Sama se k produktu nepřipojí. Schválení ji k tomuto produktu nepřidá."),
        shop=shop, shop_category=getattr(item, "category", None), product_id=product_id,
        payload={
            "url": clean_url, "shop": shop, "name": item.name, "price": item.price,
            "original_price": item.original_price, "in_stock": item.in_stock,
            "item_id": item.item_id, "image": item.image, "shop_category": item.category,
            "ean": item.ean,
        },
    )


def queue_title_match(db: Session, *, shop: str, product_id: int, item, clean_url: str, gtin: str | None) -> bool:
    return enqueue(
        db, kind="title_match", fingerprint=f"title:{shop}:{clean_url}",
        summary=f"Připojit „{item.name[:80]}“ k produktu #{product_id} podle názvu",
        reason="Nabídka nemá platné GTIN. Shoda názvu je nízká jistota a čeká na schválení.",
        shop=shop, shop_category=item.category, product_id=product_id,
        payload={
            "url": clean_url, "shop": shop, "name": item.name, "price": item.price,
            "original_price": item.original_price, "in_stock": item.in_stock,
            "item_id": item.item_id, "image": item.image, "shop_category": item.category,
            "gtin": gtin, "ean": item.ean,
        },
    )


def queue_detach(db: Session, *, offer_id: int, product_id: int, shop: str, label: str,
                 shop_category: str | None, clear_ean: str | None, reason: str) -> bool:
    return enqueue(
        db, kind="detach", fingerprint=f"detach:{offer_id}",
        summary=f"Odpojit nabídku #{offer_id} od produktu #{product_id}",
        reason=reason, shop=shop, shop_category=shop_category,
        product_id=product_id, offer_id=offer_id,
        payload={"label": label[:300], "clear_ean": clear_ean},
    )
