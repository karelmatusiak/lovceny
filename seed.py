#!/usr/bin/env python3
"""Naplní DB katalogem populární elektroniky (seed_catalog.json, ~90 produktů z obchodů, které čtení
dovolují) a několika produkty z Planea s VYGENEROVANOU demo historií (price_points.is_demo = true).

Idempotentní: produkt se pozná podle URL nabídky; už existující se jen zařadí do kategorie, znovu se
nestahuje a demo historie se nepřidá podruhé. Ceny se stahují živě (robots.txt, ≥ 6 s mezi dotazy na doménu).

  python seed.py                  # katalog + demo produkty
  python seed.py --no-demo        # bez vygenerované demo historie
  python seed.py --category pracky --limit 5
  python seed.py --dry-run        # jen vypíše, co by přidal (nic nestahuje, nic nezapisuje)
  python seed.py --purge-demo     # smaže všechna demo data
"""
import json
from pathlib import Path
import argparse
import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from lovceny.db import SessionLocal, init_db
from lovceny.models import PricePoint, Offer
from lovceny.service import add_by_url

# (url, tvar demo historie)
PRODUCTS = [
    ("https://www.planeo.cz/apple-airpods-3-s-nabijecim-pouzdrem-magsafe", "buy"),
    ("https://www.planeo.cz/apple-iphone-15-128-gb-black", "fake"),
    ("https://www.planeo.cz/jbl-flip-6-teal", "wait"),
    ("https://www.planeo.cz/xiaomi-redmi-note-12-5g-4-128-gb-onyx-gray", "buy"),
]
# Alza/Datart: zdroj = feed (zatím nenastaven) → nabídka „cena bude doplněna“
FEED_OFFERS = [
    ("https://www.alza.cz/search.htm?exps=iPhone%2015%20128GB", 1),
    ("https://www.datart.cz/vyhledavani?q=iPhone%2015%20128GB", 1),
]


def demo_history(db, offer, shape, days=120):
    cur = float(offer.price)
    rnd = random.Random(offer.id)
    today = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    for d in range(days, 0, -1):
        t = today - timedelta(days=d)
        wave = math.sin(d / 9) * 0.015 + rnd.uniform(-0.008, 0.008)
        if shape == "buy":      # dlouho dráž, teď u minima
            f = 1.10 + 0.04 * (d / days) + wave
        elif shape == "fake":   # obvyklá cena = dnešní, 3 týdny zpět zdraženo o ~18 %
            f = (1.19 if 4 <= d <= 20 else 1.0) + wave
        else:                   # wait: před měsícem výrazně levněji
            f = (0.88 if 25 <= d <= 45 else 0.97) + wave
        db.add(PricePoint(offer=offer, price=round(cur * f, -1) - 10 if cur > 1000 else round(cur * f),
                          original_price=None, captured_at=t, is_demo=True, source="demo"))


CATALOG_FILE = Path(__file__).with_name("seed_catalog.json")


def load_catalog() -> list[dict]:
    """[{cat, name, url}] – kategorie musí existovat, URL patřit obchodu se source=scrape."""
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def _apply_meta(prod, it):
    """Název z katalogu, když obchod žádný nedal (název je jen odvozený z URL), a obrázek z výpisu obchodu."""
    from lovceny.service import _name_from_url
    from lovceny.util import slugify
    if any(prod.name == _name_from_url(o.url) for o in prod.offers):
        prod.name, prod.slug = it["name"], slugify(it["name"])
    if not prod.image_url and it.get("image"):
        prod.image_url = it["image"]
    if not prod.category_slug:
        prod.category_slug = it["cat"]


def seed_catalog(db, items, dry_run=False, fetch=True) -> dict:
    from lovceny.categories import BY_SLUG
    from lovceny.shops import shop_for_url
    stats = {"new": 0, "existing": 0, "priced": 0, "failed": 0}
    for it in items:
        assert it["cat"] in BY_SLUG, it
        shop = shop_for_url(it["url"])
        url = shop.canonical_url(it["url"]) if shop else it["url"]
        o = db.scalar(select(Offer).where(Offer.url == url))
        if o:
            stats["existing"] += 1
            _apply_meta(o.product, it)
            db.commit()
            continue
        if dry_run:
            print(f"+  [{it['cat']}] {it['name']}  {url}")
            stats["new"] += 1
            continue
        prod, msg = add_by_url(db, url, fetch_now=fetch)
        if prod is None:
            print(f"!! {it['name']}: {msg}")
            stats["failed"] += 1
            continue
        prod.category_slug = it["cat"]
        _apply_meta(prod, it)
        db.commit()
        stats["new"] += 1
        price = prod.offers[0].price
        stats["priced" if price else "failed"] += 1
        print(f"{'OK' if price else '!!'} [{it['cat']}] {prod.name} – {price or msg}")
    return stats


def retire_vestavne(db, dry_run=False) -> dict:
    """Odstraní nabídky vestavne-spotrebice.cz (ConnectTimeout z GitHub Actions) a osamělé produkty."""
    from sqlalchemy import or_
    rows = db.scalars(select(Offer).where(or_(Offer.shop == "vestavne",
                                              Offer.url.contains("vestavne-spotrebice.cz")))).all()
    stats = {"offers": 0, "products": 0}
    if dry_run:
        stats["offers"] = len(rows)
        return stats
    products = {o.product_id: o.product for o in rows}
    for o in rows:
        db.delete(o)
        stats["offers"] += 1
    db.flush()
    from lovceny.models import Product
    for pid, prod in products.items():
        db.refresh(prod)
        if not prod.offers:
            db.delete(prod)
            stats["products"] += 1
    db.commit()
    return stats


def seed_from_feeds(db, *, istyle_limit=25, sencor_limit=25, dry_run=False) -> dict:
    """Konzervativně přidá produkty z iStyle/Sencor Heureka feedů do existujících kategorií.

    Bereme jen položky, které classify() zařadí (Apple u iStyle, spotřebiče u Sencoru).
    Idempotentní přes apply_feed (URL/EAN).
    """
    from lovceny.shops import SHOPS, feed as feedmod
    from lovceny.service import apply_feed
    from lovceny.categories import classify

    stats = {}
    targets = [
        ("istyle", istyle_limit, ("telefony", "notebooky", "sluchatka", "chytre-hodinky", "fotoaparaty", "chytra-domacnost")),
        ("sencor", sencor_limit, ("vysavace", "kavovary", "televize", "chytra-domacnost")),
    ]
    for slug, limit, cats in targets:
        shop = SHOPS[slug]
        src = shop.feed_url
        if not src:
            stats[slug] = {"error": "no feed url"}
            continue
        if dry_run:
            print(f"[dry-run] {slug}: feed {src} (limit {limit}, cats {cats})")
            stats[slug] = {"dry_run": True, "limit": limit}
            continue
        try:
            items = feedmod.load(src)
        except Exception as e:  # noqa: BLE001
            print(f"!! feed {slug}: {e}")
            stats[slug] = {"error": str(e)}
            continue
        picked = []
        for it in items:
            cat = classify(it.name, it.category)
            if cat not in cats:
                continue
            # iStyle: držet se Apple / značkových produktů, ne příslušenství bez modelu
            if slug == "istyle":
                n = (it.name or "").lower()
                apple = ("iphone", "ipad", "airpods", "apple watch", "imac", "mac mini", "mac studio",
                         "macbook air", "macbook pro", "apple macbook")
                if not any(k in n for k in apple):
                    continue
            picked.append(it)
            if len(picked) >= limit:
                break
        st = apply_feed(db, slug, picked, create_missing=True, max_create=limit)
        print(f"Feed seed {slug}: loaded={len(items)} picked={len(picked)} → {st}")
        stats[slug] = {"loaded": len(items), "picked": len(picked), **st}
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-demo", action="store_true")
    ap.add_argument("--purge-demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--category")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--from-feeds", action="store_true",
                    help="doplnit katalog z iStyle/Sencor Heureka feedů (konzervativně)")
    ap.add_argument("--feed-limit", type=int, default=25,
                    help="max. nových produktů na obchod při --from-feeds")
    a = ap.parse_args(argv)
    init_db()
    db = SessionLocal()
    if a.purge_demo:
        n = db.execute(delete(PricePoint).where(PricePoint.is_demo.is_(True))).rowcount
        db.commit()
        print(f"Smazáno {n} demo bodů.")
        return
    items = [i for i in load_catalog() if not a.category or i["cat"] == a.category][:a.limit]
    print(f"Katalog: {len(items)} položek")
    st = seed_catalog(db, items, dry_run=a.dry_run)
    print(f"Katalog hotov: {st}")
    retired = retire_vestavne(db, dry_run=a.dry_run)
    print(f"Vestavné nabídky pryč: {retired}")
    if a.from_feeds:
        fs = seed_from_feeds(db, istyle_limit=a.feed_limit, sencor_limit=a.feed_limit, dry_run=a.dry_run)
        print(f"Feed seed: {fs}")
    if a.dry_run or a.category or a.limit:
        return
    products = []
    for url, shape in PRODUCTS:
        prod, msg = add_by_url(db, url)
        print(f"{'OK ' if not msg else '!! '} {prod.name if prod else url} {msg}")
        if prod is None:
            continue
        products.append(prod)
        o = prod.offers[0]
        if not a.no_demo and o.price and not db.scalar(select(PricePoint).where(PricePoint.offer_id == o.id, PricePoint.is_demo.is_(True))):
            demo_history(db, o, shape)
            db.commit()
    for url, idx in FEED_OFFERS:
        from lovceny.shops import shop_for_url
        if idx < len(products) and not db.scalar(select(Offer).where(Offer.url == url)):
            db.add(Offer(product=products[idx], shop=shop_for_url(url).slug, url=url, status="pending"))
    db.commit()
    print("Seed hotov.")


if __name__ == "__main__":
    main()
