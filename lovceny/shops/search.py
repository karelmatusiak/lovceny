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


def parse_4home(page: str, base: str = "https://www.4home.cz/") -> list[SearchHit]:
    import json
    hits = []
    for b in _blocks(page, r'<article class="products-list-item'):
        m = re.search(r"data-clickvaluesdatalayer='([^']+)'", b)
        if not m:
            continue
        try:
            it = json.loads(_html.unescape(m.group(1)))["ecommerce"]["items"][0]
        except (ValueError, KeyError, IndexError):
            continue
        pr = it.get("price") or {}
        price, orig = pr.get("czk_with_vat"), pr.get("czk_with_vat_original")
        if not price or not it.get("item_id"):
            continue
        img = re.search(r'<img[^>]+(?:data-srcset|src)="(https://cdn\.4home\.cz/[^" ]+)', b)
        hits.append(SearchHit("4home", it.get("item_name", ""), float(price), urljoin(base, it["item_id"]),
                              img.group(1) if img else None, float(orig) if orig and orig > price else None))
    return hits


def search_4home(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.4home.cz/hledat/?q=" + quote_plus(query))
    return parse_4home(page, final)


def parse_vestavne(page: str, base: str = "https://www.vestavne-spotrebice.cz/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'<div class="polozka_vypis_produkt">'):
        a = re.search(r'<h2 class="h2_nazev_produktu_vypis"><a href="([^"]+)">(.*?)</a></h2>', b, re.S)
        cat = re.search(r'<h3 class="h3_kategorie_produktu_vypis">(.*?)</h3>', b, re.S)
        pr = re.search(r'class="cena_akt_vypis_produkt">(.*?)</span>', b, re.S)
        if not (a and pr and (price := _price(pr.group(1)))):
            continue
        orig_m = re.search(r'class="cena_pred_vypis_produkt">(.*?)</span>', b, re.S)
        o = _price(orig_m.group(1)) if orig_m else None
        name = _text(a.group(2)) + (f" ({_text(cat.group(1))})" if cat else "")
        hits.append(SearchHit("vestavne", name, price, urljoin(base, a.group(1)), _img(b, base), o if o and o > price else None))
    return hits


def search_vestavne(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.vestavne-spotrebice.cz/search/?search_value=" + quote_plus(query))
    return parse_vestavne(page, final)


def parse_eta(page: str, base: str = "https://www.eta.cz/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'(?=<a tabindex="-1" href="[^"]+" class="product-tile__img-link")'):
        a = re.search(r'<h2 class="product-tile__title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        pr = re.search(r'price-box__price--action[^>]*>(.*?)</div>', b, re.S)
        if not (a and pr and (price := _price(pr.group(1)))):
            continue
        orig_m = re.search(r'price-box__price--old"[^>]*>(?:<span class="sr-only">[^<]*</span>)?([^<]*Kč)', b)
        o = _price(orig_m.group(1)) if orig_m else None
        hits.append(SearchHit("eta", _text(a.group(2)), price, urljoin(base, a.group(1)), _img(b, base), o if o and o > price else None))
    return hits


def parse_eta_detail(page: str):
    from .scrape import ScrapeResult
    name = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    pr = re.search(r'price-box__price--action price-box__price--large[^>]*>(.*?)</div>', page, re.S)
    price = _price(_text(pr.group(1)).replace(",-", "") + " Kč") if pr else None
    if price is None:
        return None
    img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return ScrapeResult(price, None, None, _text(name.group(1)) if name else None,
                        urljoin("https://www.eta.cz/", img.group(1)) if img else None, None, None)


def search_eta(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.eta.cz/shop/vyhledavani/?q=" + quote_plus(query))
    return parse_eta(page, final)


def parse_onlineshop(page: str, base: str = "https://www.onlineshop.cz/") -> list[SearchHit]:
    """Cena „běžná“ / výchozí, ne cena po zadání slevového kódu (tu dostane jen ten, kdo kód zadá)."""
    hits = []
    for b in _blocks(page, r'class="f-n-product n-product"'):
        a = re.search(r'<h3 class="n-product-title"><a\s+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not a:
            continue
        items = re.findall(r'class="pr-pc-item ([a-z -]+)" data-value="([\d.]+)"', b)
        price = orig = None
        for cls, val in items:
            c = cls.split()
            if "discount-code" in c or "ex-vat" in c or "bazar" in c:
                continue
            if price is None:
                price = float(val)
            elif "bezna" in c and float(val) > price:
                orig = float(val)
        if price is None:
            continue
        img = re.search(r'<img[^>]+src="(https://static\.onlineshop\.cz/data/eshop_online/product/[^"]+)"', b)
        hits.append(SearchHit("onlineshop", _text(a.group(2)), price, urljoin(base, _html.unescape(a.group(1))),
                              _html.unescape(img.group(1)) if img else None, orig))
    return hits


def search_onlineshop(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.onlineshop.cz/vyhledavani/?q=" + quote_plus(query))
    return parse_onlineshop(page, final)


def parse_delonghi(page: str, base: str = "https://www.delonghi.com/") -> list[SearchHit]:
    """Oficiální e-shop De'Longhi CZ. Karta produktu nemá stabilní třídy (CSS-in-JS), proto
    skupiny podle URL produktu a ceny podle textu „aktuální cena“ / „původní cena“."""
    links = [(m.start(), _html.unescape(m.group(1))) for m in re.finditer(r'href="(/cs-cz/p/[^"]+\.html[^"]*)"', page)]
    groups, cur = [], None
    for pos, url in links:
        if cur is None or url != cur[1]:
            cur = [pos, url]
            groups.append(cur)
    hits, seen = [], set()
    for i, (pos, url) in enumerate(groups):
        end = groups[i + 1][0] if i + 1 < len(groups) else pos + 40000
        seg = page[pos:end]
        name = next((t for t in re.findall(r'href="/cs-cz/p/[^"]+">([^<]+)</a>', seg) if t.strip()), None)
        pr = re.search(r'aktuální cena ([^<]+)', seg)
        if not (name and pr) or url in seen or (price := _price(pr.group(1))) is None:
            continue
        seen.add(url)
        o = re.search(r'původní cena ([^<]+)', seg)
        orig = _price(o.group(1)) if o else None
        model = re.search(r'/([A-Z0-9][A-Z0-9.\-]+)\.html', url)
        nm = f"De'Longhi {_text(name)}" + (f" {model.group(1)}" if model and model.group(1) not in name else "")
        img = re.search(r'<img[^>]+src="(https://dam\.delonghi\.com/[^"]+)"', seg)
        hits.append(SearchHit("delonghi", nm, price, urljoin(base, url), img.group(1) if img else None,
                              orig if orig and orig > price else None))
    return hits


def search_delonghi(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.delonghi.com/cs-cz/search?q=" + quote_plus(query))
    return parse_delonghi(page, final)



def parse_mobilfone_detail(page: str):
    """Mobilfone (PrestaShop 1.x) nemá JSON-LD → cena z #our_price_display, název z h1/title."""
    from .scrape import ScrapeResult
    pr = re.search(r'id="our_price_display"[^>]*>(.*?)</span>', page, re.S)
    price = _price(pr.group(1)) if pr else None
    if price is None:
        return None
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    title = re.search(r"<title>(.*?)(?: - Mobilfone|</title>)", page, re.S)
    nm = (_text(h1.group(1)) if h1 else "") or (_text(title.group(1)) if title else None)
    img = re.search(r'<img[^>]+id="bigpic"[^>]+src="([^"]+)"', page) or re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return ScrapeResult(price, None, None, nm, img.group(1) if img else None, None, None)



def parse_quikset(page: str, base: str = "https://www.quikset.cz/") -> list[SearchHit]:
    hits = []
    ms = list(re.finditer(r'<h2 class="product-title"[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', page, re.S))
    for i, m in enumerate(ms):
        seg = page[m.end():ms[i + 1].start() if i + 1 < len(ms) else m.end() + 8000]
        pr = re.search(r'class="price__base"[^>]*>(.*?)</div>', seg, re.S)
        if not (pr and (price := _price(pr.group(1)))):
            continue
        o = re.search(r'class="price__(?:old|orig|before)[^"]*"[^>]*>(.*?)</', seg, re.S)
        orig = _price(o.group(1)) if o else None
        imgs = re.findall(r"<img[^>]+src='(https://www\.quikset\.cz/upload/[^']+)'", page[max(0, m.start() - 4000):m.start()])
        img = imgs[-1].replace("_p.", "_v.") if imgs else None
        hits.append(SearchHit("quikset", _text(m.group(2)), price, urljoin(base, m.group(1)), img,
                              orig if orig and orig > price else None))
    return hits


def parse_quikset_detail(page: str):
    from .scrape import ScrapeResult
    pr = re.search(r'class="price__base"[^>]*>(.*?)</', page, re.S)
    price = _price(pr.group(1)) if pr else None
    if price is None:
        return None
    name = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    return ScrapeResult(price, None, None, _text(name.group(1)) if name else None, img.group(1) if img else None, None, None)


def search_quikset(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.quikset.cz/hledani?q=" + quote_plus(query))
    return parse_quikset(page, final)



def parse_mobilyostrava(page: str, base: str = "https://www.mobilyostrava.cz/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'<div class="item-wrap">'):
        a = re.search(r'<h3>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        pr = re.search(r'<p class="price">(.*?)</p>', b, re.S)
        if not (a and pr and (price := _price(pr.group(1)))):
            continue
        img = re.search(r'<img src="([^"]+)"', b)
        hits.append(SearchHit("mobilyostrava", _text(a.group(2)), price, urljoin(base, a.group(1)),
                              urljoin(base, img.group(1)) if img else None))
    return hits


def search_mobilyostrava(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.mobilyostrava.cz/cz/hledat/?search_nazev=" + quote_plus(query))
    return parse_mobilyostrava(page, "https://www.mobilyostrava.cz/")



def parse_mobilprovas(page: str, base: str = "https://www.mobilprovas.cz/") -> list[SearchHit]:
    hits = []
    for b in _blocks(page, r'<div class="produkt lft'):
        a = re.search(r'<h2\s*><a href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        pr = re.search(r'<strong style="font-size: 1\.5em;"\s*>(.*?)</', b, re.S)
        if not (a and pr and (price := _price(pr.group(1)))):
            continue
        img = re.search(r'<img src="(katalog/thumbnails/[^"]+)"', b)
        hits.append(SearchHit("mobilprovas", _text(a.group(2)), price, urljoin(base, a.group(1)),
                              urljoin(base, img.group(1)) if img and "no_photo" not in img.group(1) else None))
    return hits


def search_mobilprovas(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.mobilprovas.cz/search/?find=" + quote_plus(query))
    return parse_mobilprovas(page, "https://www.mobilprovas.cz/")



def parse_ellexgk(page: str, base: str = "https://www.ellexgk.cz/") -> list[SearchHit]:
    """Ellex GK (Sony Center, platforma Upgates)."""
    hits = []
    ms = list(re.finditer(r'<h4 class="[^"]*p-i-header[^"]*"><a href="([^"]+)"[^>]*>(.*?)</a>', page, re.S))
    for i, m in enumerate(ms):
        seg = page[m.end():ms[i + 1].start() if i + 1 < len(ms) else m.end() + 6000]
        pr = re.search(r'class="p-i-price[^"]*">\s*(?:<del[^>]*>(.*?)</del>\s*)?<strong[^>]*>(.*?)</strong>', seg, re.S)
        if not (pr and (price := _price(pr.group(2)))):
            continue
        o = _price(pr.group(1)) if pr.group(1) else None
        first = page.find(f'href="{m.group(1)}"', max(0, m.start() - 6000))
        imgs = re.findall(r'<img src="(https://[^"]+cdn-upgates\.com/[^"]+)"', page[first:m.start()]) if first >= 0 else []
        hits.append(SearchHit("ellexgk", _text(m.group(2)), price, urljoin(base, m.group(1)), imgs[0] if imgs else None,
                              o if o and o > price else None))
    return hits


def search_ellexgk(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.ellexgk.cz/search?phrase=" + quote_plus(query))
    return parse_ellexgk(page, final)



def parse_woocommerce(page: str, shop: str, base: str) -> list[SearchHit]:
    """Standardní výpis WooCommerce: cena v <ins> (akce) nebo přímo, původní v <del>. 0 Kč = cena na dotaz → vynecháme."""
    hits = []
    for m in re.finditer(r'<a href="([^"]+)" class="woocommerce-LoopProduct-link[^"]*">(.*?)</a>', page, re.S):
        b = m.group(2)
        n = re.search(r'woocommerce-loop-product__title">(.*?)</h2>', b, re.S)
        pb = re.search(r'<span class="price">(.*)', b, re.S)
        if not (n and pb):
            continue
        pt = pb.group(1)
        ins = re.search(r'<ins[^>]*>(.*?)</ins>', pt, re.S)
        dl = re.search(r'<del[^>]*>(.*?)</del>', pt, re.S)
        price = _price(_text(ins.group(1) if ins else pt))
        if not price:
            continue
        o = _price(_text(dl.group(1))) if dl else None
        img = re.search(r'<img[^>]+src="([^"]+)"', b)
        hits.append(SearchHit(shop, _text(n.group(1)), price, urljoin(base, m.group(1)), img.group(1) if img else None,
                              o if o and o > price else None))
    return hits


def parse_itex24(page: str, base: str = "https://www.itex24.cz/") -> list[SearchHit]:
    return parse_woocommerce(page, "itex24", base)


def search_itex24(query: str, get=None) -> list[SearchHit]:
    page, final = (get or http_get)("https://www.itex24.cz/?s=" + quote_plus(query) + "&post_type=product")
    return parse_itex24(page, final)


# ---------- orchestrace ----------
_cache: dict[str, tuple[float, LiveResult]] = {}
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="search")


def _fold(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()


def _stem(t: str) -> str:
    """Hrubé ořezání koncovek: pračka/pračky/praček → prack, lednice/lednička → ledni."""
    t = _fold(t)
    return t if len(t) <= 4 else t[:max(4, len(t) - 2)]


# kmeny se stejným významem (po _stem): obchody říkají lednici „chladnička“, televizi „televizor/TV“…
SYNONYMS = [{"ledni", "chladni", "chladnick", "lednick", "kombinovana chladni"}, {"televi", "tv", "televiz"},
            {"vysava", "vysavac"}, {"mycka", "mycky", "mycc"}, {"susic", "susick"},
            {"notebo", "laptop"}, {"sluchat", "sluchatk"}]


def _alts(stem: str) -> set[str]:
    for g in SYNONYMS:
        if any(stem.startswith(x) or x.startswith(stem) for x in g):
            return g | {stem}
    return {stem}


def _slug_words(url: str) -> str:
    """Slova z URL (vestavne-spotrebice.cz má kategorii jen v adrese: …-sety_trouba_deska_mycka/)."""
    return re.sub(r"[-_/+,.]+", " ", urlparse(url).path)


def _matches(name: str, query: str) -> bool:
    n = _fold(name)
    return all(any(re.search(r"(?<![a-z0-9])" + re.escape(a), n) for a in _alts(_stem(t))) for t in query.split())


ACCESSORY_WORDS = ("pouzdro", "obal", "kryt", "fólie", "folie", "sklo", "ochrann", "screenshield", "řemín", "remin",
                   "držák", "drzak", "kabel", "nabíječ", "nabijec", "adaptér", "adapter", "lens protection",
                   "redukce", "stylus", "ochrana", "panzerglass", "bumper", "silikonov", "quickfit", "tvrzen", "magsafe", "popruh", "náramek pro", "nálepk", "flip", "case")


ACCESSORY_WORDS_EXTRA = ("stojan", "sklicko", "lcd displej", "displej +", "nahradni dil", "dotykova plocha", "dotykove sklo", "odvapn", "pohlcovac", "miska", "nahradni", "sacky", "filtr ", "filtry",
                         "nabijeci stanice", "dobijeci stanice", "sada prislusenstvi", "prislusenstvi", "baterie pro",
                         "hadice", "drzak", "odvapnovac", "cistic", "cistici", "batoh", "backpack", "live cards",
                         "darkova karta", "charging", "taska", "brasna", "stolek", "sul ", "sul do", "tablety", "kapsle",
                         "lestidlo", "detska kuchynka", "hracka", "mezikus", "spojovaci", "vodni filtr", "antena",
                         "shine & protect", "odstranovac", "cistic", "vune", "osvezovac", "skrin", "televizni stena",
                         "regal", "komoda", "bigjigs", "tidlo", "detsk", "pro deti")


def is_accessory(name: str, query: str = "") -> bool:
    n = name.lower()
    if any(w in n for w in ACCESSORY_WORDS):
        return True
    f = _fold(name)
    if any(w in f for w in ACCESSORY_WORDS_EXTRA):
        return True
    # „miska pod lednici“, „stojan na televize“, „baterie pro Dyson“: hledaný výraz až za předložkou
    for t in query.split():
        st = _stem(t)
        if re.search(r"\b(?:pro|do|pod|na|ke?|k)\s+(?:\S+\s+){0,2}" + re.escape(st), f):
            return True
    return False


def drop_accessories(hits: list[SearchHit], query: str) -> list[SearchHit]:
    """Když dotaz nehledá příslušenství, schováme obaly/fólie/řemínky – jinak by „nejlevnější“ byly kryty."""
    if is_accessory(query):
        return hits
    main = [h for h in hits if not is_accessory(h.name, query)]
    return main or hits


def live_search(query: str, shops=None, timeout: float = TIMEOUT_S, limit_per_shop: int = 12) -> LiveResult:
    from .registry import SHOPS
    key = " ".join(query.lower().split())
    now = time.monotonic()
    c = _cache.get(key)
    if c and now - c[0] < CACHE_TTL_S and shops is None:
        return LiveResult(c[1].hits, c[1].shops, cached=True)
    targets = [s for s in (shops or SHOPS.values()) if s.searcher and not getattr(s, 'retired', False)]
    futs = {_pool.submit(s.searcher, query): s for s in targets}
    done, _ = wait(futs, timeout=timeout)
    results, hits = [], []
    for f, s in futs.items():
        if f not in done:
            f.cancel()
            results.append(ShopResult(s.slug, "timeout", note="neodpověděl včas"))
            continue
        try:
            hs = drop_accessories([h for h in f.result() if _matches(h.name + (" " + _slug_words(h.url) if getattr(s, "extra", {}).get("match_slug") else ""), query)], query)
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


def find_cached_hit(url: str) -> SearchHit | None:
    """Najde výsledek živého hledání v cache podle URL (pro stránku /nahled – bez nového dotazu do obchodu)."""
    from ..util import normalize_url
    want = normalize_url(url)
    for _t, res in list(_cache.values()):
        for h in res.hits:
            if normalize_url(h.url) == want:
                return h
    return None
