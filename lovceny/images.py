"""Proxy a zmenšování obrázků produktů z obchodů → WebP v několika šířkách, s diskovou LRU cache.

Bezpečnost: stahujeme jen z hostitelů našich obchodů (domény z registry + jejich CDN), jen http(s),
max. MAX_BYTES, timeout, redirect jen opět na povolený host, jen obrázky (Pillow ověří formát),
ochrana proti „dekompresní bombě“ (MAX_PIXELS). Při chybě endpoint vrátí malý placeholder
(Cache-Control: private, no-store) – nikdy 302 na originál (Planeo/Fast JPEG bývá i >3 MB).
Paměť (Render free 512 MiB): najednou jen IMG_CONCURRENCY resize (výchozí 1), download max 8 MB
(JPEG dekód přes Image.draft), close(), semafor kolem miss cesty. Cache na disku IMG_CACHE_MAX_MB (výchozí 40).
"""
import hashlib
import io
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin, urlunparse, quote

import httpx

log = logging.getLogger("lovceny.images")

WIDTHS = (80, 160, 320, 640)
# Těsné limity: 1 velký Shopify JPEG (~1 MB) se v Pillow snadno nafoukne na desítky MB;
# 4 souběžné PNG 6k×4k lokálně = ~760 MB RSS → OOM na free 512 MiB.
# Download cap: shop CDN JPEGs (Planeo/Fast) often 2–4 MB. Decode stays small via Image.draft.
# Peak RSS stays low with IMG_CONCURRENCY=1; do not raise concurrency with this limit.
MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 8_000_000  # ~8 MP (např. 3500×2300)
# 1×1 transparent WebP – error response that cards can show or hide; never cache at CF.
PLACEHOLDER_WEBP = bytes.fromhex(
    "524946464000000057454250565038580a00000010000000000000000000"
    "414c504802000000000056503820180000003001009d012a010001000cc0ce25a400037000feec670000"
)
TIMEOUT = httpx.Timeout(6.0, connect=4.0)
MAX_REDIRECTS = 3
QUALITY = 78
FAIL_TTL_S = 600  # neúspěšné URL chvíli nezkoušíme znovu (šetrnost k obchodům)
# CDN obchodů, které nejsou poddoménou jejich hlavní domény (Planeo/4Home → fast.eu)
EXTRA_IMAGE_HOSTS = ("fast.eu", "sony-center.s3.cdn-upgates.com", "cdn.shopify.com",
                      "res.cloudinary.com", "cdn.myshoptet.com", "allegroimg.com")
USER_AGENT = "LovcenyBot/0.1 (+https://lovceny.cz/pro-obchody) image-resizer"

_fail: dict[str, float] = {}
_robots: dict[str, tuple[float, object]] = {}
ROBOTS_TTL_S = 24 * 3600
_lock = threading.Lock()
_last_evict = 0.0


def _concurrency() -> int:
    try:
        return max(1, min(4, int(os.environ.get("IMG_CONCURRENCY") or "1")))
    except ValueError:
        return 1


# Semafor kolem download+resize (cache hit mimo). Lazy kvůli testům měnícím env.
_sem: threading.BoundedSemaphore | None = None
_sem_n: int | None = None


def _resize_sem() -> threading.BoundedSemaphore:
    global _sem, _sem_n
    n = _concurrency()
    if _sem is None or _sem_n != n:
        _sem = threading.BoundedSemaphore(n)
        _sem_n = n
    return _sem


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
        return int(float(os.environ.get("IMG_CACHE_MAX_MB") or 40) * 1024 * 1024)
    except ValueError:
        return 40 * 1024 * 1024


def _key(url: str, w: int) -> str:
    return hashlib.sha256(f"{w}|{url}".encode()).hexdigest()


# Hostitelé, od kterých free resizer wsrv.nl/images.weserv.nl nedostane obrázek (404/blokace).
# Planeo/Fast: lokální /img (po zapnutí Cloudflare orange-cloud se cacheuje na edge).
# allegroimg.com: wsrv vrací 404 i timeout; velikost už má jejich CDN (/s256, /s512),
# takže je neposíláme ani na wsrv, ani na /img (ten by stáhl /original/ ~230 KB).
WSRV_BLOCKED_HOSTS = ("data.planeo.cz", "planeo.cz", "fast.eu", "mc-static.fast.eu", "allegroimg.com")
WSRV_BASE = "https://wsrv.nl/"
# Allegro CDN: /original/ ~230 KB, /s512/ ~47 KB, /s256/ ~29 KB, /s128/ ~7 KB.
_ALLEGRO_SIZE = re.compile(r"s(\d+)", re.I)


def wsrv_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in WSRV_BLOCKED_HOSTS)


def _allegro_parsed(url: str):
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host != "allegroimg.com" and not host.endswith(".allegroimg.com"):
        return None
    return p


def is_allegro_cdn(url: str | None) -> bool:
    return bool(url) and _allegro_parsed(url) is not None


def allegro_size_for_width(w: int) -> str:
    """Náhled karty (slot ≤320 px, tj. CSS ~120 px @2×) → s256. Širší slot → s512. Nikdy /original/."""
    return "s256" if w <= 320 else "s512"


def allegro_sized_url(url: str | None, size: str) -> str | None:
    """Přepíše první segment cesty (/original/ nebo /sNNN/) na `size`. Jinak None."""
    if not url:
        return None
    p = _allegro_parsed(url)
    if p is None:
        return None
    parts = p.path.split("/")
    # path začíná „/“, size je parts[1]
    if len(parts) < 3 or not parts[1]:
        return None
    token = parts[1]
    low = token.lower()
    if low != "original" and not _ALLEGRO_SIZE.fullmatch(token):
        return None
    if low == size.lower():
        return url
    parts[1] = size
    return urlunparse(p._replace(path="/".join(parts)))


def normalize_allegro_image(url: str | None) -> str | None:
    """URL k uložení: /original/ a /s1024+/ → /s512/. Menší rendice (s128–s512) necháme."""
    if not url or not is_allegro_cdn(url):
        return url
    p = _allegro_parsed(url)
    token = (p.path.split("/")[1:] or [""])[0].lower() if p is not None else ""
    if token == "original":
        return allegro_sized_url(url, "s512") or url
    m = _ALLEGRO_SIZE.fullmatch(token)
    if m and int(m.group(1)) > 512:
        return allegro_sized_url(url, "s512") or url
    return url


def local_proxy_url(url: str | None, w: int) -> str:
    """Naše cesta končí na .webp → Cloudflare free ji bere jako cacheovatelný asset."""
    return f"/img/{w}.webp?u={quote(url or '', safe='')}"


def wsrv_url(url: str, w: int) -> str:
    """Free image CDN: resize + WebP, edge cache u wsrv (nebudí Render)."""
    return f"{WSRV_BASE}?url={quote(url, safe='')}&w={w}&output=webp&q={QUALITY}&we"


def proxy_url(url: str | None, w: int) -> str:
    """Primární URL náhledu.

    Allegro: přímá CDN rendice (s256/s512). wsrv na allegroimg 404uje a /img by
    na free tieru stahoval /original/. Planeo/Fast a další blokované hosty: /img.
    Ostatní povolené hosty: wsrv.nl.
    """
    if not url:
        return local_proxy_url(url, w)
    if is_allegro_cdn(url):
        return allegro_sized_url(url, allegro_size_for_width(w)) or url
    if wsrv_blocked(url):
        return local_proxy_url(url, w)
    return wsrv_url(url, w)


def _srcset_value(url: str, ws: list[int]) -> str:
    """srcset. Stejná URL (Allegro s256 pro víc slotů) se zapíše jednou, s nejširším deskriptorem."""
    parts: list[tuple[str, int]] = []
    index: dict[str, int] = {}
    for w in ws:
        u = proxy_url(url, w)
        i = index.get(u)
        if i is None:
            index[u] = len(parts)
            parts.append((u, w))
        else:
            parts[i] = (u, w)
    return ", ".join(f"{u} {w}w" for u, w in parts)


def srcset(url: str | None, px: int) -> dict | None:
    """Atributy <img> pro zobrazenou velikost px (CSS px): src (≈2×) + srcset šířek do ≥ 3× px.
    Nepovolený host → None (šablona použije originál).
    fallback = lokální /img když selže wsrv; prázdné když už je primární /img
    nebo jde o Allegro CDN (onerror nesmí stáhnout /original/ přes /img).
    Nikdy originál Planeo/Fast – multi‑MB JPEG."""
    if not url or not is_allowed(url):
        return None
    big = next((w for w in WIDTHS if w >= px * 3), WIDTHS[-1])  # do DPR 3
    ws = [w for w in WIDTHS if w <= big]
    base = next((w for w in WIDTHS if w >= px * 2), WIDTHS[-1])
    src = proxy_url(url, base)
    local = local_proxy_url(url, base)
    if is_allegro_cdn(url):
        fallback = ""
        orig = normalize_allegro_image(url) or url
    else:
        fallback = local if src != local else ""
        orig = url
    return {
        "src": src,
        "srcset": _srcset_value(url, ws),
        "sizes": f"{px}px",
        "fallback": fallback,
        "orig": orig,
    }


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
    c = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8", "Accept-Language": "cs,en;q=0.5"})
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
    im = None
    try:
        im = Image.open(io.BytesIO(data))
        # JPEG: dekóduj hned ve zmenšeném měřítku (méně RAM než full decode + resize)
        # size z hlavičky (bez plného dekódování) – ochrana proti dekompresní bombě
        if im.width * im.height > MAX_PIXELS:
            raise ImageError("příliš mnoho pixelů")
        fmt = (im.format or "").upper()
        if fmt == "JPEG":
            # draft → load dekóduje hned v cílovém měřítku (Planeo/Fast multi‑MB JPEG, nízká RAM)
            try:
                im.draft("RGB", (w, w * 4))
            except Exception:
                pass
            try:
                im.load()
            except Exception as e:
                raise ImageError(f"neplatný obrázek: {type(e).__name__}") from e
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if ("transparency" in im.info or im.mode in ("LA", "PA", "P")) else "RGB")
        if im.width > w:
            nh = max(1, round(im.height * w / im.width))
            resized = im.resize((w, nh), Image.LANCZOS)
            try:
                im.close()
            except Exception:
                pass
            im = resized
        out = io.BytesIO()
        # method=2: méně peaku než 4, stále dobrý WebP
        im.save(out, "WEBP", quality=QUALITY, method=2)
        return out.getvalue()
    except ImageError:
        raise
    except Exception as e:  # Pillow: nepodporovaný/poškozený soubor, bomba…
        if type(e).__name__ in ("DecompressionBombError", "DecompressionBombWarning"):
            raise ImageError("příliš mnoho pixelů") from e
        raise ImageError(f"neplatný obrázek: {type(e).__name__}") from e
    finally:
        if im is not None:
            try:
                im.close()
            except Exception:
                pass


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
    sem = _resize_sem()
    if not sem.acquire(timeout=12.0):
        raise ImageError("příliš vytíženo")
    try:
        # double-check cache: jiný thread mohl mezitím zapsat
        try:
            data = f.read_bytes()
            os.utime(f)
            return data
        except OSError:
            pass
        try:
            # Allegro /original/ nenecháme stáhnout ani když někdo trefí /img přímo.
            fetch = url
            if is_allegro_cdn(url):
                fetch = allegro_sized_url(url, allegro_size_for_width(w)) or url
            data = resize_webp(_download(fetch, client), w)
        except ImageError:
            with _lock:
                _fail[url] = time.monotonic()
                if len(_fail) > 2000:
                    # drop oldest half by timestamp
                    old = sorted(_fail, key=_fail.get)[:1000]
                    for k in old:
                        _fail.pop(k, None)
            raise
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, f)
        except OSError as e:
            log.warning("cache obrázků: zápis selhal: %s", e)
        evict()
        return data
    finally:
        sem.release()


def clear_failures():
    with _lock:
        _fail.clear()
        _robots.clear()
