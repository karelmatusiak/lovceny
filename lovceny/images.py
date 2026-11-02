"""Proxy a zmenšování obrázků produktů z obchodů → WebP v několika šířkách, s diskovou LRU cache.

Bezpečnost: stahujeme jen z hostitelů našich obchodů (domény z registry + jejich CDN), jen http(s),
max. MAX_BYTES, timeout, redirect jen opět na povolený host, jen obrázky (Pillow ověří formát),
ochrana proti „dekompresní bombě“ (MAX_PIXELS). Při chybě šablona/endpoint spadne na originální URL.
Cache: IMG_CACHE_DIR (výchozí /tmp/lovceny-img – na Renderu je disk dočasný, to nevadí),
IMG_CACHE_MAX_MB (výchozí 150). Nejdéle nepoužité soubory mažeme (LRU podle mtime, hit = touch).
"""
import hashlib
import io
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote

import httpx

log = logging.getLogger("lovceny.images")

WIDTHS = (80, 160, 320, 640)
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
TIMEOUT = httpx.Timeout(6.0, connect=4.0)
MAX_REDIRECTS = 3
QUALITY = 78
FAIL_TTL_S = 600  # neúspěšné URL chvíli nezkoušíme znovu (šetrnost k obchodům)
# CDN obchodů, které nejsou poddoménou jejich hlavní domény (Planeo/4Home → fast.eu)
EXTRA_IMAGE_HOSTS = ("fast.eu", "sony-center.s3.cdn-upgates.com", "cdn.shopify.com")
USER_AGENT = "LovcenyBot/0.1 (+https://lovceny.cz/pro-obchody) image-resizer"

_fail: dict[str, float] = {}
_robots: dict[str, tuple[float, object]] = {}
ROBOTS_TTL_S = 24 * 3600
_lock = threading.Lock()
_last_evict = 0.0


def allowed_hosts() -> tuple[str, ...]:
    from .shops import SHOPS
    hosts = {d.lower() for s in SHOPS.values() for d in s.domains}
    return tuple(sorted(hosts | set(EXTRA_IMAGE_HOSTS)))


def is_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not host or p.username or p.password or len(url) > 2000:
        return False
    if p.port not in (None, 80, 443):
        return False
    return any(host == h or host.endswith("." + h) for h in allowed_hosts())


def cache_dir() -> Path:
    d = Path(os.environ.get("IMG_CACHE_DIR") or "/tmp/lovceny-img")
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_cap_bytes() -> int:
    try:
        return int(float(os.environ.get("IMG_CACHE_MAX_MB") or 150) * 1024 * 1024)
    except ValueError:
        return 150 * 1024 * 1024


def _key(url: str, w: int) -> str:
    return hashlib.sha256(f"{w}|{url}".encode()).hexdigest()


def proxy_url(url: str | None, w: int) -> str:
    return f"/img/{w}?u={quote(url or '', safe='')}"


def srcset(url: str | None, px: int) -> dict | None:
    """Atributy <img> pro zobrazenou velikost px (CSS px): src (≈2×) + srcset šířek do ≥ 3× px.
    Nepovolený host → None (šablona použije originál)."""
    if not url or not is_allowed(url):
        return None
    big = next((w for w in WIDTHS if w >= px * 3), WIDTHS[-1])  # do DPR 3
    ws = [w for w in WIDTHS if w <= big]
    base = next((w for w in WIDTHS if w >= px * 2), WIDTHS[-1])
    return {"src": proxy_url(url, base), "srcset": ", ".join(f"{proxy_url(url, w)} {w}w" for w in ws),
            "sizes": f"{px}px"}


class ImageError(Exception):
    pass


def robots_ok(url: str, client: httpx.Client) -> bool:
    """robots.txt hostitele obrázku (cache 24 h). Nedostupný robots.txt = povoleno (jako u stahovače)."""
    from .shops.robots import Robots
    from .shops.scrape import ROBOTS_AGENT
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    hit = _robots.get(base)
    if not hit or time.monotonic() - hit[0] > ROBOTS_TTL_S:
        rp = Robots()
        try:
            r = client.get(base + "/robots.txt", follow_redirects=True)
            if r.status_code < 400:
                rp = Robots(r.text)
        except httpx.HTTPError:
            pass
        hit = _robots[base] = (time.monotonic(), rp)
    return hit[1].can_fetch(ROBOTS_AGENT, url)


def _download(url: str, client: httpx.Client | None = None) -> bytes:
    own = client is None
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not is_allowed(url):
                raise ImageError("host není povolený")
            if not robots_ok(url, c):
                raise ImageError("robots.txt nepovoluje")
            with c.stream("GET", url, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    raise ImageError(f"HTTP {r.status_code}")
                ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ct and not (ct.startswith("image/") or ct == "application/octet-stream"):
                    raise ImageError(f"není obrázek ({ct})")
                if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                    raise ImageError("příliš velký")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise ImageError("příliš velký")
                return bytes(buf)
        raise ImageError("příliš mnoho přesměrování")
    except httpx.HTTPError as e:
        raise ImageError(type(e).__name__) from e
    finally:
        if own:
            c.close()


def resize_webp(data: bytes, w: int) -> bytes:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            raise ImageError("příliš mnoho pixelů")
        im.draft("RGB", (w * 2, w * 2))  # JPEG: rychlé dekódování ve zmenšeném měřítku
        im = ImageOps.exif_transpose(im)
        im.load()
    except ImageError:
        raise
    except Exception as e:  # Pillow: nepodporovaný/poškozený soubor, bomba…
        raise ImageError(f"neplatný obrázek: {type(e).__name__}") from e
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if ("transparency" in im.info or im.mode in ("LA", "PA", "P")) else "RGB")
    if im.width > w:
        im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=QUALITY, method=4)
    return out.getvalue()


def evict(force: bool = False) -> int:
    """Smaže nejdéle nepoužité soubory nad limit (cíl 90 % limitu). Vrací počet smazaných."""
    global _last_evict
    if not force and time.monotonic() - _last_evict < 30:
        return 0
    _last_evict = time.monotonic()
    files = []
    for f in cache_dir().glob("*.webp"):
        try:
            st = f.stat()
            files.append((st.st_mtime, st.st_size, f))
        except OSError:
            pass
    total, cap, n = sum(s for _, s, _ in files), cache_cap_bytes(), 0
    if total <= cap:
        return 0
    for _, size, f in sorted(files):
        if total <= cap * 0.9:
            break
        try:
            f.unlink()
            total -= size
            n += 1
        except OSError:
            pass
    return n


def get(url: str, w: int, client: httpx.Client | None = None) -> bytes:
    """WebP bajty pro (url, w) – z cache, jinak stáhnout + zmenšit + uložit. Vyhazuje ImageError."""
    if w not in WIDTHS:
        raise ImageError("nepodporovaná šířka")
    if not is_allowed(url):
        raise ImageError("host není povolený")
    f = cache_dir() / (_key(url, w) + ".webp")
    try:
        data = f.read_bytes()
        os.utime(f)  # LRU: poslední použití
        return data
    except OSError:
        pass
    with _lock:
        t = _fail.get(url)
    if t and time.monotonic() - t < FAIL_TTL_S:
        raise ImageError("nedávno selhalo")
    try:
        data = resize_webp(_download(url, client), w)
    except ImageError:
        with _lock:
            _fail[url] = time.monotonic()
            if len(_fail) > 5000:
                _fail.clear()
        raise
    tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, f)
    except OSError as e:
        log.warning("cache obrázků: zápis selhal: %s", e)
    evict()
    return data


def clear_failures():
    with _lock:
        _fail.clear()
        _robots.clear()
