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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

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


def rows(db, now: datetime | None = None, category: str = "", shop: str = "", allow_demo: bool | None = None,
         require_fresh: bool = True) -> list[BfRow]:
    from .deals import include_demo, _points_for
    from .verdict import compute, daily_best
    now = now or datetime.now(timezone.utc)
    allow = include_demo() if allow_demo is None else allow_demo
    q = select(Product).options(selectinload(Product.offers).selectinload(Offer.points))
    if category:
        q = q.where(Product.category_slug == category)
    out = []
    for p in db.scalars(q).all():
        cands = [o for o in p.offers if o.price is not None and o.status == "ok" and (not require_fresh or _fresh(o, now))]
        best = min(cands, key=lambda o: float(o.price), default=None)
        if not best or (shop and best.shop != shop):
            continue
        pts, saw_demo = _points_for(p, allow)
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
