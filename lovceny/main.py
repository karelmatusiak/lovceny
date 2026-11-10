import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request, Form, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_

from .db import SessionLocal, init_db
from .models import Product, Offer, PricePoint, Watch, PushSubscription
from .categories import CATEGORIES, BY_SLUG, classify, is_accessory_text
from . import push as pushmod
from . import seo
from .web import static_url, canonical, public_url, CacheHeadersMiddleware, add_compression, AnalyticsMiddleware
from .shops import SHOPS, get_shop
from .service import add_by_url, create_watch, search_db, offers_for_query, smart_offers
from .verdict import compute
from .deals import real_deals
from .chart import build_chart, verdict_note
from .util import czk, cz_count, cz_plural
from .names import card_label, short_name, uniquify_short_names
from .availability import (
    offer_is_buyable, best_buyable_offer, last_known_offer,
    sort_offers_for_display,
)
from . import images
from . import analytics

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
app = FastAPI(title="Lovceny")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.filters["czk"] = czk
tpl.env.globals["cz_count"] = cz_count
tpl.env.globals["cz_plural"] = cz_plural
tpl.env.globals["SHOPS"] = SHOPS
tpl.env.globals["get_shop"] = get_shop
tpl.env.globals["static"] = static_url
tpl.env.globals["canonical"] = canonical
tpl.env.globals["public_url"] = public_url
tpl.env.globals["CATEGORIES"] = CATEGORIES
tpl.env.filters["tojson_ld"] = seo.jsonld
tpl.env.globals["img_attrs"] = images.srcset
tpl.env.globals["img_canon"] = images.normalize_allegro_image
tpl.env.filters["fromjson"] = json.loads
from . import seasons  # noqa: E402
tpl.env.globals["season_tip"] = seasons.category_tip
tpl.env.filters["pretty_name"] = lambda q: __import__("lovceny.verify", fromlist=["x"]).pretty_name(q)
tpl.env.filters["short_name"] = short_name
tpl.env.globals["card_label"] = card_label


def assign_display_names(products):
    """Nastaví ephemeral product.display_name (unikátní short name)."""
    products = [p for p in products if p is not None]
    if not products:
        return products
    shorts = uniquify_short_names([p.name or "" for p in products])
    for p, s in zip(products, shorts):
        p.display_name = s
    return products


# Globální mapa short names (unikátní napříč katalogem). TTL + clear po fetch_prices.
_DISPLAY_NAMES_TTL_S = 300
_display_names_cache: tuple[float, dict[int, str]] | None = None


def clear_display_names_cache() -> None:
    global _display_names_cache
    _display_names_cache = None


def assign_all_display_names(db):
    """id → unikátní short name. Cache 5 min; bez načítání celých Product ORM objektů na hit."""
    import time
    global _display_names_cache
    now = time.monotonic()
    if _display_names_cache and now - _display_names_cache[0] < _DISPLAY_NAMES_TTL_S:
        return _display_names_cache[1]
    rows = db.execute(select(Product.id, Product.name)).all()
    shorts = uniquify_short_names([(n or "") for _, n in rows])
    mapping = {int(pid): s for (pid, _), s in zip(rows, shorts)}
    _display_names_cache = (now, mapping)
    return mapping


def dname(p):
    """Viditelný název mimo uloženou identitu. Schema dál používá short display_name."""
    return card_label(getattr(p, "name", "") or "")


tpl.env.globals["dname"] = dname


def affiliate_on() -> bool:
    """Affiliate upozornění se ukáže, až když jsou odkazy opravdu provizní:
    LOVCENY_AFFILIATE=1, affiliate ID Alza/Datart, eHUB a_bid + a_aid, nebo Dognet chid."""
    import os
    if os.environ.get("LOVCENY_AFFILIATE", "0") == "1":
        return True
    return any(s.affiliate_active for s in SHOPS.values())


tpl.env.globals["AFFILIATE_ON"] = affiliate_on


def shop_rel(shop) -> str:
    """sponsored jen u aktivního affiliate; jinak nofollow noopener."""
    if shop is not None and getattr(shop, "affiliate_active", False):
        return "nofollow sponsored noopener"
    return "nofollow noopener"


tpl.env.globals["shop_rel"] = shop_rel


def _prague(d, fmt="%-d. %-m. %H:%M"):
    """Čas pro lidi v Česku (DB ukládá UTC)."""
    if not d:
        return ""
    from zoneinfo import ZoneInfo
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(ZoneInfo("Europe/Prague")).strftime(fmt)


tpl.env.filters["prague"] = _prague
app.add_middleware(AnalyticsMiddleware)
app.add_middleware(CacheHeadersMiddleware)
add_compression(app)


@app.on_event("startup")
def _startup():
    init_db()
    # Obnov soft-tombstony za hard-delete před soft-retire (301 /produkt → kategorie).
    from .retire_tombstones import restore_tombstones
    from .catalog_repair import repair_catalog
    log = logging.getLogger("lovceny")
    db = SessionLocal()
    try:
        restore_tombstones(db)
        st = repair_catalog(db, dry_run=False)
        log.info("Úklid katalogu při startu: %s", {k: st[k] for k in st if k != "samples"})
    except Exception:
        log.exception("Úklid katalogu při startu selhal")
        db.rollback()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def priced_offers(offers):
    """Nabídky s cenou – bez ceny (pending feed / nikdy nenačtené) se v UI neukazují,
    ať Alza/Datart bez feedu a mrtvé scrape obchody nerozbíjejí produktové stránky.
    Až feed/scrape cenu doplní, nabídka se objeví sama."""
    return [o for o in offers if o.price is not None]


def _merged_target(db, p: Product):
    """Přeživší produkt po sloučení, nebo None (301 zůstane na kategorii)."""
    seen = set()
    cur = p
    while cur is not None and cur.redirect_product_id and cur.id not in seen:
        seen.add(cur.id)
        nxt = db.get(Product, cur.redirect_product_id)
        if nxt is None:
            return None
        if nxt.retired_at is None:
            return nxt
        cur = nxt
    return None


def product_summary(p: Product):
    """Nejlepší *koupitelná* nabídka (ne „Není skladem“) + verdikt.

    Verdikt počítáme z koupitelné ceny; když žádná není, z poslední známé
    (historie zůstává), ale `best` je None → UI bez CTA.
    """
    best = best_buyable_offer(p.offers)
    last = last_known_offer(p.offers)
    ref = best or last
    pts = [pt for o in p.offers for pt in o.points]
    v = compute(pts, float(ref.price) if ref else None,
                float(ref.original_price) if ref and ref.original_price else None)
    return best, v


def price_history_thin(db) -> bool:
    """True while average history is too short for reliable Kupuj/Počkej verdicts."""
    from .verdict import MIN_DAYS
    n_priced = db.scalar(select(func.count()).select_from(Offer).where(Offer.price.isnot(None))) or 0
    if n_priced == 0:
        return True
    n_pts = db.scalar(select(func.count()).select_from(PricePoint)) or 0
    return n_pts < n_priced * MIN_DAYS


def _slevy_promo(rows):
    """Fallback na /slevy: týden historie, slušný název, skladem. Čerstvý Allegro výplň ne."""
    out = []
    for p, best, v in rows:
        if best is None or best.in_stock is False:
            continue
        if getattr(v, "code", None) == "none":
            continue
        from .matching import decent_title
        if not decent_title(p.name or ""):
            continue
        out.append((p, best, v))
    return out


def priced_product_cards(db, *, limit: int = 6, order: str = "new"):
    """Karty produktů s aspoň jednou cenou – bez ceny do výpisů nepatří."""
    from sqlalchemy.orm import selectinload
    q = (select(Product).where(Product.retired_at.is_(None))
         .options(selectinload(Product.offers).selectinload(Offer.points)))
    if order == "watched":
        watched_ids = [r[0] for r in db.execute(
            select(Watch.product_id, func.count(Watch.id).label("n"))
            .group_by(Watch.product_id).order_by(func.count(Watch.id).desc()).limit(limit * 4)
        ).all()]
        if watched_ids:
            products = db.scalars(q.where(Product.id.in_(watched_ids))).all()
            by_id = {p.id: p for p in products}
            products = [by_id[i] for i in watched_ids if i in by_id]
        else:
            # Bez hlídání neukazujeme „nejnovější“ pod nadpisem Nejsledovanější.
            return []
    else:
        products = db.scalars(q.order_by(Product.created_at.desc()).limit(limit * 4)).all()
    from .availability import offer_is_buyable
    scored = []
    for p in products:
        if is_accessory_text(p.name or "", p.category):
            continue
        best, v = product_summary(p)
        if best is None:
            continue
        n_shops = len({o.shop for o in p.offers if offer_is_buyable(o) and o.price is not None})
        scored.append((0 if n_shops >= 2 else 1, p, best, v))
    scored.sort(key=lambda x: x[0])
    out = [(p, best, v) for _, p, best, v in scored[:limit]]
    assign_display_names([p for p, _, _ in out])
    return out


def gemini_search_on() -> bool:
    import os
    return bool(os.environ.get("GEMINI_API_KEY"))


tpl.env.globals["AI_SEARCH_ON"] = gemini_search_on


def _wants_html_404(request: Request) -> bool:
    path = request.url.path or "/"
    if path.startswith("/api/") or path.startswith("/ext/"):
        return False
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return False
    if not accept or "text/html" in accept or "*/*" in accept:
        return True
    return False


from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and _wants_html_404(request):
        return tpl.TemplateResponse(
            request, "404.html",
            {"path": request.url.path, "noindex": True},
            status_code=404,
        )
    headers = getattr(exc, "headers", None) or None
    detail = exc.detail
    if isinstance(detail, (dict, list)):
        return JSONResponse(detail, status_code=exc.status_code, headers=headers)
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=headers)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def home(request: Request, db=Depends(get_db)):
    cards = priced_product_cards(db, limit=6, order="new")
    deals = real_deals(db, top=3)["buys"]
    from . import blackfriday as bfmod
    from .hero import hero_showcase
    bf = {"season": bfmod.season(), "text": bfmod.countdown_text()} if bfmod.banner_active() else None
    cat_counts = dict(db.execute(
        select(Product.category_slug, func.count(Product.id))
        .where(Product.retired_at.is_(None))
        .group_by(Product.category_slug)).all())
    return tpl.TemplateResponse(request, "index.html", {
        "cards": cards, "deals": deals, "bf": bf,
        "thin_history": price_history_thin(db),
        "hero_picks": hero_showcase(db),
        "cat_counts": cat_counts,
        "ld": [seo.website_ld(), seo.organization_ld()],
    })


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    path = BASE / "static" / "brand" / "favicon.ico"
    return FileResponse(path, media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/kontakt")
def kontakt_get():
    return RedirectResponse("/o-nas#kontakt", status_code=302)


@app.get("/slevy", response_class=HTMLResponse)
def deals_page(request: Request, db=Depends(get_db)):
    data = real_deals(db)
    assign_display_names([r.product for r in data.get("buys", []) + data.get("fakes", []) if getattr(r, "product", None)])
    # Karty a „blízko minima“ jsou jen prázdný stav. Když Kupuj existuje, šablona je nečte
    # a jejich selectinload historie by zbytečně držel TTFB.
    near, newest, watched = [], [], []
    if not data.get("buys"):
        from . import deals as dealsmod
        near = dealsmod.near_minima(db, limit=12)
        newest = priced_product_cards(db, limit=6, order="new")
        watched = priced_product_cards(db, limit=6, order="watched")
    return tpl.TemplateResponse(request, "deals.html", {
        **data,
        "push_key": pushmod.public_key() if pushmod.enabled() else "",
        "email_on": False,
        "thin_history": price_history_thin(db),
        "newest": _slevy_promo(newest),
        "watched": _slevy_promo(watched),
        "near": near,
    })


@app.get("/ochrana-osobnich-udaju", response_class=HTMLResponse)
def privacy(request: Request):
    return tpl.TemplateResponse(request, "privacy.html", {})


@app.get("/cookies", response_class=HTMLResponse)
def cookies(request: Request):
    return tpl.TemplateResponse(request, "cookies.html", {})


@app.get("/hledat", response_class=HTMLResponse)
def search(request: Request, q: str = "", zive: int = 0, razeni: str = "",
           skladem: str = "", db=Depends(get_db)):
    q = q.strip()
    if not q:
        return RedirectResponse("/", 303)
    if re.match(r"^(https?://|www\.)", q, re.I):
        url = q if q.lower().startswith("http") else "https://" + q
        prod, msg = add_by_url(db, url)
        request.state.an = {"kind": "search", "query": q, "results": 0 if prod is None else 1}
        if prod is None:
            return tpl.TemplateResponse(request, "search.html", {"q": q, "results": [], "error": msg, "noindex": True}, status_code=422)
        r = f"/produkt/{prod.id}-{prod.slug}"
        return RedirectResponse(r + (f"?msg={msg}" if msg else ""), 303)
    products = search_db(db, q)
    assign_display_names(products)
    results = []
    for p in products:
        best, v = product_summary(p)
        # Do výsledků i OOS (poslední cena) – bez CTA „Do obchodu“ v šabloně.
        display = best or last_known_offer(p.offers)
        if display is not None:
            results.append((p, display, v))
    only_stock = skladem in ("1", "ano", "true")
    if only_stock:
        results = [(p, b, v) for p, b, v in results if b.in_stock is not False]
    if razeni == "cena-desc":
        results.sort(key=lambda r: (
            r[1].in_stock is False,
            -(float(r[1].price) if r[1].price is not None else 0),
        ))
    else:
        results.sort(key=lambda r: (
            r[1].in_stock is False,
            float(r[1].price) if r[1].price is not None else 1e18,
        ))
        razeni = "cena" if razeni != "cena" else razeni
    request.state.an = {"kind": "search", "query": q, "results": len(results)}
    # Málo shod v DB → živé hledání v obchodech. Bez JS (?zive=1) se načte rovnou, jinak přes fetch.
    want_live = len(results) < LIVE_THRESHOLD
    ctx = {
        "q": q, "results": results, "error": None, "want_live": want_live, "noindex": True,
        "razeni": "cena-desc" if razeni == "cena-desc" else "cena",
        "skladem": only_stock,
    }
    if want_live and zive:
        ctx.update(_live_ctx(db, q))
    return tpl.TemplateResponse(request, "search.html", ctx)


LIVE_THRESHOLD = 4


def _live_ctx(db, q: str) -> dict:
    r = smart_offers(db, q)
    return {**r, "live_done": True}


@app.get("/hledat/zive", response_class=HTMLResponse)
def search_live(request: Request, q: str = "", db=Depends(get_db)):
    q = q.strip()[:100]
    ctx = {"q": q, **(_live_ctx(db, q) if q else {"offers": [], "statuses": [], "live_done": True})}
    if q:
        request.state.an = {"kind": "live", "query": q, "results": len(ctx.get("offers") or []), "path": "/hledat"}
    return tpl.TemplateResponse(request, "_live.html", ctx)


TRACK_PER_IP_HOUR = 10    # nové produkty ze živého hledání na IP za hodinu
TRACK_GLOBAL_DAY = 300    # strop nových produktů za den (šetrnost k obchodům i DB)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "?"


def _track_allowed(db, request: Request) -> bool:
    """Rate limit jen pro NOVÉ produkty (už sledované URL nic nestojí). IP ukládáme jen jako hash."""
    import hashlib
    from .models import TrackRequest
    now = datetime.now(timezone.utc)
    ip_hash = hashlib.sha256(("lovceny:" + _client_ip(request)).encode()).hexdigest()
    db.query(TrackRequest).filter(TrackRequest.created_at < now - timedelta(days=1)).delete()
    per_ip = db.scalar(select(func.count(TrackRequest.id)).where(TrackRequest.ip_hash == ip_hash,
                                                              TrackRequest.created_at >= now - timedelta(hours=1)))
    total = db.scalar(select(func.count(TrackRequest.id)))
    if per_ip >= TRACK_PER_IP_HOUR or total >= TRACK_GLOBAL_DAY:
        db.commit()
        return False
    db.add(TrackRequest(ip_hash=ip_hash, created_at=now))
    db.commit()
    return True


@app.post("/sledovat")
def track(request: Request, url: str = Form(...), compare: int = Form(0), db=Depends(get_db)):
    from .shops import shop_for_url
    from .util import normalize_url
    shop = shop_for_url(normalize_url(url))
    if shop is None:
        raise HTTPException(422, "Tento obchod zatím nesledujeme.")
    existing = db.scalar(select(Offer).where(Offer.url == shop.canonical_url(url)))
    if existing is None and not _track_allowed(db, request):
        return tpl.TemplateResponse(request, "message.html", {
            "title": "Chvíli počkej, prosím",
            "text": "Za poslední hodinu jsi přidal(a) hodně nových produktů. Abychom obchody nezahlcovali, "
                    "zkus to prosím znovu za chvíli. Už sledované produkty můžeš otevírat bez omezení."}, status_code=429)
    prod, msg = add_by_url(db, url)
    if prod is None:
        raise HTTPException(422, msg)
    if compare:  # „Porovnat“ u živého výsledku: nejdřív sledovat, pak přidat do porovnání (JS sloučí s výběrem)
        return RedirectResponse(f"/porovnat?pridat={prod.id}", 303)
    r = f"/produkt/{prod.id}-{prod.slug}"
    return RedirectResponse(r + (f"?msg={quote(msg)}" if msg else ""), 303)


@app.get("/nahled", response_class=HTMLResponse)
def preview(request: Request, url: str = "", db=Depends(get_db)):
    """Otevřený výsledek živého hledání: co víme z výpisu obchodu + „Sledovat tento produkt“.
    Nic znovu nestahuje (data z cache živého hledání); už sledovaný produkt rovnou přesměruje."""
    from .shops import shop_for_url
    from .shops.search import find_cached_hit
    from .util import normalize_url
    shop = shop_for_url(normalize_url(url)) if url else None
    if shop is None:
        raise HTTPException(404)
    curl = shop.canonical_url(url)
    existing = db.scalar(select(Offer).where(Offer.url == curl))
    if existing:
        return RedirectResponse(f"/produkt/{existing.product.id}-{existing.product.slug}", 303)
    hit = find_cached_hit(url) or find_cached_hit(curl)
    return tpl.TemplateResponse(request, "preview.html", {"hit": hit, "url": url, "shop": shop,
                                                          "noindex": True}, headers={"X-Robots-Tag": "noindex"})


@app.api_route("/produkt/{pid:int}-{slug}", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/produkt/{pid:int}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def product(request: Request, pid: int, slug: str = "", msg: str = "", ok: str = "", db=Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404)
    # Soft-retired: 301 na kategorii (nebo /), ať staré URL nepřijdou o SEO/uživatele.
    if p.retired_at is not None:
        target_prod = _merged_target(db, p)
        if target_prod is not None:
            return RedirectResponse(f"/produkt/{target_prod.id}-{target_prod.slug}", status_code=301)
        cat = BY_SLUG.get(p.category_slug or "")
        target = f"/kategorie/{cat.slug}" if cat else "/"
        return RedirectResponse(target, status_code=301)
    # Aktivní: špatný slug → kanonická URL (zachovat canonical chování).
    if slug and slug != p.slug:
        q = request.url.query
        dest = f"/produkt/{p.id}-{p.slug}" + (f"?{q}" if q else "")
        return RedirectResponse(dest, status_code=301)
    best, v = product_summary(p)
    last_known = last_known_offer(p.offers)
    offers = sort_offers_for_display(p.offers)
    pts = [pt for o in p.offers for pt in o.points]
    display_price = best or last_known
    live = float(display_price.price) if display_price is not None and display_price.price is not None else None
    ranges = build_chart(pts, verdict=v, live_price=live)
    cat = BY_SLUG.get(p.category_slug or "")
    # unikátní short name napříč katalogem (mapa s TTL – ne SELECT * Product na každý request)
    names = assign_all_display_names(db)
    p.display_name = names.get(p.id) or short_name(p.name or "")
    dn = dname(p)
    crumbs = [("Lovceny", "/")] + ([(cat.name, f"/kategorie/{cat.slug}")] if cat else []) + [(dn, f"/produkt/{p.id}-{p.slug}")]
    has_demo = any(pt.is_demo for pt in pts)
    all_oos = best is None and last_known is not None
    display_price = best or last_known
    request.state.an = {"product_id": p.id, "path": f"/produkt/{p.id}"}
    share_price = f" – teď od {czk(best.price)}" if best else (
        f" – naposledy {czk(last_known.price)} (není skladem)" if last_known else "")
    return tpl.TemplateResponse(request, "product.html", {
        "p": p, "best": best, "last_known": last_known, "all_oos": all_oos,
        "display_price": display_price, "v": v, "offers": offers,
        "chart_ranges": ranges, "chart_note": verdict_note(v, ranges),
        "push_key": pushmod.public_key() if pushmod.enabled() else "", "cat": cat, "crumbs": crumbs,
        "canonical_path": f"/produkt/{p.id}-{p.slug}",
        "ld": [seo.product_ld(p, offers, v), seo.breadcrumb_ld(crumbs)],
        "has_demo": has_demo, "msg": msg, "ok": ok, "get_shop": get_shop,
        "share_url": canonical(f"/produkt/{p.id}-{p.slug}"),
        "share_text": f"{dn}" + share_price + f" ({v.headline}) na Lovceny"})


@app.post("/produkt/{pid}/hlidat")
def watch(pid: int, email: str = Form(...), target_price: float = Form(...),
          only_in_stock: bool = Form(False), db=Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or target_price <= 0:
        return RedirectResponse(f"/produkt/{p.id}-{p.slug}?msg=Zkontroluj e-mail a cílovou cenu.#hlidani", 303)
    create_watch(db, p, email, target_price, only_in_stock)
    return RedirectResponse(f"/produkt/{p.id}-{p.slug}?ok=1#hlidani", 303)


@app.get("/hlidani/potvrdit/{token}", response_class=HTMLResponse)
def confirm(request: Request, token: str, db=Depends(get_db)):
    w = db.scalar(select(Watch).where(Watch.token == token))
    if not w:
        raise HTTPException(404)
    w.confirmed = True
    db.commit()
    return tpl.TemplateResponse(request, "message.html", {"title": "Hlídání ceny potvrzeno 🔔", "noindex": True, "art": "hlidani",
                                "art_alt": "Ilustrace: oranžový zvoneček upozornění, obálka a graf klesající ceny",
                                "text": f"Dáme vědět, až {w.product.name} klesne na {czk(w.target_price)}."})


@app.get("/hlidani/zrusit/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str, db=Depends(get_db)):
    """Odhlášení jedním klikem: hlídání i e-mail rovnou smažeme. Opakované kliknutí nic nerozbije."""
    w = db.scalar(select(Watch).where(Watch.token == token))
    if w:
        db.delete(w)
        db.commit()
    return tpl.TemplateResponse(request, "message.html", {
        "title": "Odhlášeno", "noindex": True, "text": "Hlídání ceny je zrušené a tvůj e-mail jsme u něj smazali."})


@app.get("/obchody", response_class=HTMLResponse)
def shops(request: Request):
    listed = [s for s in SHOPS.values() if not s.retired]
    listed.sort(key=lambda s: (0 if s.status_label()[0] == "ok" else 1, s.name.lower()))
    return tpl.TemplateResponse(request, "shops.html", {"shops": listed})


_health_cache: dict = {}


@app.api_route("/healthz", methods=["GET", "HEAD"])
def health(db: int = 0):
    """Lehký endpoint pro pingy/uptime monitory: bez dotazu do DB (nebudí Neon, nepálí jeho compute).
    /healthz?db=1 → diagnostika DB (SELECT 1 + počty, počty cachované 10 min)."""
    from .db import engine
    out = {"ok": True, "db": engine.url.get_backend_name()}
    if not db:
        return out
    import time
    from sqlalchemy import text
    t0 = time.monotonic()
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
            out["db_ok"] = True
            out["db_ms"] = round((time.monotonic() - t0) * 1000)
            c = _health_cache
            if not c or time.monotonic() - c["t"] > 600:
                c.update(t=time.monotonic(), products=s.scalar(select(func.count(Product.id))),
                         watches=s.scalar(select(func.count(Watch.id))))
            out.update(products=c["products"], watches=c["watches"], counts_age_s=round(time.monotonic() - c["t"]))
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, db_ok=False, error=type(e).__name__)
        from fastapi.responses import JSONResponse
        return JSONResponse(out, status_code=503)
    out["db_host"] = (engine.url.host or "")[:12]
    return out


# Staré odkazy z e-mailů (/pes/…) – trvalé přesměrování na /hlidani/…
@app.get("/pes/{action}/{token}")
def legacy_watch_links(action: str, token: str):
    if action not in ("potvrdit", "zrusit"):
        raise HTTPException(404)
    return RedirectResponse(f"/hlidani/{action}/{token}", 301)


# ---------- porovnání (v7: Srovnání produktů /porovnat?p=…) ----------
COMPARE_MAX = 3
COMPARE_COLORS = ["#0B6E4F", "#C2410C", "#2B5BD7"]


def parse_ids(raw: str) -> list[int]:
    out = []
    for part in re.split(r"[,\s]+", raw or ""):
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out[:COMPARE_MAX]


def compare_token(p: Product) -> str:
    return f"{p.id}-{p.slug}"


def resolve_compare(db, raw: str) -> list[Product]:
    """?p=12-iphone-16,samsung-qe65 → produkty. Token „ID-slug“ (jednoznačný) nebo jen slug / jen ID."""
    out: list[Product] = []
    for part in re.split(r"[,\s]+", (raw or "")[:600]):
        part = part.strip().strip("/").lower()
        if not part or len(out) >= COMPARE_MAX:
            continue
        m = re.match(r"^(\d+)(?:-[a-z0-9-]*)?$", part)
        p = db.get(Product, int(m.group(1))) if m else (
            db.scalar(select(Product).where(Product.slug == part).order_by(Product.id).limit(1))
            if re.match(r"^[a-z0-9-]{1,200}$", part) else None)
        if p and p.retired_at is None and all(x.id != p.id for x in out):
            out.append(p)
    return out


@app.get("/porovnani")
def compare_legacy(ids: str = "", pridat: str = "", db=Depends(get_db)):
    """Původní adresa (?ids=1,2) → nové /porovnat?p=… (301; ?pridat= zachová)."""
    toks = [compare_token(p) for p in (db.get(Product, i) for i in parse_ids(ids)) if p]
    q = ("p=" + ",".join(toks)) if toks else ""
    if pridat.isdigit():
        q += ("&" if q else "") + f"pridat={pridat}"
    return RedirectResponse("/porovnat" + (f"?{q}" if q else ""), 301)


@app.get("/porovnat", response_class=HTMLResponse)
def compare(request: Request, p: str = "", pridat: str = "", db=Depends(get_db)):
    from .verdict import daily_best
    from .chart import build_multi, DASHES
    from . import specs as specsmod
    from statistics import median
    products = resolve_compare(db, p)
    if pridat.isdigit() and len(products) < COMPARE_MAX:
        extra = db.get(Product, int(pridat))
        if extra and all(x.id != extra.id for x in products):
            products.append(extra)
    assign_display_names(products)
    now = datetime.now(timezone.utc)
    items = []
    for i, prod in enumerate(products):
        best, v = product_summary(prod)
        pts = [pt for o in prod.offers for pt in o.points]
        # nejnižší cena za 30 dní PŘED dneškem (stejně jako u § 12a a na /black-friday)
        d30 = {d: x for d, x in daily_best(pts, now - timedelta(days=30)).items() if d < now.date()}
        min30 = min(d30.items(), key=lambda x: (x[1], x[0]), default=None)
        if v.code == "fake" and v.ref_min30 is not None:   # stejné číslo jako u štítku (okno před akcí)
            min30 = (v.ref_date, v.ref_min30)
        d90 = list(daily_best(pts, now - timedelta(days=90)).values())
        priced = sorted(priced_offers(prod.offers), key=lambda o: float(o.price))
        items.append({"p": prod, "best": best, "v": v, "best_shop": get_shop(best.shop) if best else None,
                      "offers": priced,
                      "n_shops": len(priced), "color": COMPARE_COLORS[i], "dash": DASHES[i],
                      "min30": min30[1] if min30 else None, "min30_date": min30[0] if min30 else None,
                      "usual": median(d90) if d90 else None, "days": len(daily_best(pts, now - timedelta(days=3650))),
                      "has_demo": any(getattr(pt, "is_demo", False) for pt in pts), "token": compare_token(prod),
                      "pts": pts})
    priced_items = [it for it in items if it["best"]]
    cheapest = min(priced_items, key=lambda it: float(it["best"].price))["p"].id if len(priced_items) > 1 else None
    ranges = build_multi([{"name": it["p"].name[:60], "color": it["color"], "dash": it["dash"], "points": it["pts"]}
                          for it in items], now) if len(items) >= 2 else []
    spec_rows = specsmod.table([it["p"].name for it in items])
    tokens = ",".join(it["token"] for it in items)
    title = " vs. ".join(it["p"].name[:40] for it in items) if len(items) >= 2 else "Srovnání produktů"
    return tpl.TemplateResponse(request, "compare.html", {
        "items": items, "ids": ",".join(str(it["p"].id) for it in items), "tokens": tokens, "pridat": pridat,
        "cheapest": cheapest, "ranges": ranges, "spec_rows": spec_rows, "get_shop": get_shop, "max": COMPARE_MAX,
        "page_title": title, "share_url": f"{public_url()}/porovnat?p={tokens}" if tokens else f"{public_url()}/porovnat",
        "noindex": True, "has_demo": any(it["has_demo"] for it in items)})


# ---------- Web Push ----------
PRECACHE_STATIC = ["style.css", "app.css", "compare.js", "pwa.js", "brand/logo.svg", "brand/ikona.svg",
                   "brand/icon-192.png", "brand/favicon-32.png"]


@app.get("/sw.js")
def service_worker():
    import hashlib
    urls = [static_url(f) for f in PRECACHE_STATIC]
    body = tpl.env.get_template("sw.js").render(version="", precache=urls)
    version = hashlib.sha256((body + "".join(urls)).encode()).hexdigest()[:10]
    return Response(tpl.env.get_template("sw.js").render(version=version, precache=urls), media_type="text/javascript",
                    headers={"Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    icons = [{"src": static_url("brand/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
             {"src": static_url("brand/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
             {"src": static_url("brand/icon-maskable-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
             {"src": static_url("brand/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]
    data = {"name": "Lovceny – poctivý hlídač cen elektroniky", "short_name": "Lovceny", "id": "/",
            "description": "Historie cen elektroniky a spotřebičů z českých e-shopů, verdikt Kupuj / Počkej a hlídání ceny.",
            "lang": "cs", "dir": "ltr", "start_url": "/?zdroj=pwa", "scope": "/", "display": "standalone",
            "orientation": "portrait-primary", "theme_color": "#0B6E4F", "background_color": "#FBF8F3",
            "categories": ["shopping", "utilities"], "icons": icons,
            "shortcuts": [{"name": "Skutečné slevy", "url": "/slevy", "icons": [icons[0]]},
                          {"name": "Kategorie", "url": "/kategorie", "icons": [icons[0]]}]}
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return tpl.TemplateResponse(request, "offline.html", {"noindex": True})


@app.post("/api/push/odebirat")
def push_subscribe(payload: dict = Body(...), db=Depends(get_db)):
    if not pushmod.enabled():
        raise HTTPException(404, "Upozornění v prohlížeči nejsou zapnutá.")
    try:
        pid = int(payload.get("product_id") or 0)
        target = float(payload.get("target_price") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, "Neplatná data.")
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404, "Produkt neexistuje.")
    try:
        s = pushmod.subscribe(db, p, payload.get("subscription") or {}, target, bool(payload.get("only_in_stock")))
    except pushmod.PushError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "token": s.token}


@app.post("/api/push/zrusit")
def push_unsubscribe(payload: dict = Body(...), db=Depends(get_db)):
    n = pushmod.unsubscribe(db, token=str(payload.get("token") or ""), endpoint=str(payload.get("endpoint") or ""),
                            product_id=int(payload.get("product_id") or 0) or None)
    return {"ok": True, "deleted": n}


@app.get("/hlidani/push/zrusit/{token}", response_class=HTMLResponse)
def push_unsubscribe_link(request: Request, token: str, db=Depends(get_db)):
    pushmod.unsubscribe(db, token=token)
    return tpl.TemplateResponse(request, "message.html", {
        "title": "Upozornění vypnuto", "noindex": True,
        "text": "Upozornění v prohlížeči pro tento produkt jsme zrušili a odběr smazali."})


# ---------- SEO: kategorie, nejlevnější, ve slevě, sitemap, robots ----------
def _cat_or_404(slug: str):
    c = BY_SLUG.get(slug)
    if not c:
        raise HTTPException(404)
    return c


def _cat_rows(db, c):
    """Category product rows; TTL-cached as (product_id, best_offer_id|None, Verdict)."""
    from sqlalchemy.orm import selectinload
    from . import catcache
    from .verdict import Verdict

    cached = catcache.get(c.slug)
    if cached is not None:
        ids = [pid for pid, _, _ in cached]
        if not ids:
            return []
        products = db.scalars(select(Product).where(Product.id.in_(ids))
                              .options(selectinload(Product.offers))).all()
        by_id = {p.id: p for p in products}
        out = []
        for pid, bid, v in cached:
            p = by_id.get(pid)
            if not p or p.retired_at is not None:
                continue
            if is_accessory_text(p.name or "", p.category):
                continue
            guessed = classify(p.name or "")
            if guessed and guessed != c.slug:
                continue
            best = next((o for o in p.offers if o.id == bid), None) if bid else None
            if best is None:
                best = last_known_offer(p.offers)
            if best is None:
                continue
            out.append((p, best, v))
        assign_display_names([p for p, _, _ in out])
        return out

    products = db.scalars(select(Product).where(
        Product.category_slug == c.slug, Product.retired_at.is_(None))
                          .options(selectinload(Product.offers).selectinload(Offer.points))).all()
    rows = []
    payload = []
    for p in products:
        if is_accessory_text(p.name or "", p.category):
            continue
        guessed = classify(p.name or "")
        if guessed and guessed != c.slug:
            continue
        buyable, v = product_summary(p)
        # Do výpisu kategorie necháme i čistě OOS (poslední cena), bez buy CTA v šabloně.
        display = buyable or last_known_offer(p.offers)
        if display is None:
            continue
        rows.append((p, display, v))
        payload.append((p.id, buyable.id if buyable else None, v))
    rows = sorted(rows, key=lambda r: (
        r[1].in_stock is False,  # OOS dolů
        float(r[1].price) if r[1] else 0,
        r[0].id,  # stejná cena nesmí mezi stránkami přeskočit ani se opakovat
    ))
    # payload order follows rows after sort
    by_pid = {pid: (pid, bid, v) for pid, bid, v in payload}
    payload = [by_pid[p.id] for p, _, _ in rows]
    catcache.set(c.slug, payload)
    assign_display_names([p for p, _, _ in rows])
    return rows


def _ve_sleve_rows(rows):
    """Filter/sort category rows for /ve-sleve without scanning the whole catalog."""
    scored = []
    for p, best, v in rows:
        if not best or best.in_stock is False or v.code != "buy" or not v.median90:
            continue
        med = float(v.median90)
        cur = float(best.price)
        if med <= 0:
            continue
        pct = (med - cur) / med * 100
        if pct < 1:
            continue
        scored.append(((p, best, v), pct))
    scored.sort(key=lambda x: (-x[1], x[0][0].id))
    return [r for r, _ in scored]


# Výpis kategorie. 24 karet je jedna obrazovka na desktopu a pořád srozumitelný seznam na mobilu.
CATEGORY_PAGE_SIZE = 24


def _category_pages(n: int) -> int:
    if n <= 0:
        return 0
    return (n + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE


def _parse_strana(raw: str | None) -> tuple[int, bool]:
    """Číslo stránky a příznak, že `strana=1` má zmizet z URL (kanonická první stránka)."""
    if raw is None or str(raw).strip() == "":
        return 1, False
    text = str(raw).strip()
    if not text.isdigit():
        raise HTTPException(404)
    n = int(text)
    if n < 1:
        raise HTTPException(404)
    return n, n == 1


def _cat_url(path: str, params: list[tuple[str, str]], page: int) -> str:
    q = list(params)
    if page > 1:
        q.append(("strana", str(page)))
    return path + (("?" + urlencode(q)) if q else "")


def _pager_numbers(page: int, pages: int) -> list[int | None]:
    """Čísla stránek; None je mezera. Do devíti stránek (sluchátka) ukážeme všechny."""
    if pages <= 9:
        return list(range(1, pages + 1))
    wanted = {1, pages, page - 1, page, page + 1}
    if page <= 3:
        wanted.update({2, 3, 4})
    if page >= pages - 2:
        wanted.update({pages - 1, pages - 2, pages - 3})
    nums = sorted(n for n in wanted if 1 <= n <= pages)
    out: list[int | None] = []
    prev = 0
    for n in nums:
        if prev and n > prev + 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


CAT_VIEWS = {
    "": ("{name} – ceny, historie a verdikt", "Porovnání cen {gen} z českých e-shopů s historií ceny a verdiktem Kupuj/Počkej. Bez placených pozic, řazeno jen podle ceny."),
    "nejlevnejsi": ("Nejlevnější {name_l} – aktuální ceny", "Nejlevnější {name_l} v českých e-shopech právě teď. Žebříček podle aktuální ceny, s historií a srovnáním slev s nejnižší cenou za 30 dní."),
    "ve-sleve": ("{name} ve slevě – jen skutečné slevy", "{name} se skutečnou slevou: cena u 90denního minima a pod obvyklou cenou. Slevy, u kterých jsme za 30 dní naměřili nižší cenu, nezařazujeme."),
}


@app.get("/kategorie", response_class=HTMLResponse)
def categories_index(request: Request, db=Depends(get_db)):
    counts = dict(db.execute(
        select(Product.category_slug, func.count(Product.id))
        .where(Product.retired_at.is_(None))
        .group_by(Product.category_slug)).all())
    crumbs = [("Lovceny", "/"), ("Kategorie", "/kategorie")]
    return tpl.TemplateResponse(request, "categories.html", {"counts": counts, "crumbs": crumbs,
                                                             "ld": [seo.breadcrumb_ld(crumbs)]})


@app.get("/kategorie/{slug}", response_class=HTMLResponse)
@app.get("/kategorie/{slug}/{view}", response_class=HTMLResponse)
def category(request: Request, slug: str, view: str = "", db=Depends(get_db),
             znacka: str = "", od: str = "", do: str = "", skladem: str = "", strana: str = ""):
    c = _cat_or_404(slug)
    if view not in CAT_VIEWS:
        raise HTTPException(404)
    rows = _cat_rows(db, c)
    if view == "ve-sleve":
        rows = _ve_sleve_rows(rows)
    else:
        rows = [r for r in rows if r[1] is not None]
    from .names import guess_brand
    brands = sorted({b for b in (guess_brand(p.name or "") for p, _, _ in rows) if b})
    brand = (znacka or "").strip()
    if brand:
        rows = [(p, o, v) for p, o, v in rows if guess_brand(p.name or "") == brand]
    def _f(s):
        try:
            return float(str(s).replace(",", ".").replace(" ", ""))
        except (TypeError, ValueError):
            return None
    pmin, pmax = _f(od), _f(do)
    if pmin is not None:
        rows = [(p, o, v) for p, o, v in rows if o and float(o.price) >= pmin]
    if pmax is not None:
        rows = [(p, o, v) for p, o, v in rows if o and float(o.price) <= pmax]
    only_stock = skladem in ("1", "ano", "true")
    if only_stock:
        rows = [(p, o, v) for p, o, v in rows if o and o.in_stock is not False]
    if view == "nejlevnejsi":
        rows = rows[:20]
    total = len(rows)
    pages = _category_pages(total)
    page, strip_strana = _parse_strana(strana)
    title_t, desc_t = CAT_VIEWS[view]
    fmt = {"name": c.name, "name_l": c.name[:1].lower() + c.name[1:], "gen": c.gen}
    path = f"/kategorie/{slug}" + (f"/{view}" if view else "")
    crumbs = [("Lovceny", "/"), (c.name, f"/kategorie/{slug}")] + ([({"nejlevnejsi": "Nejlevnější", "ve-sleve": "Ve slevě"}[view], path)] if view else [])
    priced = [float(b.price) for _, b, _ in rows if b]
    from .categories import CATEGORY_INTROS
    fq: list[tuple[str, str]] = []
    if brand:
        fq.append(("znacka", brand))
    if od.strip():
        fq.append(("od", od.strip()))
    if do.strip():
        fq.append(("do", do.strip()))
    if only_stock:
        fq.append(("skladem", "1"))
    if strip_strana:
        return RedirectResponse(_cat_url(path, fq, 1), status_code=301)
    if page > 1 and page > pages:
        raise HTTPException(404)
    start = (page - 1) * CATEGORY_PAGE_SIZE
    page_rows = rows[start:start + CATEGORY_PAGE_SIZE]
    filter_qs = ("?" + urlencode(fq)) if fq else ""
    canon_path = _cat_url(path, fq, page)
    range_from = start + 1 if total else 0
    range_to = start + len(page_rows)
    return tpl.TemplateResponse(request, "category.html", {
        "c": c, "view": view, "rows": page_rows, "crumbs": crumbs, "canonical_path": canon_path,
        "push_key": pushmod.public_key() if pushmod.enabled() else "", "email_on": digestmod.email_enabled(),
        "page_title": title_t.format(**fmt), "page_desc": desc_t.format(**fmt) + (f" Ceny od {czk(min(priced))}." if priced else ""),
        "thin_history": price_history_thin(db),
        "cat_intro": CATEGORY_INTROS.get(slug) if not view else None,
        "brands": brands, "filter_brand": brand, "filter_od": od.strip(), "filter_do": do.strip(),
        "filter_skladem": only_stock, "filter_qs": filter_qs, "filters_active": bool(fq),
        "page": page, "pages": pages, "total": total,
        "range_from": range_from, "range_to": range_to,
        "page_prev": _cat_url(path, fq, page - 1) if page > 1 else "",
        "page_next": _cat_url(path, fq, page + 1) if page < pages else "",
        "page_links": [(n, _cat_url(path, fq, n) if n else "") for n in _pager_numbers(page, pages)],
        "ld": [seo.breadcrumb_ld(crumbs), seo.itemlist_ld(page_rows, canon_path, start=start)]})


@app.get("/sitemap.xml")
def sitemap(db=Depends(get_db)):
    return Response(seo.sitemap_xml(db), media_type="application/xml")


@app.get("/robots.txt")
def robots_txt():
    return Response(seo.robots_txt(), media_type="text/plain")


# ---------- Obrázky produktů: proxy + zmenšení do WebP ----------
def _image_proxy_response(w: int, u: str):
    """Zmenšený WebP z povoleného obchodu.

    Chyba → HTTP 404 bez obrázku (private, no-store). 200 s 1×1 průhledným WebP
    nespustí onerror, takže karta vypadá prázdně. 404 ho spustí a šablona ukáže
    „fotka chybí“. Nikdy 302 na originál (Planeo/Fast JPEG bývá i >3 MB).
    """
    if w not in images.WIDTHS or not images.is_allowed(u):
        raise HTTPException(404)
    try:
        data = images.get(u, w)
    except images.ImageError as e:
        logging.getLogger("lovceny.images").info("obrázek %s (%d): %s → chyba náhledu", u[:120], w, e)
        # no-store: Cloudflare nesmí cachovat chybu (dřív se HIT-ovalo 302 na multi‑MB JPEG).
        return Response(
            b"",
            status_code=404,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
                "X-Lovceny-Img": "error",
            },
        )
    import hashlib
    return Response(data, media_type="image/webp", headers={
        "Cache-Control": "public, max-age=31536000, immutable", "ETag": '"' + hashlib.md5(data).hexdigest() + '"',
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


@app.get("/img/{w}.webp")
def image_proxy_webp(w: int, u: str = ""):
    """Stejné jako /img/{w}, cesta s příponou .webp kvůli Cloudflare free cache (extension)."""
    return _image_proxy_response(w, u)


@app.get("/img/{w:int}")
def image_proxy(w: int, u: str = ""):
    """Zpětná kompatibilita starých /img/{w}?u=… URL (stále cacheované přes Cache Rule)."""
    return _image_proxy_response(w, u)


# ---------- Stav obchodů (monitoring blokací) ----------
@app.get("/stav", response_class=HTMLResponse)
def status_page(request: Request, db=Depends(get_db)):
    from . import monitor
    hist = monitor.history(db, days=14)
    health = monitor.evaluate(db, list(hist["last"].values()))
    rows = []
    for s in SHOPS.values():
        if s.retired:
            continue
        h = health["shops"].get(s.slug)
        last = hist["last"].get(s.slug)
        if not h and not last:
            continue
        cells = []
        for d in hist["days"]:
            ok, att = hist["agg"][s.slug][d] if s.slug in hist["agg"] else (0, 0)
            cells.append({"day": d, "ok": ok, "att": att, "rate": (ok / att) if att else None})
        lo = datetime.fromisoformat(h["last_ok"]) if h and h["last_ok"] else None
        rows.append({"shop": s, "h": h, "last": last, "cells": cells, "last_ok": lo})
    rows.sort(key=lambda r: (r["h"]["ok"] if r["h"] else True, r["shop"].name))
    last_run = max((r["last"].run_at for r in rows if r["last"]), default=None)
    return tpl.TemplateResponse(request, "status.html", {
        "rows": rows, "days": hist["days"], "last_run": last_run, "noindex": True,
        "stale_days": monitor.STALE_DAYS, "min_attempts": monitor.MIN_ATTEMPTS,
        "crumbs": [("Lovceny", "/"), ("Stav obchodů", "/stav")]})


# ---------- Admin (analytika, zprávy) ----------
def require_admin(request: Request):
    import base64
    import hmac
    import os
    token = os.environ.get("ADMIN_TOKEN", "")
    if len(token) < 16:  # nenastavený / slabý token → admin neexistuje
        raise HTTPException(404)
    auth = request.headers.get("authorization", "")
    given = ""
    if auth.lower().startswith("basic "):
        try:
            given = base64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")[2]
        except ValueError:
            given = ""
    elif auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "Přihlášení vyžadováno", headers={"WWW-Authenticate": 'Basic realm="Lovceny admin", charset="UTF-8"'})
    return True


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, dni: int = 30, _=Depends(require_admin), db=Depends(get_db)):
    from .models import ContactMessage
    dni = dni if dni in (7, 30, 90) else 30
    rep = analytics.report(db, days=dni)
    msgs = db.scalars(select(ContactMessage).order_by(ContactMessage.created_at.desc()).limit(50)).all()
    return tpl.TemplateResponse(request, "admin.html", {"r": rep, "msgs": msgs, "dni": dni, "noindex": True},
                                headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


# ---------- O nás, FAQ, kontakt ----------
FAQ = [
    ("Platí vám obchody za lepší umístění?",
     "Ne. Pořadí nabídek je vždy jen podle ceny (nejlevnější nahoře). Placené pozice ani „doporučené“ obchody nemáme a mít nebudeme."),
    ("Jak vzniká verdikt Kupuj / Počkej / Za 30 dní bylo levněji?",
     "Porovnáme dnešní cenu s naměřenou historií: Kupuj, když je cena do 3 % nad minimem za 90 dní. Za 30 dní bylo levněji, když se sleva počítá z ceny o víc než 5 % vyšší, než je nejnižší cena naměřená za 30 dní před akcí (stejně jako referenční cena podle § 12a zákona o ochraně spotřebitele), nebo když cena během posledních 3 týdnů vzrostla o 12 % a víc a sleva ji vrátila zpět. U štítku vždy uvádíme naměřenou cenu a datum. Jinak Počkej. Při méně než 7 dnech dat píšeme „Sbíráme data“. Verdikt je algoritmický odhad, ne záruka. Celá metodika je na stránce Jak řadíme a hodnotíme."),
    ("Odkud berete ceny?",
     "Z veřejných stránek produktů v obchodech, které automatické čtení dovolují (respektujeme robots.txt a čteme šetrně, nejvýš jednou za pár hodin), a z oficiálních feedů obchodů. Ochranu proti botům nikdy neobcházíme – obchod, který čtení blokuje, prostě nesledujeme."),
    ("Jak často se ceny aktualizují?",
     "Třikrát denně. Stav jednotlivých obchodů a čerstvost jejich cen je veřejně na stránce Stav obchodů."),
    ("Je Lovceny zdarma?",
     "Ano, pro tebe zdarma a bez registrace. Hlídání ceny e-mailem nebo upozorněním v prohlížeči je také zdarma."),
    ("Jak budete vydělávat?",
     "Zatím nijak – affiliate odkazy ještě nejsou zapnuté. Až je spustíme, když po kliknutí do obchodu nakoupíš, obchod nám může zaplatit malou provizi. Cena pro tebe je stejná a na pořadí ani verdikt to nemá žádný vliv. Affiliate odkazy vždy označíme."),
    ("Sledujete mě? Potřebujete cookie lištu?",
     "Nesledujeme. Nepoužíváme cookies ani analytiku třetích stran, jen vlastní anonymní statistiku návštěvnosti bez osobních údajů. Proto žádnou cookie lištu nepotřebujeme."),
    ("Můžu si Lovceny dát na plochu telefonu?",
     "Ano. V Chrome/Edge na Androidu zvol „Přidat na plochu“ / „Nainstalovat aplikaci“. Na iPhonu v Safari klepni na Sdílet a pak „Přidat na plochu“ – teprve takto přidaný web může na iPhonu posílat upozornění na pokles ceny."),
]
CONTACT_TOPICS = {"chyba": "Nahlásit chybu v ceně", "obchod": "Návrh obchodu ke sledování", "spoluprace": "Spolupráce / obchod",
                  "soukromi": "Osobní údaje (GDPR)", "obchod-oprava": "Oprava/vyřazení obchodu",
                  "jine": "Něco jiného"}
_contact_rl: dict[str, list[float]] = {}


def _faq_items():
    """FAQ s odpovědí o vydělávání podle AFFILIATE_ON."""
    items = list(FAQ)
    earn_on = ("Jak budete vydělávat?",
               "Některé odkazy jsou affiliate (provizní): když po kliknutí do obchodu nakoupíš, obchod nám může zaplatit malou provizi. Cena pro tebe je stejná a na pořadí ani verdikt to nemá žádný vliv. Affiliate odkazy vždy označíme.")
    earn_off = ("Jak budete vydělávat?",
                "Zatím nijak – affiliate odkazy ještě nejsou zapnuté. Až je spustíme, když po kliknutí do obchodu nakoupíš, obchod nám může zaplatit malou provizi. Cena pro tebe je stejná a na pořadí ani verdikt to nemá žádný vliv. Affiliate odkazy vždy označíme.")
    out = []
    for q, a in items:
        if q == "Jak budete vydělávat?":
            out.append(earn_on if affiliate_on() else earn_off)
        else:
            out.append((q, a))
    return out


def _faq_ld():
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in _faq_items()]}


@app.get("/o-nas", response_class=HTMLResponse)
def about(request: Request, odeslano: int = 0, chyba: str = "", tema: str = ""):
    crumbs = [("Lovceny", "/"), ("O nás", "/o-nas")]
    faq = _faq_items()
    return tpl.TemplateResponse(request, "about.html", {
        "faq": faq, "topics": CONTACT_TOPICS, "topic": tema if tema in CONTACT_TOPICS else "", "sent": odeslano, "err": chyba[:200], "crumbs": crumbs,
        "ld": [_faq_ld(), seo.breadcrumb_ld(crumbs)]})


@app.post("/kontakt")
def contact(request: Request, message: str = Form(""), name: str = Form(""), reply_to: str = Form(""),
            topic: str = Form("jine"), website: str = Form(""), db=Depends(get_db)):
    """Kontaktní formulář → DB (čte se v /admin). Honeypot `website`, limit 5/h na IP (jen v paměti, IP neukládáme)."""
    import hashlib
    import time
    from .models import ContactMessage
    if website:  # bot vyplnil skryté pole – tváříme se, že je hotovo
        return RedirectResponse("/o-nas?odeslano=1#kontakt", 303)
    message, name, reply_to = message.strip(), name.strip()[:100], reply_to.strip()[:320]
    if not 10 <= len(message) <= 3000:
        return RedirectResponse("/o-nas?chyba=" + quote("Napiš prosím zprávu (10 až 3000 znaků).") + "#kontakt", 303)
    if reply_to and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", reply_to):
        return RedirectResponse("/o-nas?chyba=" + quote("E-mail pro odpověď nevypadá správně (nebo ho nech prázdný).") + "#kontakt", 303)
    key = hashlib.sha256(("kontakt:" + _client_ip(request)).encode()).hexdigest()
    now_m = time.monotonic()
    hits = [t for t in _contact_rl.get(key, []) if now_m - t < 3600]
    day_total = db.scalar(select(func.count(ContactMessage.id)).where(
        ContactMessage.created_at >= datetime.now(timezone.utc) - timedelta(days=1)))
    if len(hits) >= 5 or day_total >= 100:
        return RedirectResponse("/o-nas?chyba=" + quote("Zpráv je teď moc, zkus to prosím za hodinu.") + "#kontakt", 303)
    _contact_rl[key] = hits + [now_m]
    db.add(ContactMessage(name=name or None, reply_to=reply_to or None,
                          topic=topic if topic in CONTACT_TOPICS else "jine", message=message))
    from .models import ContactMessage as CM
    db.query(CM).filter(CM.created_at < datetime.now(timezone.utc) - timedelta(days=365)).delete()
    db.commit()
    return RedirectResponse("/o-nas?odeslano=1#kontakt", 303)


# ---------- Transparentnost: Jak řadíme, Pro e-shopy, Podmínky ----------
REMOVE_DAYS = 7  # závazek: oprava / vyřazení obchodu do 7 dní


def _fetch_times() -> str:
    """Časy stahování z .github/workflows/fetch.yml (UTC) převedené na pražský čas."""
    from zoneinfo import ZoneInfo
    m, hrs = 17, [5, 11, 17]
    wf = BASE.parent / ".github" / "workflows" / "fetch.yml"
    try:
        mt = re.search(r'cron:\s*"(\d+)\s+([\d,]+)\s', wf.read_text())
        if mt:
            m, hrs = int(mt.group(1)), [int(h) for h in mt.group(2).split(",")]
    except OSError:
        pass
    today = datetime.now(timezone.utc).replace(minute=m, second=0, microsecond=0)
    loc = [today.replace(hour=h).astimezone(ZoneInfo("Europe/Prague")).strftime("%-H:%M") for h in hrs]
    return ", ".join(loc)


@app.get("/jak-radime", response_class=HTMLResponse)
def ranking_page(request: Request):
    from . import verdict as vmod
    from .shops import search as smod
    crumbs = [("Lovceny", "/"), ("Jak řadíme a hodnotíme", "/jak-radime")]
    shops = sorted((s for s in SHOPS.values() if not s.retired),
                   key=lambda s: (s.status != "ok", s.name.lower()))
    return tpl.TemplateResponse(request, "jak_radime.html", {
        "crumbs": crumbs, "ld": [seo.breadcrumb_ld(crumbs)], "shops": shops, "t": vmod,
        "pct": lambda x: round(x * 100), "fetch_times": _fetch_times(), "min_age": 2,
        "live_cache_min": smod.CACHE_TTL_S // 60})


@app.get("/pro-obchody", response_class=HTMLResponse)
def for_shops_page(request: Request):
    from .shops import scrape, search as smod
    crumbs = [("Lovceny", "/"), ("Pro e-shopy", "/pro-obchody")]
    return tpl.TemplateResponse(request, "pro_obchody.html", {
        "crumbs": crumbs, "ld": [seo.breadcrumb_ld(crumbs)], "ua": scrape.USER_AGENT,
        "scrape_delay": f"{scrape.MIN_DELAY_S:.0f}", "search_delay": f"{smod.MIN_DELAY_S:.0f}",
        "remove_days": REMOVE_DAYS})


@app.get("/podminky", response_class=HTMLResponse)
def terms_page(request: Request):
    crumbs = [("Lovceny", "/"), ("Podmínky užívání", "/podminky")]
    return tpl.TemplateResponse(request, "terms.html", {"crumbs": crumbs, "ld": [seo.breadcrumb_ld(crumbs)]})


# staré / alternativní adresy → 301
for _old, _new in {"/metodika": "/jak-radime", "/bot": "/pro-obchody", "/pro-e-shopy": "/pro-obchody",
                   "/podminky-uzivani": "/podminky"}.items():
    app.add_api_route(_old, (lambda n: (lambda: RedirectResponse(n, 301)))(_new), methods=["GET"], include_in_schema=False)


# ---------- v6: Kdy nakupovat ----------
@app.get("/kdy-nakupovat", response_class=HTMLResponse)
def when_to_buy(request: Request, db=Depends(get_db)):
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Prague"))
    ps = seasons.periods(now.year)
    by_month = {m: [p for p in ps if m in p.months] for m in range(1, 13)}
    crumbs = [("Lovceny", "/"), ("Kdy nakupovat", "/kdy-nakupovat")]
    return tpl.TemplateResponse(request, "kdy_nakupovat.html", {
        "periods": ps, "by_month": by_month, "months": seasons.MONTHS, "this_month": now.month,
        "insights": seasons.insights(db), "tips": seasons.CATEGORY_TIPS, "cat_by": BY_SLUG, "t": seasons,
        "crumbs": crumbs, "ld": [seo.breadcrumb_ld(crumbs)]})


# ---------- v6: Ověř slevu ----------
from . import verify as verifymod  # noqa: E402


def _verify_share_text(r) -> str:
    n = (r.name or r.query or "produkt")[:80]
    if r.check and r.check.enough and r.check.flag:
        return f"{n}: za 30 dní bylo levněji ({czk(r.check.min30)}) – ověřeno na Lovceny"
    if r.price:
        return f"{n} za {czk(r.price)} – ověření slevy na Lovceny"
    return f"{n} – ověření slevy na Lovceny"


@app.get("/over-slevu", response_class=HTMLResponse)
def verify_page(request: Request, url: str = "", jinde: int = 0, db=Depends(get_db)):
    from urllib.parse import urlparse as _up
    url_in = url.strip()[:2000]
    ctx = {"r": None, "url_in": url_in, "error": "", "limited": False, "live_done": False,
           "crumbs": [("Lovceny", "/"), ("Ověř slevu", "/over-slevu")]}
    if not url_in:
        return tpl.TemplateResponse(request, "over_slevu.html", ctx)
    ctx["noindex"] = True
    try:
        clean = verifymod.clean_url(url_in)
    except verifymod.BadUrl as e:
        ctx["error"] = str(e)
        return tpl.TemplateResponse(request, "over_slevu.html", ctx, status_code=422)
    key_hit = verifymod.cached(verifymod.cache_key(clean, live=False))
    if key_hit is None and not verifymod.rate_ok(_client_ip(request)):
        ctx["limited"] = True
        return tpl.TemplateResponse(request, "over_slevu.html", ctx, status_code=429)
    r, _ = verifymod.verify_cached(db, clean, live=False)
    if jinde:
        verifymod._elsewhere(db, r)
        ctx["live_done"] = True
    ctx.update(r=r, url_in=r.url, host=(_up(r.url).hostname or ""),
               share_url=canonical("/over-slevu", "url=" + quote(r.url, safe="")), share_text=_verify_share_text(r))
    request.state.an = {"kind": "view", "path": "/over-slevu"}
    return tpl.TemplateResponse(request, "over_slevu.html", ctx, headers={"X-Robots-Tag": "noindex"})


@app.get("/over-slevu/jinde", response_class=HTMLResponse)
def verify_elsewhere(request: Request, url: str = "", db=Depends(get_db)):
    """Fragment „Nabídky v dalších obchodech“ (živé hledání má vlastní cache 10 min)."""
    try:
        r, _ = verifymod.verify_cached(db, url, live=False)
    except verifymod.BadUrl:
        raise HTTPException(422)
    if not r.elsewhere and r.query:
        verifymod._elsewhere(db, r)
    return tpl.TemplateResponse(request, "_over_jinde.html", {"r": r}, headers={"X-Robots-Tag": "noindex"})


# ---------- v6: veřejné API pro rozšíření prohlížeče ----------
EXT_ORIGIN_RE = re.compile(r"^(chrome-extension|moz-extension)://[a-z0-9-]{8,64}$")
API_PER_IP_HOUR = 60
API_CACHE_S = 900


def _ext_origin(request: Request) -> str:
    """CORS jen pro rozšíření. EXTENSION_ORIGINS (čárkami) = přesný seznam po zveřejnění v obchodech."""
    import os
    o = request.headers.get("origin", "")
    allowed = [x.strip() for x in os.environ.get("EXTENSION_ORIGINS", "").split(",") if x.strip()]
    if allowed:
        return o if o in allowed else ""
    return o if EXT_ORIGIN_RE.match(o) else ""


def _api_headers(request: Request, cache: bool = True) -> dict:
    h = {"Vary": "Origin", "X-Robots-Tag": "noindex",
         "Cache-Control": f"private, max-age={API_CACHE_S}" if cache else "no-store"}
    o = _ext_origin(request)
    if o:
        h.update({"Access-Control-Allow-Origin": o, "Access-Control-Allow-Methods": "GET",
                  "Access-Control-Max-Age": "86400"})
    return h


def verify_json(r) -> dict:
    lp = r.lowest_elsewhere
    v = r.verdict
    c = r.check
    out = {
        "ok": True, "status": r.status, "message": r.message, "url": r.url,
        "shop": {"slug": r.shop.slug, "name": r.shop.name, "supported": r.shop.status == "ok"} if r.shop else None,
        "name": r.name, "price": r.price, "original_price": r.original_price,
        "tracked": None, "check30": None,
        "lowest_elsewhere": {"shop": lp.shop, "shop_name": SHOPS[lp.shop].name if lp.shop in SHOPS else lp.shop,
                             "name": lp.name, "price": lp.price, "url": lp.url} if lp else None,
        "elsewhere_count": len(r.elsewhere),
        "lovceny_url": canonical("/over-slevu", "url=" + quote(r.url, safe="")),
    }
    if r.product is not None and v is not None:
        out["tracked"] = {"id": r.product.id, "url": canonical(f"/produkt/{r.product.id}-{r.product.slug}"),
                          "verdict": {"code": v.code, "label": v.headline, "reason": v.reason}}
    if c is not None and c.enough:
        out["check30"] = {"min30": c.min30, "date": c.min30_date.isoformat(), "flag": c.flag, "days": c.days,
                          "label": "Za 30 dní bylo levněji" if c.flag else ""}
    return out


@app.options("/api/v1/overit")
def api_verify_preflight(request: Request):
    return Response(status_code=204, headers=_api_headers(request))


@app.get("/api/v1/overit")
def api_verify(request: Request, url: str = "", name: str = "", price: float = 0, original: float = 0,
               db=Depends(get_db)):
    """Veřejné JSON API pro rozšíření: verdikt, kontrola 30 dní, nejnižší cena jinde.
    Rozšíření posílá název a cenu z otevřené stránky (pak stránku obchodu vůbec nestahujeme)."""
    try:
        clean = verifymod.clean_url(url)
    except verifymod.BadUrl as e:
        return JSONResponse({"ok": False, "error": str(e)}, 422, headers=_api_headers(request, cache=False))
    kw = {"live": True, "hint_name": name.strip()[:200] or None,
          "hint_price": price if 0 < price < 10_000_000 else None,
          "hint_original": original if 0 < original < 10_000_000 else None}
    if verifymod.cached(verifymod.cache_key(clean, **kw)) is None and \
            not verifymod.rate_ok(_client_ip(request), per_hour=API_PER_IP_HOUR, scope="api"):
        return JSONResponse({"ok": False, "error": "Příliš mnoho dotazů, zkus to za chvíli."}, 429,
                            headers={**_api_headers(request, cache=False), "Retry-After": "600"})
    r, hit = verifymod.verify_cached(db, clean, **kw)
    return JSONResponse({**verify_json(r), "cached": hit}, headers=_api_headers(request))


# ---------- v6: Rozšíření do prohlížeče ----------
_ext_zip: dict[str, bytes] = {}


def _ext_build():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lovceny_ext_build", BASE.parent / "extension" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.get("/rozsireni", response_class=HTMLResponse)
def extension_page(request: Request):
    b = _ext_build()
    shops = sorted({SHOPS[s].name for s in SHOPS if SHOPS[s].status == "ok"}, key=str.lower)
    crumbs = [("Lovceny", "/"), ("Rozšíření do prohlížeče", "/rozsireni")]
    return tpl.TemplateResponse(request, "rozsireni.html", {"shops": shops, "version": b.VERSION, "crumbs": crumbs,
                                                             "ld": [seo.breadcrumb_ld(crumbs)]})


@app.get("/rozsireni/lovceny-{target}.zip")
def extension_zip(target: str):
    """Zip rozšíření sestavený z repozitáře (stejný jako `python extension/build.py`)."""
    if target not in ("chrome", "firefox"):
        raise HTTPException(404)
    if target not in _ext_zip:
        b = _ext_build()
        _ext_zip[target] = b.zip_bytes(target, api=public_url())
    return Response(_ext_zip[target], media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="lovceny-{target}.zip"', "Cache-Control": "public, max-age=3600",
        "X-Robots-Tag": "noindex"})


# ---------- v6: Týdenní přehled slev ----------
from . import digest as digestmod  # noqa: E402


def _digest_ctx(request: Request, **kw) -> dict:
    return {"push_key": pushmod.public_key() if pushmod.enabled() else "", "email_on": digestmod.email_enabled(), **kw}


@app.get("/prehled", response_class=HTMLResponse)
def digest_archive(request: Request, email: str = "", db=Depends(get_db)):
    from .models import WeeklyDigest
    rows = db.scalars(select(WeeklyDigest).order_by(WeeklyDigest.year.desc(), WeeklyDigest.week.desc()).limit(104)).all()
    crumbs = [("Lovceny", "/"), ("Týdenní přehled slev", "/prehled")]
    weeks = [(d, digestmod.slug(d), digestmod.week_range(d.year, d.week), len(digestmod.items_of(d))) for d in rows]
    return tpl.TemplateResponse(request, "prehled.html", _digest_ctx(
        request, weeks=weeks, crumbs=crumbs, ld=[seo.breadcrumb_ld(crumbs)], email_msg=email[:40]))


@app.get("/prehled/{year:int}-{week:int}", response_class=HTMLResponse)
def digest_week(request: Request, year: int, week: int, db=Depends(get_db)):
    from .models import WeeklyDigest
    d = db.scalar(select(WeeklyDigest).where(WeeklyDigest.year == year, WeeklyDigest.week == week))
    if d is None:
        raise HTTPException(404)
    sl = digestmod.slug(d)
    prev = db.scalar(select(WeeklyDigest).where((WeeklyDigest.year * 100 + WeeklyDigest.week) < year * 100 + week)
                     .order_by(WeeklyDigest.year.desc(), WeeklyDigest.week.desc()).limit(1))
    nxt = db.scalar(select(WeeklyDigest).where((WeeklyDigest.year * 100 + WeeklyDigest.week) > year * 100 + week)
                    .order_by(WeeklyDigest.year, WeeklyDigest.week).limit(1))
    crumbs = [("Lovceny", "/"), ("Týdenní přehled slev", "/prehled"), (f"{week}. týden {year}", f"/prehled/{sl}")]
    items = digestmod.items_of(d)
    return tpl.TemplateResponse(request, "prehled_tyden.html", _digest_ctx(
        request, d=d, items=items, range=digestmod.week_range(year, week), crumbs=crumbs, canonical_path=f"/prehled/{sl}",
        prev=prev and digestmod.slug(prev), nxt=nxt and digestmod.slug(nxt), ld=[seo.breadcrumb_ld(crumbs)]))


@app.post("/api/push/tema")
def push_topic_subscribe(payload: dict = Body(...), db=Depends(get_db)):
    if not pushmod.enabled():
        raise HTTPException(404, "Upozornění v prohlížeči nejsou zapnutá.")
    try:
        s = pushmod.subscribe_topic(db, str(payload.get("topic") or ""), payload.get("subscription") or {})
    except pushmod.PushError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "token": s.token}


@app.post("/api/push/tema/zrusit")
def push_topic_unsubscribe(payload: dict = Body(...), db=Depends(get_db)):
    n = pushmod.unsubscribe_topic(db, token=str(payload.get("token") or ""), endpoint=str(payload.get("endpoint") or ""))
    return {"ok": True, "deleted": n}


@app.get("/prehled/odhlasit/{token}", response_class=HTMLResponse)
def digest_push_unsub_link(request: Request, token: str, db=Depends(get_db)):
    pushmod.unsubscribe_topic(db, token=token)
    return tpl.TemplateResponse(request, "message.html", {
        "title": "Týdenní přehled vypnut", "noindex": True,
        "text": "Týdenní přehled slev už do tohoto prohlížeče posílat nebudeme a odběr jsme smazali."})


@app.post("/prehled/email")
def digest_email_subscribe(request: Request, email: str = Form(""), website: str = Form(""), db=Depends(get_db)):
    if not digestmod.email_enabled():
        raise HTTPException(404)
    if website:
        return RedirectResponse("/prehled?email=odeslano#email", 303)
    email = email.strip().lower()[:320]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return RedirectResponse("/prehled?email=chyba#email", 303)
    if not verifymod.rate_ok(_client_ip(request), per_hour=5, scope="prehled-email"):
        return RedirectResponse("/prehled?email=limit#email", 303)
    digestmod.email_subscribe(db, email)
    return RedirectResponse("/prehled?email=odeslano#email", 303)


@app.get("/prehled/email/potvrdit/{token}", response_class=HTMLResponse)
def digest_email_confirm(request: Request, token: str, db=Depends(get_db)):
    from .models import DigestEmail
    e = db.scalar(select(DigestEmail).where(DigestEmail.token == token))
    if not e:
        raise HTTPException(404)
    e.confirmed = True
    db.commit()
    return tpl.TemplateResponse(request, "message.html", {"title": "Odběr potvrzen", "noindex": True,
                                "text": "Týdenní přehled slev ti budeme posílat každý pátek. Odhlásit se můžeš odkazem v každém e-mailu."})


@app.get("/prehled/email/zrusit/{token}", response_class=HTMLResponse)
def digest_email_unsub(request: Request, token: str, db=Depends(get_db)):
    from .models import DigestEmail
    e = db.scalar(select(DigestEmail).where(DigestEmail.token == token))
    if e:
        db.delete(e)
        db.commit()
    return tpl.TemplateResponse(request, "message.html", {"title": "Odhlášeno", "noindex": True,
                                "text": "Týdenní přehled ti už posílat nebudeme a tvůj e-mail jsme smazali."})


# ---------- v7: Hlídání podle rozpočtu ----------
from . import budget as budgetmod  # noqa: E402

BUDGET_PER_IP_HOUR = 10


def _budget_ctx(**kw) -> dict:
    return {"push_key": pushmod.public_key() if pushmod.enabled() else "", "email_on": digestmod.email_enabled(), **kw}


@app.get("/hlidat-rozpocet", response_class=HTMLResponse)
def budget_page(request: Request, kategorie: str = "", hledat: str = "", max: str = "", email: str = "",
                category: str = "", keyword: str = "", max_price: str = "", db=Depends(get_db)):
    kategorie, hledat, max = kategorie or category, hledat or keyword, max or max_price
    cat = BY_SLUG.get(kategorie)
    kw = budgetmod.clean_keyword(hledat)
    try:
        mp = float(max.replace(" ", "").replace("\u00a0", "")) if max else None
    except ValueError:
        mp = None
    preview = None
    if cat and mp and budgetmod.MIN_PRICE <= mp <= budgetmod.MAX_PRICE:
        preview = budgetmod.matches(db, cat.slug, kw, mp, fresh_only=False)
    crumbs = [("Lovceny", "/"), ("Hlídání podle rozpočtu", "/hlidat-rozpocet")]
    return tpl.TemplateResponse(request, "budget.html", _budget_ctx(
        cat=cat, keyword=kw or "", max_price=int(mp) if mp else "", preview=preview, crumbs=crumbs,
        ld=[seo.breadcrumb_ld(crumbs)], email_msg=email[:20], canonical_path="/hlidat-rozpocet",
        noindex=bool(request.url.query and not email), limits={"day": budgetmod.MAX_PER_DAY, "per": budgetmod.MAX_PER_ENDPOINT}))


@app.post("/api/rozpocet")
def budget_subscribe(request: Request, payload: dict = Body(...), db=Depends(get_db)):
    if not pushmod.enabled():
        raise HTTPException(404, "Upozornění v prohlížeči nejsou zapnutá.")
    if not verifymod.rate_ok(_client_ip(request), per_hour=BUDGET_PER_IP_HOUR, scope="rozpocet"):
        raise HTTPException(429, "Moc pokusů za hodinu. Zkus to prosím později.")
    try:
        b = budgetmod.subscribe_push(db, str(payload.get("category") or ""), str(payload.get("keyword") or ""),
                                     payload.get("max_price"), payload.get("subscription") or {},
                                     bool(payload.get("only_in_stock")))
    except budgetmod.BudgetError as e:
        raise HTTPException(422, str(e))
    now_in = len(budgetmod.matches(db, b.category_slug, b.keyword, float(b.max_price), b.only_in_stock, fresh_only=False))
    return {"ok": True, "token": b.token, "manage_url": f"/hlidat-rozpocet/{b.token}", "label": budgetmod.describe(b),
            "now_matching": now_in}


@app.post("/api/rozpocet/moje")
def budget_mine(request: Request, payload: dict = Body(...), db=Depends(get_db)):
    if not verifymod.rate_ok(_client_ip(request), per_hour=120, scope="rozpocet-moje"):
        raise HTTPException(429, "Moc pokusů za hodinu.")
    endpoint = str(payload.get("endpoint") or "")
    if not pushmod.valid_endpoint(endpoint):
        return {"items": []}
    return {"items": [{"token": b.token, "label": budgetmod.describe(b), "manage_url": f"/hlidat-rozpocet/{b.token}"}
                      for b in budgetmod.by_endpoint(db, endpoint)]}


@app.post("/api/rozpocet/zrusit")
def budget_unsubscribe_api(payload: dict = Body(...), db=Depends(get_db)):
    return {"ok": True, "deleted": budgetmod.delete(db, str(payload.get("token") or ""))}


@app.post("/hlidat-rozpocet/email")
def budget_email_subscribe(request: Request, category: str = Form(""), keyword: str = Form(""), max_price: str = Form(""),
                           email: str = Form(""), only_in_stock: str = Form(""), website: str = Form(""),
                           db=Depends(get_db)):
    if not digestmod.email_enabled():
        raise HTTPException(404)
    back = f"/hlidat-rozpocet?kategorie={quote(category)}&email="
    if website:
        return RedirectResponse(back + "odeslano#nastavit", 303)
    email = email.strip().lower()[:320]
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return RedirectResponse(back + "chyba#nastavit", 303)
    if not verifymod.rate_ok(_client_ip(request), per_hour=5, scope="rozpocet-email"):
        return RedirectResponse(back + "limit#nastavit", 303)
    try:
        budgetmod.subscribe_email(db, category, keyword, max_price.replace(" ", ""), email, bool(only_in_stock))
    except budgetmod.BudgetError:
        return RedirectResponse(back + "chyba#nastavit", 303)
    return RedirectResponse(back + "odeslano#nastavit", 303)


def _budget_or_404(db, token: str):
    from .models import BudgetWatch
    b = db.scalar(select(BudgetWatch).where(BudgetWatch.token == token)) if token else None
    if not b:
        raise HTTPException(404)
    return b


@app.get("/hlidat-rozpocet/potvrdit/{token}", response_class=HTMLResponse)
def budget_confirm(request: Request, token: str, db=Depends(get_db)):
    b = _budget_or_404(db, token)
    b.confirmed = True
    db.commit()
    return RedirectResponse(f"/hlidat-rozpocet/{b.token}?potvrzeno=1", 303)


@app.get("/hlidat-rozpocet/zrusit/{token}", response_class=HTMLResponse)
def budget_unsubscribe_link(request: Request, token: str, db=Depends(get_db)):
    budgetmod.delete(db, token)
    return tpl.TemplateResponse(request, "message.html", {
        "title": "Hlídání rozpočtu zrušeno", "noindex": True,
        "text": "Hlídání jsme zrušili a smazali i s odběrem. Nové si můžeš kdykoli nastavit na stránce Hlídání podle rozpočtu."})


@app.get("/hlidat-rozpocet/{token}", response_class=HTMLResponse)
def budget_manage(request: Request, token: str, potvrzeno: int = 0, zmeneno: int = 0, chyba: str = "", db=Depends(get_db)):
    b = _budget_or_404(db, token)
    found = budgetmod.matches(db, b.category_slug, b.keyword, float(b.max_price), b.only_in_stock, fresh_only=False)
    return tpl.TemplateResponse(request, "budget_manage.html", _budget_ctx(
        b=b, cat=BY_SLUG.get(b.category_slug), label=budgetmod.describe(b), found=found, noindex=True,
        potvrzeno=potvrzeno, zmeneno=zmeneno, chyba=chyba[:80]))


@app.post("/hlidat-rozpocet/{token}/upravit")
def budget_edit(token: str, max_price: str = Form(""), db=Depends(get_db)):
    b = _budget_or_404(db, token)
    try:
        budgetmod.update(db, b, max_price.replace(" ", ""))
    except budgetmod.BudgetError as e:
        return RedirectResponse(f"/hlidat-rozpocet/{token}?chyba={quote(str(e))}", 303)
    return RedirectResponse(f"/hlidat-rozpocet/{token}?zmeneno=1", 303)


@app.post("/hlidat-rozpocet/{token}/zrusit")
def budget_delete(token: str, db=Depends(get_db)):
    budgetmod.delete(db, token)
    return RedirectResponse("/hlidat-rozpocet/zrusit/-", 303)


# ---------- v7: Black Friday ----------
from . import blackfriday as bfmod  # noqa: E402

BF_FAQ = [
    ("Co říká § 12a zákona o ochraně spotřebitele?",
     "Když obchod oznamuje slevu, musí jako původní cenu uvést nejnižší cenu, za kterou zboží nabízel v posledních 30 dnech před slevou. Sleva se tedy má počítat z této ceny, ne z ceny, která platila jen krátce nebo kdysi."),
    ("Jak poznáte, že sleva na Black Friday je opravdu výhodná?",
     "Porovnáme dnešní cenu s cenami, které jsme v obchodech sami naměřili: s nejnižší cenou za 30 dní, s minimem a obvyklou cenou za 90 dní a s nejnižší cenou za celou dobu sledování. Nehodnotíme obchody, jen ukazujeme čísla a data."),
    ("Co znamená štítek „Za 30 dní bylo levněji“?",
     "Že jsme za 30 dní před akcí naměřili nižší cenu, než ze které obchod slevu počítá, nebo že cena krátce před slevou vzrostla. U štítku vždy uvádíme naměřenou cenu a datum. Obchod mohl mít jiné ceny v kanálech, které nevidíme (např. v aplikaci nebo na prodejně)."),
    ("Kdy je Black Friday 2026?",
     "Black Friday připadá na pátek 27. listopadu 2026, Cyber Monday na pondělí 30. listopadu 2026. Mnoho obchodů ale začíná s akcemi už začátkem listopadu."),
]


@app.get("/black-friday", response_class=HTMLResponse)
def black_friday(request: Request, kategorie: str = "", obchod: str = "", db=Depends(get_db)):
    kategorie = kategorie if kategorie in BY_SLUG else ""
    obchod = obchod if obchod in SHOPS else ""
    s = bfmod.season()
    bundle = bfmod.listing(db)
    all_rows = bundle.rows
    drops = bundle.drops
    if kategorie:
        drops = [d for d in drops if d.product.category_slug == kategorie]
    if obchod:
        drops = [d for d in drops if d.offer.shop == obchod]
    real_groups = bfmod.group_by_category(drops, only_slug=kategorie)
    rows = [r for r in all_rows if (not kategorie or r.product.category_slug == kategorie) and (not obchod or r.best.shop == obchod)]
    cats = sorted({r.product.category_slug for r in all_rows if r.product.category_slug}, key=lambda c: [x.slug for x in CATEGORIES].index(c))
    shops = sorted({r.best.shop for r in all_rows}, key=lambda x: SHOPS[x].name if x in SHOPS else x)
    crumbs = [("Lovceny", "/"), (f"Black Friday {s.year}", "/black-friday")]
    ld = [seo.breadcrumb_ld(crumbs),
          {"@context": "https://schema.org", "@type": "FAQPage",
           "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in BF_FAQ]}]
    if rows:
        ld.append({"@context": "https://schema.org", "@type": "ItemList", "url": seo._abs("/black-friday"),
                   "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": seo._abs(f"/produkt/{r.product.id}-{r.product.slug}"),
                                        "name": r.product.name} for i, r in enumerate(rows[:30])]})
    return tpl.TemplateResponse(request, "black_friday.html", {
        "s": s, "today": bfmod.today_prague(), "countdown": bfmod.countdown_text(), "phase": s.phase(bfmod.today_prague()),
        "rows": rows, "sm": bfmod.summary(rows), "lowest": [r for r in rows if r.is_lowest][:6], "cats": cats, "shops": shops,
        "real_groups": real_groups,
        "kategorie": kategorie, "obchod": obchod, "crumbs": crumbs, "ld": ld, "faq": BF_FAQ, "cz_date": bfmod.cz_date,
        "canonical_path": "/black-friday", "noindex": bool(kategorie or obchod), "get_shop": get_shop,
        "push_key": pushmod.public_key() if pushmod.enabled() else "", "include_demo": bfmod_include_demo(),
        "bf_iso": datetime.combine(s.bf, datetime.min.time(), tzinfo=bfmod.PRAGUE).isoformat()})


def bfmod_include_demo() -> bool:
    from .deals import include_demo
    return include_demo()


@app.get("/black-friday/odhlasit/{token}", response_class=HTMLResponse)
def bf_push_unsub_link(request: Request, token: str, db=Depends(get_db)):
    pushmod.unsubscribe_topic(db, token=token, topic=bfmod.TOPIC)
    return tpl.TemplateResponse(request, "message.html", {
        "title": "Black Friday přehled vypnut", "noindex": True,
        "text": "Black Friday přehled už do tohoto prohlížeče posílat nebudeme a odběr jsme smazali."})
