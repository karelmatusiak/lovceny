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
    """„14 509 Kč“, „1 290.00 Kč“, „1.290,50 Kč“, „16 784,- Kč“ → float."""
    s = re.sub(r"<[^>]+>", "", _html.unescape(s)).replace("\xa0", "").replace("\u202f", "").replace(" ", "")
    m = re.search(r"(\d[\d.,]*)(?:,-)?Kč", s)
    if not m:
        return None
    num = m.group(1).rstrip(".,")
    dec = re.search(r"[.,](\d{1,2})$", num)
    whole = num[:dec.start()] if dec else num
    try:
        return float(re.sub(r"[.,]", "", whole) + ("." + dec.group(1) if dec else ""))
    except ValueError:
        return None


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


def search_mironet(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.mironet.cz/ProductList/showSearch?EXPF=" + quote_plus(query))
    return parse_mironet(page, final)


def _blocks(page: str, start_re: str) -> list[str]:
    return re.split(start_re, page)[1:]


def _img(b: str, base: str) -> str | None:
    m = re.search(r'<img[^>]+(?:data-original|data-src)="([^"]+)"', b) or re.search(r'<img[^>]+src="(https?:[^"]+|//[^"]+|/[^"]+)"', b)
    return urljoin(base, _html.unescape(m.group(1))) if m else None


def parse_softcom(page: str, base: str = "https://www.softcom.cz/eshop/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'<div class="code"><span>'):
        a = re.search(r'<h2><a href="([^"]+_d\d+\.html)"[^>]*>(.*?)</a>', b, re.S)
        pr = re.search(r'<div class="wvat"><span>(.*?)</span>', b, re.S)
        if a and pr and (price := _price(pr.group(1))):
            hits.append(SearchHit("softcom", _text(a.group(2)), price, urljoin(base, _html.unescape(a.group(1))), _img(b, base)))
    return hits


def parse_softcom_detail(page: str):
    """Detail Softcomu nemá JSON-LD ani meta cenu → čteme h1 a „Cena s DPH“."""
    from .scrape import ScrapeResult
    name = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    pr = re.search(r'class="prc wvat">.*?<td class="price">(.*?)</td>', page, re.S)
    price = _price(pr.group(1)) if pr else None
    if price is None:
        return None
    img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return ScrapeResult(price, None, None, _text(name.group(1)) if name else None, img.group(1) if img else None, None, None)


def search_softcom(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.softcom.cz/eshop/default.asp?cls=stoitems&fulltext=" + quote_plus(query))
    return parse_softcom(page, final)


def parse_svetpocitacu(page: str, base: str = "https://www.svetpocitacu.cz/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'<div class="product">'):
        a = re.search(r'<a href="([^"]+)"[^>]*class="product__name"[^>]*>(.*?)</a>', b, re.S)
        pr = re.search(r'class="product__price-with-vat"[^>]*>(.*?)</div>', b, re.S)
        if a and pr and (price := _price(pr.group(1))):
            orig = re.search(r'class="product__price-(?:old|before|original)[^"]*"[^>]*>(.*?)</', b, re.S)
            o = _price(orig.group(1)) if orig else None
            hits.append(SearchHit("svetpocitacu", _text(a.group(2)), price, urljoin(base, a.group(1)), _img(b, base),
                                  o if o and o > price else None))
    return hits


def search_svetpocitacu(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.svetpocitacu.cz/hledani/?q=" + quote_plus(query))
    return parse_svetpocitacu(page, final)


def parse_abcom(page: str, base: str = "https://www.ab-com.cz/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'<div class="list-products__item js-list-products-item">'):
        a = re.search(r'<a href="([^"]+)" class="list-products__item__link"', b)
        n = re.search(r'class="list-products__item__title[^"]*"[^>]*>(.*?)</h2>', b, re.S)
        pr = re.search(r'list-products__item__add__prices__item--main"[^>]*>\s*<span>(.*?)</span>', b, re.S)
        if a and n and pr and (price := _price(pr.group(1))):
            hits.append(SearchHit("abcom", _text(n.group(1)), price, urljoin(base, a.group(1)), _img(b, base)))
    return hits


def search_abcom(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.ab-com.cz/hledani?q=" + quote_plus(query))
    return parse_abcom(page, final)


# ---------- orchestrace ----------
_cache: dict[str, tuple[float, LiveResult]] = {}
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="search")


def _matches(name: str, query: str) -> bool:
    n = name.lower()
    return all(t in n for t in query.lower().split())


ACCESSORY_WORDS = ("pouzdro", "obal", "kryt", "fólie", "folie", "sklo", "ochrann", "screenshield", "řemín", "remin",
                   "držák", "drzak", "kabel", "nabíječ", "nabijec", "adaptér", "adapter", "lens protection",
                   "redukce", "stylus", "ochrana", "panzerglass", "bumper", "silikonov", "quickfit", "tvrzen", "magsafe", "popruh", "náramek pro", "nálepk", "flip", "case")


def is_accessory(name: str) -> bool:
    n = name.lower()
    return any(w in n for w in ACCESSORY_WORDS)


def drop_accessories(hits: list[SearchHit], query: str) -> list[SearchHit]:
    """Když dotaz nehledá příslušenství, schováme obaly/fólie/řemínky – jinak by „nejlevnější“ byly kryty."""
    if is_accessory(query):
        return hits
    main = [h for h in hits if not is_accessory(h.name)]
    return main or hits


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
            hs = drop_accessories([h for h in f.result() if _matches(h.name, query)] or f.result(), query)
            hs = sorted(hs, key=lambda h: h.price)[:limit_per_shop]
            results.append(ShopResult(s.slug, "ok" if hs else "empty", hs))
            hits += hs
        except SearchBlocked as e:
            results.append(ShopResult(s.slug, "robots" if str(e) == "robots" else "blocked", note=str(e)))
        except Exception as e:  # síť, změna HTML…
            log.warning("search %s failed: %s", s.slug, e)
            results.append(ShopResult(s.slug, "error", note=type(e).__name__))
    res = LiveResult(sorted(drop_accessories(hits, query), key=lambda h: h.price), results)
    if shops is None:
        _cache[key] = (now, res)
    return res


def clear_cache():
    _cache.clear()
