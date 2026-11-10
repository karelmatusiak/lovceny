"""Monitoring blokací obchodů: statistiky každého běhu stahovače (DB) + vyhodnocení zdraví obchodů.

Pravidla (vyhodnocuje se po každém běhu, výsledek čte .github/workflows/fetch.yml → GitHub issue):
  failing – v běhu selhala většina požadavků na obchod (a pokusů bylo aspoň MIN_ATTEMPTS),
  partial – blokace / captcha / robots.txt zasáhly aspoň PARTIAL_BLOCK_PERCENT % pokusů
            (a aspoň PARTIAL_MIN_EVENTS), ale ne většinu,
  stale   – poslední úspěšně stažená cena obchodu je starší než STALE_DAYS (obchod, který nikdy neuspěl, se nehlásí).

level na /stav: ok | partial | blocked (jen z posledního běhu; stale je samostatný důvod).
"""
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, delete

from .models import Offer, ShopRunStat

MIN_ATTEMPTS = 3
# Částečně blokováno: aspoň 20 % pokusů a aspoň 3 zásahy (1 captcha ze 4 není důvod k issue).
PARTIAL_BLOCK_PERCENT = 20
PARTIAL_MIN_EVENTS = 3
STALE_DAYS = 2
RETENTION_DAYS = 90
KINDS = ("ok", "blocked", "captcha", "robots", "errors", "skipped")


@dataclass
class _Acc:
    counts: Counter = field(default_factory=Counter)
    statuses: Counter = field(default_factory=Counter)
    last_error: str | None = None


class FetchStats:
    """Sběr výsledků během jednoho běhu (fetch_prices.py)."""

    def __init__(self, run_id: str | None = None):
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.shops: dict[str, _Acc] = defaultdict(_Acc)

    def add(self, shop: str, kind: str | None, status: int | None = None, error: str | None = None):
        kind = {"error": "errors"}.get(kind or "errors", kind or "errors")
        if kind not in KINDS:
            kind = "errors"
        a = self.shops[shop]
        a.counts[kind] += 1
        if status:
            a.statuses[str(status)] += 1
        if error and kind != "ok":
            a.last_error = str(error)[:300]

    def save(self, db) -> list[ShopRunStat]:
        now = datetime.now(timezone.utc)
        rows = []
        for shop, a in sorted(self.shops.items()):
            c = a.counts
            r = ShopRunStat(run_id=self.run_id, run_at=now, shop=shop, ok=c["ok"], blocked=c["blocked"],
                            captcha=c["captcha"], robots=c["robots"], errors=c["errors"], skipped=c["skipped"],
                            attempts=sum(v for k, v in c.items() if k != "skipped"),
                            statuses=json.dumps(dict(a.statuses), sort_keys=True) if a.statuses else None,
                            last_error=a.last_error)
            db.add(r)
            rows.append(r)
        db.execute(delete(ShopRunStat).where(ShopRunStat.run_at < now - timedelta(days=RETENTION_DAYS))
                   .execution_options(synchronize_session=False))
        db.commit()
        return rows


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def monitored_shops():
    """Obchody, u kterých čekáme čerstvé ceny: čtení stránek povoleno, nebo nastavený feed."""
    from .shops import SHOPS
    return [s for s in SHOPS.values()
            if (not s.retired) and (s.can_scrape or (s.source == "feed" and s.feed_url))]


def last_ok_by_shop(db) -> dict[str, datetime]:
    rows = db.execute(select(Offer.shop, func.max(Offer.last_checked)).where(Offer.status == "ok")
                      .group_by(Offer.shop)).all()
    return {s: _aware(t) for s, t in rows if t}


def offer_counts(db) -> dict[str, int]:
    return dict(db.execute(select(Offer.shop, func.count(Offer.id)).group_by(Offer.shop)).all())


def block_events(r: ShopRunStat) -> int:
    """HTTP blokace, captcha a robots.txt. Běžná chyba parseru sem nepatří."""
    return int(r.blocked or 0) + int(r.captcha or 0) + int(r.robots or 0)


def run_level(r: ShopRunStat | None) -> str:
    """ok | partial | blocked z jednoho běhu. Málo pokusů (pod MIN_ATTEMPTS) = ok."""
    if r is None or r.attempts < MIN_ATTEMPTS:
        return "ok"
    if r.failures * 2 > r.attempts:
        return "blocked"
    ev = block_events(r)
    if ev >= PARTIAL_MIN_EVENTS and ev * 100 >= r.attempts * PARTIAL_BLOCK_PERCENT:
        return "partial"
    return "ok"


def evaluate(db, run: list[ShopRunStat] | None = None, now: datetime | None = None) -> dict:
    """Zdraví obchodů → {"run_id", "checked_at", "shops": {slug: {...}}}. Jen obchody, které máme v DB."""
    now = now or datetime.now(timezone.utc)
    run_by = {r.shop: r for r in (run or [])}
    last_ok, counts = last_ok_by_shop(db), offer_counts(db)
    out = {}
    for s in monitored_shops():
        if not counts.get(s.slug):
            continue
        reasons = []
        r = run_by.get(s.slug)
        level = run_level(r)
        if r and level == "blocked":
            parts = [f"{n} {lbl}" for n, lbl in ((r.blocked, "blokováno (HTTP 403/429/503)"),
                     (r.captcha, "ochrana proti botům / captcha"), (r.robots, "zakázáno v robots.txt"),
                     (r.errors, "jiná chyba")) if n]
            reasons.append({"code": "failing", "text": f"V posledním běhu selhalo {r.failures} z {r.attempts} "
                            f"požadavků ({', '.join(parts)})."})
        elif r and level == "partial":
            ev = block_events(r)
            parts = [f"{n} {lbl}" for n, lbl in ((r.blocked, "blokováno (HTTP 403/429/503)"),
                     (r.captcha, "ochrana proti botům / captcha"), (r.robots, "zakázáno v robots.txt")) if n]
            reasons.append({"code": "partial", "text": f"V posledním běhu bylo {ev} z {r.attempts} požadavků "
                            f"blokováno ({', '.join(parts)}), {r.ok} prošlo. Ochranu proti botům neobcházíme."})
        lo = last_ok.get(s.slug)
        # Obchod, který nikdy nevrátil cenu, neoznačujeme jako „stale“ – jinak vypadá,
        # že rozbil produkty. Nabídky bez ceny se na webu stejně schovávají.
        if lo is not None and now - lo > timedelta(days=STALE_DAYS):
            from zoneinfo import ZoneInfo
            when = lo.astimezone(ZoneInfo("Europe/Prague")).strftime("%-d. %-m. %Y %H:%M") + " (čas v ČR)"
            reasons.append({"code": "stale", "text": f"Ceny nejsou aktuální déle než {STALE_DAYS} dny "
                            f"(poslední úspěšné stažení: {when})."})
        out[s.slug] = {
            "name": s.name, "ok": not reasons, "level": level, "reasons": reasons,
            "offers": counts.get(s.slug, 0),
            "last_ok": lo.isoformat() if lo else None,
            "run": {k: getattr(r, k) for k in ("attempts", "ok", "blocked", "captcha", "robots", "errors", "skipped")}
                   | {"statuses": json.loads(r.statuses) if r.statuses else {}, "last_error": r.last_error} if r else None,
        }
    return {"run_id": run[0].run_id if run else None, "checked_at": now.isoformat(), "shops": out}


def history(db, days: int = 14, now: datetime | None = None) -> dict:
    """Pro /stav: per obchod denní úspěšnost za posledních `days` dní (UTC dny)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = db.scalars(select(ShopRunStat).where(ShopRunStat.run_at >= since).order_by(ShopRunStat.run_at)).all()
    day_keys = [(now - timedelta(days=i)).date() for i in range(days - 1, -1, -1)]
    agg: dict[str, dict] = defaultdict(lambda: {d: [0, 0] for d in day_keys})
    last: dict[str, ShopRunStat] = {}
    for r in rows:
        d = _aware(r.run_at).date()
        if d in agg[r.shop]:
            agg[r.shop][d][0] += r.ok
            agg[r.shop][d][1] += r.attempts
        last[r.shop] = r
    return {"days": day_keys, "agg": agg, "last": last}
