"""Ověř slevu (/over-slevu, /api/v1/overit): uživatel vloží odkaz na produkt z libovolného e-shopu.

- Obchod je v registry a čtení dovoluje → stáhneme JEDNU stránku produktu (robots.txt, limit velikosti
  a času, přesměrování jen v rámci domén obchodu, jen veřejné IP), přečteme cenu (page_parser obchodu,
  jinak JSON-LD → microdata → meta).
- Neznámý / blokovaný obchod / feed → stránku NEstahujeme; vysvětlíme proč a zkusíme živé hledání
  podle názvu odvozeného z adresy (slug).
- Máme-li produkt v DB (stejná URL nebo EAN), ukážeme historii a kontrolu 30denního minima (§ 12a ZOS).
- Další obchody: živé hledání podle názvu (stejné jako /hledat/zive, s cache).
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin, unquote

import httpx
from sqlalchemy import select

from .models import Offer, Product
from .shops import shop_for_url
from .shops.scrape import USER_AGENT, ROBOTS_AGENT, BLOCK_MARKERS, parse_generic
from .util import normalize_url

MAX_URL_LEN = 2000
MAX_BYTES = 3 * 1024 * 1024     # stránka produktu max. 3 MB
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
DEADLINE_S = 10.0               # celkový čas na stažení stránky
MAX_REDIRECTS = 3
CACHE_TTL_S = 900               # výsledek ověření (bez živého hledání) 15 min
PER_IP_HOUR = 20                # nová ověření (mimo cache) na IP hash za hodinu
GLOBAL_DAY = 1000               # strop ověření za den
MIN_HISTORY_DAYS = 7            # kontrola 30 dní jen s aspoň týdnem měření (stejně jako verdikt)


class BadUrl(ValueError):
    pass


# ---------- validace URL a SSRF ochrana ----------
def _ip_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                                or a.is_reserved or a.is_unspecified)


def clean_url(raw: str) -> str:
    """Zkontroluje vložený odkaz. Vrací normalizovanou URL, jinak BadUrl s českou hláškou."""
    u = (raw or "").strip()
    if not u:
        raise BadUrl("Vlož odkaz na produkt.")
    if len(u) > MAX_URL_LEN:
        raise BadUrl("Odkaz je příliš dlouhý.")
    if re.match(r"^www\.", u, re.I):
        u = "https://" + u
    try:
        p = urlparse(u)
    except ValueError:
        raise BadUrl("Tohle nevypadá jako odkaz.")
    if p.scheme.lower() not in ("http", "https"):
        raise BadUrl("Podporujeme jen odkazy začínající http:// nebo https://.")
    host = (p.hostname or "").lower().rstrip(".")
    if not host or p.username or p.password:
        raise BadUrl("Tohle nevypadá jako odkaz na e-shop.")
    try:
        port = p.port
    except ValueError:
        raise BadUrl("Neplatný port v odkazu.")
    if port not in (None, 80, 443):
        raise BadUrl("Odkazy s neobvyklým portem neověřujeme.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")) or "." not in host:
        raise BadUrl("Tohle nevypadá jako veřejný e-shop.")
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise BadUrl("Zadej prosím adresu obchodu, ne IP adresu.")
    return normalize_url(u)


FAKEIP_NET = ipaddress.ip_network("198.18.0.0/15")


def _fakeip_ok(ip: str) -> bool:
    """Jen pro vývoj za proxy s „fake-IP“ DNS (rozsah 198.18.0.0/15): LOVCENY_DNS_FAKEIP_OK=1. V produkci nenastavovat."""
    import os
    if os.environ.get("LOVCENY_DNS_FAKEIP_OK") != "1":
        return False
    try:
        return ipaddress.ip_address(ip) in FAKEIP_NET
    except ValueError:
        return False


def resolves_public(host: str) -> bool:
    """Všechny DNS záznamy hostitele musí být veřejné adresy (žádné 127.0.0.1, 10.x, 169.254.x…)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    ips = {i[4][0] for i in infos}
    return bool(ips) and all(_ip_public(ip) or _fakeip_ok(ip) for ip in ips)


class FetchError(Exception):
    """kind: robots | blocked | captcha | too_big | timeout | error | redirect"""
    def __init__(self, kind: str, msg: str = ""):
        super().__init__(msg or kind)
        self.kind = kind


_last: dict[str, float] = {}
_lock = threading.Lock()
MIN_DELAY_S = 2.0


def _in_domains(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def safe_fetch(url: str, domains, resolver=resolves_public, transport=None) -> tuple[str, str]:
    """Stáhne stránku produktu: robots.txt, jen domény obchodu, jen veřejné IP, max. MAX_BYTES a DEADLINE_S.
    Ochranu proti botům neobchází (403/429/503 nebo JS challenge → FetchError)."""
    from .shops.search import _robots_for
    t0 = time.monotonic()
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9",
                               "Accept": "text/html,application/xhtml+xml"},
                      follow_redirects=False, timeout=TIMEOUT, transport=transport) as c:
        cur = url
        for _ in range(MAX_REDIRECTS + 1):
            p = urlparse(cur)
            host = (p.hostname or "").lower()
            if p.scheme not in ("http", "https") or not _in_domains(host, domains) or p.port not in (None, 80, 443):
                raise FetchError("redirect", "přesměrování mimo obchod")
            if not resolver(host):
                raise FetchError("error", "adresa obchodu nevede na veřejný server")
            base = f"{p.scheme}://{p.netloc}"
            with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=TIMEOUT,
                              transport=transport) as rc:
                robots = _robots_for(rc, base)
            if not robots.can_fetch(ROBOTS_AGENT, cur):
                raise FetchError("robots", "robots.txt obchodu čtení této stránky nedovoluje")
            with _lock:
                wait_s = _last.get(host, 0) + MIN_DELAY_S - time.monotonic()
                _last[host] = time.monotonic() + max(wait_s, 0)
            if wait_s > 0:
                time.sleep(min(wait_s, MIN_DELAY_S))
            try:
                with c.stream("GET", cur) as r:
                    if r.is_redirect:
                        loc = r.headers.get("location", "")
                        cur = urljoin(cur, loc)
                        continue
                    if r.status_code in (403, 429, 503):
                        raise FetchError("blocked", f"obchod odpověděl HTTP {r.status_code}")
                    if r.status_code >= 400:
                        raise FetchError("error", f"obchod odpověděl HTTP {r.status_code}")
                    ct = r.headers.get("content-type", "")
                    if ct and "html" not in ct and "xml" not in ct:
                        raise FetchError("error", "odkaz nevede na webovou stránku")
                    if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                        raise FetchError("too_big", "stránka je příliš velká")
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > MAX_BYTES:
                            raise FetchError("too_big", "stránka je příliš velká")
                        if time.monotonic() - t0 > DEADLINE_S:
                            raise FetchError("timeout", "obchod neodpověděl včas")
                    text = bytes(buf).decode(r.encoding or "utf-8", "replace")
            except httpx.TimeoutException:
                raise FetchError("timeout", "obchod neodpověděl včas")
            except httpx.HTTPError as e:
                raise FetchError("error", f"chyba spojení ({type(e).__name__})")
            if len(text) < 20000 and any(m in text[:3000] for m in BLOCK_MARKERS):
                raise FetchError("captcha", "obchod chrání stránku proti robotům")
            return text, cur
        raise FetchError("redirect", "příliš mnoho přesměrování")


# ---------- název z adresy ----------
_NOISE = re.compile(r"^(p|d|dp|i|id|pd|prod|product|produkt|detail|item)?\d{3,}$|^[a-f0-9]{8,}$", re.I)
_STOP = {"html", "htm", "php", "aspx", "asp", "detail", "produkt", "product", "p", "d", "dp", "cz", "www", "eshop", "shop",
         "zbozi", "item", "default", "index"}


def name_from_url(url: str) -> str:
    """Odhad názvu produktu z adresy: „/apple-iphone-16-128gb-cerny-d1234567.htm“ → „apple iphone 16 128gb cerny“."""
    p = urlparse(url)
    segs = [unquote(s) for s in p.path.split("/") if s]
    best = ""
    for s in reversed(segs):
        s = re.sub(r"\.(html?|php|aspx?)$", "", s, flags=re.I)
        words = [w for w in re.split(r"[-_+\s.]+", s) if w]
        words = [w for w in words if not _NOISE.match(w) and w.lower() not in _STOP]
        if len(words) >= 2 and sum(ch.isalpha() for ch in "".join(words)) >= 4:
            best = " ".join(words)
            break
    best = re.sub(r"\s+", " ", best).strip()
    return " ".join(best.split()[:8])[:100]


_UNITS = {"gb": "GB", "tb": "TB", "mb": "MB", "mah": "mAh", "hz": "Hz", "w": "W"}


def pretty_name(q: str) -> str:
    """Zobrazení názvu odhadnutého z adresy: „samsung galaxy s25 256gb“ → „Samsung Galaxy S25 256GB“."""
    out = []
    for w in (q or "").split():
        m = re.fullmatch(r"(\d+)(gb|tb|mb|mah|hz|w)", w, re.I)
        if m:
            out.append(m.group(1) + _UNITS[m.group(2).lower()])
        elif w.islower():
            out.append(w[:1].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out)


# ---------- výsledek ----------
@dataclass
class Check30:
    """Kontrola podle § 12a ZOS z našich měření (jen když máme ≥ MIN_HISTORY_DAYS dní)."""
    days: int
    min30: float | None = None
    min30_date: object = None
    current: float | None = None
    claimed: float | None = None     # „původní“ cena podle obchodu
    enough: bool = False
    flag: bool = False               # sleva se počítá z ceny > 105 % minima za 30 dní
    has_demo: bool = False


@dataclass
class VerifyResult:
    url: str
    status: str                 # ok | unknown_shop | blocked_shop | feed | robots | blocked | not_product | error
    message: str = ""
    shop: object = None
    name: str | None = None
    price: float | None = None
    original_price: float | None = None
    in_stock: bool | None = None
    image: str | None = None
    ean: str | None = None
    query: str = ""             # podle čeho hledáme jinde
    product: Product | None = None
    verdict: object = None
    check: Check30 | None = None
    elsewhere: list = field(default_factory=list)  # SearchHit/podobné, seřazené podle ceny
    live_statuses: list = field(default_factory=list)
    can_track: bool = False
    fetched_at: datetime | None = None
    cached: bool = False

    @property
    def lowest_elsewhere(self):
        return self.elsewhere[0] if self.elsewhere else None


STATUS_TEXT = {
    "unknown_shop": "Tento obchod zatím nesledujeme, takže jeho stránku nečteme. Zkusili jsme najít stejný produkt v obchodech, které sledujeme.",
    "blocked_shop": "Tento obchod automatické čtení blokuje nebo zakazuje – jeho stránku z ohleduplnosti nečteme. Zkusili jsme najít produkt jinde.",
    "feed": "U tohoto obchodu bereme ceny jen z oficiálního feedu, stránku přímo nečteme.",
    "robots": "Obchod v souboru robots.txt čtení této stránky nedovoluje, proto ji nečteme.",
    "blocked": "Obchod teď automatické čtení odmítl (ochrana proti robotům). Neobcházíme ji.",
    "not_product": "Na stránce jsme nenašli cenu produktu. Je to opravdu odkaz na konkrétní produkt?",
    "error": "Stránku obchodu se nepodařilo načíst.",
}

_cache: dict[str, tuple[float, VerifyResult]] = {}
_rl: dict[str, list[float]] = {}
_day = {"day": "", "n": 0}


def rate_ok(ip: str, per_hour: int | None = None, scope: str = "overit") -> bool:
    """Rate limit v paměti; IP jen jako hash (nikam se neukládá)."""
    per_hour = per_hour or PER_IP_HOUR
    key = hashlib.sha256(f"lovceny:{scope}:{ip}".encode()).hexdigest()
    now = time.monotonic()
    hits = [t for t in _rl.get(key, []) if now - t < 3600]
    today = datetime.now(timezone.utc).date().isoformat()
    if _day["day"] != today:
        _day.update(day=today, n=0)
    if len(hits) >= per_hour or _day["n"] >= GLOBAL_DAY:
        _rl[key] = hits
        return False
    _rl[key] = hits + [now]
    _day["n"] += 1
    if len(_rl) > 5000:  # úklid
        for k in [k for k, v in _rl.items() if not v or now - v[-1] > 3600]:
            _rl.pop(k, None)
    return True


def cached(url: str) -> VerifyResult | None:
    c = _cache.get(url)
    if c and time.monotonic() - c[0] < CACHE_TTL_S:
        return c[1]
    return None


def clear_cache():
    _cache.clear()
    _rl.clear()
    _day.update(day="", n=0)


def _include_demo() -> bool:
    from .deals import include_demo
    return include_demo()


def check30(product: Product, current: float | None, claimed: float | None, now: datetime | None = None) -> Check30:
    from .verdict import daily_best, REF_OVER_MIN30
    now = now or datetime.now(timezone.utc)
    pts = [pt for o in product.offers for pt in o.points if _include_demo() or not pt.is_demo]
    days90 = daily_best(pts, now - timedelta(days=90))
    last30 = {d: v for d, v in days90.items() if now.date() - timedelta(days=30) <= d < now.date()}
    c = Check30(days=len(days90), current=current, claimed=claimed, has_demo=any(pt.is_demo for pt in pts))
    if len(days90) < MIN_HISTORY_DAYS or not last30:
        return c
    d0, v0 = min(last30.items(), key=lambda x: (x[1], x[0]))
    c.min30, c.min30_date, c.enough = v0, d0, True
    c.flag = bool(claimed and current and claimed > v0 * (1 + REF_OVER_MIN30) and current < claimed)
    return c


def _find_product(db, url: str, shop, ean: str | None) -> Product | None:
    cands = {url}
    if shop:
        cands.add(shop.canonical_url(url))
    o = db.scalar(select(Offer).where(Offer.url.in_(cands)))
    if o:
        return o.product
    if ean and re.fullmatch(r"\d{8,14}", ean):
        return db.scalar(select(Product).where(Product.ean == ean))
    return None


def verify(db, raw_url: str, *, live: bool = True, fetch=None, hint_name: str | None = None,
           hint_price: float | None = None, hint_original: float | None = None) -> VerifyResult:
    """Hlavní logika. hint_* = údaje, které poslalo rozšíření z otevřené stránky (pak stránku nestahujeme)."""
    from .verdict import compute
    fetch = fetch or safe_fetch
    url = clean_url(raw_url)
    shop = shop_for_url(url)
    res = VerifyResult(url=url, status="ok", shop=shop, fetched_at=datetime.now(timezone.utc))
    if shop is None:
        res.status = "unknown_shop"
    elif shop.status != "ok":
        res.status = "blocked_shop"
    elif shop.source == "feed":
        res.status = "feed"
    if hint_price and hint_price > 0:
        res.name, res.price = (hint_name or "").strip()[:200] or None, float(hint_price)
        res.original_price = float(hint_original) if hint_original and hint_original > hint_price else None
        if shop is not None and shop.status == "ok":
            res.status = "ok"
    elif res.status == "ok":
        try:
            html, final = fetch(shop.canonical_url(url), shop.domains)
            r = (shop.page_parser or parse_generic)(html) or (parse_generic(html) if shop.page_parser else None)
            if r is None or not r.price:
                res.status = "not_product"
            else:
                res.name, res.price, res.original_price = r.name, float(r.price), r.original_price
                res.in_stock, res.image, res.ean = r.in_stock, r.image, r.ean
        except FetchError as e:
            res.status = e.kind if e.kind in ("robots", "blocked") else ("blocked" if e.kind == "captcha" else "error")
            res.message = str(e)
    res.product = _find_product(db, url, shop, res.ean)
    if res.product is not None:
        priced = [o for o in res.product.offers if o.price is not None]
        best = min(priced, key=lambda o: float(o.price), default=None)
        cur = res.price or (float(best.price) if best else None)
        pts = [pt for o in res.product.offers for pt in o.points]
        res.verdict = compute(pts, cur, res.original_price)
        res.check = check30(res.product, cur, res.original_price)
        res.name = res.name or res.product.name
    res.query = _query(res.name or "") or name_from_url(url)
    res.can_track = bool(shop and shop.status == "ok" and res.product is None
                         and (shop.source == "feed" or res.status == "ok"))
    if not res.message:
        res.message = STATUS_TEXT.get(res.status, "")
    if live and res.query:
        _elsewhere(db, res)
    return res


def _query(name: str) -> str:
    """Z celého názvu nechá prvních ~6 slov (bez závorek a barev za čárkou) – lepší shoda v obchodech."""
    n = re.sub(r"\(.*?\)|\[.*?\]", " ", name).split(",")[0]
    return " ".join(n.split()[:6])[:100]


def _elsewhere(db, res: VerifyResult):
    from .shops.search import live_search, SearchHit
    from .util import normalize_url as nu
    own = nu(res.url)
    out = []
    if res.product is not None:
        for o in res.product.offers:
            if o.price is not None and o.status == "ok" and nu(o.url) != own:
                out.append(SearchHit(o.shop, res.product.name, float(o.price), o.url))
    try:
        lr = live_search(res.query)
        res.live_statuses = lr.shops
        seen = {nu(h.url) for h in out} | {own}
        same_shop = res.shop.slug if res.shop else None
        for h in lr.hits:
            if nu(h.url) in seen or (same_shop and h.shop == same_shop and res.status == "ok"):
                continue
            seen.add(nu(h.url))
            out.append(h)
    except Exception:  # noqa: BLE001 – živé hledání je jen doplněk
        pass
    res.elsewhere = sorted(out, key=lambda h: h.price)[:8]


def cache_key(url: str, **kw) -> str:
    return url + "|" + "|".join(str(kw.get(k) or "") for k in ("hint_name", "hint_price", "hint_original", "live"))


def verify_cached(db, raw_url: str, **kw) -> tuple[VerifyResult, bool]:
    """→ (výsledek, z_cache). Klíč = normalizovaná URL (+ údaje z rozšíření)."""
    url = clean_url(raw_url)
    key = cache_key(url, **kw)
    c = cached(key)
    if c is not None:
        if c.product is not None:  # objekt z jiné session → načti znovu (verdikt zůstává z cache)
            c.product = db.get(Product, c.product.id)
        return c, True
    r = verify(db, url, **kw)
    _cache[key] = (time.monotonic(), r)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
            _cache.pop(k, None)
    return r, False
