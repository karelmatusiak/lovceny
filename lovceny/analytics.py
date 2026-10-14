"""Vlastní analytika bez cookies a bez osobních údajů.

Ukládáme: cestu stránky (bez query), doménu odkazujícího webu (jen cizí), normalizovaný hledaný dotaz
(bez e-mailů, čísel, odkazů), počet výsledků, ID produktu. Unikátní návštěvníky počítáme otiskem
sha256(denní sůl + IP + User-Agent); sůl je náhodná, platí jeden den a pak ji mažeme, otisky u událostí
starších 24 h nastavíme na NULL (předtím je sečteme do analytics_daily). IP ani UA se nikam neukládají.
DNT: 1 / Sec-GPC: 1 → nic nezaznamenáme. Boty (User-Agent) také ne.
"""
import hashlib
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select, func, delete, update, distinct

from .models import AnalyticsEvent, AnalyticsDaily, AnalyticsSalt, Product

log = logging.getLogger("lovceny.analytics")
TZ = ZoneInfo("Europe/Prague")
RETENTION_DAYS = 400
BOT_RX = re.compile(r"bot|crawl|spider|slurp|lighthouse|headless|preview|monitor|uptime|curl|wget|python|httpx|"
                    r"facebookexternalhit|embedly|axios|go-http|java/|okhttp|scrapy|pingdom|render", re.I)
SKIP_PREFIXES = ("/static/", "/img/", "/api/", "/admin", "/healthz", "/sw.js", "/manifest", "/hledat/zive",
                 "/robots.txt", "/sitemap.xml", "/favicon", "/offline", "/.well-known")
_cleanup_at = {"t": None}
PAGE_KINDS = ("view", "search")  # zobrazení stránky (živé hledání je jen doplněk dotazu)


def today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(TZ).date().isoformat()


def opted_out(headers) -> bool:
    return headers.get("dnt") == "1" or headers.get("sec-gpc") == "1"


def is_bot(ua: str) -> bool:
    return not ua or bool(BOT_RX.search(ua))


def should_track(path: str, method: str, headers) -> bool:
    return (method == "GET" and not path.startswith(SKIP_PREFIXES) and not opted_out(headers)
            and not is_bot(headers.get("user-agent", "")))


def ref_domain(referer: str, own_host: str) -> str | None:
    try:
        h = (urlparse(referer).hostname or "").lower()
    except ValueError:
        return None
    if not h or h == own_host.lower().split(":")[0]:
        return None
    h = h[4:] if h.startswith("www.") else h
    if h in ("localhost",) or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", h):
        return None
    if h.startswith(("android-app:", )):
        return None
    return h[:100]


_PII = [re.compile(p, re.I) for p in (r"\S+@\S+", r"https?://\S+|www\.\S+", r"\+?\d[\d\s-]{5,}\d")]


def normalize_query(q: str) -> str | None:
    """Dotaz pro statistiku: malá písmena, bez e-mailů/odkazů/dlouhých čísel, zkrácené mezery, max 60 znaků.
    Číslo modelu (např. „s24“, „wh-1000xm5“) zůstává; dlouhé řady číslic (telefon, EAN) mizí."""
    if not q:
        return None
    if re.match(r"^\s*(https?://|www\.)", q, re.I):
        return "[odkaz z obchodu]"
    s = unicodedata.normalize("NFC", q).lower()
    for rx in _PII:
        s = rx.sub(" ", s)
    s = re.sub(r"[^\w\s.+\-/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()[:60].strip()
    return s or None


def _salt(db, day: str) -> str:
    row = db.get(AnalyticsSalt, day)
    if row is None:
        row = AnalyticsSalt(day=day, salt=secrets.token_hex(32))
        db.add(row)
        try:
            db.commit()
        except Exception:  # souběžný požadavek sůl už vytvořil
            db.rollback()
            row = db.get(AnalyticsSalt, day)
    return row.salt


def visitor_hash(db, day: str, ip: str, ua: str) -> str:
    return hashlib.sha256(f"{_salt(db, day)}|{ip}|{ua}".encode()).hexdigest()[:32]


def record(db, *, path: str, kind: str = "view", ip: str = "", ua: str = "", referer: str = "", host: str = "",
           query: str | None = None, results: int | None = None, product_id: int | None = None,
           now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    day = today(now)
    db.add(AnalyticsEvent(created_at=now, day=day, kind=kind, path=path[:200], ref_domain=ref_domain(referer, host),
                          query=normalize_query(query) if query else None, results=results, product_id=product_id,
                          visitor=visitor_hash(db, day, ip, ua)))
    db.commit()
    if _cleanup_at["t"] is None or now - _cleanup_at["t"] > timedelta(hours=1):
        _cleanup_at["t"] = now
        cleanup(db, now)


def cleanup(db, now: datetime | None = None) -> dict:
    """Souhrn uniků za uzavřené dny → analytics_daily; otisky starší 24 h → NULL; staré soli pryč."""
    now = now or datetime.now(timezone.utc)
    cur = today(now)
    days = db.scalars(select(AnalyticsEvent.day).distinct().where(AnalyticsEvent.day < cur,
                                                                 AnalyticsEvent.visitor.is_not(None))).all()
    for d in days:
        views, uniq = db.execute(select(func.count(AnalyticsEvent.id), func.count(distinct(AnalyticsEvent.visitor)))
                                 .where(AnalyticsEvent.day == d, AnalyticsEvent.kind.in_(PAGE_KINDS))).one()
        row = db.get(AnalyticsDaily, d) or AnalyticsDaily(day=d, views=0, uniques=0)
        row.views, row.uniques = views, max(row.uniques or 0, uniq)
        db.add(row)
    db.flush()
    n = db.execute(update(AnalyticsEvent).where(AnalyticsEvent.created_at < now - timedelta(hours=24),
                                                AnalyticsEvent.visitor.is_not(None)).values(visitor=None)
                   .execution_options(synchronize_session=False)).rowcount
    db.execute(delete(AnalyticsSalt).where(AnalyticsSalt.day < cur).execution_options(synchronize_session=False))
    db.execute(delete(AnalyticsEvent).where(AnalyticsEvent.created_at < now - timedelta(days=RETENTION_DAYS))
               .execution_options(synchronize_session=False))
    db.commit()
    return {"rolled_days": len(days), "hashes_cleared": n}


def report(db, days: int = 30, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cur = today(now)
    since_day = (now.astimezone(TZ).date() - timedelta(days=days - 1)).isoformat()
    E = AnalyticsEvent
    views = E.kind.in_(PAGE_KINDS)
    per_day = dict(db.execute(select(E.day, func.count(E.id)).where(views, E.day >= since_day).group_by(E.day)).all())
    daily = {r.day: r for r in db.scalars(select(AnalyticsDaily).where(AnalyticsDaily.day >= since_day)).all()}
    today_uniques = db.scalar(select(func.count(distinct(E.visitor))).where(views, E.day == cur)) or 0
    series = []
    for i in range(days - 1, -1, -1):
        d = (now.astimezone(TZ).date() - timedelta(days=i)).isoformat()
        u = today_uniques if d == cur else (daily[d].uniques if d in daily else None)
        series.append({"day": d, "views": per_day.get(d, 0), "uniques": u})

    def top(col, where, n=15):
        return db.execute(select(col, func.count(E.id).label("n")).where(where, E.day >= since_day)
                          .group_by(col).order_by(func.count(E.id).desc(), col).limit(n)).all()

    searches = db.execute(select(E.query, func.count(E.id), func.max(E.results)).where(
        E.kind.in_(("search", "live")), E.query.is_not(None), E.day >= since_day).group_by(E.query)).all()
    counts = dict(db.execute(select(E.query, func.count(E.id)).where(E.kind == "search", E.query.is_not(None),
                                                                     E.day >= since_day).group_by(E.query)).all())
    top_searches = sorted(((q, counts.get(q, n), mx) for q, n, mx in searches if q in counts), key=lambda x: (-x[1], x[0]))
    zero = [(q, n) for q, n, mx in top_searches if not mx][:20]
    prod_rows = top(E.product_id, (E.product_id.is_not(None)) & views, 10)
    names = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_([r[0] for r in prod_rows]))).all()} if prod_rows else {}
    return {
        "days": days, "series": series, "total_views": sum(s["views"] for s in series),
        "today_uniques": today_uniques,
        "sum_uniques": sum(s["uniques"] or 0 for s in series),
        "top_pages": top(E.path, views),
        "top_refs": top(E.ref_domain, views & E.ref_domain.is_not(None), 10),
        "top_searches": top_searches[:20], "zero_searches": zero,
        "top_products": [(names.get(pid), n) for pid, n in prod_rows if names.get(pid)],
    }
