import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, or_

from .db import SessionLocal, init_db
from .models import Product, Offer, PricePoint, Watch
from .shops import SHOPS, get_shop
from .service import add_by_url, create_watch, search_db, offers_for_query, smart_offers
from .verdict import compute
from .deals import real_deals
from .util import czk

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).parent
app = FastAPI(title="Lovceny")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")
tpl.env.filters["czk"] = czk
tpl.env.globals["SHOPS"] = SHOPS


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
            return tpl.TemplateResponse(request, "search.html", {"q": q, "results": [], "error": msg}, status_code=422)
        r = f"/produkt/{prod.id}-{prod.slug}"
        return RedirectResponse(r + (f"?msg={msg}" if msg else ""), 303)
    results = [(p, *product_summary(p)) for p in search_db(db, q)]
    # Málo shod v DB → živé hledání v obchodech. Bez JS (?zive=1) se načte rovnou, jinak přes fetch.
    want_live = len(results) < LIVE_THRESHOLD
    ctx = {"q": q, "results": results, "error": None, "want_live": want_live}
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


@app.post("/sledovat")
def track(url: str = Form(...), compare: int = Form(0), db=Depends(get_db)):
    prod, msg = add_by_url(db, url)
    if prod is None:
        raise HTTPException(422, msg)
    if compare:  # „Porovnat“ u živého výsledku: nejdřív sledovat, pak přidat do porovnání (JS sloučí s výběrem)
        return RedirectResponse(f"/porovnani?pridat={prod.id}", 303)
    r = f"/produkt/{prod.id}-{prod.slug}"
    return RedirectResponse(r + (f"?msg={quote(msg)}" if msg else ""), 303)


@app.get("/produkt/{pid:int}-{slug}", response_class=HTMLResponse)
@app.get("/produkt/{pid:int}", response_class=HTMLResponse)
def product(request: Request, pid: int, slug: str = "", msg: str = "", ok: str = "", db=Depends(get_db)):
    p = db.get(Product, pid)
    if not p:
        raise HTTPException(404)
    best, v = product_summary(p)
    offers = sorted(p.offers, key=lambda o: (o.price is None, float(o.price or 0)))
    since = datetime.now(timezone.utc) - timedelta(days=365)
    datasets, has_demo = [], False
    for o in p.offers:
        s = get_shop(o.shop)
        pts = [pt for pt in o.points if (pt.captured_at if pt.captured_at.tzinfo else pt.captured_at.replace(tzinfo=timezone.utc)) >= since]
        if not pts:
            continue
        has_demo |= any(pt.is_demo for pt in pts)
        datasets.append({"label": s.name if s else o.shop, "color": s.color if s else "#0B6E4F",
                         "data": [{"x": pt.captured_at.date().isoformat(), "y": float(pt.price), "demo": pt.is_demo}
                                  for pt in pts]})
    chart = {"datasets": datasets, "min": v.min90, "median": v.median90}
    return tpl.TemplateResponse(request, "product.html", {
        "p": p, "best": best, "v": v, "offers": offers, "chart_json": json.dumps(chart),
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
    return tpl.TemplateResponse(request, "message.html", {"title": "Hlídání ceny potvrzeno 🔔",
                                "text": f"Dáme vědět, až {w.product.name} klesne na {czk(w.target_price)}."})


@app.get("/hlidani/zrusit/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str, db=Depends(get_db)):
    w = db.scalar(select(Watch).where(Watch.token == token))
    if not w:
        raise HTTPException(404)
    w.active = False
    db.commit()
    return tpl.TemplateResponse(request, "message.html", {"title": "Odhlášeno", "text": "Hlídání ceny je vypnuté."})


@app.get("/obchody", response_class=HTMLResponse)
def shops(request: Request):
    return tpl.TemplateResponse(request, "shops.html", {})


@app.get("/healthz")
def health(db=Depends(get_db)):
    from .db import engine
    return {"ok": True, "db": engine.url.get_backend_name(), "db_host": (engine.url.host or "")[:12],
            "products": db.scalar(select(func.count(Product.id)))}


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
        "chart_json": json.dumps({"datasets": datasets}), "get_shop": get_shop, "max": COMPARE_MAX})
