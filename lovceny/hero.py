"""Výběr produktů do hero showcase na homepage.

Jen produkty s cenou i obrázkem. Preferuje největší reálnou slevu oproti
mediánu (příp. maximu) při dostatku historie; při řídké historii doplní
nejsledovanější a nejnovější s cenou. Krátká process-cache (~90 s).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from .models import Product, Offer, Watch
from .verdict import compute, MIN_DAYS, Verdict
from .deals import _points_for, include_demo

HERO_LIMIT = 4
HERO_TTL_S = 90
_SCAN = 120

_cache: dict[str, tuple[float, list]] = {}


@dataclass
class HeroPick:
    product: Product
    best: Offer
    verdict: Verdict
    badge: str | None  # „−12 %“ / „Nejnižší cena“ / None
    score: float = 0.0


def clear_cache() -> None:
    _cache.clear()


def _badge_for(cur: float, v: Verdict, hist_n: int) -> str | None:
    """Štítek jen když je pravdivý – nehádat při řídké historii."""
    if hist_n < MIN_DAYS:
        # Tenká historie: štítek jen když je aktuální cena zjevně pod maximem z naměřených bodů
        if v.max90 and cur < float(v.max90) * 0.97:
            pct = round((1 - cur / float(v.max90)) * 100)
            if pct >= 5:
                return f"−{pct} %"
        return None
    if v.min90 is not None and cur <= float(v.min90) * 1.005:
        return "Nejnižší cena"
    if v.median90 and float(v.median90) > 0 and cur < float(v.median90):
        pct = round((1 - cur / float(v.median90)) * 100)
        if pct >= 3:
            return f"−{pct} %"
    if v.max90 and float(v.max90) > 0 and cur < float(v.max90) * 0.95:
        pct = round((1 - cur / float(v.max90)) * 100)
        if pct >= 8:
            return f"−{pct} %"
    return None


def _score(cur: float, v: Verdict, hist_n: int, watches: int) -> float:
    s = float(watches) * 0.5
    if hist_n >= MIN_DAYS and v.median90 and float(v.median90) > 0:
        save = max(0.0, float(v.median90) - cur)
        s += (save / float(v.median90)) * 100  # % úspora vs medián
        if v.min90 is not None and cur <= float(v.min90) * 1.03:
            s += 25  # u minima
    elif hist_n >= 2 and v.max90 and float(v.max90) > cur:
        s += (1 - cur / float(v.max90)) * 40
    # lehce preferuj levnější populární položky při shodě
    s += max(0.0, 5000 - min(cur, 5000)) / 5000
    return s


def _load_watch_counts(db, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(Watch.product_id, func.count(Watch.id))
        .where(Watch.product_id.in_(ids))
        .group_by(Watch.product_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows}


def _candidates(db) -> list[HeroPick]:
    allow = include_demo()
    products = db.scalars(
        select(Product)
        .options(selectinload(Product.offers).selectinload(Offer.points))
        .where(Product.image_url.isnot(None), Product.image_url != "")
        .order_by(Product.created_at.desc())
        .limit(_SCAN)
    ).all()
    watches = _load_watch_counts(db, [p.id for p in products])
    now = datetime.now(timezone.utc)
    out: list[HeroPick] = []
    for p in products:
        if not (p.image_url or "").strip():
            continue
        priced = [o for o in p.offers if o.price is not None]
        best = min(priced, key=lambda o: float(o.price), default=None)
        if not best:
            continue
        pts, _ = _points_for(p, allow)
        # denní unikátní body pro hist_n
        days = {pt.captured_at.date() for pt in pts if getattr(pt, "captured_at", None)}
        hist_n = len(days) if days else len(pts)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None, now=now)
        cur = float(best.price)
        # doplň max/min z bodů i při thin (compute nastaví none)
        if hist_n < MIN_DAYS and pts:
            vals = [float(pt.price) for pt in pts if pt.price is not None]
            if vals:
                v.min90 = min(vals) if v.min90 is None else v.min90
                v.max90 = max(vals) if v.max90 is None else v.max90
                v.median90 = median(vals) if v.median90 is None and len(vals) >= 2 else v.median90
        badge = _badge_for(cur, v, hist_n)
        score = _score(cur, v, hist_n, watches.get(p.id, 0))
        out.append(HeroPick(p, best, v, badge, score))
    return out


def hero_showcase(db, *, limit: int = HERO_LIMIT) -> list[HeroPick]:
    """Až `limit` karet pro hero. Cache 90 s (id + badge), objekty vždy z aktuální session."""
    now = time.time()
    hit = _cache.get("picks")
    if hit and now - hit[0] < HERO_TTL_S:
        cached = hit[1]
        if not cached:
            return []
        ids = [c["id"] for c in cached]
        products = db.scalars(
            select(Product)
            .options(selectinload(Product.offers).selectinload(Offer.points))
            .where(Product.id.in_(ids))
        ).all()
        by_id = {p.id: p for p in products}
        picks: list[HeroPick] = []
        for c in cached:
            p = by_id.get(c["id"])
            if not p or not (p.image_url or "").strip():
                continue
            priced = [o for o in p.offers if o.price is not None]
            best = min(priced, key=lambda o: float(o.price), default=None)
            if not best:
                continue
            pts, _ = _points_for(p, include_demo())
            v = compute(pts, float(best.price),
                        float(best.original_price) if best.original_price else None)
            picks.append(HeroPick(p, best, v, c["badge"], c["score"]))
            if len(picks) >= limit:
                break
        if picks:
            return picks[:limit]
        # cache miss on rehydrate → fall through

    cands = _candidates(db)
    # 1) se slevovým skóre / štítkem napřed
    ranked = sorted(cands, key=lambda h: (h.badge is not None, h.score), reverse=True)
    chosen: list[HeroPick] = []
    seen: set[int] = set()
    for h in ranked:
        if h.product.id in seen:
            continue
        chosen.append(h)
        seen.add(h.product.id)
        if len(chosen) >= limit:
            break
    # 2) kdyby málo, doplň podle skóre (už sorted)
    payload = [{"id": h.product.id, "badge": h.badge, "score": h.score} for h in chosen]
    _cache["picks"] = (now, payload)
    return chosen[:limit]
