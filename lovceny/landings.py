"""Statické vstupní stránky z vlastních dat: nejlevnější řada a kategorie do ceny.

Stránka vznikne jen při aspoň MIN_ITEMS produktech, které teď jdou koupit a jsou skladem.
Pod prahem je adresa 301 na kategorii (ne 404). Stejná sada produktů se na vyšší strop
neopakuje — ten strop vede na levnější stránku, která už tu sadu má.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from .categories import BY_SLUG, is_accessory_text
from .honesty import offer_matches_product, prague_day
from .util import cz_count, czk

MIN_ITEMS = 3
MAX_SHOWN = 48
_CACHE_TTL_S = 7 * 60
_lock = Lock()
_cache: tuple[float, tuple, list] | None = None


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None


@dataclass(frozen=True)
class SeriesSpec:
    slug: str
    label: str
    category: str
    pattern: re.Pattern

    @property
    def path(self) -> str:
        return f"/nejlevnejsi/{self.slug}"


@dataclass(frozen=True)
class CapSpec:
    category: str
    amount: int

    @property
    def path(self) -> str:
        return f"/kategorie/{self.category}/do/{self.amount}"


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern)


SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("iphone-16", "iPhone 16", "telefony", _rx(r"\biphone\s*16\b")),
    SeriesSpec("iphone-15", "iPhone 15", "telefony", _rx(r"\biphone\s*15\b")),
    SeriesSpec("iphone-14", "iPhone 14", "telefony", _rx(r"\biphone\s*14\b")),
    SeriesSpec("galaxy-s25", "Galaxy S25", "telefony", _rx(r"\bgalaxy\s*s25\b")),
    SeriesSpec("galaxy-s24", "Galaxy S24", "telefony", _rx(r"\bgalaxy\s*s24\b")),
    SeriesSpec("galaxy-a55", "Galaxy A55", "telefony", _rx(r"\bgalaxy\s*a55\b")),
    SeriesSpec("galaxy-a54", "Galaxy A54", "telefony", _rx(r"\bgalaxy\s*a54\b")),
    SeriesSpec("pixel-9", "Pixel 9", "telefony", _rx(r"\bpixel\s*9\b")),
    SeriesSpec("airpods", "AirPods", "sluchatka", _rx(r"\bairpods\b")),
    SeriesSpec("ps5", "PS5", "konzole", _rx(r"\b(?:ps\s*5|playstation\s*5)\b")),
    SeriesSpec("xbox-series", "Xbox Series", "konzole", _rx(r"\bxbox\s+series\b")),
    SeriesSpec("nintendo-switch", "Nintendo Switch", "konzole", _rx(r"\bnintendo\s+switch\b")),
    SeriesSpec("macbook-air", "MacBook Air", "notebooky", _rx(r"\bmacbook\s+air\b")),
    SeriesSpec("macbook-pro", "MacBook Pro", "notebooky", _rx(r"\bmacbook\s+pro\b")),
    SeriesSpec("dyson", "Dyson", "vysavace", _rx(r"\bdyson\b")),
    SeriesSpec("roborock", "Roborock", "vysavace", _rx(r"\broborock\b")),
    SeriesSpec("apple-watch", "Apple Watch", "chytre-hodinky", _rx(r"\bapple\s+watch\b")),
    SeriesSpec("galaxy-watch", "Galaxy Watch", "chytre-hodinky", _rx(r"\bgalaxy\s+watch\b")),
)
SERIES_BY_SLUG = {s.slug: s for s in SERIES}

# Kulaté stropy, od nižšího. Vyšší se nepublikuje, když má stejné produkty jako nižší hotová stránka.
PRICE_CAPS: dict[str, tuple[int, ...]] = {
    "telefony": (5000, 10000, 15000, 20000),
    "notebooky": (15000, 20000, 30000),
    "pocitace": (15000, 25000),
    "sluchatka": (1000, 2000, 5000),
    "pracky": (10000, 15000, 20000),
    "lednice": (10000, 15000, 25000),
    "kavovary": (5000, 10000, 15000),
    "vysavace": (3000, 5000, 10000),
    "konzole": (10000, 15000),
    "chytre-hodinky": (3000, 5000, 10000),
    "fotoaparaty": (10000, 20000),
    "televize": (10000, 15000, 25000),
    "chytra-domacnost": (1000, 3000, 5000),
}


def series_specs() -> tuple[SeriesSpec, ...]:
    return SERIES


def cap_specs() -> list[CapSpec]:
    return [CapSpec(slug, amount) for slug, amounts in PRICE_CAPS.items() for amount in amounts]


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return raw


@dataclass
class Landing:
    kind: str
    path: str
    h1: str
    title: str
    description: str
    intro: str
    category_slug: str
    rows: list
    total: int
    price_min: float | None
    price_max: float | None
    updated: datetime | None
    ready: bool
    redirect: str

    @property
    def updated_label(self) -> str:
        day = prague_day(self.updated)
        return f"{day.day}. {day.month}. {day.year}" if day else ""


def _intro(h1: str, total: int, lo: float, hi: float, updated: datetime | None) -> str:
    text = (
        f"{h1}: {cz_count(total, 'produkt skladem', 'produkty skladem', 'produktů skladem')}. "
        f"Ceny jsou od {czk(lo)} do {czk(hi)}."
    )
    day = prague_day(updated)
    if day:
        text += f" Naposledy jsme cenu ověřili {day.day}. {day.month}. {day.year}."
    text += " Řazeno jen podle ceny, bez placených pozic."
    return text


def _ready(kind: str, path: str, h1: str, category: str, chosen: list, redirect: str) -> Landing:
    chosen = sorted(chosen, key=lambda h: (float(h.offer.price), h.product.id or 0))
    prices = [float(h.offer.price) for h in chosen]
    checks = [h.offer.last_checked for h in chosen if getattr(h.offer, "last_checked", None)]
    updated = max(checks) if checks else None
    lo, hi = min(prices), max(prices)
    intro = _intro(h1, len(chosen), lo, hi, updated)
    return Landing(
        kind, path, h1, f"{h1} – aktuální ceny", intro, intro, category,
        chosen[:MAX_SHOWN], len(chosen), lo, hi, updated, True, redirect,
    )


def _thin(kind: str, path: str, h1: str, category: str, redirect: str) -> Landing:
    return Landing(kind, path, h1, h1, "", "", category, [], 0, None, None, None, False, redirect)


def _stock_best(product, offers, cutoffs):
    from .offer_trust import offer_is_promoted
    usable = []
    for offer in offers:
        if offer.in_stock is not True or offer.price is None:
            continue
        if not offer_matches_product(offer, product):
            continue
        if not offer_is_promoted(offer, cutoffs, product):
            continue
        usable.append(offer)
    return min(usable, key=lambda o: float(o.price), default=None)


@dataclass
class _Hit:
    product: object
    offer: object


def _hits(db) -> list[_Hit]:
    from .deals import load_offers_for, public_products
    from .matching import decent_title
    from .offer_trust import use_cutoffs
    cutoffs = use_cutoffs(db)
    products = public_products(db)
    offers_by = load_offers_for(db, products)
    out = []
    for product in products:
        if not product.category_slug or product.category_slug not in BY_SLUG:
            continue
        if is_accessory_text(product.name or "", product.category) or not decent_title(product.name or ""):
            continue
        best = _stock_best(product, offers_by.get(product.id, []), cutoffs)
        if best is None:
            continue
        out.append(_Hit(product, best))
    return out


def _build(db) -> list[Landing]:
    hits = _hits(db)
    by_cat: dict[str, list[_Hit]] = {}
    for hit in hits:
        by_cat.setdefault(hit.product.category_slug, []).append(hit)
    pages: list[Landing] = []
    for spec in SERIES:
        cat = BY_SLUG[spec.category]
        matched = [h for h in by_cat.get(spec.category, []) if spec.pattern.search(_fold(h.product.name or ""))]
        target = f"/kategorie/{cat.slug}"
        h1 = f"Nejlevnější {spec.label}"
        if len(matched) < MIN_ITEMS:
            pages.append(_thin("series", spec.path, h1, cat.slug, target))
        else:
            pages.append(_ready("series", spec.path, h1, cat.slug, matched, target))
    for slug, amounts in PRICE_CAPS.items():
        cat = BY_SLUG[slug]
        pool = sorted(by_cat.get(slug, []), key=lambda h: (float(h.offer.price), h.product.id or 0))
        published_ids: tuple | None = None
        published_path = ""
        for amount in amounts:
            chosen = [h for h in pool if float(h.offer.price) <= amount]
            ids = tuple(h.product.id for h in chosen)
            path = f"/kategorie/{slug}/do/{amount}"
            h1 = f"{cat.name} do {czk(amount)}"
            target = f"/kategorie/{slug}"
            if len(chosen) < MIN_ITEMS:
                pages.append(_thin("cap", path, h1, slug, target))
                continue
            if published_ids is not None and ids == published_ids:
                pages.append(_thin("cap", path, h1, slug, published_path))
                continue
            published_ids = ids
            published_path = path
            pages.append(_ready("cap", path, h1, slug, chosen, target))
    return pages


def _pack(pages: list[Landing]) -> list[dict]:
    packed = []
    for page in pages:
        packed.append({
            "kind": page.kind, "path": page.path, "h1": page.h1, "title": page.title,
            "description": page.description, "intro": page.intro, "category_slug": page.category_slug,
            "rows": [{"product_id": h.product.id, "offer_id": h.offer.id} for h in page.rows],
            "total": page.total, "price_min": page.price_min, "price_max": page.price_max,
            "updated": page.updated, "ready": page.ready, "redirect": page.redirect,
        })
    return packed


def _unpack(db, packed: list[dict]) -> list[Landing]:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from .models import Product
    ids = [row["product_id"] for page in packed for row in page["rows"]]
    by = {}
    if ids:
        products = db.scalars(
            select(Product).where(Product.id.in_(set(ids))).options(selectinload(Product.offers))
        ).all()
        by = {p.id: p for p in products}
    out = []
    for page in packed:
        rows = []
        for row in page["rows"]:
            product = by.get(row["product_id"])
            if product is None or product.retired_at is not None or product.catalog_hidden or product.needs_review:
                continue
            offer = next((o for o in product.offers if o.id == row["offer_id"]), None)
            if offer is None or offer.in_stock is not True or offer.price is None:
                continue
            if not offer_matches_product(offer, product):
                continue
            rows.append(_Hit(product, offer))
        out.append(Landing(
            page["kind"], page["path"], page["h1"], page["title"], page["description"], page["intro"],
            page["category_slug"], rows, page["total"], page["price_min"], page["price_max"],
            page["updated"], page["ready"], page["redirect"],
        ))
    return out


def all_landings(db) -> list[Landing]:
    global _cache
    from .deals import catalog_stamp
    stamp = catalog_stamp(db)
    now = time.monotonic()
    with _lock:
        hit = _cache
    if hit and hit[1] == stamp and now - hit[0] < _CACHE_TTL_S:
        return _unpack(db, hit[2])
    pages = _build(db)
    with _lock:
        _cache = (now, stamp, _pack(pages))
    return pages


def ready_landings(db) -> list[Landing]:
    return [p for p in all_landings(db) if p.ready]


def for_category(db, slug: str) -> list[Landing]:
    return [p for p in ready_landings(db) if p.category_slug == slug]


def published_paths(db) -> list[str]:
    return [p.path for p in ready_landings(db)]


def find_series(db, slug: str) -> Landing | None:
    spec = SERIES_BY_SLUG.get(slug)
    if spec is None:
        return None
    return next((p for p in all_landings(db) if p.path == spec.path), None)


def find_cap(db, slug: str, amount: int) -> Landing | None:
    if slug not in PRICE_CAPS or amount not in PRICE_CAPS[slug]:
        return None
    path = f"/kategorie/{slug}/do/{amount}"
    return next((p for p in all_landings(db) if p.path == path), None)
