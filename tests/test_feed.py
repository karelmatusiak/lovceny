from pathlib import Path

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, Product
from lovceny.service import add_by_url, apply_feed
from lovceny.shops import feed, get_shop, shop_for_url

SAMPLE = Path(__file__).parent.parent / "sample_feeds" / "heureka_sample.xml"


def test_parse_heureka_xml():
    items = feed.load(str(SAMPLE))
    assert len(items) == 2  # položka bez ceny přeskočena
    assert items[0].item_id == "RI045b3" and items[0].price == 18990 and items[0].ean == "0195949035838"
    assert items[1].price == 2490.0 and items[1].in_stock is False


def test_parse_csv():
    items = feed.parse_csv("ITEM_ID;PRODUCTNAME;URL;PRICE_VAT;EAN\n1;Test;https://www.datart.cz/x.html;999;123\n")
    assert items[0].price == 999 and items[0].url.endswith("x.html")


def test_paste_alza_url_is_pending_then_feed_fills_price():
    init_db()
    db = SessionLocal()
    url = "https://www.alza.cz/iphone-15-128gb-cerna-demo.htm"
    prod, msg = add_by_url(db, url, fetch_now=False)
    assert "cena bude doplněna" in msg
    o = prod.offers[0]
    assert o.shop == "alza" and o.status == "pending" and o.price is None

    stats = apply_feed(db, "alza", feed.load(str(SAMPLE)))
    assert stats == {"matched": 1, "created": 0, "skipped": 1}
    db.refresh(o)
    assert float(o.price) == 18990 and o.status == "ok" and o.external_id == "RI045b3"
    assert o.product.name == "Apple iPhone 15 128GB černá"
    assert len(o.points) == 1 and o.points[0].source == "feed" and not o.points[0].is_demo

    stats = apply_feed(db, "alza", feed.load(str(SAMPLE)), create_missing=True)
    assert stats["created"] == 1
    assert db.query(Product).filter(Product.ean == "6925281993145").count() == 1


def test_affiliate_link(monkeypatch):
    alza = get_shop("alza")
    assert alza.affiliate_link("https://www.alza.cz/x.htm") == "https://www.alza.cz/x.htm"
    monkeypatch.setenv("ALZA_AFFILIATE_ID", "12345")
    assert alza.affiliate_link("https://www.alza.cz/x.htm?a=1") == "https://www.alza.cz/x.htm?a=1&idp=12345"
    assert shop_for_url("https://m.datart.cz/foo").slug == "datart"
