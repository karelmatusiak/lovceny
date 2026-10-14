"""Šetrné stahování stránek produktů a čtení ceny z JSON-LD / meta tagů."""
import json
import re
import time
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .robots import Robots

log = logging.getLogger("lovceny.scrape")

BOT_INFO_URL = "https://lovceny.cz/pro-obchody"  # stránka pro e-shopy (popis bota, vyřazení)
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0 Safari/537.36 LovcenyBot/0.1 (+" + BOT_INFO_URL + ")")
ROBOTS_AGENT = "LovcenyBot"
MIN_DELAY_S = 6.0  # min. rozestup dvou požadavků na stejnou doménu

BLOCK_MARKERS = ("bobcmn", "cf-chl", "challenge-platform", "Please enable JS", "captcha", "Access Denied")


class Blocked(Exception):
    """kind: http (403/429/503) | captcha (JS challenge / captcha stránka) | robots (robots.txt nepovoluje)."""
    def __init__(self, msg: str, kind: str = "http", status: int | None = None):
        super().__init__(msg)
        self.kind, self.status = kind, status


@dataclass
class ScrapeResult:
    price: float
    original_price: float | None
    in_stock: bool | None
    name: str | None
    image: str | None
    ean: str | None
    category: str | None


class PoliteClient:
    def __init__(self):
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "cs-CZ,cs;q=0.9"},
                                   follow_redirects=True, timeout=20)
        self._robots: dict[str, Robots] = {}
        self._last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = Robots()
            try:
                r = self.client.get(base + "/robots.txt")
                if r.status_code < 400:  # robots chybí → vše povoleno (403 řeší detekce blokace)
                    rp = Robots(r.text)
            except httpx.HTTPError:
                pass
            self._robots[base] = rp
        return self._robots[base].can_fetch(ROBOTS_AGENT, url)

    def get(self, url: str) -> str:
        if not self.allowed(url):
            raise Blocked("robots.txt nepovoluje", kind="robots")
        host = urlparse(url).netloc
        wait = self._last.get(host, 0) + MIN_DELAY_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        r = self.client.get(url)
        self._last[host] = time.monotonic()
        if r.status_code in (403, 429, 503):
            body = r.text[:5000]
            if any(m.lower() in body.lower() for m in BLOCK_MARKERS):
                raise Blocked(f"HTTP {r.status_code} + ochrana proti botům", kind="captcha", status=r.status_code)
            raise Blocked(f"HTTP {r.status_code}", kind="http", status=r.status_code)
        r.raise_for_status()
        head = r.text[:3000]
        if len(r.text) < 20000 and any(m in head for m in BLOCK_MARKERS):
            raise Blocked("ochrana proti botům (JS challenge)", kind="captcha", status=r.status_code)
        self.last_status = r.status_code
        return r.text


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.]", "", str(v)).replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _iter_jsonld(html: str):
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                yield d
                if "@graph" in d:
                    stack.append(d["@graph"])


def parse_product(html: str) -> ScrapeResult | None:
    for d in _iter_jsonld(html):
        t = d.get("@type")
        if t != "Product" and not (isinstance(t, list) and "Product" in t):
            continue
        offers = d.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if offers.get("@type") == "AggregateOffer":
            price = _num(offers.get("lowPrice"))
        else:
            price = _num(offers.get("price"))
        original = None
        specs = offers.get("priceSpecification") or []
        if isinstance(specs, dict):
            specs = [specs]
        for sp in specs:
            pt = str(sp.get("priceType", ""))
            if "Strikethrough" in pt or "ListPrice" in pt:
                original = _num(sp.get("price"))
            elif price is None:
                price = _num(sp.get("price"))
        if price is None:
            continue
        avail = str(offers.get("availability", ""))
        in_stock = None
        if avail:
            in_stock = any(k in avail for k in ("InStock", "InStoreOnly", "LimitedAvailability", "OnlineOnly"))
        img = d.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        return ScrapeResult(price=price, original_price=original if original and original > price else None,
                            in_stock=in_stock, name=d.get("name"), image=img,
                            ean=str(d.get("gtin13") or d.get("gtin") or "") or None,
                            category=(offers.get("category") if isinstance(offers.get("category"), str) else None))
    # fallback: meta tagy
    m = re.search(r'<meta[^>]+(?:property|itemprop)="(?:product:price:amount|price)"[^>]+content="([^"]+)"', html)
    if m:
        name = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
        return ScrapeResult(_num(m.group(1)), None, None, name.group(1) if name else None, None, None, None)
    return None





def _md(html: str, prop: str) -> str | None:
    """První hodnota schema.org microdata vlastnosti (atribut content, jinak text elementu)."""
    for m in re.finditer(r'<[a-z][a-z0-9]*\b[^>]*\bitemprop=["\']' + prop + r'["\'][^>]*>', html, re.I):
        tag = m.group(0)
        c = re.search(r'\bcontent=["\']([^"\']*)["\']', tag, re.I)
        if c and c.group(1).strip():
            return c.group(1).strip()
        txt = re.match(r'([^<]*)', html[m.end():m.end() + 300])
        if txt and txt.group(1).strip():
            return txt.group(1).strip()
    return None


def parse_microdata(html: str) -> ScrapeResult | None:
    """schema.org/Product jako microdata (itemscope/itemprop) – fallback pro obchody bez JSON-LD."""
    if not re.search(r'itemtype=["\']https?://schema\.org/Product["\']', html, re.I):
        return None
    price = _num(_md(html, "price") or _md(html, "lowPrice"))
    if not price:
        return None
    name = _md(html, "name")
    avail = _md(html, "availability") or ""
    if not avail:
        a = re.search(r'itemprop=["\']availability["\'][^>]*href=["\']([^"\']+)', html, re.I)
        avail = a.group(1) if a else ""
    in_stock = any(k in avail for k in ("InStock", "InStoreOnly", "LimitedAvailability", "OnlineOnly")) if avail else None
    img = re.search(r'<img\b[^>]*itemprop=["\']image["\'][^>]*\bsrc=["\']([^"\']+)', html, re.I) or \
        re.search(r'itemprop=["\']image["\'][^>]*content=["\']([^"\']+)', html, re.I)
    gtin = _md(html, "gtin13") or _md(html, "gtin")
    import html as _h
    return ScrapeResult(price=price, original_price=None, in_stock=in_stock,
                        name=_h.unescape(name) if name else None, image=img.group(1) if img else None,
                        ean=gtin, category=None)


def parse_generic(html: str) -> ScrapeResult | None:
    """Obecný parser libovolného obchodu: JSON-LD → microdata → meta tagy."""
    for d in _iter_jsonld(html):
        t = d.get("@type")
        if t == "Product" or (isinstance(t, list) and "Product" in t):
            r = parse_product(html)
            if r:
                return r
            break
    return parse_microdata(html) or parse_product(html)
