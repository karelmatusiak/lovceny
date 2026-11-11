"""Feedy jedou vedle sebe a scrapery mají strop, ať běh skončí před 30 minutami."""
import time

from fetch_prices import (
    FEED_WORKERS,
    SCRAPE_BUDGET_S,
    iter_feed_results,
    scrape_pause_reason,
)
from lovceny.shops.feed import FEED_TOTAL_S
from lovceny.shops.scrape import PoliteClient


def test_budgets_fit_under_the_workflow_limit():
    # Feedy se překrývají, takže do limitu jde jeden strop, ne součet obchodů.
    assert FEED_WORKERS >= 2
    assert FEED_TOTAL_S + SCRAPE_BUDGET_S < 25 * 60
    assert scrape_pause_reason("softcom", 10, 10, 3) == "errors"
    assert scrape_pause_reason("softcom", 100, 10, 0) == "shop"
    assert scrape_pause_reason("mironet", 100, 10, 0) is None
    assert scrape_pause_reason("softcom", 1, SCRAPE_BUDGET_S + 1, 0) == "overall"


def test_scrape_timeout_is_short():
    client = PoliteClient()
    try:
        assert client.client.timeout.read <= 8
        assert client.client.timeout.connect <= 5
    finally:
        client.client.close()


def test_stuck_feed_is_skipped_while_a_fast_one_finishes():
    flag = {"stop": False}

    def loader(slug, _src):
        if slug == "stuck":
            while not flag["stop"]:
                time.sleep(0.02)
            return ["late"]
        return ["ok"]

    try:
        t0 = time.perf_counter()
        rows = list(iter_feed_results(
            [("fast", "a"), ("stuck", "b")], wall_s=0.35, workers=2, loader=loader,
        ))
        elapsed = time.perf_counter() - t0
    finally:
        flag["stop"] = True
    assert elapsed < 3
    by = {slug: (items, err) for slug, items, err, _seconds in rows}
    assert by["fast"] == (["ok"], None)
    assert by["stuck"][0] is None and isinstance(by["stuck"][1], TimeoutError)
