"""eHUB affiliate wrapping + Heureka feed matching for iStyle/Sencor."""
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, Product
from lovceny.service import apply_feed
from lovceny.shops import feed, get_shop
from lovceny.shops.ehub import ehub_aid, ehub_click_url
from lovceny.util import slugify

FIXTURE = Path(__file__).parent / "fixtures" / "ehub_heureka_mini.xml"


def test_ehub_aid_default_and_empty(monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    assert ehub_aid() == "667205fc"
    monkeypatch.setenv("EHUB_AID", "")
    assert ehub_aid() == ""
    monkeypatch.setenv("EHUB_AID", "  abc  ")
    assert ehub_aid() == "abc"


def test_ehub_click_url_encoding(monkeypatch):
    monkeypatch.setenv("EHUB_AID", "667205fc")
    dest = "https://istyle.cz/products/iphone?x=1&y=2"
    u = ehub_click_url("92ecd9a4", dest)
    p = urlparse(u)
    assert p.netloc == "ehub.cz"
    assert p.path.endswith("click.php")
    q = parse_qs(p.query)
    assert q["a_aid"] == ["667205fc"]
    assert q["a_bid"] == ["92ecd9a4"]
    assert unquote(q["desturl"][0]) == dest


def test_shop_affiliate_link_ehub(monkeypatch):
    monkeypatch.setenv("EHUB_AID", "667205fc")
    istyle = get_shop("istyle")
    sencor = get_shop("sencor")
    smarty = get_shop("smarty")
    url = "https://istyle.cz/products/iphone-15"
    wrapped = istyle.affiliate_link(url)
    assert "ehub.cz" in wrapped and "a_bid=92ecd9a4" in wrapped and "desturl=" in wrapped
    assert "a_bid=67b5afc3" in sencor.affiliate_link("https://www.sencor.cz/x")
    assert "a_bid=1651aa06" in smarty.affiliate_link("https://www.smarty.cz/x")
    monkeypatch.setenv("EHUB_AID", "")
    assert istyle.affiliate_link(url) == url
    assert not istyle.affiliate_active


def test_status_labels_ehub_shops(monkeypatch):
    monkeypatch.delenv("ISTYLE_FEED_URL", raising=False)
    monkeypatch.delenv("SENCOR_FEED_URL", raising=False)
    st, label = get_shop("istyle").status_label()
    assert st == "ok" and "feed" in label
    st, label = get_shop("sencor").status_label()
    assert st == "ok" and "feed" in label
    st, label = get_shop("smarty").status_label()
    assert st == "ok" and "affiliate" in label.lower()


def test_parse_stream_and_strip_tracking():
    items = feed.load(str(FIXTURE))
    assert len(items) == 2
    assert items[0].item_id == "IS1" and items[0].price == 19990
    assert "utm_source" not in items[0].url
    assert items[0].url.startswith("https://istyle.cz/")
    assert items[1].ean == "8590669123456" and items[1].in_stock is True


def test_names_match_conservative():
    assert feed.names_match("Sencor SVC 7811WH", "Sencor SVC 7811WH bílý")
    assert not feed.names_match("Sencor", "Sencor SVC 7811WH")
    assert not feed.names_match("Apple iPhone 15", "Apple iPhone 14")


def test_apply_feed_attach_by_ean_and_create():
    init_db()
    db = SessionLocal()
    # existující produkt s EAN z iStyle feedu, bez istyle nabídky
    p = Product(name="Apple iPhone 15 128GB černá", slug=slugify("iphone-ehub"), ean="0195949035838")
    db.add(p)
    db.commit()
    items = feed.load(str(FIXTURE))
    st = apply_feed(db, "istyle", [items[0]])
    assert st["attached"] == 1 and st["matched"] == 0
    o = db.query(Offer).filter(Offer.shop == "istyle").one()
    assert float(o.price) == 19990 and o.product_id == p.id
    # druhé volání = match URL
    st2 = apply_feed(db, "istyle", [items[0]])
    assert st2["matched"] == 1 and st2["attached"] == 0
    # Sencor create_missing
    st3 = apply_feed(db, "sencor", [items[1]], create_missing=True, max_create=5)
    assert st3["created"] == 1
    assert db.query(Product).filter(Product.ean == "8590669123456").count() == 1
    db.close()


def test_obchody_shows_ehub_partners(client_db):
    c, db, make = client_db
    html = c.get("/obchody").text
    assert "iStyle" in html and "Sencor" in html and "Smarty" in html
    assert "oficiální feed" in html
    assert "affiliate odkazy" in html  # Smarty zdroj
