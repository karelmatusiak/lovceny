import json
import re

from lovceny.categories import CATEGORIES, classify


def _ld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def test_category_pages_unique_titles_and_meta(client_db):
    client, db, make = client_db
    make([12990, 12490], name="Apple iPhone 15 128GB SEO", category="telefony")
    titles, descs = set(), set()
    for c in CATEGORIES:
        for suffix in ("", "/nejlevnejsi", "/ve-sleve"):
            r = client.get(f"/kategorie/{c.slug}{suffix}")
            assert r.status_code == 200, (c.slug, suffix)
            t = re.search(r"<title>(.*?)</title>", r.text).group(1)
            d = re.search(r'<meta name="description" content="(.*?)">', r.text).group(1)
            assert t not in titles and d not in descs
            titles.add(t), descs.add(d)
            assert f'<link rel="canonical" href="https://lovceny.onrender.com/kategorie/{c.slug}{suffix}">' in r.text
            assert 'property="og:title"' in r.text and 'property="og:url"' in r.text
            types = {o["@type"] for o in _ld(r.text)}
            assert "BreadcrumbList" in types
    html = client.get("/kategorie/telefony").text
    assert "Apple iPhone 15 128GB SEO" in html and "Mobilní telefony" in html
    assert client.get("/kategorie/neexistuje").status_code == 404
    assert client.get("/kategorie/telefony/blbost").status_code == 404


def test_category_sorted_by_price_and_ve_sleve(client_db):
    client, db, make = client_db
    make([30000] * 10, name="TV drahá SEO", category="televize")
    make([9000] * 10, name="TV levná SEO", category="televize")
    make([20000] * 60 + [15000] * 3, name="TV ve slevě SEO", category="televize")
    html = client.get("/kategorie/televize/nejlevnejsi").text
    assert html.index("TV levná SEO") < html.index("TV ve slevě SEO") < html.index("TV drahá SEO")
    sale = client.get("/kategorie/televize/ve-sleve").text
    assert "TV ve slevě SEO" in sale and "TV drahá SEO" not in sale


def test_product_jsonld_and_canonical(client_db, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://lovceny.cz")
    client, db, make = client_db
    p = make([1500, 1490], name="Sony WH-1000XM5 SEO", category="sluchatka")
    html = client.get(f"/produkt/{p.id}-{p.slug}?msg=x").text
    assert f'<link rel="canonical" href="https://lovceny.cz/produkt/{p.id}-{p.slug}">' in html
    lds = {o["@type"]: o for o in _ld(html)}
    prod = lds["Product"]
    assert prod["name"] == "Sony WH-1000XM5 SEO"
    assert prod["offers"]["@type"] == "AggregateOffer" and prod["offers"]["lowPrice"] == "1490.00"
    assert prod["offers"]["priceCurrency"] == "CZK" and prod["offers"]["offerCount"] == 1
    crumbs = lds["BreadcrumbList"]["itemListElement"]
    assert [c["name"] for c in crumbs] == ["Lovceny", "Sluchátka", "Sony WH-1000XM5 SEO"]
    assert crumbs[1]["item"] == "https://lovceny.cz/kategorie/sluchatka"
    assert 'property="og:type" content="product"' in html
    assert re.search(r"<title>Sony WH-1000XM5 SEO – cena od 1\xa0490\xa0Kč", html)


def test_jsonld_escapes_script(client_db):
    client, db, make = client_db
    p = make([100], name="Zlý </script><script>alert(1)</script> výrobek")
    html = client.get(f"/produkt/{p.id}").text
    assert "<script>alert(1)" not in html


def test_sitemap_and_robots(client_db):
    client, db, make = client_db
    p = make([100], name="Sitemap test výrobek")
    sm = client.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    assert f"https://lovceny.onrender.com/produkt/{p.id}-{p.slug}" in sm.text
    assert "https://lovceny.onrender.com/kategorie/pracky/ve-sleve" in sm.text
    rb = client.get("/robots.txt").text
    assert "Sitemap: https://lovceny.onrender.com/sitemap.xml" in rb and "Disallow: /hledat" in rb and "Disallow: /api/" in rb


def test_search_and_compare_are_noindex(client_db):
    client, *_ = client_db
    assert 'name="robots" content="noindex' in client.get("/hledat?q=nic+takoveho+neexistuje").text
    assert 'name="robots" content="noindex' in client.get("/porovnani").text


def test_classify():
    assert classify("Apple iPhone 15 128GB") == "telefony"
    assert classify("3mk sklo pro iPhone 15") is None
    assert classify("Bosch WAN28263BY pračka") == "pracky"
    assert classify("DeLonghi Magnifica S ECAM") == "kavovary"
    assert classify("Canon EOS R50") == "fotoaparaty"


# ---------- výkon ----------
def test_compression_and_static_cache(client_db):
    client, *_ = client_db
    r = client.get("/kategorie", headers={"Accept-Encoding": "br"})
    assert r.headers.get("content-encoding") == "br"
    r = client.get("/kategorie", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    html = r.text
    css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', html).group(1)
    r = client.get(css)
    assert "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    assert "immutable" not in client.get("/static/style.css").headers["cache-control"]
    assert re.search(r'<script src="/static/compare\.js\?v=[0-9a-f]+" defer>', html)


def test_accessibility_basics(client_db):
    client, db, make = client_db
    p = make([1000, 990])
    for url in ["/", "/kategorie", "/kategorie/telefony", f"/produkt/{p.id}", "/slevy"]:
        html = client.get(url).text
        assert 'href="#obsah"' in html and '<main id="obsah"' in html, url
        assert '<html lang="cs">' in html
        for img in re.findall(r"<img\b[^>]*>", html):
            assert "alt=" in img, (url, img)
        for inp in re.findall(r'<input\b[^>]*type="(?:text|email|number)"[^>]*>', html):
            iid = re.search(r'id="([^"]+)"', inp)
            assert "aria-label=" in inp or (iid and f'for="{iid.group(1)}"' in html), (url, inp)
