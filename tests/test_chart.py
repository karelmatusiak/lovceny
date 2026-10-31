from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from lovceny.chart import build_chart, verdict_note
from lovceny.verdict import compute

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def pts(prices, demo=False):
    return [NS(captured_at=NOW - timedelta(days=len(prices) - 1 - i), price=p, is_demo=demo) for i, p in enumerate(prices)]


def test_ranges_stats_and_svg():
    p = pts([1200] * 60 + [1000] * 5 + [1100] * 55)  # 120 dní, minimum před ~55 dny
    r30, r90, rall = build_chart(p, now=NOW)
    assert (r30.n, r90.n, rall.n) == (31, 91, 120)
    assert r30.min == 1100 and r90.min == 1000 and rall.min == 1000
    assert rall.current == 1100 and round(rall.avg) == round((1200 * 60 + 5000 + 1100 * 55) / 120)
    assert "<svg" in r90.svg and "minimum 1\u00a0000 Kč" in r90.svg and "průměr" in r90.svg
    assert "minimum 1\u00a0100 Kč" in r30.svg and r90.svg_m.startswith("<svg")
    assert "<title" in r90.svg and "<desc" in r90.svg  # přístupnost


def test_verdict_annotation_in_svg():
    p = pts([1500] * 80 + [1000] * 10)
    v = compute(p, 1000, now=NOW)
    assert v.code == "buy"
    ranges = build_chart(p, now=NOW, verdict=v)
    assert ">Kupuj<" in ranges[1].svg
    assert verdict_note(v, ranges).startswith("Kupuj")
    p = pts([900] * 30 + [1200] * 30)
    v = compute(p, 1200, now=NOW)
    ranges = build_chart(p, now=NOW, verdict=v)
    assert v.code == "wait" and ">Počkej<" in ranges[1].svg and "Počkej" in verdict_note(v, ranges)


def test_little_data_graceful():
    r30, r90, rall = build_chart(pts([999]), now=NOW)
    assert rall.n == 1 and rall.min == 999 and rall.svg  # jeden bod nespadne
    empty = build_chart([], now=NOW)
    assert all(r.n == 0 and r.svg == "" for r in empty)


def test_flat_series_and_demo_flag():
    r = build_chart(pts([500] * 10, demo=True), now=NOW)[2]
    assert r.has_demo and "minimum 500 Kč" in r.svg


def test_product_page_renders_chart(client_db):
    client, db, make = client_db
    prod = make([1300] * 40 + [1000] * 3 + [1250] * 20)
    html = client.get(f"/produkt/{prod.id}-{prod.slug}").text
    assert 'class="chart"' in html and "30 dní" in html and "Celá historie" in html
    assert "chart.js" not in html.lower()
    assert "Minimum" in html and "Průměr" in html and "Aktuálně" in html
    empty = make([])
    html = client.get(f"/produkt/{empty.id}").text
    assert ("Historie zatím není k dispozici" in html) or ("Cena zatím není k dispozici" in html)
