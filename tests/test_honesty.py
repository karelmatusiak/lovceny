"""Je to skutečná sleva: jen naměřená historie, žádný odhad, vyprodané a skryté ven."""
import json
import re
from datetime import datetime, timedelta, timezone

from lovceny.chart import sparkline
from lovceny.honesty import assess
from lovceny.models import Offer, PricePoint
from lovceny.verdict import MIN_DAYS

BANNED = ("falešn", "falesn", "podvod", "klamav", "přifouk", "zdánliv")
NOW = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)


class _Pt:
    def __init__(self, price, captured_at):
        self.price = price
        self.captured_at = captured_at
        self.is_demo = False


def _pts(prices, now=NOW):
    n = len(prices)
    return [_Pt(p, now - timedelta(days=n - 1 - i)) for i, p in enumerate(prices)]


def test_assess_real_paper_flat_and_short_history():
    real = assess(_pts([10000] * 20 + [8000]), 8000, now=NOW)
    assert real.code == "skutecna" and round(real.real_pct) == 20
    assert real.min30 == 10000 and real.min90 == 10000
    assert "20 %" in real.text and "30" in real.text

    paper = assess(_pts([10000] * 20 + [14000] * 5 + [10500]), 10500, claimed_original=16000, now=NOW)
    assert paper.code == "papir" and paper.rose and paper.real_pct == 0
    assert paper.min30 == 10000
    assert "vystoupala" in paper.text

    flat = assess(_pts([5000] * 15), 5000, claimed_original=8000, now=NOW)
    assert flat.code == "bez" and flat.real_pct == 0
    assert "není aspoň o 3 %" in flat.text

    short = assess(_pts([10000] * 3 + [7000]), 7000, claimed_original=12000, now=NOW)
    assert short.code == "malo" and short.real_pct is None and short.days < MIN_DAYS
    assert "Verdikt o slevě nedáváme" in short.text
    assert "7" not in short.text.split("Verdikt")[0] or short.days < MIN_DAYS
    assert all(b not in (real.text + paper.text + flat.text + short.text).lower() for b in BANNED)


def test_sparkline_needs_two_measured_days():
    assert sparkline(_pts([1000]), now=NOW) == ""
    svg = sparkline(_pts([1000] * 5 + [800]), now=NOW)
    assert "<polyline" in svg and "script" not in svg and "800" not in svg.split("aria-label", 1)[0]


def _stamp(offer, when=None):
    offer.in_stock = True
    offer.status = "ok"
    offer.last_checked = when or datetime.now(timezone.utc)
    offer.last_ok_at = offer.last_checked


def test_page_sorts_real_discounts_and_hides_unsafe_products(client_db, monkeypatch):
    monkeypatch.delenv("LOVCENY_INCLUDE_DEMO", raising=False)
    monkeypatch.setenv("ALZA_AFFILIATE_ID", "testovaci")
    client, db, make = client_db
    now = datetime.now(timezone.utc)
    big = make([10000] * 20 + [8000], name="BF real telefon 20", category="telefony", original=12000, shop="alza")
    _stamp(big.offers[0], now)
    small = make([10000] * 20 + [9200], name="BF real telefon 8", category="telefony", original=11000)
    _stamp(small.offers[0], now)
    paper = make([10000] * 20 + [14000] * 5 + [10500], name="BF papir telefon", category="telefony", original=16000)
    _stamp(paper.offers[0], now)
    flat = make([5000] * 15, name="BF bez slevy telefon", category="telefony", original=8000)
    _stamp(flat.offers[0], now)
    short = make([10000] * 3 + [7000], name="BF malo dat telefon", category="telefony", original=12000)
    _stamp(short.offers[0], now)
    quiet = make([4000] * 15, name="BF tichy telefon bez originalu", category="telefony")
    _stamp(quiet.offers[0], now)
    oos = make([10000] * 12 + [5000], name="BF vyprodany telefon", category="telefony", original=9000)
    oos.offers[0].in_stock = False
    oos.offers[0].status = "ok"
    oos.offers[0].last_checked = now
    hidden = make([10000] * 12 + [6000], name="BF skryty telefon", category="telefony", original=9000)
    hidden.catalog_hidden = True
    review = make([10000] * 12 + [6000], name="BF nejisty telefon", category="telefony", original=9000)
    review.needs_review = True
    gone = make([10000] * 12 + [6000], name="Gigabyte BF telefon", category="telefony", original=9000)
    _stamp(gone.offers[0], now)
    accessory = make([10000] * 12 + [3000], name="Kryt na iPhone 16", category="telefony", original=5000)
    _stamp(accessory.offers[0], now)
    demo = make([10000] * 12 + [4000], name="BF demo telefon", category="telefony", original=9000)
    _stamp(demo.offers[0], now)
    for pt in demo.offers[0].points:
        pt.is_demo = True
    mixed = make([10000] * 12 + [9000], name="BF dostupny telefon", category="telefony", original=12000)
    _stamp(mixed.offers[0], now)
    db.add(Offer(
        product=mixed, shop="mironet", url="https://www.mironet.cz/vyprodano-1111", price=1111,
        in_stock=False, status="ok", last_checked=now, last_ok_at=now,
    ))
    stored = make([10000] * 12 + [9000], name="Apple iPhone 16 128GB overeni", category="telefony", original=12000)
    _stamp(stored.offers[0], now)
    db.add(Offer(
        product=stored, shop="mironet", url="https://www.mironet.cz/iphone-16-256gb-za-2222", price=2222,
        in_stock=True, status="ok", last_checked=now, last_ok_at=now, original_price=4000,
    ))
    db.commit()

    html = client.get("/black-friday/skutecna-sleva").text
    plain = html.replace("\xa0", " ")
    assert html.index("BF real telefon 20") < html.index("BF real telefon 8") < html.index("BF papir telefon")
    assert "BF papir telefon" in html and "Sleva jen na papíře" in html
    assert "BF bez slevy telefon" in html and "BF malo dat telefon" in html
    assert "Verdikt o slevě nedáváme" in html
    assert "BF tichy telefon bez originalu" not in html
    assert "BF vyprodany telefon" not in html and "BF skryty telefon" not in html
    assert "BF nejisty telefon" not in html and "Gigabyte" not in html and "Kryt na iPhone 16" not in html
    assert "BF demo telefon" in html and "Málo dat" in html
    assert "vyprodano-1111" not in html and "1 111" not in plain
    assert "iphone-16-256gb" not in html and "2 222" not in plain
    assert "9 000" in plain
    body = html.split('<ol class="honest-list">', 1)[1]
    row = body.split("BF dostupny telefon", 1)[1].split("<li", 1)[0]
    assert "btn-shop" in row and "nofollow" in row
    big_row = body.split("BF real telefon 20", 1)[1].split("<li", 1)[0]
    assert 'rel="nofollow sponsored noopener"' in big_row and f'/go/{big.offers[0].id}' in big_row
    hop = client.get(f"/go/{big.offers[0].id}", follow_redirects=False)
    assert hop.status_code == 302 and "idp=testovaci" in hop.headers["location"]
    assert hop.headers["cache-control"] == "no-store"
    assert "<svg" in html and "Metodika" in html and "nedoplňujeme" in html
    assert 'rel="canonical" href="https://lovceny.cz/black-friday/skutecna-sleva"' in html
    assert 'property="og:title"' in html and "Je to skutečná sleva?" in html
    assert 'property="og:description"' in html and 'property="og:url"' in html
    assert "SearchAction" not in html
    assert all(b not in html.lower() for b in BANNED)
    types = {o["@type"] for o in (json.loads(m) for m in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S))}
    assert "ItemList" in types and "BreadcrumbList" in types
    assert "/black-friday/skutecna-sleva</loc>" in client.get("/sitemap.xml").text
    assert 'href="/black-friday/skutecna-sleva"' in client.get("/black-friday").text
    assert 'href="/black-friday/skutecna-sleva"' in client.get("/o-nas").text

    product = client.get(f"/produkt/{big.id}-{big.slug}").text
    box = product.split('class="honest-box', 1)[1].split("</aside>", 1)[0]
    assert "Skutečná sleva" in box and "30" in box and 'href="/black-friday/skutecna-sleva"' in box
    short_box = client.get(f"/produkt/{short.id}-{short.slug}").text.split('class="honest-box', 1)[1].split("</aside>", 1)[0]
    assert "Málo dat" in short_box and "Verdikt o slevě nedáváme" in short_box
    assert "pod nejnižší cenou" not in short_box
    oos_html = client.get(f"/produkt/{oos.id}-{oos.slug}").text
    assert "honest-box" not in oos_html and "btn-buy" not in oos_html


def test_rise_that_beats_the_old_low_stays_a_real_discount():
    found = assess(_pts([10000] * 20 + [14000] * 4 + [8000]), 8000, now=NOW)
    assert found.code == "skutecna" and found.rose
    assert round(found.real_pct) == 20
    assert "vystoupala" in found.text
