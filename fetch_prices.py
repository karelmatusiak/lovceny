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
from lovceny.service import update_offer, apply_feed, check_watches, cleanup_watches, scrub_outlier_price_points
from lovceny.notify import get_sender

log = logging.getLogger("fetch")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--shop")
    ap.add_argument("--feed", action="append", default=[], help="shop=URL_nebo_soubor")
    ap.add_argument("--min-age-hours", type=float, default=2, help="přeskočit nedávno kontrolované nabídky")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--health-out", help="zapsat vyhodnocení zdraví obchodů (JSON) – čte ho shop_health_issues.py")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    db = SessionLocal()
    report = Counter()
    from lovceny import monitor
    stats = monitor.FetchStats()

    # 1) feedy (oficiální zdroj – Alza, Datart…)
    feeds = dict(f.split("=", 1) for f in a.feed)
    for s in SHOPS.values():
        if s.source == "feed" and s.feed_url and s.slug not in feeds:
            feeds[s.slug] = s.feed_url
    for slug, src in feeds.items():
        if a.shop and slug != a.shop:
            continue
        try:
            shop = SHOPS.get(slug)
            if shop and src == shop.feed_url:
                items = feedmod.load_shop(shop)
            else:
                items = feedmod.load(src)
            st = apply_feed(db, slug, items)
            log.info("feed %s: %s", slug, st)
            report[f"{slug}:feed_ok"] += st["matched"]
            stats.add(slug, "ok", 200)
        except Exception as e:
            db.rollback()
            log.error("feed %s selhal: %s", slug, e)
            report[f"{slug}:feed_error"] += 1
            stats.add(slug, "errors", getattr(getattr(e, "response", None), "status_code", None), str(e))
    for s in SHOPS.values():
        if s.source == "feed" and not s.feed_url and s.slug not in feeds:
            log.info("%s: feed URL není nastaven (%s) – nabídky zůstávají 'cena bude doplněna'", s.name, s.feed_url_env)

    # 2) přímé čtení stránek (jen obchody, které to dovolují)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.min_age_hours)
    q = select(Offer).where(Offer.shop.in_([s.slug for s in SHOPS.values()
                                              if s.source == "scrape" and not s.retired]))
    if a.shop:
        q = q.where(Offer.shop == a.shop)
    for o in db.scalars(q.order_by(Offer.last_checked.asc().nullsfirst()).limit(a.limit)).all():
        if o.status == "unavailable":
            # Vyřazené produkty nezkoušíme znovu (šetří kvótu a nekazí zdraví obchodu).
            report[f"{o.shop}:skipped_unavailable"] += 1
            stats.add(o.shop, "skipped")
            continue
        lc = o.last_checked
        if lc and (lc if lc.tzinfo else lc.replace(tzinfo=timezone.utc)) > cutoff:
            report[f"{o.shop}:skipped_recent"] += 1
            stats.add(o.shop, "skipped")
            continue
        st = update_offer(db, o, stats=stats)
        db.commit()
        report[f"{o.shop}:{st}"] += 1
        log.info("%-8s %-7s %s %s", o.shop, st, o.price, o.url if st == "ok" else o.last_error)

    # 2b) monitoring blokací: statistiky běhu do DB + vyhodnocení (→ GitHub issue ve workflow)
    try:
        rows = stats.save(db)
        health = monitor.evaluate(db, rows)
        bad = {k: [r["code"] for r in v["reasons"]] for k, v in health["shops"].items() if not v["ok"]}
        log.info("Zdraví obchodů: %s", bad or "vše v pořádku")
        if a.health_out:
            import json
            with open(a.health_out, "w", encoding="utf-8") as f:
                json.dump(health, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001 – monitoring nesmí shodit stahování
        db.rollback()
        log.error("Monitoring obchodů selhal: %s", e)

    # 3) hlídání cen – po každém stažení; bez duplicit (viz check_watches)
    log.info("E-maily přes: %s", get_sender().name)
    try:
        sent = check_watches(db)
        log.info("Hlídání cen: odesláno %d upozornění", sent)
    except Exception as e:  # noqa: BLE001 – upozornění nesmí shodit souhrn stahování
        db.rollback()
        log.error("Hlídání cen selhalo: %s", e)
    # 3b) upozornění v prohlížeči (Web Push) – jen když jsou nastavené VAPID klíče
    from lovceny import push
    if push.enabled():
        try:
            log.info("Web Push: %s", push.check_push(db))
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.error("Web Push selhal: %s", e)
        log.info("Úklid push odběrů (24 měsíců): %d", push.cleanup_push(db))
    else:
        log.info("Web Push vypnutý (chybí VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY)")
    # 3c) v7: hlídání podle rozpočtu (push; e-mail jen s EMAIL_BACKEND) + Black Friday přehled (jen v BF/CM)
    from lovceny import budget, blackfriday
    try:
        log.info("Hlídání rozpočtu: %s", budget.check_budgets(db))
        log.info("Úklid hlídání rozpočtu: %d", budget.cleanup(db))
    except Exception as e:  # noqa: BLE001 – nesmí shodit souhrn stahování
        db.rollback()
        log.error("Hlídání rozpočtu selhalo: %s", e)
    try:
        log.info("Black Friday přehled: %s", blackfriday.run_digest(db))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.error("Black Friday přehled selhal: %s", e)
    log.info("Úklid hlídání (24 měsíců / nepotvrzená / zrušená): %s", cleanup_watches(db))
    try:
        log.info("Úklid cenových glitchů: %d", scrub_outlier_price_points(db))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.error("Úklid cenových glitchů selhal: %s", e)
    try:
        from lovceny.catalog_repair import repair_catalog
        log.info("Úklid katalogu: %s", {k: v for k, v in repair_catalog(db).items() if k != "samples"})
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.error("Úklid katalogu selhal: %s", e)
    try:
        from lovceny import blackfriday, catcache, deals, hero
        from lovceny.main import clear_display_names_cache
        catcache.clear()
        deals.clear_cache()
        blackfriday.clear_cache()
        hero.clear_cache()
        clear_display_names_cache()
        log.info("Vyprázdněny cache kategorií, slev, Black Friday, hero a display names")
    except Exception as e:  # noqa: BLE001
        log.error("Vyprázdnění cache selhalo: %s", e)
    log.info("SOUHRN: %s", dict(report))
    db.close()
    return 1 if report and all(k.endswith(("error", "blocked")) for k in report) else 0


if __name__ == "__main__":
    sys.exit(main())
