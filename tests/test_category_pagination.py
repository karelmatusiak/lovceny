"""Stránkování kategorie: 24 na stránku, filtry a pořadí drží, špatná stránka je 404."""
import json
import re

from lovceny.models import Product


def _ids(html: str) -> list[int]:
    return [int(n) for n in re.findall(r'href="/produkt/(\d+)-', html)]


def _ld(html: str):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def _wipe(db, slug: str):
    from lovceny.catcache import clear
    db.query(Product).filter(Product.category_slug == slug).delete()
    db.commit()
    clear()


def test_209_products_are_nine_pages_without_gaps(client_db):
    c, db, make = client_db
    _wipe(db, "konzole")
    for i in range(209):
        make([1000 + i], name=f"Konzole stránka {i:03d}", shop="alza", category="konzole")
    first = c.get("/kategorie/konzole")
    assert first.status_code == 200
    assert "Zobrazeno 1–24 z 209" in first.text
    assert "produktů" in first.text
    assert 'aria-label="Stránkování"' in first.text
    assert 'href="/kategorie/konzole?strana=2"' in first.text
    assert 'rel="next"' in first.text
    assert 'rel="prev"' not in first.text
    assert 'rel="canonical" href="https://lovceny.cz/kategorie/konzole"' in first.text
    assert len(_ids(first.text)) == 24
    items = next(o for o in _ld(first.text) if o["@type"] == "ItemList")["itemListElement"]
    assert len(items) == 24 and items[0]["position"] == 1 and items[-1]["position"] == 24

    mid = c.get("/kategorie/konzole", params={"strana": 5})
    assert mid.status_code == 200
    assert "Zobrazeno 97–120 z 209" in mid.text
    assert 'rel="canonical" href="https://lovceny.cz/kategorie/konzole?strana=5"' in mid.text
    assert 'rel="prev" href="https://lovceny.cz/kategorie/konzole?strana=4"' in mid.text
    assert 'rel="next" href="https://lovceny.cz/kategorie/konzole?strana=6"' in mid.text
    assert 'aria-current="page"' in mid.text
    assert len(_ids(mid.text)) == 24

    last = c.get("/kategorie/konzole", params={"strana": 9})
    assert last.status_code == 200
    assert "Zobrazeno 193–209 z 209" in last.text
    assert len(_ids(last.text)) == 17
    assert 'rel="next"' not in last.text
    assert 'href="/kategorie/konzole?strana=8"' in last.text

    seen = []
    for n in range(1, 10):
        html = c.get("/kategorie/konzole", params={"strana": n}).text
        seen.extend(_ids(html))
    assert len(seen) == 209 and len(set(seen)) == 209
    assert seen == sorted(seen)


def test_same_price_order_is_stable_by_id(client_db):
    c, db, make = client_db
    _wipe(db, "pocitace")
    for i in range(30):
        make([4990], name=f"Počítač stejná cena {i:02d}", shop="alza", category="pocitace")
    a = _ids(c.get("/kategorie/pocitace").text)
    b = _ids(c.get("/kategorie/pocitace").text)
    page2 = _ids(c.get("/kategorie/pocitace", params={"strana": 2}).text)
    assert a == b and len(a) == 24 and len(page2) == 6
    assert a + page2 == sorted(a + page2)
    assert not set(a) & set(page2)


def test_filters_stay_on_later_pages_and_form_drops_page(client_db):
    c, db, make = client_db
    _wipe(db, "pracky")
    for i in range(30):
        make([2000 + i], name=f"Sony pračka {i:02d}", shop="alza", category="pracky")
    make([1500], name="Bosch pračka jediná", shop="alza", category="pracky")
    html = c.get("/kategorie/pracky", params={"znacka": "Sony", "od": "2000", "do": "4000", "skladem": "1", "strana": 2}).text
    assert len(_ids(html)) == 6
    assert "Bosch pračka" not in html
    assert "Sony pračka" in html
    assert 'href="/kategorie/pracky?znacka=Sony&amp;od=2000&amp;do=4000&amp;skladem=1"' in html
    assert "strana=2" in html and "strana=1" not in html
    assert 'action="/kategorie/pracky"' in html
    assert "strana" not in re.search(r'<form class="filters cat-filters"[^>]*>', html).group(0)


def test_invalid_pages_and_small_or_empty_lists(client_db):
    c, db, make = client_db
    _wipe(db, "lednice")
    assert c.get("/kategorie/lednice", params={"strana": "0"}).status_code == 404
    assert c.get("/kategorie/lednice", params={"strana": "-1"}).status_code == 404
    assert c.get("/kategorie/lednice", params={"strana": "abc"}).status_code == 404
    assert c.get("/kategorie/lednice", params={"strana": "2"}).status_code == 404
    empty = c.get("/kategorie/lednice")
    assert empty.status_code == 200 and "zatím nic nesledujeme" in empty.text
    assert "Stránkování" not in empty.text

    for i in range(3):
        make([100 + i], name=f"Malá lednice {i}", shop="alza", category="lednice")
    from lovceny.catcache import clear
    clear()
    small = c.get("/kategorie/lednice")
    assert small.status_code == 200
    assert "Zobrazeno 1–3 z 3" in small.text
    assert "Stránkování" not in small.text
    assert c.get("/kategorie/lednice", params={"strana": "2"}).status_code == 404
    one = c.get("/kategorie/lednice", params={"strana": "1"}, follow_redirects=False)
    assert one.status_code == 301
    assert one.headers["location"] == "/kategorie/lednice"

    past = c.get("/kategorie/lednice", params={"do": "50", "strana": "3"})
    assert past.status_code == 404


def test_nejlevnejsi_stays_one_page_and_cards_keep_oos_rules(client_db):
    c, db, make = client_db
    _wipe(db, "kavovary")
    for i in range(30):
        make([1000 + i], name=f"Kávovar {i:02d}", shop="alza", category="kavovary")
    oos = make([9000], name="Kávovar vyprodán", shop="alza", category="kavovary")
    oos.offers[0].in_stock = False
    db.commit()
    cheap = c.get("/kategorie/kavovary/nejlevnejsi")
    assert cheap.status_code == 200
    assert cheap.text.count('class="card pcard"') == 20
    assert "Stránkování" not in cheap.text
    assert c.get("/kategorie/kavovary/nejlevnejsi", params={"strana": 2}).status_code == 404

    last = c.get("/kategorie/kavovary", params={"strana": 2}).text
    oos_card = next(part for part in last.split('<div class="card pcard">') if "Kávovar vyprodán" in part)
    assert "Momentálně není skladem" in oos_card
    assert "btn-shop-sm" not in oos_card
    assert 'rel="nofollow noopener"' in c.get("/kategorie/kavovary").text
