"""Je to skutečná sleva? Verdikt jen z cen, které jsme sami naměřili.

Kategorie:
- skutečná sleva: dnešní nejnižší cena je aspoň o 3 % pod nejnižší cenou z dní před dneškem
- sleva jen na papíře: cena předtím vystoupala a dnešek se pod starší minimum nedostal
- bez slevy: historie už stačí, ale dnešek pod naměřené minimum neklesl
- málo dat: méně než 7 různých dní v posledních 90 dnech — verdikt neříkáme

Chybějící dny nedoplňujeme. Vyprodaná nabídka není nejlevnější. Jiná kapacita úložiště
se do ceny ani do historie nepočítá. Demo body jen při LOVCENY_INCLUDE_DEMO=1.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from threading import Lock
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .categories import is_accessory_text
from .chart import sparkline
from .models import Product
from .util import cz_count, czk
from .verdict import JUMP, MIN_DAYS, daily_best

PRAGUE = ZoneInfo("Europe/Prague")
REAL_PCT = 3.0
WINDOW_DAYS = 90
RISE_DAYS = 21
PAGE = "/black-friday/skutecna-sleva"
_CACHE_TTL_S = 7 * 60
_lock = Lock()
_cache: dict[tuple, tuple[float, tuple, dict]] = {}

LABELS = {
    "skutecna": "Skutečná sleva",
    "papir": "Sleva jen na papíře",
    "bez": "Bez slevy",
    "malo": "Málo dat",
}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def methodology() -> tuple[str, ...]:
    return (
        "Ceny stahujeme 3× denně z e-shopů, které automatické čtení dovolují. "
        "Do výpočtu bereme jen nabídky stejného výrobku: jiná kapacita úložiště se nepočítá, "
        "vyprodaná nabídka není nejlevnější a nemá tlačítko Koupit.",
        f"Verdikt dáváme až po {MIN_DAYS} různých dnech s cenou v posledních {WINDOW_DAYS} dnech. "
        "Když jich je méně, napíšeme Málo dat a slevu nehodnotíme. Chybějící dny nedoplňujeme a nic neodhadujeme. "
        "Nulové ceny a zjevné překlepy mimo pásmo ostatních měření vynecháváme stejně jako v grafu.",
        f"Skutečná sleva znamená, že dnešní nejnižší cena je aspoň o {REAL_PCT:.0f} % pod nejnižší cenou "
        "z dní před dneškem. Nejdřív bereme posledních 30 dní; když v nich měření není, dny za 90 dní. "
        "Druhé okno ukážeme vždy, když v něm nějakou cenu máme.",
        f"Sleva jen na papíře znamená, že novější část historie vystoupala aspoň o {JUMP * 100:.0f} % "
        "nad starší naměřenou úroveň a dnešní cena se pod tu starší nejnižší nedostala.",
        "Bez slevy znamená, že historie už je dost dlouhá, ale dnešní cena pod naměřené minimum neklesla. "
        "Pořadí je podle velikosti té skutečné slevy. Na pořadí nemá vliv, jestli je odkaz provizní.",
    )


def _cz_date(d: date | None) -> str:
    return f"{d.day}. {d.month}. {d.year}" if d else ""


def prague_day(moment: datetime | None) -> date | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(PRAGUE).date()


@dataclass
class Assessment:
    code: str
    label: str
    text: str
    current: float
    days: int
    min30: float | None = None
    min30_date: date | None = None
    min30_days: int = 0
    min90: float | None = None
    min90_date: date | None = None
    min90_days: int = 0
    real_pct: float | None = None
    claimed_pct: float | None = None
    rose: bool = False
    since: date | None = None
    spark: str = ""

    @property
    def show_facts(self) -> bool:
        return self.code != "malo" and (self.min30 is not None or self.min90 is not None)


def _window(series: dict, today: date, start: date) -> tuple[float, date, int] | None:
    rows = [(d, p) for d, p in series.items() if start <= d < today]
    if not rows:
        return None
    d0, p0 = min(rows, key=lambda x: (x[1], x[0]))
    return float(p0), d0, len(rows)


def _rose(prior: list[tuple[date, float]], current: float, today: date) -> tuple[bool, float | None, float | None]:
    """Vzestup v naměřených dnech. Bez staršího základu (aspoň 3 dny) ho nehlásíme."""
    if not prior or current <= 0:
        return False, None, None
    cut = today - timedelta(days=RISE_DAYS)
    older = [p for d, p in prior if d < cut]
    newer = [p for d, p in prior if d >= cut]
    base_prices: list[float] | None = None
    peak: float | None = None
    if len(older) >= 3 and newer:
        base_prices, peak = older, max(newer)
    elif len(prior) >= MIN_DAYS:
        mid = len(prior) // 2
        if mid >= 3 and len(prior) - mid >= 1:
            base_prices = [p for _, p in prior[:mid]]
            peak = max(p for _, p in prior[mid:])
    if not base_prices or peak is None:
        return False, None, None
    base = float(median(base_prices))
    if base <= 0 or peak < base * (1 + JUMP) or current >= peak:
        return False, None, None
    return True, float(peak), base


def assess(points, current: float, claimed_original: float | None = None,
           now: datetime | None = None) -> Assessment:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    cur = float(current)
    claimed = None
    if claimed_original and claimed_original > cur > 0:
        claimed = (1 - cur / float(claimed_original)) * 100
    series = daily_best(points, now - timedelta(days=WINDOW_DAYS))
    days = len(series)
    since = min(series) if series else None
    spark = sparkline(points, now=now, days=WINDOW_DAYS)
    if days < MIN_DAYS:
        if days == 0:
            text = "Zatím nemáme žádný den s naměřenou cenou. Verdikt o slevě nedáváme."
        else:
            text = f"Sledujeme teprve {cz_count(days, 'den', 'dny', 'dní')}. Verdikt o slevě nedáváme."
        return Assessment(
            "malo", LABELS["malo"], text, cur, days, claimed_pct=claimed, since=since, spark=spark,
        )
    prior = sorted((d, p) for d, p in series.items() if d < today)
    w30 = _window(series, today, today - timedelta(days=30))
    w90 = _window(series, today, today - timedelta(days=WINDOW_DAYS))
    min30, min30_date, min30_days = (w30[0], w30[1], w30[2]) if w30 else (None, None, 0)
    min90, min90_date, min90_days = (w90[0], w90[1], w90[2]) if w90 else (None, None, 0)
    if min30 is not None:
        ref, ref_date, ref_days, ref_name = min30, min30_date, min30_days, "30"
    elif min90 is not None:
        ref, ref_date, ref_days, ref_name = min90, min90_date, min90_days, "90"
    else:
        ref, ref_date, ref_days, ref_name = None, None, 0, ""
    measured = ((ref - cur) / ref * 100) if ref and cur < ref else 0.0
    rose, peak, base = _rose(prior, cur, today)
    common = dict(
        current=cur, days=days, min30=min30, min30_date=min30_date, min30_days=min30_days,
        min90=min90, min90_date=min90_date, min90_days=min90_days, claimed_pct=claimed,
        rose=rose, since=since, spark=spark,
    )
    if measured + 1e-9 >= REAL_PCT and ref is not None:
        window = f"z {ref_days} naměřených dní za posledních {ref_name} dní"
        text = (f"Dnešní nejnižší cena {czk(cur)} je o {measured:.0f} % pod nejnižší cenou {window} "
                f"({czk(ref)}, {_cz_date(ref_date)}).")
        if min30 is not None and min90 is not None and abs(min90 - min30) > 0.5:
            text += f" Nejnižší za 90 dní byla {czk(min90)} ({_cz_date(min90_date)})."
        if rose and peak is not None and base is not None:
            text += (f" Cena mezitím vystoupala na {czk(peak)}, dnešní cena je i tak pod dřívějším minimem "
                     f"(starší úroveň {czk(base)}).")
        return Assessment("skutecna", LABELS["skutecna"], text, real_pct=measured, **common)
    if rose and peak is not None and base is not None:
        extra = ""
        if ref is not None:
            extra = f" Pod nejnižší cenu z dní před dneškem ({czk(ref)}, {_cz_date(ref_date)}) se nedostala."
        text = (f"Cena v novější části historie vystoupala na {czk(peak)} "
                f"(starší naměřená úroveň byla {czk(base)}). Dnešní cena je {czk(cur)}.{extra}")
        return Assessment("papir", LABELS["papir"], text, real_pct=0.0, **common)
    if ref is not None:
        text = (f"Dnešní cena {czk(cur)} není aspoň o {REAL_PCT:.0f} % pod nejnižší cenou "
                f"z {ref_days} naměřených dní za posledních {ref_name} dní ({czk(ref)}, {_cz_date(ref_date)}).")
    else:
        text = f"Dnešní cena je {czk(cur)}. Před dneškem v okně, které měříme, žádné minimum nemáme."
    if claimed_original and claimed:
        text += f" Obchod uvádí původní cenu {czk(claimed_original)}."
    return Assessment("bez", LABELS["bez"], text, real_pct=0.0, **common)


def _offer_blob(offer) -> str:
    from .matching import label_from_url
    return " ".join(part for part in (
        label_from_url(getattr(offer, "url", "") or ""),
        getattr(offer, "url", "") or "",
        getattr(offer, "shop_category", "") or "",
    ) if part)


def offer_matches_product(offer, product) -> bool:
    """Stejný výrobek: nesmí být stažená, sporná, ani jiná kapacita."""
    if getattr(offer, "withheld", False):
        return False
    from .category_guard import storage_conflict
    from .offer_trust import offer_mismatches_product
    if offer_mismatches_product(offer, product):
        return False
    if storage_conflict(getattr(product, "name", "") or "", _offer_blob(offer)):
        return False
    return True


def _best_offer(offers, product, cutoffs=None):
    from .offer_trust import offer_is_promoted
    promoted = [o for o in offers if offer_matches_product(o, product) and offer_is_promoted(o, cutoffs, product)]
    return min(promoted, key=lambda o: float(o.price), default=None)


def _history_offers(offers, product) -> list:
    return [o for o in offers if offer_matches_product(o, product)]


def _collect_points(offers, allow_demo: bool) -> list:
    pts = []
    for offer in offers:
        for pt in getattr(offer, "points", ()) or ():
            if getattr(pt, "is_demo", False) and not allow_demo:
                continue
            pts.append(pt)
    return pts


def for_offers(product, offers, *, allow_demo: bool | None = None, now: datetime | None = None,
               cutoffs=None) -> Assessment | None:
    from .deals import include_demo
    best = _best_offer(offers, product, cutoffs)
    if best is None or best.price is None:
        return None
    allow = include_demo() if allow_demo is None else allow_demo
    pts = _collect_points(_history_offers(offers, product), allow)
    orig = float(best.original_price) if best.original_price else None
    return assess(pts, float(best.price), orig, now=now)


@dataclass
class HonestyRow:
    product: Product
    offer: object
    assessment: Assessment

    @property
    def code(self) -> str:
        return self.assessment.code

    @property
    def real_pct(self) -> float | None:
        return self.assessment.real_pct


@dataclass
class HonestyListing:
    rows: list[HonestyRow]
    counts: dict[str, int]
    earliest: date | None
    latest_check: datetime | None
    shown_note: str = ""


def _include(found: Assessment) -> bool:
    claimed = (found.claimed_pct or 0) >= REAL_PCT
    if found.code in ("skutecna", "papir"):
        return True
    return claimed


def _sort_key(row: HonestyRow):
    pct = row.assessment.real_pct if row.code == "skutecna" else 0
    order = {"skutecna": 0, "papir": 1, "bez": 2, "malo": 3}
    return (-(pct or 0), order.get(row.code, 9), row.product.id or 0)


def _present(rows: list[HonestyRow]) -> tuple[list[HonestyRow], str]:
    """Skutečné a papírové slevy celé. Bez slevy a málo dat omezíme, ať stránka nezbytní."""
    caps = {"skutecna": 10000, "papir": 10000, "bez": 40, "malo": 40}
    seen: dict[str, int] = {}
    shown = []
    hidden = 0
    for row in rows:
        n = seen.get(row.code, 0)
        if n >= caps[row.code]:
            hidden += 1
            continue
        seen[row.code] = n + 1
        shown.append(row)
    note = ""
    if hidden:
        note = f"Na stránce je {len(shown)} z {len(rows)} ověřovaných slev, seřazených podle skutečné úspory."
    return shown, note


def _compute(db, now: datetime, allow: bool) -> HonestyListing:
    from .deals import load_offers_for, load_points_for_offers, points_from_map, public_products
    from .matching import decent_title
    from .offer_trust import use_cutoffs
    cutoffs = use_cutoffs(db)
    products = [p for p in public_products(db) if p.category_slug and decent_title(p.name or "")
                and not is_accessory_text(p.name or "", p.category)]
    offers_by = load_offers_for(db, products)
    pending = []
    offer_ids = []
    for product in products:
        offs = offers_by.get(product.id, [])
        best = _best_offer(offs, product, cutoffs)
        if best is None:
            continue
        hist = _history_offers(offs, product)
        pending.append((product, best, hist))
        offer_ids.extend(o.id for o in hist if o.id is not None)
    by_pts = load_points_for_offers(db, offer_ids)
    rows = []
    for product, best, hist in pending:
        pts, _saw = points_from_map(hist, by_pts, allow)
        orig = float(best.original_price) if best.original_price else None
        found = assess(pts, float(best.price), orig, now=now)
        if not _include(found):
            continue
        rows.append(HonestyRow(product, best, found))
    rows.sort(key=_sort_key)
    counts = {code: sum(1 for r in rows if r.code == code) for code in LABELS}
    earliest = min((r.assessment.since for r in rows if r.assessment.since), default=None)
    checks = [r.offer.last_checked for r in rows if getattr(r.offer, "last_checked", None)]
    latest = max(checks) if checks else None
    shown, note = _present(rows)
    return HonestyListing(shown, counts, earliest, latest, note)


def _empty() -> HonestyListing:
    return HonestyListing([], {code: 0 for code in LABELS}, None, None, "")


def listing(db, now: datetime | None = None, allow_demo: bool | None = None) -> HonestyListing:
    from .deals import catalog_stamp, include_demo
    allow = include_demo() if allow_demo is None else allow_demo
    if now is not None or allow_demo is not None:
        return _compute(db, now or datetime.now(timezone.utc), allow)
    stamp = catalog_stamp(db)
    key = ("honesty", allow)
    t = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] == stamp and t - hit[0] < _CACHE_TTL_S:
            payload = hit[2]
        else:
            payload = None
    if payload is not None:
        return _rehydrate(db, payload)
    computed = _compute(db, datetime.now(timezone.utc), allow)
    with _lock:
        _cache[key] = (t, stamp, {
            "rows": [{"product_id": r.product.id, "offer_id": r.offer.id, "assessment": r.assessment} for r in computed.rows],
            "counts": computed.counts,
            "earliest": computed.earliest,
            "latest_check": computed.latest_check,
            "shown_note": computed.shown_note,
        })
    return computed


def _rehydrate(db, payload: dict) -> HonestyListing:
    rows_pl = payload.get("rows") or []
    if not rows_pl:
        out = _empty()
        out.counts = payload.get("counts") or out.counts
        return out
    ids = [p["product_id"] for p in rows_pl]
    products = db.scalars(
        select(Product).where(Product.id.in_(set(ids))).options(selectinload(Product.offers))
    ).all()
    by = {p.id: p for p in products}
    rows = []
    for pl in rows_pl:
        product = by.get(pl["product_id"])
        if not product or product.retired_at is not None or product.catalog_hidden or product.needs_review:
            continue
        if is_accessory_text(product.name or "", product.category):
            continue
        offer = next((o for o in product.offers if o.id == pl["offer_id"]), None)
        if offer is None or not offer_matches_product(offer, product):
            continue
        if offer.in_stock is False or offer.price is None:
            continue
        rows.append(HonestyRow(product, offer, pl["assessment"]))
    return HonestyListing(rows, payload.get("counts") or {}, payload.get("earliest"),
                          payload.get("latest_check"), payload.get("shown_note") or "")
