"""Živé vyhledávání v obchodech, které to dovolují (robots.txt, bez obcházení ochran).

Každý obchod v registry může mít `searcher` = funkce (query, http_get) -> list[SearchHit].
`live_search()` spustí všechny obchody souběžně (timeout ~8 s na obchod), výsledky
seřadí podle ceny a drží 10 minut v paměti.
"""
import html as _html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from .robots import Robots
from .scrape import USER_AGENT, ROBOTS_AGENT, BLOCK_MARKERS

log = logging.getLogger("lovceny.search")

TIMEOUT_S = 8.0
CACHE_TTL_S = 600
MIN_DELAY_S = 2.0  # rozestup živých dotazů na stejnou doménu


@dataclass
class SearchHit:
    shop: str
    name: str
    price: float
    url: str
    image: str | None = None
    original_price: float | None = None


@dataclass
class ShopResult:
    shop: str
    status: str  # ok | empty | robots | blocked | timeout | error
    hits: list[SearchHit] = field(default_factory=list)
    note: str = ""


@dataclass
class LiveResult:
    hits: list[SearchHit]
    shops: list[ShopResult]
    cached: bool = False


class SearchBlocked(Exception):
    pass


# ---------- HTTP (robots + šetrnost) ----------
_robots: dict[str, tuple[float, Robots]] = {}
_last: dict[str, float] = {}
_lock = threading.Lock()


def _robots_for(c: httpx.Client, base: str) -> Robots:
    hit = _robots.get(base)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    rp = Robots()
    try:
        r = c.get(base + "/robots.txt")
        if r.status_code < 400:
            rp = Robots(r.text)
    except httpx.HTTPError:
        pass
    _robots[base] = (time.monotonic(), rp)
    return rp


def http_get(url: str, timeout: float = TIMEOUT_S) -> tuple[str, str]:
    """Vrací (html, výsledná URL). Respektuje robots.txt i po přesměrování, neobchází ochrany."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9"},
                      follow_redirects=True, timeout=timeout) as c:
        if not _robots_for(c, base).can_fetch(ROBOTS_AGENT, url):
            raise SearchBlocked("robots")
        with _lock:
            wait_s = _last.get(p.netloc, 0) + MIN_DELAY_S - time.monotonic()
            _last[p.netloc] = time.monotonic() + max(wait_s, 0)
        if wait_s > 0:
            time.sleep(wait_s)
        r = c.get(url)
        final = str(r.url)
        if r.status_code in (403, 429, 503):
            raise SearchBlocked(f"HTTP {r.status_code}")
        r.raise_for_status()
        if len(r.text) < 20000 and any(m in r.text[:3000] for m in BLOCK_MARKERS):
            raise SearchBlocked("JS challenge")
        fp = urlparse(final)
        if fp.netloc == p.netloc and not _robots_for(c, base).can_fetch(ROBOTS_AGENT, final):
            raise SearchBlocked("robots")
        return r.text, final


# ---------- parsery ----------
def _price(s: str) -> float | None:
    s = _html.unescape(s).replace("\xa0", "").replace(" ", "")
    m = re.search(r"(\d[\d.]*)(?:,(\d{1,2}))?Kč", s)
    if not m:
        return None
    return float(m.group(1).replace(".", "") + ("." + m.group(2) if m.group(2) else ""))


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_mironet(page: str, base: str = "https://www.mironet.cz/") -> list[SearchHit]:
    hits, seen = [], set()
    blocks = re.split(r'<div\s+class="(?:item_b\b[^"]*|nej_item[^"]*)"', page)[1:]
    for b in blocks:
        name_m = re.search(r'<a href="([^"]+\+dp\d+/?)"[^>]*class="item_b_name"[^>]*>(.*?)</a>', b, re.S)
        price_m = re.search(r'class="item_b_cena"[^>]*>(.*?)</div>', b, re.S)
        if not (name_m and price_m):
            continue
        url = urljoin(base, name_m.group(1))
        price = _price(price_m.group(1))
        if price is None or url in seen:
            continue
        seen.add(url)
        img_m = re.search(r'<img[^>]+src="([^"]+/Foto/[^"]+)"', b)
        orig_m = re.search(r'class="trought"[^>]*>(.*?)</span>', b, re.S)
        orig = _price(orig_m.group(1)) if orig_m else None
        hits.append(SearchHit("mironet", _text(name_m.group(2)), price, url,
                              urljoin("https:", img_m.group(1)) if img_m else None,
                              orig if orig and orig > price else None))
    return hits


def search_mironet(query: str, get=http_get) -> list[SearchHit]:
    page, final = get("https://www.mironet.cz/ProductList/showSearch?EXPF=" + quote_plus(query))
    return parse_mironet(page, final)


# ---------- orchestrace ----------
_cache: dict[str, tuple[float, LiveResult]] = {}
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="search")


def _matches(name: str, query: str) -> bool:
    n = name.lower()
    return all(t in n for t in query.lower().split())


def live_search(query: str, shops=None, timeout: float = TIMEOUT_S, limit_per_shop: int = 12) -> LiveResult:
    from .registry import SHOPS
    key = " ".join(query.lower().split())
    now = time.monotonic()
    c = _cache.get(key)
    if c and now - c[0] < CACHE_TTL_S and shops is None:
        return LiveResult(c[1].hits, c[1].shops, cached=True)
    targets = [s for s in (shops or SHOPS.values()) if s.searcher]
    futs = {_pool.submit(s.searcher, query): s for s in targets}
    done, _ = wait(futs, timeout=timeout)
    results, hits = [], []
    for f, s in futs.items():
        if f not in done:
            f.cancel()
            results.append(ShopResult(s.slug, "timeout", note="neodpověděl včas"))
            continue
        try:
            hs = [h for h in f.result() if _matches(h.name, query)] or f.result()
            hs = sorted(hs, key=lambda h: h.price)[:limit_per_shop]
            results.append(ShopResult(s.slug, "ok" if hs else "empty", hs))
            hits += hs
        except SearchBlocked as e:
            results.append(ShopResult(s.slug, "robots" if str(e) == "robots" else "blocked", note=str(e)))
        except Exception as e:  # síť, změna HTML…
            log.warning("search %s failed: %s", s.slug, e)
            results.append(ShopResult(s.slug, "error", note=type(e).__name__))
    res = LiveResult(sorted(hits, key=lambda h: h.price), results)
    if shops is None:
        _cache[key] = (now, res)
    return res


def clear_cache():
    _cache.clear()
