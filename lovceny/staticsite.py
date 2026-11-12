"""Statický export veřejných stránek.

Volá stejné funkce jako FastAPI (šablony, verdikt, kategorie, graf). Výsledek
je strom HTML pro Cloudflare Pages. Návštěvník se k databázi nedostane.

Spuštění: python build_static.py --out dist
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from html import escape as html_escape
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.requests import Request

from . import features
from .availability import last_known_offer
from .categories import BY_SLUG, CATEGORIES, is_accessory_text
from .db import SessionLocal
from .names import guess_brand
from .shops import get_shop
from .shops.feed import strip_tracking
from .web import public_url

log = logging.getLogger("lovceny.staticsite")

# Stejné limity jako Cloudflare Pages _redirects (statické / dynamické).
REDIRECT_STATIC_LIMIT = 2000
REDIRECT_DYNAMIC_LIMIT = 100

_STRANA_TMPL = r"((?:{origin})?/kategorie/[a-z0-9-]+(?:/(?:nejlevnejsi|ve-sleve))?)\?strana=(\d+)"
_IMG_PROXY = re.compile(r"/img/\d+\.webp\?u=([^\"'\s>]+)")

LOST_WITHOUT_SERVER = [
    "Přidání produktu z odkazu (/hledat s URL, /sledovat, /nahled) a živé hledání v obchodech (/hledat/zive).",
    "Ověření libovolné stránky obchodu (/over-slevu stáhne jen to, co už je v katalogu; neznámý odkaz se nečte).",
    "Hlídání ceny e-mailem i v prohlížeči, včetně potvrzení a odhlášení tokenem (/hlidani, /pes).",
    "Kontaktní formulář (zpráva se neukládá; zůstává mailto:info@lovceny.cz).",
    "Hlídání rozpočtu, týdenní přehled jako odběr a Black Friday push (stránky, které jsou zapnuté, se vykreslí, odběr ne).",
    "Rozšíření prohlížeče a /api/v1/overit.",
    "Admin (/admin, /admin/trideni) a analytika. /stav je snímek z okamžiku generování.",
    "Zmenšování fotek přes /img (prohlížeč bere wsrv, Allegro CDN, nebo původní adresu obchodu).",
    "Špatný slug produktu (/produkt/ID-jiny-slug) vrací 301 jen když přesměrování řeší Pages Function (nad limitem _redirects). Jinak platí kanonická adresa a /produkt/ID.",
]


def request_for(path: str, query: str = "") -> Request:
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path if path.startswith("/") else "/" + path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [(b"host", b"lovceny.cz"), (b"accept", b"text/html")],
        "client": ("127.0.0.1", 0),
        "server": ("lovceny.cz", 443),
        "root_path": "",
    }
    return Request(scope)


def output_path(root: Path, url_path: str) -> Path:
    """Soubor, který Cloudflare Pages servíruje na tuhle adresu stavem 200.

    `produkt/12-slug/index.html` Pages přesměruje z `/produkt/12-slug` na
    `/produkt/12-slug/` (308). `produkt/12-slug.html` na adrese bez lomítka
    vrací 200. Kanonické URL, sitemap a odkazy lomítko na konci nemají,
    proto stránky píšeme jako `.html`. `/` zůstane `index.html`. Soubor s
    tečkou v názvu (sitemap.xml) zůstane soubor.
    """
    path = url_path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    if path in ("", "/"):
        return root / "index.html"
    name = path.rsplit("/", 1)[-1]
    rel = path.lstrip("/")
    if "." in name:
        return root / rel
    return root / f"{rel}.html"


def rewrite_img_proxy(html: str) -> str:
    """/img/{šířka}.webp?u=… na statickém hostu není. Původní adresa fotky zůstane v src."""
    def repl(match: re.Match) -> str:
        return html_escape(unquote(match.group(1)), quote=True)
    return _IMG_PROXY.sub(repl, html)


def rewrite_pagination(html: str) -> str:
    """?strana=N není na Pages jiný soubor. Stránka dostane cestu /strana/N."""
    pattern = re.compile(_STRANA_TMPL.format(origin=re.escape(public_url())))
    return pattern.sub(lambda m: f"{m.group(1)}/strana/{m.group(2)}", html)


def norm_offer_url(url: str) -> str:
    """Klíč do url.json. Stejná pravidla má katalog.js (host bez www, bez utm)."""
    cleaned = strip_tracking(url or "")
    parsed = urlparse(cleaned)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def product_path(product) -> str:
    from .names import canonical_slug
    slug = canonical_slug(getattr(product, "name", "") or "", getattr(product, "slug", "") or "")
    if slug:
        return f"/produkt/{product.id}-{slug}"
    return f"/produkt/{product.id}"


def security_headers() -> str:
    """Hlavičky pro Cloudflare Pages. Skripty jsou vlastní a vložené (menu, stránkování, onerror).

    Obrázky jdou z wsrv.nl a z CDN obchodů, odkazy do obchodů jsou běžná navigace
    (CSP je neblokuje). Affiliate `rel` se nemění.
    """
    csp = "; ".join([
        "default-src 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https://wsrv.nl https://images.weserv.nl https:",
        "font-src 'self'",
        "connect-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "upgrade-insecure-requests",
    ])
    return "\n".join([
        "/*",
        "  Strict-Transport-Security: max-age=31536000; includeSubDomains",
        "  X-Content-Type-Options: nosniff",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
        f"  Content-Security-Policy: {csp}",
        "",
    ])


def _redirect_target(db, product) -> str:
    from .main import _merged_target
    target = _merged_target(db, product)
    if target is not None and target.catalog_hidden is not True:
        return product_path(target)
    cat = BY_SLUG.get(product.category_slug or "")
    return f"/kategorie/{cat.slug}" if cat else "/"


class _Redirects:
    def __init__(self) -> None:
        self.static: list[tuple[str, str, int]] = []
        self.dynamic: list[tuple[str, str, int]] = []
        self.gone: dict[str, str] = {}
        self.live: dict[str, str] = {}

    def add(self, source: str, dest: str, code: int = 301, *, dynamic: bool = False) -> None:
        row = (source, dest, code)
        bucket = self.dynamic if dynamic or _is_dynamic(source) else self.static
        if row not in bucket:
            bucket.append(row)

    def add_retired(self, product, dest: str) -> None:
        self.gone[str(product.id)] = dest
        slug = (product.slug or "").strip("-")
        if slug:
            self.gone[f"{product.id}-{slug}"] = dest

    def add_former_slug(self, product_id: int, old_slug: str, dest: str) -> None:
        """Starý feedový slug → nová kanonická adresa. Holé /produkt/ID nechává live."""
        self.gone[f"{product_id}-{old_slug}"] = dest

    def add_live_id(self, product) -> None:
        path = product_path(product)
        if path != f"/produkt/{product.id}":
            self.live[str(product.id)] = path


def _is_dynamic(source: str) -> bool:
    return ":" in source or "*" in source


def _lines(rows: list[tuple[str, str, int]]) -> list[str]:
    return [f"{src} {dest} {code}" for src, dest, code in rows]


def write_redirects(root: Path, book: _Redirects, *, static_limit: int) -> str:
    """Soubor _redirects, dokud se vejde do limitu Pages. Nad limit produktové 301 řeší Function."""
    fixed_static = [row for row in book.static if not row[0].startswith("/produkt/")]
    product_static: list[tuple[str, str, int]] = []
    for key, dest in sorted(book.gone.items(), key=lambda item: item[0]):
        product_static.append((f"/produkt/{key}", dest, 301))
    for pid, dest in sorted(book.live.items(), key=lambda item: int(item[0])):
        product_static.append((f"/produkt/{pid}", dest, 301))
    # Statické řádky, které přidal add() a začínají /produkt/, už jsou v book.static.
    # Produktové cíle držíme v gone/live, ať je lze přesunout do funkce.
    static_rows = fixed_static
    mode = "file"
    if len(static_rows) + len(product_static) <= static_limit and len(book.dynamic) <= REDIRECT_DYNAMIC_LIMIT:
        static_rows = static_rows + product_static
    else:
        mode = "function"
        _write_product_function(root, book)
    if len(book.dynamic) > REDIRECT_DYNAMIC_LIMIT:
        raise RuntimeError(f"Dynamických přesměrování je {len(book.dynamic)}, limit Pages je {REDIRECT_DYNAMIC_LIMIT}")
    text = "\n".join(_lines(static_rows) + _lines(book.dynamic)) + "\n"
    (root / "_redirects").write_text(text, encoding="utf-8")
    return mode


def _write_product_function(root: Path, book: _Redirects) -> None:
    """Jen /produkt/*. Kanonická stránka se dál servíruje jako soubor (přes ASSETS)."""
    fn = root / "functions" / "produkt" / "[[path]].js"
    fn.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "gone": book.gone,
        "live": book.live,
    }
    fn.write_text(
        "/* Vygenerováno build_static.py. 301 vyřazených a holých /produkt/ID, když _redirects nestačí. */\n"
        "export async function onRequest(context) {\n"
        f"  const map = {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))};\n"
        "  const url = new URL(context.request.url);\n"
        "  let path = url.pathname.replace(/\\/+$/, \"\") || \"/\";\n"
        "  const m = path.match(/^\\/produkt\\/(\\d+)(?:-([a-z0-9-]*))?$/);\n"
        "  if (!m) return context.env.ASSETS.fetch(context.request);\n"
        "  const id = m[1];\n"
        "  const key = m[2] ? id + \"-\" + m[2] : id;\n"
        "  const target = map.gone[key] || map.gone[id];\n"
        "  if (target && target.startsWith(\"/\") && !target.startsWith(\"//\")) {\n"
        "    return Response.redirect(new URL(target, url.origin), 301);\n"
        "  }\n"
        "  const asset = await fetchAsset(context, path);\n"
        "  if (asset.status !== 404) return asset;\n"
        "  const live = map.live[id];\n"
        "  if (live && live.startsWith(\"/\") && !live.startsWith(\"//\") && live !== path) {\n"
        "    return Response.redirect(new URL(live, url.origin), 301);\n"
        "  }\n"
        "  return asset;\n"
        "}\n"
        "async function fetchAsset(context, pathname) {\n"
        "  const url = new URL(context.request.url);\n"
        "  url.search = \"\";\n"
        "  const candidates = [pathname];\n"
        "  if (!pathname.endsWith(\"/\")) {\n"
        "    candidates.push(pathname + \".html\");\n"
        "    candidates.push(pathname + \"/\");\n"
        "  }\n"
        "  let last = null;\n"
        "  for (const candidate of candidates) {\n"
        "    url.pathname = candidate;\n"
        "    last = await context.env.ASSETS.fetch(new Request(url, context.request));\n"
        "    if (last.status !== 404) return last;\n"
        "  }\n"
        "  return last;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "_routes.json").write_text(
        json.dumps({"version": 1, "include": ["/produkt/*"], "exclude": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def _body(response) -> str:
    raw = response.body or b""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8")


def _status(response) -> int:
    return int(response.status_code)


def _location(response) -> str:
    return response.headers.get("location") or ""


def build(out: str | Path, *, redirect_static_limit: int = REDIRECT_STATIC_LIMIT) -> dict:
    """Vygeneruje web do `out`. Vrací počty pro report. Databázi jen čte."""
    from . import catcache
    from .main import (
        about, black_friday, budget_page, categories_index, category, compare,
        honesty_page, price_landing, series_landing,
        compare_legacy, cookies, deals_page, digest_archive, extension_page,
        for_shops_page, home, kontakt_get, manifest, offline, privacy,
        product, ranking_page, robots_txt, service_worker, shops,
        status_page, terms_page, tpl, verify_page, when_to_buy,
        clear_display_names_cache,
    )
    from .models import Offer, Product, WeeklyDigest
    from .seo import sitemap_xml

    started = time.perf_counter()
    root = Path(out)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(Path(__file__).parent / "static", root / "static")
    fav = Path(__file__).parent / "static" / "brand" / "favicon.ico"
    if fav.exists():
        shutil.copyfile(fav, root / "favicon.ico")

    catcache.clear()
    clear_display_names_cache()
    tpl.env.globals["STATIC_SITE"] = True
    db = SessionLocal()
    db.expire_on_commit = False
    _read_only(db)
    pages = 0
    try:
        log.info("Načítám katalog")
        products = list(db.scalars(
            select(Product).options(
                selectinload(Product.offers).selectinload(Offer.points),
            ).order_by(Product.id)
        ).all())
        book = _Redirects()
        _fixed_redirects(book, db, request_for)

        def write_html(url_path: str, response) -> None:
            nonlocal pages
            if _status(response) in (301, 302, 303, 307, 308):
                book.add(url_path.split("?")[0], _location(response), _status(response))
                return
            if _status(response) != 200:
                raise RuntimeError(f"{url_path} vrátilo {_status(response)}")
            text = rewrite_pagination(rewrite_img_proxy(_body(response)))
            dest = output_path(root, url_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            pages += 1

        def call(url_path: str, fn, *args, **kwargs):
            try:
                response = fn(*args, **kwargs)
            except HTTPException as exc:
                if exc.status_code == 404:
                    return None
                raise
            write_html(url_path, response)
            return response

        call("/", home, request_for("/"), db=db)
        call("/slevy", deals_page, request_for("/slevy"), db=db)
        call("/kategorie", categories_index, request_for("/kategorie"), db=db)
        call("/obchody", shops, request_for("/obchody"))
        call("/o-nas", about, request_for("/o-nas"))
        call("/ochrana-osobnich-udaju", privacy, request_for("/ochrana-osobnich-udaju"))
        call("/cookies", cookies, request_for("/cookies"))
        call("/jak-radime", ranking_page, request_for("/jak-radime"))
        call("/pro-obchody", for_shops_page, request_for("/pro-obchody"))
        call("/podminky", terms_page, request_for("/podminky"))
        call("/kdy-nakupovat", when_to_buy, request_for("/kdy-nakupovat"), db=db)
        call("/over-slevu", verify_page, request_for("/over-slevu"), url="", db=db)
        call("/stav", status_page, request_for("/stav"), db=db)
        call("/black-friday", black_friday, request_for("/black-friday"), db=db)
        call("/black-friday/skutecna-sleva", honesty_page, request_for("/black-friday/skutecna-sleva"), db=db)
        call("/porovnat", compare, request_for("/porovnat"), p="", db=db)
        call("/offline", offline, request_for("/offline"))
        _render_search(root, tpl)
        pages += 1
        _render_offline_note(root, tpl)
        pages += 1
        _render_404(root, tpl)
        pages += 1

        for cat in CATEGORIES:
            pages += _render_category(root, db, cat)

        public_rows = []
        url_map: dict[str, str] = {}
        from .exclusions import is_excluded_brand
        for item in products:
            if is_excluded_brand(item.name or ""):
                book.add_retired(item, _redirect_target(db, item))
                continue
            if item.catalog_hidden:
                continue
            if item.retired_at is not None:
                book.add_retired(item, _redirect_target(db, item))
                continue
            path = product_path(item)
            prefix = f"/produkt/{item.id}-"
            public_slug = path[len(prefix):] if path.startswith(prefix) else ""
            req = request_for(path)
            try:
                response = product(req, item.id, public_slug, db=db)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            if _status(response) in (301, 302, 303, 307, 308):
                dest = _location(response)
                code = _status(response)
                book.add(path, dest, code)
                bare = f"/produkt/{item.id}"
                if path != bare:
                    book.add(bare, dest, code)
                continue
            write_html(path, response)
            book.add_live_id(item)
            stored_slug = (item.slug or "").strip("-")
            if stored_slug and stored_slug != public_slug:
                book.add_former_slug(item.id, stored_slug, path)
            if not item.needs_review:
                row = _index_row(item)
                if row is not None:
                    public_rows.append(row)
            for offer in item.offers:
                if getattr(offer, "withheld", False) or not offer.url:
                    continue
                url_map.setdefault(norm_offer_url(offer.url), path)
            if pages % 200 == 0:
                log.info("Stránek %s", pages)

        if features.digest_on():
            response = digest_archive(request_for("/prehled"), db=db)
            if _status(response) == 200:
                write_html("/prehled", response)
                for row in db.scalars(select(WeeklyDigest).order_by(WeeklyDigest.year, WeeklyDigest.week)).all():
                    from . import digest as digestmod
                    slug = digestmod.slug(row)
                    from .main import digest_week
                    call(f"/prehled/{slug}", digest_week, request_for(f"/prehled/{slug}"), row.year, row.week, db=db)
        if features.extension_on():
            response = extension_page(request_for("/rozsireni"))
            if _status(response) == 200:
                write_html("/rozsireni", response)

        data = root / "data"
        data.mkdir(parents=True)
        index_bytes = json.dumps(public_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        (data / "katalog.json").write_bytes(index_bytes)
        (data / "url.json").write_bytes(json.dumps(url_map, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        from .landings import cap_specs, series_specs
        for spec in series_specs():
            call(spec.path, series_landing, request_for(spec.path), spec.slug, db=db)
        for spec in cap_specs():
            call(spec.path, price_landing, request_for(spec.path), spec.category, spec.amount, db=db)

        xml = sitemap_xml(db)
        (root / "sitemap.xml").write_text(xml, encoding="utf-8")
        (root / "robots.txt").write_text(robots_txt().body.decode("utf-8"), encoding="utf-8")
        sw = service_worker()
        (root / "sw.js").write_bytes(sw.body)
        man = manifest()
        (root / "manifest.webmanifest").write_bytes(man.body)

        # kontakt a staré adresy, které route vrací jako redirect (už mohou být v book).
        kontakt = kontakt_get()
        book.add("/kontakt", _location(kontakt), _status(kontakt))
        legacy = compare_legacy(ids="", db=db)
        book.add("/porovnani", _location(legacy), _status(legacy))

        mode = write_redirects(root, book, static_limit=redirect_static_limit)
        (root / "_headers").write_text(security_headers(), encoding="utf-8")
        affiliate = _affiliate_stats(products)
        total_bytes = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        file_count = sum(1 for p in root.rglob("*") if p.is_file())
        seconds = round(time.perf_counter() - started, 2)
        report = {
            "pages": pages,
            "files": file_count,
            "bytes": total_bytes,
            "seconds": seconds,
            "products": len(public_rows),
            "search_index_bytes": len(index_bytes),
            "redirect_mode": mode,
            "affiliate": affiliate,
            "redirects_static": sum(1 for line in (root / "_redirects").read_text().splitlines() if line and not line.startswith("#") and "*" not in line.split()[0] and ":" not in line.split()[0]),
            "url_changes": [
                "Stránkování kategorií: /kategorie/{slug}?strana={n} je /kategorie/{slug}/strana/{n} (stejně /nejlevnejsi a /ve-sleve). Staré ?strana= prohlížeč přesměruje.",
                "Adresa /produkt/{id} bez slugu je 301 na kanonickou /produkt/{id}-{slug}. Živý web na ní vrací 200 a v HTML má kanonický odkaz.",
                "Když úklid názvu změní slug, /produkt/{id}-{starý slug} je 301 na /produkt/{id}-{nový slug}.",
            ],
            "dashboard_steps": [
                "www.lovceny.cz na https://lovceny.cz se stavem 301 nejde zapsat do Pages _redirects "
                "(domain-level redirects nejsou podporované) ani do _headers. "
                "V Cloudflare nastav Bulk Redirect nebo Redirect Rule: hostname www.lovceny.cz, "
                "cíl https://lovceny.cz, stav 301, zachovat cestu a query.",
            ],
            "not_published": LOST_WITHOUT_SERVER,
        }
        log.info("Hotovo: %s stránek, %s souborů, %s B, %s s, režim %s", pages, file_count, total_bytes, seconds, mode)
        return report
    finally:
        tpl.env.globals["STATIC_SITE"] = False
        db.rollback()
        db.close()
        catcache.clear()
        clear_display_names_cache()


def _fixed_redirects(book: _Redirects, db, request_for) -> None:
    from .main import budget_page, digest_archive, extension_page
    for source, dest in {
        "/metodika": "/jak-radime",
        "/bot": "/pro-obchody",
        "/pro-e-shopy": "/pro-obchody",
        "/podminky-uzivani": "/podminky",
    }.items():
        book.add(source, dest, 301)
    book.add("/pes/*", "/bez-serveru", 301, dynamic=True)
    book.add("/hlidani/*", "/bez-serveru", 301, dynamic=True)
    book.add("/black-friday/odhlasit/*", "/bez-serveru", 301, dynamic=True)
    book.add("/prehled/email/*", "/bez-serveru", 301, dynamic=True)
    book.add("/prehled/odhlasit/*", "/bez-serveru", 301, dynamic=True)
    book.add("/hlidat-rozpocet/*", "/bez-serveru", 301, dynamic=True)

    prehled = digest_archive(request_for("/prehled"), db=db)
    if _status(prehled) in (301, 302, 303, 307, 308):
        book.add("/prehled", _location(prehled), _status(prehled))
        book.add("/prehled/*", "/slevy", 301, dynamic=True)
    rozsireni = extension_page(request_for("/rozsireni"))
    if _status(rozsireni) in (301, 302, 303, 307, 308):
        book.add("/rozsireni", _location(rozsireni), _status(rozsireni))
        book.add("/rozsireni/*", "/", 301, dynamic=True)
    rozpocty = budget_page(request_for("/hlidat-rozpocet"), db=db)
    if _status(rozpocty) in (301, 302, 303, 307, 308):
        book.add("/hlidat-rozpocet", _location(rozpocty), _status(rozpocty))


def _render_search(root: Path, tpl) -> None:
    ctx = {
        "request": request_for("/hledat"),
        "q": "",
        "results": [],
        "error": None,
        "want_live": False,
        "live_done": False,
        "noindex": True,
        "razeni": "cena",
        "skladem": False,
    }
    html = tpl.get_template("search.html").render(ctx)
    dest = output_path(root, "/hledat")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rewrite_img_proxy(html), encoding="utf-8")


def _render_offline_note(root: Path, tpl) -> None:
    ctx = {
        "request": request_for("/bez-serveru"),
        "title": "Tohle už z webu nejde",
        "noindex": True,
        "art": "",
        "text": "Potvrzení a odhlášení hlídání, e-mailů a oznámení potřebuje server. "
                "Napiš na <!--email_off-->info@lovceny.cz<!--/email_off--> a vyřídíme to ručně.",
        "canonical_path": "/bez-serveru",
    }
    html = tpl.get_template("message.html").render(ctx)
    dest = output_path(root, "/bez-serveru")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")


def _render_404(root: Path, tpl) -> None:
    html = tpl.get_template("404.html").render({
        "request": request_for("/404"),
        "path": "",
        "noindex": True,
    })
    (root / "404.html").write_text(rewrite_img_proxy(html), encoding="utf-8")


def _render_category(root: Path, db, cat) -> int:
    from .main import category
    made = 0
    for view in ("", "nejlevnejsi", "ve-sleve"):
        page = 1
        while page < 500:
            path = f"/kategorie/{cat.slug}" + (f"/{view}" if view else "")
            if page > 1:
                path = f"{path}/strana/{page}"
            req = request_for(f"/kategorie/{cat.slug}" + (f"/{view}" if view else ""),
                              f"strana={page}" if page > 1 else "")
            try:
                response = category(req, cat.slug, view, db=db, strana=str(page) if page > 1 else "")
            except HTTPException as exc:
                if exc.status_code == 404:
                    break
                raise
            html = _body(response)
            has_next = 'rel="next"' in html
            dest = output_path(root, path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rewrite_pagination(rewrite_img_proxy(html)), encoding="utf-8")
            made += 1
            if not has_next:
                break
            page += 1
    return made


def _index_row(product) -> dict | None:
    from .main import product_summary, shop_rel
    from .names import canonical_slug, card_label
    visible = [o for o in product.offers if not getattr(o, "withheld", False)]
    best, verdict = product_summary(product)
    display = best or last_known_offer(visible)
    if display is None or display.price is None:
        return None
    shop = get_shop(display.shop) if best else get_shop(getattr(display, "shop", "") or "")
    buy = best is not None and best.in_stock is not False and shop is not None
    label = card_label(product.name or "") or (product.name or "")
    row = {
        "i": product.id,
        "s": canonical_slug(product.name or "", product.slug or ""),
        "n": label,
        "c": product.category_slug or "",
        "b": guess_brand(product.name or "") or "",
        "p": float(display.price),
        "k": 1 if (display.in_stock is not False and best is not None) else 0,
        "sn": shop.name if shop else (display.shop or ""),
        "v": verdict.code,
        "vl": verdict.headline,
        "a": 1 if is_accessory_text(product.name or "", product.category) else 0,
    }
    if product.name and product.name != label:
        row["f"] = product.name
    if product.ean:
        row["e"] = product.ean
    if buy and shop is not None:
        row["h"] = shop.affiliate_link(best.url)
        row["r"] = shop_rel(shop)
    return row


def _read_only(db) -> None:
    """Na Postgresu další příkazy v relaci jen čtou. SQLite (testy) to neumí."""
    from sqlalchemy import text
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(text("SET default_transaction_read_only = on"))
    db.rollback()


def _affiliate_stats(products) -> list[dict]:
    """Kolik tlačítek Do obchodu má obal affiliate a kolik jde na holou URL z databáze.

    Uložená URL je po importu očištěná (utm pryč, TradeDoubler a Allegro affiliate rozbalené).
    Obalení se dělá až při renderu. Hodnoty tajemství sem nepatří, jen počty.
    """
    from .offer_trust import offer_is_promoted
    tallies: dict[str, dict] = {}
    for product in products:
        if product.retired_at is not None or product.catalog_hidden or product.needs_review:
            continue
        for offer in product.offers:
            if offer.price is None or offer.in_stock is False or getattr(offer, "withheld", False):
                continue
            if not offer_is_promoted(offer, product=product):
                continue
            shop = get_shop(offer.shop)
            if shop is None:
                continue
            href = shop.affiliate_link(offer.url or "")
            row = tallies.setdefault(shop.slug, {
                "shop": shop.name,
                "wrapper": _wrapper_kind(shop),
                "buy_links": 0,
                "wrapped": 0,
                "plain": 0,
            })
            row["buy_links"] += 1
            if href != (offer.url or ""):
                row["wrapped"] += 1
            else:
                row["plain"] += 1
    return [tallies[key] for key in sorted(tallies)]


def _wrapper_kind(shop) -> str:
    if shop.affiliate_env:
        return shop.affiliate_env
    if shop.ehub_bid:
        return "EHUB_AID"
    if shop.dognet_chid:
        return "DOGNET_CHID"
    return ""


def main(argv=None) -> int:
    import argparse
    import os
    os.environ.setdefault("LOVCENY_SKIP_DATA_FIXES", "1")
    parser = argparse.ArgumentParser(description="Vygeneruje statický Lovceny z databáze.")
    parser.add_argument("--out", default="dist")
    parser.add_argument("--report", default="build-report.json")
    parser.add_argument("--redirect-limit", type=int, default=REDIRECT_STATIC_LIMIT)
    parser.add_argument("--migrate", action="store_true",
                        help="Pustí Alembic upgrade. Bez flagu build jen čte.")
    args = parser.parse_args(argv)
    if args.migrate:
        from .db import init_db
        init_db()
    report = build(args.out, redirect_static_limit=args.redirect_limit)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Stránek: {report['pages']}\n"
        f"Souborů: {report['files']}\n"
        f"Velikost: {report['bytes']} B\n"
        f"Čas: {report['seconds']} s\n"
        f"Produktů v indexu: {report['products']}\n"
        f"Index: {report['search_index_bytes']} B\n"
        f"Přesměrování: {report['redirect_mode']}\n"
        "Affiliate: " + json.dumps(report.get("affiliate") or [], ensure_ascii=False) + "\n"
        "URL, které se změnily:\n- " + "\n- ".join(report["url_changes"])
    )
    if report["pages"] < 1:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
