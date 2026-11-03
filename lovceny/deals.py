"""Skutečné slevy: produkty s verdiktem Kupuj (u 90denního minima), řazené podle úspory
oproti mediánu 90 dní; a samostatný seznam „Za 30 dní bylo levněji“ (interní kód verdiktu „fake“).

Demo body (PricePoint.is_demo) se v produkci z výpočtu vyřazují (LOVCENY_INCLUDE_DEMO≠1).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Product, Offer
from .verdict import compute, Verdict
from .availability import best_buyable_offer


def include_demo() -> bool:
    return os.environ.get("LOVCENY_INCLUDE_DEMO", "0") == "1"


# Homepage volá real_deals(top=3) – krátký TTL, aby se neběžel product_rows(500) na každý request.
_DEALS_TTL_S = 90
_deals_cache: dict[tuple, tuple[float, dict]] = {}


def clear_cache() -> None:
    _deals_cache.clear()


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


def product_rows(db, *, allow_demo: bool | None = None, limit_products: int = 500) -> list[DealRow]:
    allow = include_demo() if allow_demo is None else allow_demo
    products = db.scalars(
        select(Product).where(Product.retired_at.is_(None))
        .options(selectinload(Product.offers).selectinload(Offer.points))
        .order_by(Product.created_at.desc()).limit(limit_products)
    ).all()
    now = datetime.now(timezone.utc)
    rows = []
    for p in products:
        best = best_buyable_offer(p.offers)
        if not best:
            continue
        pts, saw_demo = _points_for(p, allow)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None, now=now)
        if v.code not in ("buy", "fake"):
            continue
        md = float(v.median90 or 0)
        cur = float(v.current or best.price)
        save = max(0.0, md - cur) if md else 0.0
        pct = (save / md * 100) if md else 0.0
        rows.append(DealRow(p, best, v, save, pct, has_demo=saw_demo and allow))
    return rows


def _row_payload(r: DealRow) -> dict:
    return {
        "id": r.product.id,
        "savings_czk": r.savings_czk,
        "savings_pct": r.savings_pct,
        "has_demo": r.has_demo,
        "verdict_code": r.verdict.code,
    }


def _rehydrate(db, payloads: list[dict], allow_demo: bool) -> list[DealRow]:
    """Obnoví DealRow z aktuální session (cache drží jen id + čísla, ne ORM)."""
    if not payloads:
        return []
    ids = [p["id"] for p in payloads]
    products = db.scalars(
        select(Product)
        .where(Product.id.in_(ids), Product.retired_at.is_(None))
        .options(selectinload(Product.offers).selectinload(Offer.points))
    ).all()
    by_id = {p.id: p for p in products}
    now = datetime.now(timezone.utc)
    out: list[DealRow] = []
    for pl in payloads:
        p = by_id.get(pl["id"])
        if not p:
            continue
        best = best_buyable_offer(p.offers)
        if not best:
            continue
        pts, _ = _points_for(p, allow_demo)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None, now=now)
        out.append(DealRow(p, best, v, float(pl["savings_czk"]), float(pl["savings_pct"]),
                           has_demo=bool(pl.get("has_demo"))))
    return out


def real_deals(db, *, top: int = 40, fake_limit: int = 20) -> dict:
    """Kupuj / fake seznamy. Stejný výstup; krátká in‑process cache (id payload + rehydrate)."""
    import time
    from .names import uniquify_short_names
    key = (int(top), int(fake_limit), include_demo())
    hit = _deals_cache.get(key)
    now = time.monotonic()
    allow = include_demo()
    if hit and now - hit[0] < _DEALS_TTL_S:
        cached = hit[1]
        buys = _rehydrate(db, cached.get("buys") or [], allow)
        fakes = _rehydrate(db, cached.get("fakes") or [], allow)
        prods = [r.product for r in buys + fakes]
        if prods:
            shorts = uniquify_short_names([p.name or "" for p in prods])
            for p, s in zip(prods, shorts):
                p.display_name = s
        return {"buys": buys, "fakes": fakes, "include_demo": allow}
    # Stejný candidate set jako dřív (limit 500) – TTL šetří opakované miss, ne mění výstup.
    rows = product_rows(db)
    buys = sorted((r for r in rows if r.verdict.code == "buy"),
                  key=lambda r: (r.savings_czk, r.savings_pct), reverse=True)[:top]
    fakes = [r for r in rows if r.verdict.code == "fake"][:fake_limit]
    prods = [r.product for r in buys + fakes if getattr(r, "product", None)]
    if prods:
        shorts = uniquify_short_names([p.name or "" for p in prods])
        for p, s in zip(prods, shorts):
            p.display_name = s
    _deals_cache[key] = (now, {
        "buys": [_row_payload(r) for r in buys],
        "fakes": [_row_payload(r) for r in fakes],
    })
    return {"buys": buys, "fakes": fakes, "include_demo": allow}
