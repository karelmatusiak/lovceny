"""Nejoblíbenější produkty s fotkou a cenou ve více obchodech.

Pořadí: zobrazení produktu a hlídání, když jsou. Jinak počet obchodů.
Sleva v pořadí jen když ji spočítáme z naměřeného mediánu. Výběr je
rozložený po kategoriích. Bez AI klasifikace.
"""
from __future__ import annotations

import time
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .availability import best_buyable_offer, offer_is_buyable
from .categories import BY_SLUG, CATEGORIES, is_accessory_text, is_non_new
from .exclusions import is_excluded_brand
from .hero import HeroPick, _badge_for, _bucket, _label, compared_shops, measured_discount
from .images import has_working_image
from .models import AnalyticsEvent, Offer, Product, Watch
from .verdict import compute

_TTL_S = 90
_cache: tuple[float, list[int]] | None = None


def clear_cache() -> None:
    global _cache
    _cache = None


def _views(db) -> dict[int, int]:
    rows = db.execute(
        select(AnalyticsEvent.product_id, func.count(AnalyticsEvent.id))
        .where(AnalyticsEvent.product_id.is_not(None), AnalyticsEvent.kind == "view")
        .group_by(AnalyticsEvent.product_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows if pid is not None}


def _watches(db) -> dict[int, int]:
    rows = db.execute(
        select(Watch.product_id, func.count(Watch.id)).group_by(Watch.product_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows}


def _light(db) -> list[dict]:
    buyable = (
        select(
            Offer.product_id.label("pid"),
            func.count(func.distinct(Offer.shop)).label("n_shops"),
        )
        .where(Offer.price.isnot(None), Offer.in_stock.is_not(False))
        .group_by(Offer.product_id)
        .having(func.count(func.distinct(Offer.shop)) >= 2)
        .subquery()
    )
    rows = db.execute(
        select(Product.id, Product.name, Product.category, Product.category_slug, Product.image_url,
               buyable.c.n_shops)
        .join(buyable, buyable.c.pid == Product.id)
        .where(Product.retired_at.is_(None), Product.catalog_hidden.is_(False),
               Product.needs_review.is_(False),
               Product.image_url.isnot(None), Product.image_url != "")
    ).all()
    out = []
    for pid, name, shop_cat, slug, image, n_shops in rows:
        if is_excluded_brand(name or ""):
            continue
        if not has_working_image(image):
            continue
        if is_accessory_text(name or "", shop_cat) or is_non_new(name, shop_category=shop_cat):
            continue
        if slug not in BY_SLUG:
            continue
        if _bucket(slug, name or "", shop_cat) is None:
            continue
        out.append({"id": int(pid), "slug": slug, "n_shops": int(n_shops or 0)})
    return out


def _diverse(rows: list[dict], views: dict[int, int], watches: dict[int, int], limit: int) -> list[int]:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        views_n = views.get(row["id"], 0)
        watch_n = watches.get(row["id"], 0)
        row["score"] = views_n * 5 + watch_n * 3 + row["n_shops"] * 2
        by_cat[row["slug"]].append(row)
    for items in by_cat.values():
        items.sort(key=lambda r: (r["score"], r["n_shops"], -r["id"]), reverse=True)
    chosen: list[int] = []
    while len(chosen) < limit:
        progressed = False
        for cat in CATEGORIES:
            items = by_cat.get(cat.slug)
            if not items:
                continue
            chosen.append(items.pop(0)["id"])
            progressed = True
            if len(chosen) >= limit:
                break
        if not progressed:
            break
    return chosen


def _finish(db, ids: list[int], limit: int) -> list[HeroPick]:
    if not ids:
        return []
    products = db.scalars(
        select(Product)
        .options(selectinload(Product.offers).selectinload(Offer.points))
        .where(Product.id.in_(ids), Product.retired_at.is_(None))
    ).all()
    by_id = {p.id: p for p in products}
    out: list[HeroPick] = []
    for pid in ids:
        p = by_id.get(pid)
        if p is None or not has_working_image(p.image_url):
            continue
        if is_excluded_brand(p.name or "") or is_accessory_text(p.name or "", p.category):
            continue
        if is_non_new(p.name, shop_category=p.category):
            continue
        best = best_buyable_offer(p.offers)
        if not best or not offer_is_buyable(best) or best.in_stock is False:
            continue
        n_shops = compared_shops(p.offers)
        if n_shops < 2:
            continue
        pts = [pt for o in p.offers for pt in o.points if not getattr(pt, "is_demo", False)]
        days = {pt.captured_at.date() for pt in pts if getattr(pt, "captured_at", None)}
        hist_n = len(days) if days else len(pts)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None)
        cur = float(best.price)
        usual, discount_pct = measured_discount(cur, v, pts, hist_n)
        badge = _badge_for(cur, v, hist_n)
        slug = p.category_slug if p.category_slug in BY_SLUG else ""
        out.append(HeroPick(
            p, best, v, badge if badge == "Nejnižší cena" else None, 0.0,
            category_slug=slug, category_name=_label(slug) if slug else "",
            n_shops=n_shops, usual=usual, discount_pct=discount_pct,
        ))
        if len(out) >= limit:
            break
    return out


def popular_picks(db, *, limit: int = 8) -> list[HeroPick]:
    """Skladem, s fotkou a aspoň dvěma obchody. Napříč kategoriemi."""
    from .offer_trust import use_cutoffs
    use_cutoffs(db)
    limit = max(0, int(limit))
    if limit == 0:
        return []
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < _TTL_S:
        picks = _finish(db, _cache[1], limit)
        if picks:
            return picks
    rows = _light(db)
    # Vezmeme víc kandidátů, ať po kontrole skladu a fotky zbyde celý řádek.
    ids = _diverse(rows, _views(db), _watches(db), limit * 3)
    _cache = (now, ids)
    return _finish(db, ids, limit)
