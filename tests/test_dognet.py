"""Dognet obalení odkazů a feedy Electronic-star / 4Home / Electrolux / Dyson."""
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from lovceny.categories import accept_feed_item, is_accessory_text
from lovceny.db import Base, SessionLocal, engine, init_db
from lovceny.models import Offer, Product
from lovceny.service import apply_feed
from lovceny.shops import SHOPS, feed, get_shop, shop_for_url
from lovceny.shops.dognet import dognet_chid, dognet_click_url

FIX = Path(__file__).parent / "fixtures"
GOOGLE = FIX / "dognet_google_mini.xml"
OPTIMISE = FIX / "dognet_feedoptimise_mini.xml"
TD = FIX / "dognet_tradedoubler_mini.xml"


def test_dognet_chid_default_and_empty(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    assert dognet_chid() == "9CFqgvpQ"
    assert dognet_chid("shop-code") == "shop-code"
    monkeypatch.setenv("DOGNET_CHID", "")
    assert dognet_chid("9CFqgvpQ") == ""
    monkeypatch.setenv("DOGNET_CHID", "  ABC  ")
    assert dognet_chid() == "ABC"


def test_dognet_click_url_encoding(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    dest = "https://www.dyson.cz/v15-detect?x=1&y=2"
    u = dognet_click_url(dest)
    p = urlparse(u)
    assert p.scheme == "https" and p.netloc == "go.dognet.com"
    q = parse_qs(p.query)
    assert q["chid"] == ["9CFqgvpQ"]
    assert unquote(q["url"][0]) == dest
    monkeypatch.setenv("DOGNET_CHID", "")
    assert dognet_click_url(dest, "9CFqgvpQ") == dest


def test_shop_affiliate_link_dognet(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    for slug, host in (
        ("electronicstar", "electronic-star.cz"),
        ("4home", "4home.cz"),
        ("electrolux", "electrolux.cz"),
        ("dyson", "dyson.cz"),
    ):
        shop = get_shop(slug)
        assert shop is not None and shop.dognet_chid == "9CFqgvpQ"
        assert shop.source == "feed" and shop.affiliate_active
        dest = f"https://www.{host}/produkt"
        wrapped = shop.affiliate_link(dest)
        assert wrapped.startswith("https://go.dognet.com/?chid=9CFqgvpQ&url=")
        assert unquote(parse_qs(urlparse(wrapped).query)["url"][0]) == dest
    monkeypatch.setenv("DOGNET_CHID", "")
    assert not get_shop("dyson").affiliate_active
    assert get_shop("dyson").affiliate_link("https://www.dyson.cz/v15") == "https://www.dyson.cz/v15"


def test_status_and_feed_defaults(monkeypatch):
    for key in ("ELECTRONICSTAR_FEED_URL", "FOURHOME_FEED_URL", "ELECTROLUX_FEED_URL", "DYSON_FEED_URL"):
        monkeypatch.delenv(key, raising=False)
    for slug in ("electronicstar", "4home", "electrolux", "dyson"):
        st, label = get_shop(slug).status_label()
        assert st == "ok" and label == "oficiální feed", (slug, st, label)
    star = get_shop("electronicstar")
    assert star.feed_url.startswith("http://feedy.elestar.eu/data/dognet_elstar_cz.xml")
    assert any("mergado.com" in u for u in star.feed_sources())
    assert "google-products.xml" in get_shop("4home").feed_url
    assert get_shop("4home").trailing_slash
    assert "feedoptimise.com" in get_shop("electrolux").feed_url
    assert "tradedoubler.com" in get_shop("dyson").feed_url and "fid=50990" in get_shop("dyson").feed_url
    monkeypatch.setenv("ELECTRONICSTAR_FEED_URL", "")
    monkeypatch.setenv("FOURHOME_FEED_URL", "")
    monkeypatch.setenv("DYSON_FEED_URL", "")
    assert get_shop("electronicstar").feed_url.startswith("http://feedy.elestar.eu/")
    assert "4home.cz" in get_shop("4home").feed_url
    monkeypatch.setenv("ELECTRONICSTAR_FEED_URL", "https://example.test/star.xml")
    assert get_shop("electronicstar").feed_sources() == ["https://example.test/star.xml"]
    monkeypatch.setenv("DYSON_FEED_URL", "https://example.test/dyson.xml")
    assert get_shop("dyson").feed_url == "https://example.test/dyson.xml"


def test_domains_and_blocked_mall_czc_unchanged():
    assert shop_for_url("https://www.electronic-star.cz/x").slug == "electronicstar"
    assert shop_for_url("https://www.elestar.eu/x").slug == "electronicstar"
    assert shop_for_url("https://www.electrolux.cz/kitchen/oven/x").slug == "electrolux"
    assert shop_for_url("https://www.dyson.cz/v15").slug == "dyson"
    assert shop_for_url("https://www.4home.cz/kavovar/").slug == "4home"
    assert get_shop("mall").status == "blocked" and get_shop("mall").source == "scrape"
    assert get_shop("czc").status == "blocked" and get_shop("czc").source == "scrape"
    assert "allegro" not in SHOPS
    assert get_shop("4home").canonical_url("https://www.4home.cz/kavovar") == "https://www.4home.cz/kavovar/"


def test_parse_google_4home_feed():
    items = feed.load(str(GOOGLE))
    assert len(items) == 2
    vac, filtr = items
    assert vac.item_id == "123" and vac.price == 2049 and vac.ean == "8590669000001"
    assert vac.in_stock is True
    assert vac.url == "https://www.4home.cz/sencor-svc-7814vt-tycovy-vysavac-2v1"
    assert "utm_source" not in vac.url
    assert "Vysavače" in (vac.category or "") and "|" in (vac.category or "")
    assert vac.image.startswith("https://cdn.4home.cz/")
    assert filtr.in_stock is False
    assert filtr.price == 135


def test_parse_feedoptimise_strips_tracking():
    items = feed.load(str(OPTIMISE))
    assert len(items) == 2
    washer = items[0]
    assert washer.item_id == "949280001" and washer.price == 12990
    assert washer.original_price == 14990 and washer.in_stock is True
    assert washer.ean == "7333394000001"
    assert washer.url == "https://www.electrolux.cz/laundry/washing-machines/ew6f1495"
    assert "fo_c" not in washer.url and "fo_k" not in washer.url
    assert "Pračky" in (washer.category or "")
    assert items[1].in_stock is False


def test_parse_tradedoubler_unwraps_product_url():
    items, total = feed.parse_tradedoubler(TD.read_bytes())
    assert total == 1 and len(items) == 1
    it = items[0]
    assert it.item_id == "494719-01"
    assert it.url == "https://www.dyson.cz/v15-detect-absolute"
    assert "tradedoubler" not in it.url
    assert it.price == 19090 and it.in_stock is True
    assert it.ean == "5025155000001"
    assert it.category == "Bezkabelové vysavače"
    assert it.image.startswith("https://www.dyson.cz/")


def test_tradedoubler_pagination_stops_on_total(monkeypatch):
    page1 = TD.read_bytes().replace(b"<totalHits>1</totalHits>", b"<totalHits>2</totalHits>")
    page2 = """<?xml version="1.0"?>
<result xmlns="urn:com:tradedoubler:pf:model:xml:output" xmlns:ns2="urn:com:tradedoubler:pf:model:xml:common">
  <productHeader><totalHits>2</totalHits></productHeader>
  <products><product>
    <ns2:name>Vysavač Dyson V8</ns2:name>
    <ns2:categories><ns2:category name="Vysavače"/></ns2:categories>
    <offers><offer sourceProductId="v8">
      <ns2:productUrl>https://www.dyson.cz/v8</ns2:productUrl>
      <priceHistory><ns2:price currency="CZK">8990</ns2:price></priceHistory>
    </offer></offers>
  </product></products>
</result>""".encode()
    seen = []

    def fake_get(url):
        m = re.search(r";page=(\d+)", url)
        page = int(m.group(1))
        seen.append(page)
        if page == 1:
            return page1
        if page == 2:
            return page2
        return b"<result></result>"

    monkeypatch.setattr(feed, "_http_get", fake_get)
    url = "https://api.tradedoubler.com/1.0/products.xml;page=1;pageSize=1;fid=50990?token=public"
    items = feed.load(url)
    assert seen == [1, 2]
    assert [i.url for i in items] == [
        "https://www.dyson.cz/v15-detect-absolute",
        "https://www.dyson.cz/v8",
    ]


def test_accept_dognet_categories_skip_accessories():
    assert accept_feed_item(
        "Sencor SVC 7814VT tyčový vysavač 2v1",
        "Úklid | Vysavače | Akumulátorové vysavače", 2049,
        url="https://www.4home.cz/sencor-svc-7814vt/",
    ) == "vysavace"
    assert accept_feed_item(
        "Akumulátorový vysavač Dyson V15 Detect Absolute",
        "Bezkabelové vysavače", 19090,
        url="https://www.dyson.cz/v15-detect-absolute",
    ) == "vysavace"
    assert accept_feed_item(
        "Akumulátorový vysavač Dyson V15 Detect Absolute + sada pro šetrný úklid",
        "Bezkabelové vysavače", 20290,
    ) == "vysavace"
    assert accept_feed_item(
        "Electrolux EW6F1495 pračka předem plněná",
        "Péče o prádlo > Pračky > Pračky hluboké předem plněné", 12990,
    ) == "pracky"
    assert accept_feed_item(
        "Klarstein Retro lednice",
        "Domácnost | Velké kuchyňské spotřebiče | Lednice | Retro lednice", 8990,
    ) == "lednice"
    assert accept_feed_item(
        "Klarstein Steakhouse skříň na zrání masa",
        "Domácnost | Lednice | Lednice na zrání masa", 49089,
    ) is None
    assert accept_feed_item(
        "Klarstein lednice na nápoje",
        "Domácnost | Lednice | Lednice na nápoje", 5990,
    ) is None
    assert accept_feed_item(
        "Klarstein PopLife lednice na nápoje",
        "Domácnost | Lednice | Retro lednice", 15799,
    ) is None
    assert accept_feed_item(
        "Klarstein Reinraum průmyslový vysavač",
        "Domácnost | Vysavače | Průmyslové vysavače", 6699,
    ) is None
    assert accept_feed_item(
        "Klarstein pákový kávovar",
        "Domácnost | Malé kuchyňské spotřebiče | Kávovary | Pákové kávovary", 3990,
    ) == "kavovary"
    # příslušenství a cizí kategorie
    assert is_accessory_text("Concept HEPA filtr stanice", "Příslušenství k vysavačům")
    assert is_accessory_text("Replacement filter for Dyson V15", "Vysavače")
    assert is_accessory_text("Náhradní kabel Dyson V15", "Vysavače")
    assert is_accessory_text("Náhradní sáček do vysavače", "Vysavače")
    assert accept_feed_item("Concept HEPA filtr", "Úklid > Vysavače > Příslušenství k vysavačům", 135) is None
    assert accept_feed_item("Replacement filter Dyson V15", "Bezkabelové vysavače", 590) is None
    assert accept_feed_item("Napájecí cable Dyson", "Vysavače", 299) is None
    assert accept_feed_item("Náhradní sáček Dyson", "Vysavače", 199) is None
    assert accept_feed_item(
        "Fieldmann elektrický zahradní vysavač",
        "Zahrada > Vysavače listí", 2489,
    ) is None
    assert accept_feed_item(
        "Electrolux vinotéka 40 lahví",
        "Chladničky & Mrazničky > Vinotéky", 12990,
    ) is None
    assert accept_feed_item(
        "Dyson Supersonic vysoušeč vlasů",
        "Dyson Supersonic Vysoušeče vlasů", 9590,
    ) is None
    assert accept_feed_item("Electrolux náhradní filtr pračky", "Příslušenství", 399) is None


def test_apply_feed_4home_keeps_trailing_slash_and_skips_filter():
    init_db()
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    items = feed.load(str(GOOGLE))
    st = apply_feed(db, "4home", items, create_missing=True, max_create=5)
    assert st["created"] == 1, st
    assert st["skipped_accessory"] >= 1
    p = db.query(Product).one()
    assert p.category_slug == "vysavace"
    o = p.offers[0]
    assert o.url == "https://www.4home.cz/sencor-svc-7814vt-tycovy-vysavac-2v1/"
    assert float(o.price) == 2049 and o.in_stock is True
    # existující URL s lomítkem se při dalším feedu spáruje, nevznikne duplicita
    st2 = apply_feed(db, "4home", items, create_missing=True, max_create=5)
    assert st2["matched"] >= 1 and st2["created"] == 0
    assert db.query(Offer).filter(Offer.shop == "4home").count() == 1
    db.close()


def test_apply_feed_dyson_does_not_create_filter(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    init_db()
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    vac = feed.load(str(TD))[0]
    filtr = feed.FeedItem(
        "fil1", "HEPA filter pro Dyson V15",
        "https://www.dyson.cz/hepa-filter-v15", 590,
        category="Bezkabelové vysavače", in_stock=True,
    )
    st = apply_feed(db, "dyson", [vac, filtr], create_missing=True, max_create=5)
    assert st["created"] == 1 and st["skipped_accessory"] >= 1, st
    p = db.query(Product).one()
    assert "filtr" not in p.name.lower() and "filter" not in p.name.lower()
    assert p.category_slug == "vysavace"
    db.close()


def test_obchody_lists_dognet_feeds(client_db, monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    for key in ("ELECTRONICSTAR_FEED_URL", "FOURHOME_FEED_URL", "ELECTROLUX_FEED_URL", "DYSON_FEED_URL"):
        monkeypatch.delenv(key, raising=False)
    c, db, make = client_db
    html = c.get("/obchody").text
    for name in ("Electronic-star", "4Home", "Electrolux", "Dyson"):
        assert name in html
    assert html.count("oficiální feed") >= 4
    assert "Dognet" in html


def test_product_page_dognet_rel(client_db, monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    c, db, make = client_db
    p = make([19090], name="Dyson V15 Detect page", shop="dyson", category="vysavace")
    o = p.offers[0]
    o.url = "https://www.dyson.cz/v15-detect-page-test"
    o.in_stock = True
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    assert "go.dognet.com" in html and "chid=9CFqgvpQ" in html
    assert "nofollow sponsored noopener" in html
    assert "Dyson" in html


def test_oos_hides_buy(client_db, monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    c, db, make = client_db
    p = make([12990], name="Electrolux pračka OOS", shop="electrolux", category="pracky")
    o = p.offers[0]
    o.url = "https://www.electrolux.cz/washer-oos-test"
    o.in_stock = False
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    assert "btn-buy" not in html and "Koupit za" not in html
    assert "Není skladem" in html
