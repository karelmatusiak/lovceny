"""v7 – Black Friday: /black-friday, banner na úvodu a jednorázový Web Push přehled.

Data jen naše: aktuální nejnižší ověřená cena, historie, kterou sami měříme, a „původní“ cena, kterou uvádí
obchod. Demo body se nepočítají (pokud není LOVCENY_INCLUDE_DEMO=1 – jen pro vývoj).

Datum: Black Friday = pátek po čtvrtém čtvrtku v listopadu (2026 → 27. 11.), Cyber Monday = +3 dny.
Konfigurace (env, vše volitelné):
  BF_BANNER_FROM / BF_BANNER_TO   období banneru na úvodu, formát MM-DD nebo YYYY-MM-DD (výchozí 11-01 až 12-01)
  BF_DIGEST_DAYS                  dny rozeslání push přehledu, čárkami (výchozí „bf,cm“ = Black Friday a Cyber Monday)
  BF_DIGEST_HOUR                  od kolika hodin (Praha) se smí poslat (výchozí 7 – první běh fetch.yml po 7:00)
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .categories import is_accessory_text
from .models import Product, Offer, PushTopicSubscription, TopicSend
from .util import czk
log = logging.getLogger("lovceny.blackfriday")
PRAGUE = ZoneInfo("Europe/Prague")
TOPIC = "black-friday"
FRESH_DAYS = 3
MIN_CLAIMED_PCT = 3.0     # sleva podle obchodu aspoň 3 %
MIN_REAL_PCT = 3.0        # nebo náš verdikt Kupuj a aspoň 3 % pod obvyklou cenou (medián 90 dní)
LOWEST_MIN_DAYS = 30      # „nejnižší, co jsme naměřili“ jen při aspoň 30 dnech historie
TOP_DIGEST = 5
# Stejné okno jako /slevy. Razítko katalogu cache shodí hned po zápisu nové ceny.
_CACHE_TTL_S = 7 * 60
_cache_lock = Lock()
_rows_cache: dict[tuple, tuple[float, tuple, list]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _rows_cache.clear()
MONTHS = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září", "října", "listopadu", "prosince"]


def today_prague(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(PRAGUE).date()


def bf_date(year: int) -> date:
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=22)


def cz_date(d: date, year: bool = True) -> str:
    return f"{d.day}. {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def _parse(v: str, year: int, default: date) -> date:
    v = (v or "").strip()
    try:
        if len(v) == 5:
            return date(year, int(v[:2]), int(v[3:]))
        if len(v) == 10:
            return date.fromisoformat(v)
    except ValueError:
        log.warning("Neplatné datum v konfiguraci Black Friday: %r", v)
    return default


def season_year(today: date) -> int:
    """Rok sezóny: po 15. prosinci už odpočítáváme další rok."""
    return today.year + 1 if (today.month == 12 and today.day > 15) else today.year


@dataclass
class Season:
    year: int
    bf: date
    cm: date
    banner_from: date
    banner_to: date

    def days_to(self, today: date) -> int:
        return (self.bf - today).days

    def phase(self, today: date) -> str:
        if today < self.bf - timedelta(days=6):
            return "pred"
        if today < self.bf:
            return "tyden"
        if today <= self.cm:
            return "probiha"
        return "po"


def season(now: datetime | None = None) -> Season:
    t = today_prague(now)
    y = season_year(t)
    bf = bf_date(y)
    return Season(y, bf, bf + timedelta(days=3),
                  _parse(os.environ.get("BF_BANNER_FROM", ""), y, date(y, 11, 1)),
                  _parse(os.environ.get("BF_BANNER_TO", ""), y, date(y, 12, 1)))


def banner_active(now: datetime | None = None) -> bool:
    s, t = season(now), today_prague(now)
    return s.banner_from <= t <= s.banner_to


def banner_short(now: datetime | None = None) -> str:
    """Jeden krátký řádek do pruhu. Dlouhý text nechává countdown_text."""
    s, t = season(now), today_prague(now)
    n = s.days_to(t)
    if n > 1:
        word = "dny" if 2 <= n <= 4 else "dní"
        return f"Black Friday za {n} {word}"
    if n == 1:
        return "Black Friday začíná zítra"
    if n == 0:
        return "Black Friday je dnes"
    if t <= s.cm:
        return "Black Friday víkend"
    return f"Black Friday {s.year}"


def countdown_text(now: datetime | None = None) -> str:
    s, t = season(now), today_prague(now)
    n = s.days_to(t)
    if n > 1:
        word = "dny" if 2 <= n <= 4 else "dní"
        return f"Black Friday {s.year} je za {n} {word} – v pátek {cz_date(s.bf, False)}."
    if n == 1:
        return f"Black Friday začíná zítra – v pátek {cz_date(s.bf, False)}."
    if n == 0:
        return "Black Friday je dnes."
    if t <= s.cm:
        return f"Black Friday víkend probíhá – do Cyber Monday ({cz_date(s.cm, False)})."
    return f"Black Friday {s.year} skončil. Slevy dál měříme stejně poctivě."


# ---------- slevy ----------
@dataclass
class BfRow:
    product: Product
    best: Offer
    verdict: object
    claimed_pct: float | None     # sleva podle obchodu (vůči jeho „původní“ ceně)
    real_pct: float               # vůči obvyklé ceně (medián 90 dní), ≥ 0
    min30: float | None
    min30_date: date | None
    lowest: float | None          # nejnižší cena za celou dobu sledování
    days: int
    is_lowest: bool               # dnešní cena = nejnižší, co jsme kdy naměřili (při ≥ 30 dnech dat)
    has_demo: bool = False

    @property
    def kind(self) -> str:
        return self.verdict.code


def _fresh(o: Offer, now: datetime) -> bool:
    lc = o.last_checked
    return bool(lc) and (lc if lc.tzinfo else lc.replace(tzinfo=timezone.utc)) >= now - timedelta(days=FRESH_DAYS)


@dataclass
class RealDrop:
    """Produkt skladem, jehož dnešní nejnižší cena je pod naším mediánem 30 dní."""
    product: Product
    offer: Offer
    days: int
    min30: float
    min30_date: date | None
    median30: float
    drop_czk: float
    drop_pct: float
    at_min30: bool


@dataclass
class Listing:
    rows: list[BfRow]
    drops: list[RealDrop]


def _row_payload(r: BfRow) -> dict:
    return {
        "product_id": r.product.id, "offer_id": r.best.id, "verdict": r.verdict,
        "claimed_pct": r.claimed_pct, "real_pct": r.real_pct, "min30": r.min30, "min30_date": r.min30_date,
        "lowest": r.lowest, "days": r.days, "is_lowest": r.is_lowest, "has_demo": r.has_demo,
    }


def _drop_payload(r: RealDrop) -> dict:
    return {
        "product_id": r.product.id, "offer_id": r.offer.id, "days": r.days, "min30": r.min30,
        "min30_date": r.min30_date, "median30": r.median30, "drop_czk": r.drop_czk,
        "drop_pct": r.drop_pct, "at_min30": r.at_min30,
    }


def _prepare(db, now: datetime, *, category: str = "", require_fresh: bool = True):
    """Jedno načtení produktů, nabídek a historie pro verdikty i přehled skutečných slev."""
    from .deals import load_offers_for, load_points_for_offers
    from .offer_trust import offer_is_promoted, use_cutoffs
    cutoffs = use_cutoffs(db)
    q = select(Product).where(
        Product.retired_at.is_(None), Product.catalog_hidden.is_(False), Product.needs_review.is_(False))
    if category:
        q = q.where(Product.category_slug == category)
    products = db.scalars(q).all()
    offers_by = load_offers_for(db, products)
    row_pending = []
    drop_pending = []
    offer_ids = []
    seen: set[int] = set()

    def take(offs):
        for o in offs:
            if o.id is not None and o.id not in seen:
                seen.add(o.id)
                offer_ids.append(o.id)

    for p in products:
        if is_accessory_text(p.name or "", p.category):
            continue
        offs = offers_by.get(p.id, [])
        cands = [o for o in offs if offer_is_promoted(o, cutoffs, p) and o.status == "ok"
                 and (not require_fresh or _fresh(o, now))]
        best = min(cands, key=lambda o: float(o.price), default=None)
        if best is not None:
            row_pending.append((p, offs, best))
            take(offs)
        stock = [o for o in offs if offer_is_promoted(o, cutoffs, p) and o.in_stock is True
                 and o.status == "ok" and o.price is not None
                 and (not require_fresh or _fresh(o, now))]
        best_stock = min(stock, key=lambda o: float(o.price), default=None)
        if best_stock is not None:
            drop_pending.append((p, offs, best_stock))
            take(offs)
    return row_pending, drop_pending, load_points_for_offers(db, offer_ids)


def _rows_from(pending, by_pts, allow: bool, now: datetime, shop: str) -> list[BfRow]:
    from .deals import points_from_map
    from .verdict import compute, daily_best
    out = []
    for p, offs, best in pending:
        if shop and best.shop != shop:
            continue
        pts, saw_demo = points_from_map(offs, by_pts, allow)
        cur = float(best.price)
        orig = float(best.original_price) if best.original_price else None
        v = compute(pts, cur, orig, now=now)
        claimed = (1 - cur / orig) * 100 if orig and orig > cur else None
        md = float(v.median90 or 0)
        real = max(0.0, (md - cur) / md * 100) if md else 0.0
        if not ((claimed or 0) >= MIN_CLAIMED_PCT or (v.code == "buy" and real >= MIN_REAL_PCT)):
            continue
        allday = daily_best(pts, datetime(1970, 1, 1, tzinfo=timezone.utc))
        d30 = {d: x for d, x in allday.items() if d >= (now - timedelta(days=30)).date() and d < now.date()}
        m30 = min(d30.items(), key=lambda x: (x[1], x[0]), default=None)
        if v.code == "fake" and v.ref_min30 is not None:   # stejné číslo jako u štítku (okno před akcí)
            m30 = (v.ref_date, v.ref_min30)
        lowest = min(allday.values()) if allday else None
        days = len(allday)
        is_low = bool(lowest is not None and days >= LOWEST_MIN_DAYS and cur <= lowest + 0.5 and v.code == "buy")
        out.append(BfRow(p, best, v, claimed, real, m30[1] if m30 else None, m30[0] if m30 else None, lowest, days,
                         is_low, has_demo=saw_demo and allow))
    order = {"buy": 0, "wait": 1, "fake": 2, "none": 3}
    out.sort(key=lambda r: (not r.is_lowest, order.get(r.kind, 4), -r.real_pct, -(r.claimed_pct or 0)))
    return out


def _drops_from(pending, by_pts, allow: bool, now: datetime) -> list[RealDrop]:
    from .deals import points_from_map
    from .verdict import history_drop
    out = []
    for p, offs, best in pending:
        if not p.category_slug:
            continue
        pts, _ = points_from_map(offs, by_pts, allow)
        hd = history_drop(pts, float(best.price), now=now)
        if hd is None:
            continue
        out.append(RealDrop(
            p, best, hd.days, hd.min30, hd.min30_date, hd.median30, hd.drop_czk, hd.drop_pct, hd.at_min30,
        ))
    return out


def _compute_rows(db, now: datetime, category: str, shop: str, allow: bool, require_fresh: bool) -> list[BfRow]:
    pending, _, by_pts = _prepare(db, now, category=category, require_fresh=require_fresh)
    return _rows_from(pending, by_pts, allow, now, shop)


def _compute_listing(db, now: datetime, allow: bool) -> Listing:
    row_pending, drop_pending, by_pts = _prepare(db, now, require_fresh=True)
    return Listing(
        _rows_from(row_pending, by_pts, allow, now, ""),
        _drops_from(drop_pending, by_pts, allow, now),
    )


def _rehydrate_listing(db, payload: dict) -> Listing:
    row_pl = payload.get("rows") or []
    drop_pl = payload.get("drops") or []
    ids = [p["product_id"] for p in row_pl] + [p["product_id"] for p in drop_pl]
    if not ids:
        return Listing([], [])
    products = db.scalars(
        select(Product).where(Product.id.in_(set(ids))).options(selectinload(Product.offers))
    ).all()
    by = {p.id: p for p in products}

    def offer_of(p, oid):
        return next((o for o in p.offers if o.id == oid), None)

    rows = []
    for pl in row_pl:
        p = by.get(pl["product_id"])
        if not p or p.retired_at is not None:
            continue
        best = offer_of(p, pl["offer_id"])
        if best is None:
            continue
        rows.append(BfRow(
            p, best, pl["verdict"], pl["claimed_pct"], pl["real_pct"], pl["min30"], pl["min30_date"],
            pl["lowest"], pl["days"], pl["is_lowest"], pl["has_demo"],
        ))
    drops = []
    for pl in drop_pl:
        p = by.get(pl["product_id"])
        if not p or p.retired_at is not None or not p.category_slug:
            continue
        best = offer_of(p, pl["offer_id"])
        if best is None or best.in_stock is not True:
            continue
        drops.append(RealDrop(
            p, best, pl["days"], pl["min30"], pl["min30_date"], pl["median30"],
            pl["drop_czk"], pl["drop_pct"], pl["at_min30"],
        ))
    return Listing(rows, drops)


def listing(db, now: datetime | None = None, allow_demo: bool | None = None) -> Listing:
    """Verdikty i přehled skutečných slev z jednoho načtení historie. Cache sdílí razítko katalogu."""
    from .deals import include_demo, catalog_stamp
    allow = include_demo() if allow_demo is None else allow_demo
    # Pevný čas (digest) cache nepoužije — přehled na stránce ano.
    if now is not None or allow_demo is not None:
        when = now or datetime.now(timezone.utc)
        return _compute_listing(db, when, allow)
    stamp = catalog_stamp(db)
    key = ("listing", allow)
    t = time.monotonic()
    with _cache_lock:
        hit = _rows_cache.get(key)
        if hit and hit[1] == stamp and t - hit[0] < _CACHE_TTL_S:
            payload = hit[2]
        else:
            payload = None
    if payload is not None:
        return _rehydrate_listing(db, payload)
    computed = _compute_listing(db, datetime.now(timezone.utc), allow)
    with _cache_lock:
        _rows_cache[key] = (t, stamp, {
            "rows": [_row_payload(r) for r in computed.rows],
            "drops": [_drop_payload(r) for r in computed.drops],
        })
    return computed


def group_by_category(drops: list[RealDrop], only_slug: str = "") -> list[tuple]:
    """Kategorie v pořadí menu. Prázdný seznam u kategorie = žádný skutečný pokles, ne výplň."""
    from .categories import CATEGORIES
    chosen = [c for c in CATEGORIES if not only_slug or c.slug == only_slug]
    buckets: dict[str, list[RealDrop]] = {c.slug: [] for c in chosen}
    for d in drops:
        if d.product.category_slug in buckets:
            buckets[d.product.category_slug].append(d)
    for items in buckets.values():
        items.sort(key=lambda r: (-r.drop_pct, -r.drop_czk, r.product.id))
    return [(c, buckets[c.slug]) for c in chosen]


def rows(db, now: datetime | None = None, category: str = "", shop: str = "", allow_demo: bool | None = None,
         require_fresh: bool = True) -> list[BfRow]:
    from .deals import include_demo
    cacheable = now is None and not category and not shop and allow_demo is None and require_fresh
    if cacheable:
        return listing(db).rows
    allow = include_demo() if allow_demo is None else allow_demo
    when = now or datetime.now(timezone.utc)
    return _compute_rows(db, when, category, shop, allow, require_fresh)


def summary(rs: list[BfRow]) -> dict:
    return {"total": len(rs), "lowest": sum(r.is_lowest for r in rs), "buy": sum(r.kind == "buy" for r in rs),
            "wait": sum(r.kind == "wait" for r in rs), "fake": sum(r.kind == "fake" for r in rs),
            "none": sum(r.kind == "none" for r in rs)}


# ---------- push přehled ----------
def digest_days(s: Season) -> list[date]:
    raw = os.environ.get("BF_DIGEST_DAYS", "bf,cm")
    out = []
    for part in raw.split(","):
        part = part.strip().lower()
        if part == "bf":
            out.append(s.bf)
        elif part == "cm":
            out.append(s.cm)
        elif part:
            d = _parse(part, s.year, None)
            if d:
                out.append(d)
    return out


def digest_payload(rs: list[BfRow], sub, base: str, day: date, s: Season) -> dict:
    low = [r for r in rs if r.is_lowest]
    top = (low or [r for r in rs if r.kind == "buy"])[:1]
    label = "Black Friday" if day == s.bf else ("Cyber Monday" if day == s.cm else "Black Friday slevy")
    n = len(low)
    if n:
        word = "cena" if n == 1 else ("ceny" if n <= 4 else "cen")
        title = f"{label}: {n} {word} na nejnižší úrovni, jakou jsme naměřili"
    else:
        title = f"{label}: přehled slev podle historie cen"
    body = (f"Např. {top[0].product.name[:60]} za {czk(top[0].best.price)}." if top
            else "Podívej se, které slevy jsou opravdu u minima a kde bylo za 30 dní levněji.")
    return {"title": title, "body": body, "url": f"{base}/black-friday?utm_source=push", "tag": "lovceny-black-friday",
            "unsubscribe": f"{base}/black-friday/odhlasit/{sub.token}", "open_label": "Zobrazit slevy",
            "stop_label": "Odhlásit Black Friday"}


def run_digest(db, now: datetime | None = None, sender=None, force: bool = False) -> dict:
    """Volá fetch_prices.py po stažení cen. V den rozeslání (BF / CM) po BF_DIGEST_HOUR pošle jednou."""
    from . import push as pushmod
    from .emails import public_url
    now = now or datetime.now(timezone.utc)
    s, local = season(now), now.astimezone(PRAGUE)
    day = local.date()
    if not force and (day not in digest_days(s) or local.hour < int(os.environ.get("BF_DIGEST_HOUR", "7"))):
        return {"skipped": "dnes se Black Friday přehled neposílá"}
    key = f"{TOPIC}:{day.isoformat()}"
    if db.scalar(select(TopicSend).where(TopicSend.key == key)):
        return {"skipped": "už odesláno"}
    sender = sender or pushmod.send
    if sender is pushmod.send and not pushmod.enabled():
        return {"skipped": "VAPID klíče chybí"}
    rs = rows(db, now)
    if not rs:
        return {"skipped": "žádné slevy"}
    rec = TopicSend(key=key, sent_at=now, items=json.dumps([r.product.id for r in rs[:TOP_DIGEST]]))
    db.add(rec)
    db.commit()   # nejdřív zámek (idempotence i při pádu uprostřed)
    stats = {"sent": 0, "gone": 0, "error": 0}
    base = public_url()
    for sub in db.scalars(select(PushTopicSubscription).where(PushTopicSubscription.topic == TOPIC)).all():
        st = sender(sub, digest_payload(rs, sub, base, day, s))
        if st == "ok":
            sub.last_sent_at = now
            stats["sent"] += 1
        elif st == "gone":
            db.delete(sub)
            stats["gone"] += 1
        else:
            stats["error"] += 1
    rec.sent = stats["sent"]
    db.commit()
    return stats
