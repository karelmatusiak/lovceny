#!/usr/bin/env python3
"""Aktualizace cen všech sledovaných nabídek. Spouští GitHub Actions cron (viz .github/workflows/fetch.yml).

  python fetch_prices.py                 # všechny obchody
  python fetch_prices.py --shop planeo   # jen jeden obchod
  python fetch_prices.py --feed alza=sample_feeds/heureka_sample.xml   # ruční import feedu
"""
import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer
from lovceny.shops import SHOPS
from lovceny.shops import feed as feedmod
from lovceny.service import update_offer, apply_feed, check_watches

log = logging.getLogger("fetch")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shop")
    ap.add_argument("--feed", action="append", default=[], help="shop=URL_nebo_soubor")
    ap.add_argument("--min-age-hours", type=float, default=2, help="přeskočit nedávno kontrolované nabídky")
    ap.add_argument("--limit", type=int, default=500)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    db = SessionLocal()
    report = Counter()

    # 1) feedy (oficiální zdroj – Alza, Datart…)
    feeds = dict(f.split("=", 1) for f in a.feed)
    for s in SHOPS.values():
        if s.source == "feed" and s.feed_url and s.slug not in feeds:
            feeds[s.slug] = s.feed_url
    for slug, src in feeds.items():
        if a.shop and slug != a.shop:
            continue
        try:
            st = apply_feed(db, slug, feedmod.load(src))
            log.info("feed %s: %s", slug, st)
            report[f"{slug}:feed_ok"] += st["matched"]
        except Exception as e:
            db.rollback()
            log.error("feed %s selhal: %s", slug, e)
            report[f"{slug}:feed_error"] += 1
    for s in SHOPS.values():
        if s.source == "feed" and not s.feed_url and s.slug not in feeds:
            log.info("%s: feed URL není nastaven (%s) – nabídky zůstávají 'cena bude doplněna'", s.name, s.feed_url_env)

    # 2) přímé čtení stránek (jen obchody, které to dovolují)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.min_age_hours)
    q = select(Offer).where(Offer.shop.in_([s.slug for s in SHOPS.values() if s.source == "scrape"]))
    if a.shop:
        q = q.where(Offer.shop == a.shop)
    for o in db.scalars(q.order_by(Offer.last_checked.asc().nullsfirst()).limit(a.limit)).all():
        lc = o.last_checked
        if lc and (lc if lc.tzinfo else lc.replace(tzinfo=timezone.utc)) > cutoff:
            report[f"{o.shop}:skipped_recent"] += 1
            continue
        st = update_offer(db, o)
        db.commit()
        report[f"{o.shop}:{st}"] += 1
        log.info("%-8s %-7s %s %s", o.shop, st, o.price, o.url if st == "ok" else o.last_error)

    sent = check_watches(db)
    log.info("Hlídání cen: odesláno %d upozornění", sent)
    log.info("SOUHRN: %s", dict(report))
    db.close()
    return 1 if report and all(k.endswith(("error", "blocked")) for k in report) else 0


if __name__ == "__main__":
    sys.exit(main())
