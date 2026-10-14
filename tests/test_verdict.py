from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as P

from lovceny.verdict import compute
from lovceny.shops.scrape import parse_product

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def pts(fn, days=90):
    return [P(captured_at=NOW - timedelta(days=d), price=fn(d)) for d in range(days, 0, -1)]


def test_buy():
    assert compute(pts(lambda d: 1100), 1000, now=NOW).code == "buy"


def test_wait():
    assert compute(pts(lambda d: 900 if d > 40 else 1000), 1000, now=NOW).code == "wait"


def test_fake_jump_before_sale():
    assert compute(pts(lambda d: 1200 if 3 <= d <= 18 else 1000), 1000, now=NOW).code == "fake"


def test_fake_claimed_original():
    v = compute(pts(lambda d: 1000), 990, claimed_original=1500, now=NOW)
    assert v.code == "fake" and "původní" in v.text


def test_too_little_data():
    assert compute(pts(lambda d: 1000, days=3), 1000, now=NOW).code == "none"


def test_parse_jsonld():
    html = '''<script type="application/ld+json">{"@type":"Product","name":"X","gtin":"123",
    "offers":{"@type":"Offer","availability":"https://schema.org/InStock","priceSpecification":[
    {"@type":"UnitPriceSpecification","price":2890},{"@type":"UnitPriceSpecification","priceType":"https://schema.org/StrikethroughPrice","price":3490}]}}</script>'''
    r = parse_product(html)
    assert r.price == 2890 and r.original_price == 3490 and r.in_stock and r.ean == "123"
