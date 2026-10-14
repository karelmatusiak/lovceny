import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_

from .db import SessionLocal, init_db
from .models import Product, Offer, PricePoint, Watch, PushSubscription
from .categories import CATEGORIES, BY_SLUG
from . import push as pushmod
from . import seo
from .web import static_url, canonical, public_url, CacheHeadersMiddleware, add_compression
from .shops import SHOPS, get_shop
from .service import add_by_url, create_watch, search_db, offers_for_query, smart_offers
from .verdict import compute
from .deals import real_deals
from .chart import build_chart, verdict_note
from .util import czk

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
app = FastAPI(title="Lovceny")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.filters["czk"] = czk
tpl.env.globals["SHOPS"] = SHOPS
tpl.env.globals["static"] = static_url
tpl.env.globals["canonical"] = canonical
tpl.env.globals["public_url"] = public_url
tpl.env.globals["CATEGORIES"] = CATEGORIES
tpl.env.filters["tojson_ld"] = seo.jsonld
app.add_middleware(CacheHeadersMiddleware)
add_compression(app)


@app.on_event("startup")
def _startup():
    init_db()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def product_summary(p: Product):
    priced = [o for o in p.offers if o.price is not None]
    best = min(priced, key=lambda o: o.price, default=None)
    pts = [pt for o in p.offers for pt in o.points]
    v = compute(pts, float(best.price) if best else None,
                float(best.original_price) if best and best.original_price else None)
    return best, v


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db=Depends(get_db)):
    products = db.scalars(select(Product).order_by(Product.created_at.desc()).limit(6)).all()
    cards = [(p, *product_summary(p)) for p in products]
    deals = real_deals(db, top=3)["buys"]
    return tpl.TemplateResponse(request, "index.html", {"cards": cards, "deals": deals})


@app.get("/slevy", response_class=HTMLResponse)
def deals_page(request: Request, db=Depends(get_db)):
    return tpl.TemplateResponse(request, "deals.html", real_deals(db))


@app.get("/ochrana-osobnich-udaju", response_class=HTMLResponse)
def privacy(request: Request):
    return tpl.TemplateResponse(request, "privacy.html", {})


@app.get("/cookies", response_class=HTMLResponse)
def cookies(request: Request):
    return tpl.TemplateResponse(request, "cookies.html", {})


@app.get("/hledat", response_class=HTMLResponse)
def search(request: Request, q: str = "", zive: int = 0, db=Depends(get_db)):
    q = q.strip()
    if not q:
        return RedirectResponse("/", 303)
    if re.match(r"^(https?://|www\.)", q, re.I):
        url = q if q.lower().startswith("http") else "https://" + q
        prod, msg = add_by_url(db, url)
        if prod is None:
            return tpl.TemplateResponse(request, "search.html", {"q": q, "results": [], "error": msg, "noindex": True}, status_code=422)
        r = f"/produkt/{prod.id}-{prod.slug}"
        return RedirectResponse(r + (f"?msg={msg}" if msg else ""), 303)
    results = [(p, *product_summary(p)) for p in search_db(db, q)]
    # Málo shod v DB → živé hledání v obchodech. Bez JS (?zive=1) se načte rovnou, jinak přes fetch.
    want_live = len(results) < LIVE_THRESHOLD
    ctx = {"q": q, "results": results, "error": None, "want_live": want_live, "noindex": True}
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
        return RedirectResponse(f"/porovnani?pridat={prod.id}", 303)
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


@app.get("/produkt/{pid:int}-{slug}", response_class=HTMLResponse)
@app.get("/produkt/{pid:int}", response_class=HTMLResponse)
def product(request: Request, pid: int, slug: str = "", msg: str = "", ok: str = "", db=Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404)
    best, v = product_summary(p)
    offers = sorted(p.offers, key=lambda o: (o.price is None, float(o.price or 0)))
    pts = [pt for o in p.offers for pt in o.points]
    ranges = build_chart(pts, verdict=v)
    cat = BY_SLUG.get(p.category_slug or "")
    crumbs = [("Lovceny", "/")] + ([(cat.name, f"/kategorie/{cat.slug}")] if cat else []) + [(p.name, f"/produkt/{p.id}-{p.slug}")]
    has_demo = any(pt.is_demo for pt in pts)
    return tpl.TemplateResponse(request, "product.html", {
        "p": p, "best": best, "v": v, "offers": offers, "chart_ranges": ranges, "chart_note": verdict_note(v, ranges),
        "push_key": pushmod.public_key() if pushmod.enabled() else "", "cat": cat, "crumbs": crumbs,
        "canonical_path": f"/produkt/{p.id}-{p.slug}",
        "ld": [seo.product_ld(p, offers, v), seo.breadcrumb_ld(crumbs)],
        "has_demo": has_demo, "msg": msg, "ok": ok, "get_shop": get_shop})


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
    return tpl.TemplateResponse(request, "shops.html", {})


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


# ---------- porovnání ----------
COMPARE_MAX = 4
COMPARE_COLORS = ["#0B6E4F", "#FF7A45", "#2B5BD7", "#B3261E"]


def parse_ids(raw: str) -> list[int]:
    out = []
    for part in re.split(r"[,\s]+", raw or ""):
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out[:COMPARE_MAX]


@app.get("/porovnani", response_class=HTMLResponse)
def compare(request: Request, ids: str = "", pridat: str = "", db=Depends(get_db)):
    from .verdict import daily_best
    wanted = parse_ids(ids)
    if pridat.isdigit() and int(pridat) not in wanted and len(wanted) < COMPARE_MAX:
        wanted.append(int(pridat))
    items, datasets = [], []
    since = datetime.now(timezone.utc) - timedelta(days=365)
    for pid in wanted:
        p = db.get(Product, pid)
        if not p:
            continue
        best, v = product_summary(p)
        offers = sorted(p.offers, key=lambda o: (o.price is None, float(o.price or 0)))
        color = COMPARE_COLORS[len(items) % len(COMPARE_COLORS)]
        series = daily_best([pt for o in p.offers for pt in o.points], since)
        datasets.append({"label": p.name[:40], "color": color,
                         "data": [{"x": d.isoformat(), "y": round(val, 2)} for d, val in series.items()]})
        items.append({"p": p, "best": best, "v": v, "offers": offers, "color": color,
                      "best_shop": get_shop(best.shop) if best else None})
    return tpl.TemplateResponse(request, "compare.html", {
        "items": items, "ids": ",".join(str(i["p"].id) for i in items), "pridat": pridat,
        "chart_json": json.dumps({"datasets": datasets}), "get_shop": get_shop, "max": COMPARE_MAX, "noindex": True})


# ---------- Web Push ----------
@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE / "static" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/"})


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
    from sqlalchemy.orm import selectinload
    products = db.scalars(select(Product).where(Product.category_slug == c.slug)
                          .options(selectinload(Product.offers).selectinload(Offer.points))).all()
    rows = [(p, *product_summary(p)) for p in products]
    return sorted(rows, key=lambda r: (r[1] is None, float(r[1].price) if r[1] else 0))


CAT_VIEWS = {
    "": ("{name} – ceny, historie a verdikt", "Porovnání cen {gen} z českých e-shopů s historií ceny a verdiktem Kupuj/Počkej. Bez placených pozic, řazeno jen podle ceny."),
    "nejlevnejsi": ("Nejlevnější {name_l} – aktuální ceny", "Nejlevnější {name_l} v českých e-shopech právě teď. Žebříček podle skutečné ceny, s historií a upozorněním na falešné slevy."),
    "ve-sleve": ("{name} ve slevě – jen skutečné slevy", "{name} se skutečnou slevou: cena u 90denního minima a pod obvyklou cenou. Falešné slevy vyřazujeme."),
}


@app.get("/kategorie", response_class=HTMLResponse)
def categories_index(request: Request, db=Depends(get_db)):
    counts = dict(db.execute(select(Product.category_slug, func.count(Product.id)).group_by(Product.category_slug)).all())
    crumbs = [("Lovceny", "/"), ("Kategorie", "/kategorie")]
    return tpl.TemplateResponse(request, "categories.html", {"counts": counts, "crumbs": crumbs,
                                                             "ld": [seo.breadcrumb_ld(crumbs)]})


@app.get("/kategorie/{slug}", response_class=HTMLResponse)
@app.get("/kategorie/{slug}/{view}", response_class=HTMLResponse)
def category(request: Request, slug: str, view: str = "", db=Depends(get_db)):
    c = _cat_or_404(slug)
    if view not in CAT_VIEWS:
        raise HTTPException(404)
    rows = _cat_rows(db, c)
    if view == "ve-sleve":
        from .deals import product_rows
        ids = {p.id for p, _, _ in rows}
        deal = {r.product.id: r for r in product_rows(db) if r.verdict.code == "buy" and r.product.id in ids and r.savings_pct >= 1}
        rows = sorted([r for r in rows if r[0].id in deal], key=lambda r: -deal[r[0].id].savings_pct)
    else:
        rows = [r for r in rows if r[1] is not None] + ([] if view else [r for r in rows if r[1] is None])
    if view == "nejlevnejsi":
        rows = rows[:20]
    t, d = CAT_VIEWS[view]
    fmt = {"name": c.name, "name_l": c.name[:1].lower() + c.name[1:], "gen": c.gen}
    path = f"/kategorie/{slug}" + (f"/{view}" if view else "")
    crumbs = [("Lovceny", "/"), (c.name, f"/kategorie/{slug}")] + ([({"nejlevnejsi": "Nejlevnější", "ve-sleve": "Ve slevě"}[view], path)] if view else [])
    priced = [float(b.price) for _, b, _ in rows if b]
    return tpl.TemplateResponse(request, "category.html", {
        "c": c, "view": view, "rows": rows, "crumbs": crumbs, "canonical_path": path,
        "page_title": t.format(**fmt), "page_desc": d.format(**fmt) + (f" Ceny od {czk(min(priced))}." if priced else ""),
        "ld": [seo.breadcrumb_ld(crumbs), seo.itemlist_ld(rows, path)]})


@app.get("/sitemap.xml")
def sitemap(db=Depends(get_db)):
    return Response(seo.sitemap_xml(db), media_type="application/xml")


@app.get("/robots.txt")
def robots_txt():
    return Response(seo.robots_txt(), media_type="text/plain")
