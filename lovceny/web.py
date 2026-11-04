"""Výkon a SEO pomocníci: verzované statické soubory, cache hlavičky, komprese, kanonické URL."""
import hashlib
import os
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
# Kanonická produkční doména – SEO (canonical, og:url, JSON-LD, sitemap) sem vždy.
CANONICAL_PUBLIC_URL = "https://lovceny.cz"
DEFAULT_PUBLIC_URL = CANONICAL_PUBLIC_URL


def public_url() -> str:
    """Absolutní veřejná URL. Legacy onrender host přepisujeme na lovceny.cz."""
    raw = (os.environ.get("PUBLIC_URL") or DEFAULT_PUBLIC_URL).rstrip("/")
    if not raw or "onrender.com" in raw.lower():
        return CANONICAL_PUBLIC_URL
    return raw


def canonical(path: str, query: str = "") -> str:
    return public_url() + path + (f"?{query}" if query else "")


@lru_cache(maxsize=256)
def _hash(rel: str, mtime: float) -> str:
    return hashlib.sha256((STATIC_DIR / rel).read_bytes()).hexdigest()[:10]


def static_url(rel: str) -> str:
    """/static/<rel>?v=<hash obsahu> – změna souboru = nové URL, takže smíme cachovat navždy."""
    rel = rel.lstrip("/")
    f = STATIC_DIR / rel
    try:
        return f"/static/{rel}?v={_hash(rel, f.stat().st_mtime)}"
    except OSError:
        return f"/static/{rel}"


class CacheHeadersMiddleware:
    """Statické soubory s ?v= → 1 rok immutable; bez verze 1 den. HTML stránky: krátká cache, bez uložení u proxy
    pro osobní odpovědi. Doplňuje i základní bezpečnostní hlavičky."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        qs = scope.get("query_string", b"")

        async def _send(msg):
            if msg["type"] == "http.response.start":
                h = [(k, v) for k, v in msg.get("headers", []) if k.lower() != b"cache-control"] \
                    if path.startswith("/static/") else list(msg.get("headers", []))
                names = {k.lower() for k, _ in h}
                if path.startswith("/static/") and msg.get("status", 200) == 200:
                    cc = b"public, max-age=31536000, immutable" if b"v=" in qs else b"public, max-age=86400"
                    h.append((b"cache-control", cc))
                elif path == "/sw.js":
                    h.append((b"cache-control", b"no-cache"))
                elif b"cache-control" not in names:
                    h.append((b"cache-control", b"public, max-age=0, must-revalidate"
                              if path in ("/sitemap.xml", "/robots.txt") else b"no-cache"))
                h += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin")]
                msg = {**msg, "headers": h}
            await send(msg)
        return await self.app(scope, receive, _send)


def add_compression(app):
    """Brotli (pokud ho klient umí), jinak gzip. Malé odpovědi (<500 B) nekomprimujeme."""
    try:
        from brotli_asgi import BrotliMiddleware
        app.add_middleware(BrotliMiddleware, minimum_size=500, quality=5, gzip_fallback=True)
    except ImportError:  # pragma: no cover
        from starlette.middleware.gzip import GZipMiddleware
        app.add_middleware(GZipMiddleware, minimum_size=500)


class AnalyticsMiddleware:
    """Vlastní anonymní analytika (lovceny/analytics.py). Zápis až po odeslání odpovědi, ve vlákně,
    chyba nikdy nerozbije stránku. Zaznamenává jen GET HTML stránky s 200 (a dotazy živého hledání)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from . import analytics
        from starlette.datastructures import Headers
        headers = Headers(scope=scope)
        path = scope.get("path", "")
        live = path == "/hledat/zive"
        if not (live or analytics.should_track(path, scope.get("method", ""), headers)) or \
                (live and (analytics.opted_out(headers) or analytics.is_bot(headers.get("user-agent", "")))):
            return await self.app(scope, receive, send)
        info = {"status": 0, "html": False}

        async def _send(msg):
            if msg["type"] == "http.response.start":
                info["status"] = msg.get("status", 0)
                ct = dict((k.lower(), v) for k, v in msg.get("headers", [])).get(b"content-type", b"")
                info["html"] = ct.startswith(b"text/html")
            await send(msg)
        await self.app(scope, receive, _send)
        an = (scope.get("state") or {}).get("an") or {}
        if info["status"] not in (200, 422) or not info["html"] or (live and not an):
            return
        if info["status"] == 422 and an.get("kind") != "search":
            return
        fwd = headers.get("x-forwarded-for", "")
        client = scope.get("client") or ("", 0)
        ip = fwd.split(",")[0].strip() if fwd else client[0]
        kw = {"path": an.get("path") or path, "kind": an.get("kind", "view"), "ip": ip or "",
              "ua": headers.get("user-agent", ""), "referer": headers.get("referer", ""), "host": headers.get("host", ""),
              "query": an.get("query"), "results": an.get("results"), "product_id": an.get("product_id")}

        def _write():
            from .db import SessionLocal
            try:
                with SessionLocal() as db:
                    analytics.record(db, **kw)
            except Exception as e:  # noqa: BLE001
                import logging
                logging.getLogger("lovceny.analytics").warning("analytika: %s", type(e).__name__)
        import anyio
        await anyio.to_thread.run_sync(_write)
