"""UX improve-ux-2026-11: JSON-LD, search filters, /slevy near-minima, /pro-obchody."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.models import Offer, PricePoint, Product
from lovceny.names import guess_brand


def _ld(html: str):
    import json, re
    out = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        out.append(json.loads(m.group(1)))
    return out


def test_guess_brand():
    assert guess_brand("Apple iPhone 16 Pro") == "Apple"
    assert guess_brand("Samsung Galaxy S24") == "Samsung"
    assert guess_brand("náhodný produkt XYZ") is None


def test_homepage_website_organization_ld():
    init_db()
    html = TestClient(app).get("/").text
    types = {o.get("@type") for o in _ld(html)}
    assert "WebSite" in types and "Organization" in types
    site = next(o for o in _ld(html) if o["@type"] == "WebSite")
    assert site["potentialAction"]["@type"] == "SearchAction"
    assert "{search_term_string}" in site["potentialAction"]["target"]["urlTemplate"]
    org = next(o for o in _ld(html) if o["@type"] == "Organization")
    assert org["email"] == "info@lovceny.cz"
    assert "address" not in org and "streetAddress" not in str(org)
    # no street address on homepage body (sídlo jen na /o-nas)
    low = html.lower()
    assert "ulice" not in low and "praha 1" not in low
    assert "karel.matusiak@proton.me" not in html


def test_search_visible_h1_sort_stock():
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    for name, price, stock in [
        ("Apple Test Phone A", 10000, True),
        ("Apple Test Phone B", 8000, True),
        ("Apple Test Phone C", 9000, False),
    ]:
        p = Product(name=name, slug=name.lower().replace(" ", "-"), category_slug="telefony")
        o = Offer(product=p, shop="mironet", url=f"https://www.mironet.cz/{p.slug}",
                  price=price, in_stock=stock, status="ok")
        db.add(p)
        db.flush()
        db.add(PricePoint(offer=o, price=price, captured_at=now, source="scrape"))
    db.commit()
    db.close()
    c = TestClient(app)
    html = c.get("/hledat", params={"q": "Apple Test Phone"}).text
    assert 'class="page-h1 search-h1"' in html or "Výsledky pro" in html
    assert "search-filters" in html
    assert "Jen skladem" in html
    # default cheapest first among in-stock (OOS last)
    assert html.index("Apple Test Phone B") < html.index("Apple Test Phone A")
    html2 = c.get("/hledat", params={"q": "Apple Test Phone", "razeni": "cena-desc"}).text
    assert html2.index("Apple Test Phone A") < html2.index("Apple Test Phone B")
    html3 = c.get("/hledat", params={"q": "Apple Test Phone", "skladem": "1"}).text
    assert "Apple Test Phone C" not in html3
    assert "Apple Test Phone A" in html3


def test_slevy_near_minima_fallback(monkeypatch):
    init_db()
    import lovceny.main as m
    monkeypatch.setattr(m, "real_deals", lambda db, **k: {"buys": [], "fakes": [], "include_demo": False})
    # seed a near-min product
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    p = Product(name="Near Min Mixer", slug="near-min-mixer", category_slug="vysavace")
    o = Offer(product=p, shop="mironet", url="https://www.mironet.cz/near-min-mixer",
              price=1050, status="ok", in_stock=True)
    db.add(p)
    db.flush()
    for d in range(30, 0, -1):
        db.add(PricePoint(offer=o, price=1000, captured_at=now - timedelta(days=d), source="scrape"))
    db.add(PricePoint(offer=o, price=1050, captured_at=now, source="scrape"))
    db.commit()
    db.close()
    h = TestClient(app).get("/slevy").text
    assert "Zatím tu žádné skutečné slevy" in h
    assert "Blízko 90denního minima" in h
    assert "Near Min Mixer" in h


def test_category_filters_brand_price_stock():
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    for name, price, stock in [
        ("Samsung Filter TV", 12000, True),
        ("LG Filter TV", 9000, True),
        ("Samsung Filter OOS", 11000, False),
    ]:
        p = Product(name=name, slug=name.lower().replace(" ", "-"), category_slug="televize",
                    category="Televize")
        o = Offer(product=p, shop="mironet", url=f"https://www.mironet.cz/{p.slug}",
                  price=price, in_stock=stock, status="ok")
        db.add(p)
        db.flush()
        db.add(PricePoint(offer=o, price=price, captured_at=now, source="scrape"))
    db.commit()
    db.close()
    c = TestClient(app)
    html = c.get("/kategorie/televize").text
    assert "cat-filters" in html
    assert "Samsung" in html and "LG" in html
    html_b = c.get("/kategorie/televize", params={"znacka": "Samsung"}).text
    assert "Samsung Filter TV" in html_b
    assert "LG Filter TV" not in html_b
    html_p = c.get("/kategorie/televize", params={"do": "10000"}).text
    assert "LG Filter TV" in html_p
    assert "Samsung Filter TV" not in html_p
    html_s = c.get("/kategorie/televize", params={"skladem": "1"}).text
    assert "Samsung Filter OOS" not in html_s


def test_pro_obchody_affiliate_section():
    init_db()
    html = TestClient(app).get("/pro-obchody").text
    assert "Affiliate spolupráce" in html
    assert "info@lovceny.cz" in html
    assert 'rel="sponsored"' in html or "sponsored" in html
    assert "Pořadí jen podle ceny" in html
    assert "LovcenyBot" in html
    assert "karel.matusiak@proton.me" not in html


def test_footer_no_h2_outline():
    init_db()
    html = TestClient(app).get("/").text
    foot = html.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert "<h2" not in foot
    assert 'class="foot-h"' in foot


def test_category_filter_empty_and_tabs():
    init_db()
    c = TestClient(app)
    html = c.get("/kategorie/televize", params={"znacka": "NeexistujiciZnackaXYZ"}).text
    assert "Žádný produkt neodpovídá filtrům" in html
    # tabs keep filter query
    assert 'href="/kategorie/televize?znacka=NeexistujiciZnackaXYZ"' in html or "znacka=NeexistujiciZnackaXYZ" in html
    assert "/kategorie/televize/nejlevnejsi?" in html or 'nejlevnejsi?znacka=' in html
