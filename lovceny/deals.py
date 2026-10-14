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


def include_demo() -> bool:
    return os.environ.get("LOVCENY_INCLUDE_DEMO", "0") == "1"


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
        select(Product).options(selectinload(Product.offers).selectinload(Offer.points))
        .order_by(Product.created_at.desc()).limit(limit_products)
    ).all()
    now = datetime.now(timezone.utc)
    rows = []
    for p in products:
        priced = [o for o in p.offers if o.price is not None]
        best = min(priced, key=lambda o: float(o.price), default=None)
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


def real_deals(db, *, top: int = 40, fake_limit: int = 20) -> dict:
    rows = product_rows(db)
    buys = sorted((r for r in rows if r.verdict.code == "buy"),
                  key=lambda r: (r.savings_czk, r.savings_pct), reverse=True)[:top]
    fakes = [r for r in rows if r.verdict.code == "fake"][:fake_limit]
    return {"buys": buys, "fakes": fakes, "include_demo": include_demo()}
