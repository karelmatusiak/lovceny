"""Vstupní stránky: jen dostatek zboží skladem, kanonické URL, 301 pod prahem."""
import json
import re
from datetime import datetime, timezone

from lovceny.landings import MIN_ITEMS
from lovceny.staticsite import build, security_headers


WHEN = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)


def _stock(offer, price=None):
    offer.in_stock = True
    offer.status = "ok"
    offer.last_checked = WHEN
    offer.last_ok_at = WHEN
    if price is not None:
        offer.price = price


def _ld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def test_series_and_price_caps_need_real_stock(client_db):
    client, db, make = client_db
    cheap = make([19990], name="Apple iPhone 16 128GB modry", category="telefony")
    mid = make([21990], name="Apple iPhone 16 256GB cerny", category="telefony")
    pro = make([27990], name="Apple iPhone 16 Pro 128GB", category="telefony")
    other = make([18990], name="Apple iPhone 16e 128GB", category="telefony")
    hidden = make([15990], name="Apple iPhone 16 skryty", category="telefony")
    hidden.catalog_hidden = True
    oos = make([14990], name="Apple iPhone 16 vyprodany", category="telefony")
    oos.offers[0].in_stock = False
    oos.offers[0].last_checked = WHEN
    for p in (cheap, mid, pro, other):
        _stock(p.offers[0])
    make([2490], name="AirPods 4", category="sluchatka")
    make([5490], name="AirPods Pro 2", category="sluchatka")
    vacs = []
    for i, price in enumerate((4200, 4500, 4800), start=1):
        p = make([price], name=f"Rowenta Compact {i} vysavac", category="vysavace")
        _stock(p.offers[0])
        vacs.append(p)
    dear = make([8000], name="Rowenta Compact 9 vysavac", category="vysavace")
    _stock(dear.offers[0])
    sold = make([2000], name="Rowenta Compact vyprodany vysavac", category="vysavace")
    sold.offers[0].in_stock = False
    sold.offers[0].last_checked = WHEN
    bag = make([300], name="Sacek do vysavace Rowenta", category="vysavace")
    _stock(bag.offers[0])
    gig = make([1000], name="Gigabyte vysavac V11", category="vysavace")
    _stock(gig.offers[0])
    db.commit()
    assert MIN_ITEMS == 3

    html = client.get("/nejlevnejsi/iphone-16").text.replace("\xa0", " ")
    assert "Nejlevnější iPhone 16" in html
    assert "3 produkty skladem" in html and "19 990 Kč" in html and "27 990 Kč" in html
    assert "12. 11. 2026" in html
    assert html.index("128GB modry") < html.index("256GB cerny") < html.index("16 Pro")
    assert "16e" not in html and "skryty" not in html and "vyprodany" not in html
    assert 'rel="canonical" href="https://lovceny.cz/nejlevnejsi/iphone-16"' in html
    assert 'property="og:title"' in html and 'property="og:url" content="https://lovceny.cz/nejlevnejsi/iphone-16"' in html
    assert "SearchAction" not in html
    assert 'rel="nofollow' in html and "btn-shop" in html
    items = next(o for o in _ld(html) if o["@type"] == "ItemList")
    assert items["numberOfItems"] == 3
    offer = items["itemListElement"][0]["item"]["offers"]
    assert offer["priceCurrency"] == "CZK" and offer["availability"].endswith("InStock")
    assert offer["price"] == "19990.00"
    assert "– strana" not in html.split("</title>", 1)[0]

    gone = client.get("/nejlevnejsi/airpods", follow_redirects=False)
    assert gone.status_code == 301 and gone.headers["location"] == "/kategorie/sluchatka"
    assert client.get("/nejlevnejsi/neexistuje").status_code == 404

    cap = client.get("/kategorie/vysavace/do/5000").text.replace("\xa0", " ")
    assert "Vysavače do 5 000 Kč" in cap and "3 produkty skladem" in cap
    assert "Compact 9" not in cap and "vyprodany" not in cap and "Sacek" not in cap and "Gigabyte" not in cap
    assert 'rel="canonical" href="https://lovceny.cz/kategorie/vysavace/do/5000"' in cap
    wider = client.get("/kategorie/vysavace/do/10000").text.replace("\xa0", " ")
    assert "Compact 9" in wider and "4 produkty skladem" in wider
    low = client.get("/kategorie/vysavace/do/3000", follow_redirects=False)
    assert low.status_code == 301 and low.headers["location"] == "/kategorie/vysavace"
    assert client.get("/kategorie/vysavace/do/999").status_code == 404
    assert client.get("/kategorie/telefony/blbost").status_code == 404

    phones = client.get("/kategorie/telefony").text
    assert 'href="/nejlevnejsi/iphone-16"' in phones
    assert "– strana" not in phones.split("</title>", 1)[0]
    vac_page = client.get("/kategorie/vysavace", follow_redirects=False)
    assert vac_page.status_code == 301 and vac_page.headers["location"] == "/telefony"
    home = client.get("/").text
    assert "Nejvíc nabízené modely" in home
    assert 'href="/pruvodce/' in home
    assert 'href="/kategorie/vysavace/do/5000"' not in home
    assert "SearchAction" not in home

    sm = client.get("/sitemap.xml").text
    assert "/nejlevnejsi/iphone-16</loc>" in sm
    assert "/telefony" in sm
    assert "/kategorie/vysavace" not in sm
    assert "/kategorie/notebooky" not in sm
    assert "/nejlevnejsi/airpods" not in sm
    assert "/kategorie/vysavace/do/3000" not in sm


def test_static_build_keeps_redirects_headers_and_sitemap(client_db, tmp_path):
    client, db, make = client_db
    for name, price in (
        ("Apple iPhone 16 128GB", 19990),
        ("Apple iPhone 16 256GB", 22990),
        ("Apple iPhone 16 Plus 128GB", 24990),
    ):
        p = make([price], name=name, category="telefony")
        _stock(p.offers[0])
    for name in ("AirPods 4", "AirPods Pro 2"):
        p = make([3000], name=name, category="sluchatka")
        _stock(p.offers[0])
    for price in (4100, 4300, 4600):
        p = make([price], name=f"ETA vysavac {price}", category="vysavace")
        _stock(p.offers[0])
    db.commit()

    out = tmp_path / "site"
    report = build(str(out))
    page = (out / "nejlevnejsi" / "iphone-16.html").read_text()
    assert "Nejlevnější iPhone 16" in page and "SearchAction" not in page
    assert (out / "kategorie" / "vysavace" / "do" / "5000.html").is_file()
    assert (out / "black-friday" / "skutecna-sleva.html").is_file()
    assert not (out / "nejlevnejsi" / "airpods.html").exists()
    redirects = (out / "_redirects").read_text()
    assert "/nejlevnejsi/airpods /kategorie/sluchatka 301" in redirects
    assert "/kategorie/vysavace/do/10000 /kategorie/vysavace/do/5000 301" in redirects
    assert "/kategorie/vysavace/do/3000 /kategorie/vysavace 301" in redirects
    sm = (out / "sitemap.xml").read_text()
    assert "/nejlevnejsi/iphone-16</loc>" in sm and "/nejlevnejsi/airpods" not in sm
    assert (out / "_headers").read_text() == security_headers()
    assert "www.lovceny.cz" in " ".join(report["dashboard_steps"])
    assert "SearchAction" not in (out / "index.html").read_text()
    title = (out / "kategorie" / "telefony.html").read_text().split("</title>", 1)[0]
    assert "– strana" not in title
