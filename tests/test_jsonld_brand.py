"""Product JSON-LD: značka, GTIN a AggregateOffer tak, aby šly zvalidovat."""
import json
import re
from types import SimpleNamespace

from lovceny.models import Offer
from lovceny.names import title_brand
from lovceny.seo import product_ld, validate_product_ld


def _ld(html):
    out = []
    for raw in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        out.append(json.loads(raw))
    return out


def test_title_brand_matches_card_rules_and_does_not_guess():
    assert title_brand("Sony WH-1000XM5") == "Sony"
    assert title_brand("Originální Drátová Sluchátka Jbl Tune 500 Na Uši, Černá") == "JBL"
    assert title_brand('65" TCL 65T69C titanová') == "TCL"
    assert title_brand("Notebook Hp 17-cn Intel") == "HP"
    assert title_brand("Repasovaná sluchátka Sony WH-CH720N černá") == "Sony"
    # značka až za cizím slovem, „pro Sony“, MacBook bez Apple, neznámý výrobce
    assert title_brand("Ancillary sluchátka Sony WH-1") is None
    assert title_brand("Sluchátka pro Sony WH-CH720N") is None
    assert title_brand("MacBook Air 13") is None
    assert title_brand("pákový tlakový kávovar Srebny 20 Bar") is None
    assert title_brand("Bezdrátová Sluchátka Bluetooth 5.3 Anc Enc Černá") is None
    assert title_brand("") is None


def test_product_jsonld_brand_gtin_and_aggregate(client_db, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://lovceny.cz")
    client, db, make = client_db
    p = make([1500, 1490], name="Sony WH-1000XM5 SEO", category="sluchatka")
    p.ean = "4548736132605"
    p.image_url = "https://www.mironet.cz/Foto/wh1000.jpg"
    p.offers[0].in_stock = True
    db.add(Offer(product=p, shop="alza", url="https://www.alza.cz/sony-wh-jsonld",
                 price=1890, status="ok", in_stock=False))
    db.commit()

    html = client.get(f"/produkt/{p.id}-{p.slug}").text
    prod = next(o for o in _ld(html) if o["@type"] == "Product")
    assert prod["brand"] == {"@type": "Brand", "name": "Sony"}
    assert prod["name"] == "Sony WH-1000XM5 SEO"
    assert prod["image"] == "https://www.mironet.cz/Foto/wh1000.jpg"
    assert prod["gtin13"] == "4548736132605"
    assert "gtin" not in prod or prod.get("gtin13")
    offers = prod["offers"]
    assert offers["@type"] == "AggregateOffer"
    assert offers["priceCurrency"] == "CZK"
    assert offers["lowPrice"] == "1490.00" and offers["highPrice"] == "1890.00"
    assert offers["offerCount"] == 2
    assert offers["availability"] == "https://schema.org/InStock"
    assert [o["price"] for o in offers["offers"]] == ["1490.00", "1890.00"]
    assert offers["offers"][0]["availability"] == "https://schema.org/InStock"
    assert offers["offers"][1]["availability"] == "https://schema.org/OutOfStock"
    assert offers["offers"][0]["priceCurrency"] == "CZK"
    assert validate_product_ld(prod) == []

    # explicitní pole brand má přednost před odhadem z názvu
    forced = product_ld(SimpleNamespace(
        id=p.id, slug=p.slug, name="Něco bez známé značky", display_name=None,
        brand="Xiaomi", image_url=p.image_url, ean="12345678",
        category_slug=None, category=None,
    ), p.offers)
    assert forced["brand"]["name"] == "Xiaomi"
    assert forced["gtin8"] == "12345678"
    assert validate_product_ld(forced) == []

    # neznámá značka a neplatný kód se do JSON-LD nedávají
    unknown = make([990], name="pákový tlakový kávovar Srebny 20 Bar", category="kavovary")
    unknown.ean = "abc-12"
    unknown.image_url = "https://www.example.com/kavovar.jpg"
    db.commit()
    page = client.get(f"/produkt/{unknown.id}-{unknown.slug}").text
    data = next(o for o in _ld(page) if o["@type"] == "Product")
    assert "brand" not in data
    assert not any(k.startswith("gtin") for k in data)
    assert '"@type":"Brand"' not in page


def test_all_out_of_stock_aggregate_availability(client_db):
    _, db, make = client_db
    p = make([500], name="Bosch WAN28263BY pračka", category="pracky")
    p.image_url = "https://www.example.com/pracka.jpg"
    p.ean = "4242005538737"
    p.offers[0].in_stock = False
    db.commit()
    data = product_ld(p, p.offers)
    assert data["brand"]["name"] == "Bosch"
    assert data["offers"]["availability"] == "https://schema.org/OutOfStock"
    assert data["offers"]["lowPrice"] == data["offers"]["highPrice"] == "500.00"
    assert data["offers"]["offerCount"] == 1
    assert validate_product_ld(data) == []
