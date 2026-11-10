"""Výběr produktů do hero showcase na homepage.

Až 6 karet, nejvýš jedna z kategorie a jedna z rodiny barev/variant.
Neřadí se jen 120 nejnovějších: v každé kategorii se díváme do celého katalogu,
ať čerstvý feed neutopí starší dobré produkty. Štítek jen když je pravdivý.
Krátká process-cache (~90 s).
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from .models import Product, Offer, Watch, PricePoint
from .verdict import compute, MIN_DAYS, Verdict
from .deals import _points_for, include_demo
from .availability import best_buyable_offer, offer_is_buyable
from .categories import BY_SLUG, classify, is_accessory_text
from .names import guess_brand
from .shops.feed import variant_family_key

HERO_LIMIT = 6
HERO_TTL_S = 90
# Kolik rodin v jedné kategorii plně skórujeme. Zbytek katalogu se předtím
# seřadí podle značky, počtu obchodů a náznaku slevy – ne podle created_at.
_PER_CAT = 24

# Koncovky, které u stejného modelu neznamenají jiný výrobek (barvu řeší variant_family_key).
_FAMILY_NOISE = frozenset({
    "gaming", "herni", "verze", "variant", "varianta", "edition", "edice",
    "barva", "color", "colour",
})
_APPLE_LINE = re.compile(r"\b(iphone|ipad|macbook|airpods|imac|apple)\b")

_cache: dict[str, tuple[float, list]] = {}


@dataclass
class HeroPick:
    product: Product
    best: Offer
    verdict: Verdict
    badge: str | None  # „−12 %“ / „Nejnižší cena“ / None
    score: float = 0.0
    category_slug: str = ""
    category_name: str = ""


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


def _known_brand(name: str) -> bool:
    """Značka ze seznamu karet, nebo zjevná řada Apple (iPhone není první token „Apple“)."""
    if guess_brand(name or ""):
        return True
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    folded = folded.replace("'", "").replace("’", "")
    if _APPLE_LINE.search(folded):
        return True
    return any(guess_brand(tok) for tok in re.findall(r"[a-z0-9]+", folded))


def _family_key(name: str) -> str:
    """Stejný model v jiné barvě nebo s dovětkem „(gaming variant)“ → jeden klíč.

    Kapacita a řada (Pro/Max) zůstávají, ať se neslije iPhone 15 s 15 Pro ani 128 GB s 256 GB.
    """
    raw = re.sub(r"\([^)]*\)", " ", name or "")
    raw = re.sub(r"\[[^\]]*\]", " ", raw)
    key = variant_family_key(raw)
    toks = key.split()
    while len(toks) > 2 and toks[-1] in _FAMILY_NOISE:
        toks.pop()
    return " ".join(toks)


def _score(cur: float, v: Verdict, hist_n: int, watches: int, *, known: bool) -> float:
    s = float(watches) * 0.5
    if hist_n >= MIN_DAYS and v.median90 and float(v.median90) > 0:
        save = max(0.0, float(v.median90) - cur)
        s += (save / float(v.median90)) * 100  # % úspora vs medián
        if v.min90 is not None and cur <= float(v.min90) * 1.03:
            s += 25  # u minima
        # Drobný tie-break jen když už máme historii. Při tenké historii
        # levnější no-name jinak vyhraje nad normálním výrobkem.
        s += max(0.0, 5000 - min(cur, 5000)) / 5000
    elif hist_n >= 2 and v.max90 and float(v.max90) > cur:
        s += (1 - cur / float(v.max90)) * 40
    if hist_n < MIN_DAYS and known:
        s += 16  # radši známá značka než nejlevnější bezejmenná novinka
    return s


def _bucket(category_slug: str | None, name: str, shop_category: str | None) -> str | None:
    """Kategorie pro rozmanitost. Příslušenství vrací None (do výběru nepatří)."""
    if is_accessory_text(name or "", shop_category):
        return None
    slug = (category_slug or "").strip()
    if slug in BY_SLUG:
        return slug
    guessed = classify(name or "", shop_category)
    if guessed in BY_SLUG:
        return guessed
    return "_ostatni"


def _label(bucket: str) -> str:
    cat = BY_SLUG.get(bucket)
    return cat.name if cat else ""


def _load_watch_counts(db, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(Watch.product_id, func.count(Watch.id))
        .where(Watch.product_id.in_(ids))
        .group_by(Watch.product_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows}


@dataclass(frozen=True)
class _Light:
    id: int
    name: str
    shop_category: str | None
    category_slug: str | None
    n_shops: int
    cur: float
    n_pts: int
    avg_price: float | None
    bucket: str

    def quality(self) -> float:
        """Předvýběr bez created_at: značka, víc obchodů, hlubší historie, náznak slevy."""
        s = float(min(self.n_shops, 4)) * 20 + float(min(self.n_pts, 40)) * 2
        if _known_brand(self.name):
            s += 36
        if self.n_pts >= MIN_DAYS and self.avg_price and self.avg_price > 0:
            s += max(0.0, (self.avg_price - self.cur) / self.avg_price) * 100
        return s

    def deal_hint(self) -> float:
        if self.n_pts < MIN_DAYS or not self.avg_price or self.avg_price <= 0:
            return 0.0
        return max(0.0, (self.avg_price - self.cur) / self.avg_price)


def _light_rows(db) -> list[_Light]:
    """Celý katalog s fotkou a koupitelnou nabídkou – bez řazení podle data přidání."""
    buyable = (
        select(
            Offer.product_id.label("pid"),
            func.count(func.distinct(Offer.shop)).label("n_shops"),
            func.min(Offer.price).label("min_price"),
        )
        .where(Offer.price.isnot(None), Offer.in_stock.is_not(False))
        .group_by(Offer.product_id)
        .subquery()
    )
    pts = (
        select(
            Offer.product_id.label("pid"),
            func.count(PricePoint.id).label("n_pts"),
            func.avg(PricePoint.price).label("avg_price"),
        )
        .join(PricePoint, PricePoint.offer_id == Offer.id)
        .where(PricePoint.is_demo.is_(False))
        .group_by(Offer.product_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Product.id, Product.name, Product.category, Product.category_slug, Product.image_url,
            buyable.c.n_shops, buyable.c.min_price,
            func.coalesce(pts.c.n_pts, 0), pts.c.avg_price,
        )
        .join(buyable, buyable.c.pid == Product.id)
        .outerjoin(pts, pts.c.pid == Product.id)
        .where(Product.retired_at.is_(None), Product.catalog_hidden.is_(False),
               Product.needs_review.is_(False),
               Product.image_url.isnot(None), Product.image_url != "")
    ).all()
    out: list[_Light] = []
    for pid, name, shop_cat, slug, image, n_shops, cur, n_pts, avg in rows:
        if not (image or "").strip() or cur is None:
            continue
        bucket = _bucket(slug, name or "", shop_cat)
        if bucket is None:
            continue
        out.append(_Light(
            id=int(pid), name=name or "", shop_category=shop_cat, category_slug=slug,
            n_shops=int(n_shops or 0), cur=float(cur), n_pts=int(n_pts or 0),
            avg_price=float(avg) if avg is not None else None, bucket=bucket,
        ))
    return out


def _shortlist(rows: list[_Light]) -> list[_Light]:
    """Z každé kategorie pár kandidátů. Sleva s historií nesmí vypadnout kvůli značce."""
    by_cat: dict[str, list[_Light]] = {}
    for row in rows:
        by_cat.setdefault(row.bucket, []).append(row)
    chosen: list[_Light] = []
    for members in by_cat.values():
        if len(members) <= _PER_CAT:
            chosen.extend(members)
            continue
        picked: list[_Light] = []
        seen: set[int] = set()

        def take(row: _Light) -> None:
            if row.id in seen or len(picked) >= _PER_CAT:
                return
            seen.add(row.id)
            picked.append(row)

        for row in sorted(members, key=lambda r: r.deal_hint(), reverse=True):
            if row.deal_hint() <= 0 or len(picked) >= 8:
                break
            take(row)
        for row in sorted(members, key=lambda r: r.quality(), reverse=True):
            take(row)
            if len(picked) >= _PER_CAT:
                break
        chosen.extend(picked)
    return chosen


def _score_products(db, lights: list[_Light]) -> list[HeroPick]:
    if not lights:
        return []
    by_id = {row.id: row for row in lights}
    products = db.scalars(
        select(Product)
        .options(selectinload(Product.offers).selectinload(Offer.points))
        .where(Product.id.in_(list(by_id)), Product.retired_at.is_(None))
    ).all()
    watches = _load_watch_counts(db, list(by_id))
    allow = include_demo()
    now = datetime.now(timezone.utc)
    out: list[HeroPick] = []
    for p in products:
        meta = by_id.get(p.id)
        if meta is None:
            continue
        if not (p.image_url or "").strip():
            continue
        bucket = _bucket(p.category_slug, p.name or "", p.category)
        if bucket is None:
            continue
        best = best_buyable_offer(p.offers)
        if not best or not offer_is_buyable(best):
            continue
        pts, _ = _points_for(p, allow)
        days = {pt.captured_at.date() for pt in pts if getattr(pt, "captured_at", None)}
        hist_n = len(days) if days else len(pts)
        v = compute(pts, float(best.price),
                    float(best.original_price) if best.original_price else None, now=now)
        cur = float(best.price)
        if hist_n < MIN_DAYS and pts:
            vals = [float(pt.price) for pt in pts if pt.price is not None]
            if vals:
                v.min90 = min(vals) if v.min90 is None else v.min90
                v.max90 = max(vals) if v.max90 is None else v.max90
                v.median90 = median(vals) if v.median90 is None and len(vals) >= 2 else v.median90
        known = _known_brand(p.name or "")
        badge = _badge_for(cur, v, hist_n)
        score = _score(cur, v, hist_n, watches.get(p.id, 0), known=known)
        n_shops = len({o.shop for o in p.offers if offer_is_buyable(o) and o.price is not None})
        if n_shops >= 2:
            score += 18.0
        slug = "" if bucket == "_ostatni" else bucket
        out.append(HeroPick(
            p, best, v, badge, score,
            category_slug=slug, category_name=_label(bucket),
        ))
    return out


def _choose(picks: list[HeroPick], limit: int) -> list[HeroPick]:
    """Nejlepší skóre, ale nejvýš jeden kus z kategorie a z rodiny variant."""
    ranked = sorted(picks, key=lambda h: (h.badge is not None, h.score), reverse=True)
    chosen: list[HeroPick] = []
    seen_cat: set[str] = set()
    seen_fam: set[str] = set()
    for h in ranked:
        cat = h.category_slug or "_ostatni"
        fam = _family_key(h.product.name) or f"#{h.product.id}"
        if cat in seen_cat or fam in seen_fam:
            continue
        chosen.append(h)
        seen_cat.add(cat)
        seen_fam.add(fam)
        if len(chosen) >= limit:
            break
    return chosen


def _candidates(db) -> list[HeroPick]:
    lights = _shortlist(_light_rows(db))
    return _score_products(db, lights)


def hero_showcase(db, *, limit: int = HERO_LIMIT) -> list[HeroPick]:
    """Až `limit` karet pro hero. Cache 90 s (id + badge), objekty vždy z aktuální session."""
    from .offer_trust import use_cutoffs
    use_cutoffs(db)
    limit = max(0, int(limit))
    if limit == 0:
        return []
    now = time.time()
    cache_key = f"picks:{limit}"
    hit = _cache.get(cache_key)
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
            bucket = _bucket(p.category_slug, p.name or "", p.category)
            if bucket is None:
                continue
            best = best_buyable_offer(p.offers)
            if not best or not offer_is_buyable(best):
                continue
            pts, _ = _points_for(p, include_demo())
            v = compute(pts, float(best.price),
                        float(best.original_price) if best.original_price else None)
            slug = "" if bucket == "_ostatni" else bucket
            picks.append(HeroPick(
                p, best, v, c["badge"], c["score"],
                category_slug=slug, category_name=_label(bucket),
            ))
            if len(picks) >= limit:
                break
        if picks:
            return picks[:limit]
        # cache miss on rehydrate → fall through

    chosen = _choose(_candidates(db), limit)
    payload = [{"id": h.product.id, "badge": h.badge, "score": h.score} for h in chosen]
    _cache[cache_key] = (now, payload)
    return chosen
