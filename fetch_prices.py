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
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
# Feedy jedou najednou. Jeden pomalý soubor (po hlavičce 200 ještě minutu a půl) nečeká,
# až doběhnou ostatní. Strop je v lovceny.shops.feed.FEED_TOTAL_S.
FEED_WORKERS = 6
# Scrapery: celý blok, jeden obchod, a tři chyby za sebou (Softcom 8× ReadTimeout).
SCRAPE_BUDGET_S = 12 * 60
SHOP_BUDGET_S = 90
MIRONET_BUDGET_S = 180
SHOP_FAIL_STREAK = 3


def load_feed_items(slug: str, src: str):
    shop = SHOPS.get(slug)
    if shop and src == shop.feed_url:
        return feedmod.load_shop(shop)
    return feedmod.load(src)


def iter_feed_results(jobs, *, wall_s: float | None = None, workers: int = FEED_WORKERS,
                      loader=load_feed_items):
    """Stáhne feedy vedle sebe. Obchod, který se nevejde do wall_s, se přeskočí.

    Vrací (slug, položky nebo None, chyba nebo None, sekundy). Zápis do databáze
    zůstává ve volajícím vlákně.
    """
    from lovceny.shops.feed import FEED_TOTAL_S
    if wall_s is None:
        wall_s = FEED_TOTAL_S
    if not jobs:
        return
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feed")
    futs = {}
    starts = {}
    try:
        for slug, src in jobs:
            fut = pool.submit(loader, slug, src)
            futs[fut] = slug
            starts[fut] = time.monotonic()
        pending = set(futs)
        while pending:
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            now = time.monotonic()
            for fut in done:
                seconds = now - starts[fut]
                exc = fut.exception()
                if exc is not None:
                    yield futs[fut], None, exc, seconds
                else:
                    yield futs[fut], fut.result(), None, seconds
            for fut in list(pending):
                if now - starts[fut] > wall_s:
                    pending.discard(fut)
                    yield futs[fut], None, TimeoutError(f"feed přes {wall_s:.0f} s"), now - starts[fut]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def scrape_pause_reason(shop: str, shop_elapsed: float, all_elapsed: float, fail_streak: int) -> str | None:
    """None = tahle nabídka se ještě stáhne. Jinak důvod, proč obchod nebo běh skončil."""
    if all_elapsed > SCRAPE_BUDGET_S:
        return "overall"
    limit = MIRONET_BUDGET_S if shop == "mironet" else SHOP_BUDGET_S
    if shop_elapsed > limit:
        return "shop"
    if fail_streak >= SHOP_FAIL_STREAK:
        return "errors"
    return None


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
    jobs = [(slug, src) for slug, src in feeds.items() if not a.shop or slug == a.shop]
    for slug, items, err, seconds in iter_feed_results(jobs):
        if err is not None:
            db.rollback()
            log.error("feed %s přeskočen po %.0fs: %s", slug, seconds, err)
            report[f"{slug}:feed_error"] += 1
            stats.add(slug, "errors", None, str(err))
            checkpoint()
            continue
        try:
            t0 = time.perf_counter()
            st = apply_feed(db, slug, items)
            log.info("feed %s: stažení %.0fs, zápis %.1fs, %s", slug, seconds, time.perf_counter() - t0, st)
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

    # Kryty a pouzdra, které už v DB visí na sluchátkách nebo telefonech.
    try:
        from lovceny.service import withhold_leaked_accessory_offers
        removed = withhold_leaked_accessory_offers(db)
        log.info("Příslušenství schováno: %s", removed or "nic")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.error("Úklid příslušenství selhal: %s", exc)

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
    scrape_started = time.monotonic()
    shop_started: dict[str, float] = {}
    fail_streak: Counter = Counter()
    paused: set[str] = set()
    scrape_stopped = False
    for batch in batches:
        for o in batch:
            if scrape_stopped:
                break
            if o.status == "unavailable":
                # Vyřazené produkty nezkoušíme znovu (šetří kvótu a nekazí zdraví obchodu).
                report[f"{o.shop}:skipped_unavailable"] += 1
                stats.add(o.shop, "skipped")
                continue
            if o.shop == "mironet" and mironet_paused:
                report[f"{o.shop}:skipped_backoff"] += 1
                stats.add(o.shop, "skipped")
                continue
            if o.shop in paused:
                report[f"{o.shop}:skipped_budget"] += 1
                stats.add(o.shop, "skipped")
                continue
            lc = o.last_checked
            if lc and (lc if lc.tzinfo else lc.replace(tzinfo=timezone.utc)) > cutoff:
                report[f"{o.shop}:skipped_recent"] += 1
                stats.add(o.shop, "skipped")
                continue
            shop_started.setdefault(o.shop, time.monotonic())
            reason = scrape_pause_reason(
                o.shop, time.monotonic() - shop_started[o.shop],
                time.monotonic() - scrape_started, fail_streak[o.shop],
            )
            if reason == "overall":
                log.warning("Scrapery dosáhly %s s, zbytek obchodů čeká na další běh", SCRAPE_BUDGET_S)
                scrape_stopped = True
                checkpoint()
                break
            if reason in ("shop", "errors"):
                paused.add(o.shop)
                log.warning("Obchod %s končí (%s), další nabídky se přeskočí", o.shop, reason)
                report[f"{o.shop}:skipped_budget"] += 1
                stats.add(o.shop, "skipped")
                continue
            st = update_offer(db, o, stats=stats)
            db.commit()
            report[f"{o.shop}:{st}"] += 1
            log.info("%-8s %-7s %s %s", o.shop, st, o.price, o.url if st == "ok" else o.last_error)
            if st == "ok":
                fail_streak[o.shop] = 0
            elif st in ("error", "blocked"):
                fail_streak[o.shop] += 1
            if o.shop == "mironet":
                if st == "blocked":
                    mironet_streak += 1
                    if mironet_streak >= MIRONET_BACKOFF_AFTER:
                        mironet_paused = True
                        log.warning("Mironet: %s blokací za sebou, zbytek běhu přeskakuji (captcha neobcházíme)",
                                    mironet_streak)
                elif st == "ok":
                    mironet_streak = 0
        if scrape_stopped:
            break

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
