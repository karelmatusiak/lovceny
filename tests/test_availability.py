"""Dostupnost: feed DELIVERY_DATE, schema.org, HTML, CTA / řazení nabídek."""
from types import SimpleNamespace

from lovceny.availability import (
    MAX_ORDER_DAYS,
    best_buyable_offer,
    last_known_offer,
    offer_is_buyable,
    parse_delivery_date,
    parse_html_availability,
    parse_schema_availability,
    sort_offers_for_display,
)
from lovceny.shops import feed
from lovceny.shops.scrape import parse_product


def test_parse_delivery_date_heureka_rules():
    assert parse_delivery_date(0) is True
    assert parse_delivery_date("0") is True
    assert parse_delivery_date(3) is True
    assert parse_delivery_date(str(MAX_ORDER_DAYS)) is True
    assert parse_delivery_date(MAX_ORDER_DAYS + 1) is False
    assert parse_delivery_date(-1) is False
    assert parse_delivery_date(None) is False
    assert parse_delivery_date("") is False
    assert parse_delivery_date("nope") is False


def test_parse_schema_and_html():
    assert parse_schema_availability("https://schema.org/InStock") is True
    assert parse_schema_availability("https://schema.org/PreOrder") is True
    assert parse_schema_availability("https://schema.org/OutOfStock") is False
    assert parse_schema_availability("https://schema.org/Discontinued") is False
    assert parse_schema_availability("") is None
    assert parse_html_availability("Stav: Není skladem · dodání neznámé") is False
    assert parse_html_availability("Vyprodáno do vyprodání zásob") is False
    assert parse_html_availability("Na objednávku – dodání do 5 dnů") is True
    assert parse_html_availability("Na objednávku – odešleme za 1–4 týdny") is False
    assert parse_html_availability('<strong class="dpp-stock__label--objednavka">Na objednávku</strong>') is False
    assert parse_html_availability("Skladem u dodavatele") is True


def test_offer_is_buyable_and_best():
    cheap_oos = SimpleNamespace(price=100, in_stock=False)
    mid = SimpleNamespace(price=200, in_stock=True)
    unknown = SimpleNamespace(price=150, in_stock=None)
    no_price = SimpleNamespace(price=None, in_stock=True)
    assert offer_is_buyable(cheap_oos) is False
    assert offer_is_buyable(mid) is True
    assert offer_is_buyable(unknown) is True
    assert offer_is_buyable(no_price) is False
    assert best_buyable_offer([cheap_oos, mid, unknown, no_price]) is unknown
    assert last_known_offer([cheap_oos, mid, unknown]) is cheap_oos
    ordered = sort_offers_for_display([cheap_oos, mid, unknown])
    assert [o.price for o in ordered] == [100, 150, 200]


def test_product_page_hides_cta_when_only_oos(client_db):
    client, db, make = client_db
    p = make([9990], name="TV jen OOS")
    o = p.offers[0]
    o.in_stock = False
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert "Momentálně není skladem" in html
    assert "btn-buy" not in html and "Koupit za" not in html
    assert "offer-oos" in html
    assert "Není skladem" in html
    assert "offer-oos-link" in html
    assert "btn-shop" not in html  # žádné zelené Do obchodu u OOS


def test_product_page_cta_uses_buyable_not_cheaper_oos(client_db):
    client, db, make = client_db
    p = make([15000], name="TV mix stock")
    from lovceny.models import Offer
    oos = Offer(product=p, shop="softcom", url="https://www.softcom.cz/oos-demo",
                status="ok", price=9000, in_stock=False)
    buy = Offer(product=p, shop="alza", url="https://www.alza.cz/buy-demo",
                status="ok", price=12000, in_stock=True)
    db.add_all([oos, buy])
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert "Do obchodu" in html.split("btn-primary btn-buy", 1)[1].split("</a>", 1)[0]
    assert "Alza" in html.split('class="buy-shop"', 1)[1].split("</p>", 1)[0]
    assert "Alza" in html
    # 12 000 Kč v CZ formátu
    assert "12" in html and "000" in html
    assert "offer-oos" in html
    # Levnější nabídka, která není skladem, je ve výpisu nad dražší skladem. Koupit zůstává u skladu.
    assert html.index("offer-oos") < html.index("offer-best")


def test_deals_skip_all_oos(client_db, monkeypatch):
    from lovceny import deals
    monkeypatch.setattr(deals, "include_demo", lambda: True)
    client, db, make = client_db
    p = make([5000, 4800, 4500, 4200, 4000], name="Deal OOS only")
    for o in p.offers:
        o.in_stock = False
    db.commit()
    rows = deals.product_rows(db, allow_demo=True)
    assert all(r.product.id != p.id for r in rows)


def test_feed_availability_fallback():
    xml = """<?xml version="1.0"?><SHOP>
      <SHOPITEM><ITEM_ID>1</ITEM_ID><PRODUCTNAME>A</PRODUCTNAME>
        <URL>https://www.alza.cz/a.htm</URL><PRICE_VAT>100</PRICE_VAT>
        <AVAILABILITY>1</AVAILABILITY></SHOPITEM>
      <SHOPITEM><ITEM_ID>2</ITEM_ID><PRODUCTNAME>B</PRODUCTNAME>
        <URL>https://www.alza.cz/b.htm</URL><PRICE_VAT>200</PRICE_VAT>
        <AVAILABILITY>0</AVAILABILITY></SHOPITEM>
    </SHOP>"""
    items = feed.parse_xml(xml)
    assert items[0].in_stock is True and items[1].in_stock is False


def test_scrape_jsonld_oos_and_html_fallback():
    html = """<html><script type="application/ld+json">
    {"@type":"Product","name":"X","offers":{"@type":"Offer","price":"1990",
     "priceCurrency":"CZK","availability":"https://schema.org/OutOfStock"}}
    </script></html>"""
    r = parse_product(html)
    assert r and r.price == 1990 and r.in_stock is False
    html2 = """<html><meta property="product:price:amount" content="500">
    <meta property="og:title" content="Y"><body>Stav: Vyprodáno</body></html>"""
    r2 = parse_product(html2)
    assert r2 and r2.in_stock is False
