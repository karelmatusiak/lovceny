#!/usr/bin/env python3
"""Aktualizace cen všech sledovaných nabídek. Spouští GitHub Actions cron (viz .github/workflows/fetch.yml).

  python fetch_prices.py                 # všechny obchody
  python fetch_prices.py --shop planeo   # jen jeden obchod
  python fetch_prices.py --feed alza=sample_feeds/heureka_sample.xml   # ruční import feedu

Mironet čteme sériově (concurrency 1), nejvýš MIRONET_MAX_PER_RUN nejstarších nabídek,
s delší pauzou v PoliteClient. Po MIRONET_BACKOFF_AFTER blokacích za sebou zbytek běhu
přeskočíme. Captcha se neobchází.
"""
import argparse
import logging
import signal
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

# 24 × ~14 s ≈ 6 min. Zbytek katalogu počká na další běh (3× denně), ať workflow
# (limit 30 min včetně testů) doběhne a Mironet nedostane desítky captcha za sebou.
MIRONET_MAX_PER_RUN = 24
MIRONET_BACKOFF_AFTER = 3


def scrape_slugs(only: str | None) -> list[str]:
    slugs = [s.slug for s in SHOPS.values() if s.source == "scrape" and not s.retired]
    if only:
        return [s for s in slugs if s == only]
    return slugs


def load_scrape_offers(db, slugs: list[str], limit: int) -> list[Offer]:
    """Nejstarší (NULL last_checked první) nabídky, bez vyřazených. limit omezí běh."""
    if not slugs or limit <= 0:
        return []
    q = (select(Offer)
         .where(Offer.shop.in_(slugs), Offer.status != "unavailable")
         .order_by(Offer.last_checked.asc().nullsfirst())
         .limit(limit))
    return list(db.scalars(q).all())


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
    recorded: list = []

    def checkpoint() -> None:
        """Přepíše statistiku tohoto běhu. Timeout (SIGTERM) nepřijde o už hotové obchody."""
        try:
            recorded[:] = stats.save(db) if stats.shops else []
            if a.health_out:
                import json
                health = monitor.evaluate(db, recorded)
                with open(a.health_out, "w", encoding="utf-8") as fh:
                    json.dump(health, fh, ensure_ascii=False, indent=1)
        except Exception as exc:  # noqa: BLE001 – zápis zdraví nesmí shodit zbytek
            db.rollback()
            log.error("Průběžný zápis zdraví selhal: %s", exc)

    def stop_early(signum, _frame) -> None:
        log.warning("Běh přerušen signálem %s, zapisuji dosavadní zdraví obchodů", signum)
        checkpoint()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_early)
    signal.signal(signal.SIGINT, stop_early)

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
            checkpoint()
        except Exception as e:
            db.rollback()
            log.error("feed %s selhal: %s", slug, e)
            report[f"{slug}:feed_error"] += 1
            stats.add(slug, "errors", getattr(getattr(e, "response", None), "status_code", None), str(e))
            checkpoint()
    for s in SHOPS.values():
        if s.source == "feed" and not s.feed_url and s.slug not in feeds:
            log.info("%s: feed URL není nastaven (%s) – nabídky zůstávají 'cena bude doplněna'", s.name, s.feed_url_env)

    # 2) přímé čtení stránek (jen obchody, které to dovolují). Sériově.
    # Mironet jde první a jen nejstarší nabídky: čerstvé nenecháme sežrat limit,
    # a po sérii blokací přestaneme (captcha se neobchází).
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.min_age_hours)
    slugs = scrape_slugs(a.shop)
    batches: list[list[Offer]] = []
    if "mironet" in slugs:
        batches.append(load_scrape_offers(db, ["mironet"], min(a.limit, MIRONET_MAX_PER_RUN)))
    others = [s for s in slugs if s != "mironet"]
    if others:
        batches.append(load_scrape_offers(db, others, a.limit))
    mironet_streak = 0
    mironet_paused = False
    for batch in batches:
        for o in batch:
            if o.status == "unavailable":
                # Vyřazené produkty nezkoušíme znovu (šetří kvótu a nekazí zdraví obchodu).
                report[f"{o.shop}:skipped_unavailable"] += 1
                stats.add(o.shop, "skipped")
                continue
            if o.shop == "mironet" and mironet_paused:
                report[f"{o.shop}:skipped_backoff"] += 1
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
            if o.shop == "mironet":
                if st == "blocked":
                    mironet_streak += 1
                    if mironet_streak >= MIRONET_BACKOFF_AFTER:
                        mironet_paused = True
                        log.warning("Mironet: %s blokací za sebou, zbytek běhu přeskakuji (captcha neobcházíme)",
                                    mironet_streak)
                elif st == "ok":
                    mironet_streak = 0

    # 2b) monitoring blokací. Feedy už jsou zapsané průběžně; tady doplníme scrapery.
    try:
        checkpoint()
        health = monitor.evaluate(db, recorded)
        bad = {k: [r["code"] for r in v["reasons"]] for k, v in health["shops"].items() if not v["ok"]}
        log.info("Zdraví obchodů: %s", bad or "vše v pořádku")
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
    # 3b) upozornění v prohlížeči. Fáze 1 je má vypnutá (lovceny.features.push_on).
    from lovceny import features, push
    if features.push_on() and push.enabled():
        try:
            log.info("Web Push: %s", push.check_push(db))
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.error("Web Push selhal: %s", e)
        log.info("Úklid push odběrů (24 měsíců): %d", push.cleanup_push(db))
    else:
        log.info("Web Push vypnutý (fáze 1, nebo chybí VAPID klíče)")
    # 3c) rozpočet je ve fázi 1 vypnutý. Black Friday stránka zůstává; push přehled ne.
    from lovceny import budget, blackfriday
    if features.budget_on():
        try:
            log.info("Hlídání rozpočtu: %s", budget.check_budgets(db))
            log.info("Úklid hlídání rozpočtu: %d", budget.cleanup(db))
        except Exception as e:  # noqa: BLE001 – nesmí shodit souhrn stahování
            db.rollback()
            log.error("Hlídání rozpočtu selhalo: %s", e)
    else:
        log.info("Hlídání rozpočtu je vypnuté (fáze 1)")
    if features.push_on():
        try:
            log.info("Black Friday přehled: %s", blackfriday.run_digest(db))
        except Exception as e:  # noqa: BLE001
            db.rollback()
            log.error("Black Friday přehled selhal: %s", e)
    else:
        log.info("Black Friday push přehled je vypnutý (fáze 1)")
    log.info("Úklid hlídání (24 měsíců / nepotvrzená / zrušená): %s", cleanup_watches(db))
    try:
        log.info("Úklid cenových glitchů: %d", scrub_outlier_price_points(db))
    except Exception as e:  # noqa: BLE001
        db.rollback()
        log.error("Úklid cenových glitchů selhal: %s", e)
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
