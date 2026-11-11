"""Skutečné slevy: produkty s verdiktem Kupuj (u 90denního minima), řazené podle úspory
oproti mediánu 90 dní; a samostatný seznam „Za 30 dní bylo levněji“ (interní kód verdiktu „fake“).

Demo body (PricePoint.is_demo) se v produkci z výpočtu vyřazují (LOVCENY_INCLUDE_DEMO≠1).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Product, Offer, PricePoint
from .verdict import compute, Verdict
from .availability import best_buyable_offer
from .categories import is_accessory_text
from .matching import decent_title

# Skutečná sleva: aspoň 3 % pod obvyklou cenou (medián 90 dní). Samotné „jsme na minimu“
# u ploché historie sleva není.
MIN_DEAL_DROP_PCT = 3.0


def include_demo() -> bool:
    return os.environ.get("LOVCENY_INCLUDE_DEMO", "0") == "1"


# Krátká cache vypočtených řádků. Nová cena (price_points / offers) změní razítko hned,
# i když stahování běží v jiném procesu. TTL jen pro posun „teď“ (okno 90 dní, čerstvost).
_DEALS_TTL_S = 7 * 60
_deals_cache: dict[tuple, tuple[float, tuple, dict]] = {}
_deals_lock = Lock()


def clear_cache() -> None:
    with _deals_lock:
        _deals_cache.clear()


class LitePoint:
    """Cena z jednoho SELECTu (bez ORM PricePoint). compute()/daily_best čtou jen tyhle atributy."""
    __slots__ = ("price", "captured_at", "is_demo")

    def __init__(self, price, captured_at, is_demo):
        self.price = price
        self.captured_at = captured_at
        self.is_demo = bool(is_demo)


def catalog_stamp(db) -> tuple:
    """Razítko dat pro cache. Nový bod, nová nabídka i aktualizace last_checked ho změní."""
    from sqlalchemy import text
    row = db.execute(text(
        "SELECT (SELECT MAX(id) FROM price_points),"
        " (SELECT MAX(id) FROM offers),"
        " (SELECT MAX(last_checked) FROM offers)"
    )).one()
    return (row[0] or 0, row[1] or 0, str(row[2] or ""))


def load_offers_for(db, products) -> dict[int, list]:
    """Nabídky produktů jedním dotazem (ne selectinload po 500)."""
    pids = [p.id for p in products if getattr(p, "id", None) is not None]
    by: dict[int, list] = {pid: [] for pid in pids}
    if not pids:
        return by
    chunk = 2000
    for i in range(0, len(pids), chunk):
        part = pids[i:i + chunk]
        for o in db.scalars(select(Offer).where(Offer.product_id.in_(part))):
            by.setdefault(o.product_id, []).append(o)
    return by


def load_points_for_offers(db, offer_ids) -> dict[int, list]:
    """Jen sloupce, které verdikt potřebuje. Index (offer_id, captured_at)."""
    ids = [i for i in dict.fromkeys(offer_ids) if i is not None]
    by: dict[int, list] = {}
    if not ids:
        return by
    chunk = 2000
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        rows = db.execute(
            select(PricePoint.offer_id, PricePoint.price, PricePoint.captured_at, PricePoint.is_demo)
            .where(PricePoint.offer_id.in_(part))
        )
        for oid, price, captured, demo in rows:
            by.setdefault(oid, []).append(LitePoint(price, captured, demo))
    return by


def points_from_map(offers, by_offer: dict, allow_demo: bool) -> tuple[list, bool]:
    pts, saw_demo = [], False
    for o in offers:
        for pt in by_offer.get(o.id, ()):
            if pt.is_demo:
                saw_demo = True
                if not allow_demo:
                    continue
            pts.append(pt)
    return pts, saw_demo


@dataclass
class DealRow:
    product: Product
    best: Offer
    verdict: Verdict
    savings_czk: float  # medián − aktuální (≥ 0 u Kupuj)
    savings_pct: float
    has_demo: bool = False


def _points_for(p: Product, allow_demo: bool) -> tuple[list, bool]:
    pts, saw_demo = [], False
    for o in p.offers:
        for pt in o.points:
            if pt.is_demo:
                saw_demo = True
                if not allow_demo:
                    continue
            pts.append(pt)
    return pts, saw_demo


def _series_offers(product, offers):
    """Do verdiktu patří jen nabídka stejného výrobku. Sporná a stažená cena medián nehne."""
    from .offer_trust import offer_mismatches_product
    kept = []
    for offer in offers:
        if getattr(offer, "withheld", False):
            continue
        if offer_mismatches_product(offer, product):
            continue
        kept.append(offer)
    return kept


def public_products(db, *, limit_products: int | None = None) -> list:
    """Všechny veřejné produkty, od nejstaršího id. Okno „500 nejnovějších“ vynechalo
    produkty s týdnem cen, protože nově zveřejněné ho zaplnily."""
    from .exclusions import is_excluded_brand
    rows = db.scalars(
        select(Product).where(
            Product.retired_at.is_(None), Product.catalog_hidden.is_(False), Product.needs_review.is_(False))
        .order_by(Product.id.asc())
    ).all()
    rows = [p for p in rows if not is_excluded_brand(p.name or "")]
    if limit_products is not None:
        return list(rows[:limit_products])
    return list(rows)


def product_rows(db, *, allow_demo: bool | None = None, limit_products: int | None = None) -> list[DealRow]:
    allow = include_demo() if allow_demo is None else allow_demo
    from .offer_trust import use_cutoffs
    use_cutoffs(db)
    products = public_products(db, limit_products=limit_products)
    offers_by = load_offers_for(db, products)
    pending = []
    offer_ids = []
    for p in products:
        if is_accessory_text(p.name or "", p.category) or not decent_title(p.name or ""):
            continue
        offs = _series_offers(p, offers_by.get(p.id, []))
        best = best_buyable_offer(offs)
        if not best or best.in_stock is False:
            continue
        pending.append((p, offs, best))
        offer_ids.extend(o.id for o in offs)
    by_pts = load_points_for_offers(db, offer_ids)
    now = datetime.now(timezone.utc)
    rows = []
    for p, offs, best in pending:
        pts, saw_demo = points_from_map(offs, by_pts, allow)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None, now=now)
        if v.code not in ("buy", "fake"):
            continue
        md = float(v.median90 or 0)
        cur = float(v.current or best.price)
        save = max(0.0, md - cur) if md else 0.0
        pct = (save / md * 100) if md else 0.0
        if v.code == "buy" and pct < MIN_DEAL_DROP_PCT:
            continue
        rows.append(DealRow(p, best, v, save, pct, has_demo=saw_demo and allow))
    return rows


def _row_payload(r: DealRow) -> dict:
    return {
        "id": r.product.id,
        "offer_id": r.best.id,
        "savings_czk": r.savings_czk,
        "savings_pct": r.savings_pct,
        "has_demo": r.has_demo,
        "verdict": r.verdict,
    }


def _rehydrate(db, payloads: list[dict]) -> list[DealRow]:
    """Obnoví DealRow z aktuální session. Verdikt je ze snímku – znovu nenačítá historii."""
    if not payloads:
        return []
    ids = [p["id"] for p in payloads]
    products = db.scalars(
        select(Product)
        .where(Product.id.in_(ids), Product.retired_at.is_(None))
        .options(selectinload(Product.offers))
    ).all()
    by_id = {p.id: p for p in products}
    out: list[DealRow] = []
    for pl in payloads:
        p = by_id.get(pl["id"])
        if not p:
            continue
        from .offer_trust import offer_is_promoted
        if p.needs_review:
            continue
        best = next((o for o in p.offers if o.id == pl.get("offer_id")), None)
        if best is not None and not offer_is_promoted(best, product=p):
            best = None
        if best is None:
            best = best_buyable_offer(p.offers)
        if not best:
            continue
        out.append(DealRow(p, best, pl["verdict"], float(pl["savings_czk"]), float(pl["savings_pct"]),
                           has_demo=bool(pl.get("has_demo"))))
    return out


def _name_rows(rows: list[DealRow]) -> None:
    from .names import uniquify_short_names
    prods = [r.product for r in rows if getattr(r, "product", None)]
    if not prods:
        return
    shorts = uniquify_short_names([p.name or "" for p in prods])
    for p, s in zip(prods, shorts):
        p.display_name = s


def _full_payload(db) -> tuple[dict, bool, tuple[list, list] | None]:
    """Celé seznamy Kupuj/fake (před ořezem top). Sdílí je homepage (top=3) i /slevy.

    Třetí hodnota jsou právě spočítané řádky (bez druhého SELECTu). None = cache hit.
    """
    import time
    allow = include_demo()
    stamp = catalog_stamp(db)
    now = time.monotonic()
    key = ("full", allow)
    with _deals_lock:
        hit = _deals_cache.get(key)
        if hit and hit[1] == stamp and now - hit[0] < _DEALS_TTL_S:
            return hit[2], allow, None
    rows = product_rows(db, allow_demo=allow)
    buys = sorted((r for r in rows if r.verdict.code == "buy"),
                  key=lambda r: (r.savings_czk, r.savings_pct), reverse=True)
    fakes = [r for r in rows if r.verdict.code == "fake"]
    payload = {
        "buys": [_row_payload(r) for r in buys],
        "fakes": [_row_payload(r) for r in fakes],
    }
    with _deals_lock:
        _deals_cache[key] = (now, stamp, payload)
    return payload, allow, (buys, fakes)


def real_deals(db, *, top: int = 40, fake_limit: int = 20) -> dict:
    """Kupuj / fake seznamy. Stejný výstup; cache drží verdikt, hit nenačítá price_points."""
    payload, allow, fresh = _full_payload(db)
    if fresh is not None:
        buys, fakes = fresh[0][:int(top)], fresh[1][:int(fake_limit)]
    else:
        buys_pl = (payload.get("buys") or [])[:int(top)]
        fakes_pl = (payload.get("fakes") or [])[:int(fake_limit)]
        by_id = {r.product.id: r for r in _rehydrate(db, buys_pl + fakes_pl)}
        buys = [by_id[p["id"]] for p in buys_pl if p["id"] in by_id]
        fakes = [by_id[p["id"]] for p in fakes_pl if p["id"] in by_id]
    _name_rows(buys + fakes)
    return {"buys": buys, "fakes": fakes, "include_demo": allow}


def near_minima(db, *, limit: int = 12, max_over: float = 0.12) -> list[DealRow]:
    """Produkty blízko 90denního minima (pro prázdný /slevy fallback).

    Samostatný sken (ne product_rows) – ten filtruje jen buy/fake. Sem patří i
    verdikt Počkej, pokud je cena do max_over nad min90. Kupuj vynecháváme.
    """
    import time
    allow = include_demo()
    stamp = catalog_stamp(db)
    now_m = time.monotonic()
    key = ("near", int(limit), float(max_over), allow)
    with _deals_lock:
        hit = _deals_cache.get(key)
        if hit and hit[1] == stamp and now_m - hit[0] < _DEALS_TTL_S:
            cached = hit[2]
        else:
            cached = None
    if cached is not None:
        out = _rehydrate(db, cached)[:limit]
        _name_rows(out)
        return out
    from .offer_trust import use_cutoffs
    use_cutoffs(db)
    products = public_products(db)
    offers_by = load_offers_for(db, products)
    pending = []
    offer_ids = []
    for p in products:
        if is_accessory_text(p.name or "", p.category) or not decent_title(p.name or ""):
            continue
        offs = _series_offers(p, offers_by.get(p.id, []))
        best = best_buyable_offer(offs)
        if not best or best.in_stock is False:
            continue
        pending.append((p, offs, best))
        offer_ids.extend(o.id for o in offs)
    by_pts = load_points_for_offers(db, offer_ids)
    now = datetime.now(timezone.utc)
    scored: list[tuple[float, DealRow]] = []
    for p, offs, best in pending:
        pts, saw_demo = points_from_map(offs, by_pts, allow)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None, now=now)
        if v.code == "buy":
            continue
        mn = v.min90
        if mn is None:
            continue
        cur = float(best.price)
        base = float(mn)
        if base <= 0:
            continue
        over = max(0.0, (cur / base) - 1.0)
        if over > max_over:
            continue
        md = float(v.median90 or 0)
        save = max(0.0, md - cur) if md else 0.0
        pct = (save / md * 100) if md else 0.0
        scored.append((over, DealRow(p, best, v, save, pct, has_demo=saw_demo and allow)))
    scored.sort(key=lambda x: (x[0], -x[1].savings_pct, -x[1].savings_czk))
    out = [r for _, r in scored[:limit]]
    with _deals_lock:
        _deals_cache[key] = (now_m, stamp, [_row_payload(r) for r in out])
    _name_rows(out)
    return out

