#!/usr/bin/env python3
"""Naplní DB několika skutečnými produkty z Planea (ceny se stáhnou živě) a doplní
VYGENEROVANOU demo historii (price_points.is_demo = true), aby grafy něco ukazovaly.

  python seed.py            # přidá produkty + demo historii
  python seed.py --no-demo  # jen skutečné produkty
  python seed.py --purge-demo  # smaže všechna demo data
"""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-demo", action="store_true")
    ap.add_argument("--purge-demo", action="store_true")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    if a.purge_demo:
        n = db.execute(delete(PricePoint).where(PricePoint.is_demo.is_(True))).rowcount
        db.commit()
        print(f"Smazáno {n} demo bodů.")
        return
    products = []
    for url, shape in PRODUCTS:
        prod, msg = add_by_url(db, url)
        print(f"{'OK ' if not msg else '!! '} {prod.name if prod else url} {msg}")
        products.append(prod)
        o = prod.offers[0]
        if not a.no_demo and o.price and not db.scalar(select(PricePoint).where(PricePoint.offer_id == o.id, PricePoint.is_demo.is_(True))):
            demo_history(db, o, shape)
            db.commit()
    for url, idx in FEED_OFFERS:
        from lovceny.shops import shop_for_url
        if not db.scalar(select(Offer).where(Offer.url == url)):
            db.add(Offer(product=products[idx], shop=shop_for_url(url).slug, url=url, status="pending"))
    db.commit()
    print("Seed hotov.")


if __name__ == "__main__":
    main()
