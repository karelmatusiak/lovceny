"""Dognet obalení odkazů a feedy Electronic-star / 4Home / Electrolux / Dyson / Allegro."""
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from lovceny.categories import accept_feed_item, is_accessory_text
from lovceny.shops.feed import TD_MULTI_TOTAL_CAP
from lovceny.db import Base, SessionLocal, engine, init_db
from lovceny.models import Offer, Product
from lovceny.service import apply_feed
from lovceny.shops import feed, get_shop, shop_for_url
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
        ("allegro", "allegro.cz"),
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
    for key in ("ELECTRONICSTAR_FEED_URL", "FOURHOME_FEED_URL", "ELECTROLUX_FEED_URL", "DYSON_FEED_URL", "ALLEGRO_FEED_URL"):
        monkeypatch.delenv(key, raising=False)
    for slug in ("electronicstar", "4home", "electrolux", "dyson", "allegro"):
        st, label = get_shop(slug).status_label()
        assert st == "ok" and label == "oficiální feed", (slug, st, label)
    star = get_shop("electronicstar")
    assert star.feed_url.startswith("http://feedy.elestar.eu/data/dognet_elstar_cz.xml")
    assert any("mergado.com" in u for u in star.feed_sources())
    assert "google-products.xml" in get_shop("4home").feed_url
    assert get_shop("4home").trailing_slash
    assert "feedoptimise.com" in get_shop("electrolux").feed_url
    assert "tradedoubler.com" in get_shop("dyson").feed_url and "fid=50990" in get_shop("dyson").feed_url
    allegro = get_shop("allegro")
    assert "tradedoubler.com" in allegro.feed_url and "fid=257027" in allegro.feed_url
    assert "pageSize=100" in allegro.feed_url and ";q=" not in allegro.feed_url
    queries = list(allegro.extra["td_queries"])
    assert len(queries) >= 8
    assert all("q=" in q and "minPrice=" in q for q in queries)
    assert allegro.extra["td_page_cap"] <= 5
    assert allegro.extra["td_total_cap"] <= TD_MULTI_TOTAL_CAP
    assert allegro.extra["dognet_campaign"] == "493"
    monkeypatch.setenv("ELECTRONICSTAR_FEED_URL", "")
    monkeypatch.setenv("FOURHOME_FEED_URL", "")
    monkeypatch.setenv("DYSON_FEED_URL", "")
    monkeypatch.setenv("ALLEGRO_FEED_URL", "")
    assert get_shop("electronicstar").feed_url.startswith("http://feedy.elestar.eu/")
    assert "4home.cz" in get_shop("4home").feed_url
    assert "fid=257027" in get_shop("allegro").feed_url
    monkeypatch.setenv("ELECTRONICSTAR_FEED_URL", "https://example.test/star.xml")
    assert get_shop("electronicstar").feed_sources() == ["https://example.test/star.xml"]
    monkeypatch.setenv("DYSON_FEED_URL", "https://example.test/dyson.xml")
    assert get_shop("dyson").feed_url == "https://example.test/dyson.xml"
    monkeypatch.setenv("ALLEGRO_FEED_URL", "https://example.test/allegro.xml")
    assert get_shop("allegro").feed_url == "https://example.test/allegro.xml"


def test_domains_and_blocked_mall_czc_unchanged():
    assert shop_for_url("https://www.electronic-star.cz/x").slug == "electronicstar"
    assert shop_for_url("https://www.elestar.eu/x").slug == "electronicstar"
    assert shop_for_url("https://www.electrolux.cz/kitchen/oven/x").slug == "electrolux"
    assert shop_for_url("https://www.dyson.cz/v15").slug == "dyson"
    assert shop_for_url("https://www.4home.cz/kavovar/").slug == "4home"
    assert shop_for_url("https://allegro.cz/nabidka/iphone-18").slug == "allegro"
    assert shop_for_url("https://www.allegro.cz/nabidka/iphone-18").slug == "allegro"
    assert get_shop("mall").status == "blocked" and get_shop("mall").source == "scrape"
    assert get_shop("czc").status == "blocked" and get_shop("czc").source == "scrape"
    assert get_shop("mall").dognet_chid is None and get_shop("czc").dognet_chid is None
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
    for name in ("Electronic-star", "4Home", "Electrolux", "Dyson", "Allegro"):
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


PHONE = "Elektronika > Telefony a příslušenství > Smartphony a mobilní telefony"
CASE = "Elektronika > Telefony a příslušenství > Příslušenství GSM > Obaly a kryty"
NB = "Elektronika > Počítače > Notebooky"
PC = "Elektronika > Počítače > Stolní počítače"
TABLET = "Elektronika > Počítače > Tablety > Zařízení"
TV = "Elektronika > Audio, video a domácí spotřebiče > Televize a video > Televizory"
MOUNT = "Elektronika > Audio, video a domácí spotřebiče > Televize a video > Stolky, držáky, police > Držáky"
WASH = "Elektronika > Audio, video a domácí spotřebiče > Volně stojící domácí spotřebiče > Pračky"
FRIDGE = "Elektronika > Audio, video a domácí spotřebiče > Volně stojící domácí spotřebiče > Ledničky"
VAC = "Elektronika > Audio, video a domácí spotřebiče > Malé domácí spotřebiče > Domácí spotřebiče > Vysavače > Tyčové vysavače"
HOSE = "Elektronika > Audio, video a domácí spotřebiče > Malé domácí spotřebiče > Domácí spotřebiče > Vysavače > Trubky a hadice k vysavačům"
COFFEE = "Elektronika > Audio, video a domácí spotřebiče > Malé domácí spotřebiče > Do kuchyně > Kávovary > Kávovary na espresso"
HEAD = "Elektronika > Audio, video a domácí spotřebiče > Sluchátka > Bezdrátová"
WATCH = "Elektronika > Telefony a příslušenství > Chytré hodinky a příslušenství > Chytré hodinky"
BAND = "Elektronika > Telefony a příslušenství > Chytré hodinky a příslušenství > Pásky a řemínky"
REMOTE = "Elektronika > Audio, video a domácí spotřebiče > Televize a video > Dálková ovládání"


def _td_allegro_xml(name, product_url, price="8990", cat="x", ean="", hits="1", source_id="1"):
    ean_xml = f"<ns2:field name=\"gtin\">{ean}</ns2:field>" if ean else ""
    return f"""<?xml version="1.0"?>
<result xmlns="urn:com:tradedoubler:pf:model:xml:output" xmlns:ns2="urn:com:tradedoubler:pf:model:xml:common">
  <productHeader><totalHits>{hits}</totalHits></productHeader>
  <products><product>
    <ns2:name>{name}</ns2:name>
    <ns2:categories><ns2:category name="{cat}"/></ns2:categories>
    <ns2:fields><ns2:field name="availability">in stock</ns2:field>{ean_xml}</ns2:fields>
    <offers><offer sourceProductId="{source_id}">
      <ns2:productUrl>{product_url}</ns2:productUrl>
      <priceHistory><ns2:price currency="CZK">{price}</ns2:price></priceHistory>
    </offer></offers>
  </product></products>
</result>""".encode()


def test_allegro_affiliate_url_unwraps_before_dognet(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    nabidka = "https://allegro.cz/nabidka/apple-iphone-15-128gb-18924115297"
    affiliate = "https://allegro.cz/affiliate?redirect_url=" + quote(
        nabidka + "?utm_feed=abc&utm_source=ctr&utm_medium=afiliacja", safe=""
    )
    wrapped_td = (
        "https://pdt.tradedoubler.com/click?a(1)p(2)product(257027-1)url("
        + quote(affiliate, safe="") + ")"
    )
    assert feed.clean_product_url(wrapped_td) == nabidka
    assert feed.clean_product_url(affiliate) == nabidka
    shop = get_shop("allegro")
    assert shop.canonical_url(affiliate) == nabidka
    wrapped = shop.affiliate_link(shop.canonical_url(affiliate))
    assert wrapped.startswith("https://go.dognet.com/?chid=9CFqgvpQ&url=")
    assert unquote(parse_qs(urlparse(wrapped).query)["url"][0]) == nabidka
    # cizí host se affiliate parametrem nenecháme rozbalit
    assert "evil.com" in feed.clean_product_url("https://allegro.cz.evil.com/affiliate?redirect_url=" + quote(nabidka))


def test_parse_allegro_tradedoubler_cleans_gtin_and_url():
    nabidka = "https://allegro.cz/nabidka/samsung-galaxy-a36-42"
    affiliate = "https://allegro.cz/affiliate?redirect_url=" + quote(nabidka + "?utm_campaign=x", safe="")
    td = "https://pdt.tradedoubler.com/click?a(1)p(2)url(" + quote(affiliate, safe="") + ")"
    # GTIN-14 s platným EAN-13 (prefix 0)
    ean13 = "5901234123457"
    # checksum nemusí sedět — ověříme jen, že platný EAN se zkrátí
    body = "590123412345"
    digits = [int(c) for c in body]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    ean13 = body + str((10 - (total % 10)) % 10)
    raw = _td_allegro_xml(
        "Smartphone Samsung Galaxy A36 128GB", td, ean="0" + ean13, cat=PHONE, source_id="42",
    )
    items, total_hits = feed.parse_tradedoubler(raw)
    assert total_hits == 1 and len(items) == 1
    it = items[0]
    assert it.url == nabidka
    assert "affiliate" not in it.url and "tradedoubler" not in it.url
    assert it.ean == ean13
    assert it.item_id == "42"
    assert accept_feed_item(it.name, it.category, it.price, url=it.url) == "telefony"


def test_allegro_categories_skip_accessories():
    assert accept_feed_item("Apple iPhone 15 128GB černá", PHONE, 18990, url="https://allegro.cz/nabidka/iphone") == "telefony"
    assert not is_accessory_text("Apple iPhone 15 128GB černá", PHONE)
    assert accept_feed_item("Notebook Lenovo IdeaPad 15 16GB", NB, 14990) == "notebooky"
    assert accept_feed_item("Počítač Lenovo ThinkCentre M70", PC, 12990) == "pocitace"
    assert accept_feed_item("Televizor LG OLED55C45 55 palců", TV, 24990) == "televize"
    assert accept_feed_item("Pračka Bosch WAN282ECO předem plněná", WASH, 9990) == "pracky"
    assert accept_feed_item("Lednička Samsung kombinovaná", FRIDGE, 11990) == "lednice"
    assert accept_feed_item("Kompresorová chladnička 92 L do obytného vozu", FRIDGE, 3274) is None
    assert accept_feed_item("Tyčový vysavač Dyson V8 Origin", VAC, 8490) == "vysavace"
    assert accept_feed_item("Kávovar Philips LatteGo EP4441 automatický", COFFEE, 11610) == "kavovary"
    assert accept_feed_item("Sluchátka Sony WH-1000XM5 černá", HEAD, 6990) == "sluchatka"
    assert accept_feed_item("Chytré hodinky Garmin Venu 3", WATCH, 8990) == "chytre-hodinky"
    # tablety Lovceny jako kategorii nemají
    assert accept_feed_item("Tablet Samsung Galaxy Tab A9", TABLET, 4990) is None
    samples = [
        ("Pouzdro na iPhone 15", CASE),
        ("Kryt na Samsung Galaxy", CASE),
        ("Obal na iPhone 15", CASE),
        ("USB-C kabel 2 m", PHONE),
        ("HEPA filtr do vysavače Dyson", VAC),
        ("Náhradní sáček do vysavače", VAC),
        ("Nabíječka USB-C 25W", PHONE),
        ("Stylus Pencil pro iPad", TABLET),
        ("Dálkový ovladač LG Magic Remote", REMOTE),
        ("Ovladač", REMOTE),
        ("Hadice k vysavači 3 m", HOSE),
        ("Pásek na hodinky 22 mm", BAND),
        ("Držák na televizi 55 palců", MOUNT),
    ]
    for name, cat in samples:
        assert is_accessory_text(name, cat), name
        assert accept_feed_item(name, cat, 2500) is None, name
    assert not is_accessory_text("Sony DualSense ovladač černý", "Elektronika > Konzole a herní automaty > PlayStation 5 > Konzole")
    # rodič „příslušenství“ neshodí telefon ani hodinky
    assert not is_accessory_text("Apple iPhone 15 128GB", PHONE)
    assert not is_accessory_text("Chytré hodinky Garmin Venu 3", WATCH)


def test_allegro_td_queries_stay_bounded(monkeypatch):
    base = "https://api.tradedoubler.com/1.0/products.xml;page=1;pageSize=1;fid=257027?token=public"
    calls = []

    def fake_get(url):
        calls.append(url)
        page = int(re.search(r";page=(\d+)", url).group(1))
        q = re.search(r";q=([^;?]+)", url)
        label = unquote(q.group(1)) if q else "x"
        body = _td_allegro_xml(
            f"Notebook Lenovo {label} {page}",
            f"https://allegro.cz/nabidka/{label}-{page}",
            cat=NB, hits="50", source_id=f"{label}-{page}",
        )
        return body

    monkeypatch.setattr(feed, "_http_get", fake_get)

    class Shop:
        extra = {
            "td_queries": ["minPrice=5000;q=notebook", "minPrice=8000;q=macbook", "minPrice=1;q=třetí"],
            "td_page_cap": 2,
            "td_total_cap": 3,
        }

        def feed_sources(self):
            return [base]

    items = feed.load_shop(Shop())
    assert len(calls) == 3
    assert sum("q=notebook" in u for u in calls) == 2
    assert sum("q=macbook" in u for u in calls) == 1
    assert all("q=" not in u or "pageSize=1" in u for u in calls)
    assert len(items) == 3
    # override s vlastním ;q= nespouští sadu dotazů
    calls.clear()

    class One:
        extra = {"td_queries": ["minPrice=1;q=notebook"], "td_page_cap": 2, "td_total_cap": 30}

        def feed_sources(self):
            return ["https://api.tradedoubler.com/1.0/products.xml;page=1;pageSize=1;fid=1;q=only?token=t"]

    def fake_one(url):
        calls.append(url)
        return _td_allegro_xml("Notebook Lenovo only", "https://allegro.cz/nabidka/only", cat=NB, hits="1")

    monkeypatch.setattr(feed, "_http_get", fake_one)
    got = feed.load_shop(One())
    assert len(calls) == 1 and "q=only" in calls[0] and "q=notebook" not in calls[0]
    assert len(got) == 1


def test_apply_feed_allegro_matches_ean_skips_case(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    init_db()
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    phone = feed.FeedItem(
        "p1", "Apple iPhone 15 128GB černá",
        "https://allegro.cz/nabidka/apple-iphone-15-128gb",
        17483, ean="0195949036576", category=PHONE, in_stock=True,
    )
    case = feed.FeedItem(
        "c1", "Pouzdro na iPhone 15",
        "https://allegro.cz/nabidka/pouzdro-iphone-15",
        399, ean="1234567890123", category=CASE, in_stock=True,
    )
    existing = Product(name="Apple iPhone 15 128GB", slug="iphone-15-allegro-ean", ean="0195949036576",
                       category_slug="telefony", category="Mobilní telefony")
    db.add(Offer(product=existing, shop="mironet", url="https://www.mironet.cz/iphone-15-allegro-ean",
                 status="ok", price=17990, in_stock=True))
    db.commit()
    st = apply_feed(db, "allegro", [case, phone], create_missing=True, max_create=5)
    assert st["skipped_accessory"] >= 1, st
    assert st["created"] == 0, st
    assert st["attached"] == 1, st
    offers = db.query(Offer).filter(Offer.shop == "allegro").all()
    assert len(offers) == 1
    assert offers[0].url == phone.url
    assert offers[0].product_id == existing.id
    db.close()


def test_allegro_product_page_dognet_and_oos(client_db, monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    c, db, make = client_db
    p = make([17483], name="iPhone 15 Allegro", shop="allegro", category="telefony")
    o = p.offers[0]
    o.url = "https://allegro.cz/nabidka/iphone-15-page"
    o.in_stock = True
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    assert "go.dognet.com" in html and "chid=9CFqgvpQ" in html
    assert "allegro.cz" in html
    o.in_stock = False
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    assert "btn-buy" not in html and "Koupit za" not in html
    assert "Není skladem" in html


def test_allegro_seed_spreads_categories_and_prefers_ean():
    from seed import _feed_brand, allegro_seed_limit, pick_feed_create_items
    assert allegro_seed_limit(25) == 60
    assert allegro_seed_limit(10) == 10
    assert _feed_brand("Notebook Hp 14-ep") == "hp"
    assert _feed_brand("Notebook Lenovo IdeaPad") == "lenovo"
    assert _feed_brand("MacBook Air 13") == "apple"
    assert _feed_brand("Sencor SVC 45RD") == "sencor"
    cheap = feed.FeedItem("a", "Apple iPhone 15 128GB černá", "https://allegro.cz/nabidka/ip-cheap",
                           15000, ean=None, category=PHONE, in_stock=True)
    with_ean = feed.FeedItem("b", "Apple iPhone 15 128GB černá", "https://allegro.cz/nabidka/ip-ean",
                              18990, ean="0195949036576", category=PHONE, in_stock=True)
    vac = feed.FeedItem("v", "Tyčový vysavač Dyson V15", "https://allegro.cz/nabidka/v15",
                         19090, ean="5025155000001", category=VAC, in_stock=True)
    picked = pick_feed_create_items(
        [cheap, with_ean, vac], ("telefony", "vysavace"),
        prefer_ean=True, spread_categories=True, max_per_brand_cat=4,
    )
    urls = [p.url for p in picked]
    assert "https://allegro.cz/nabidka/ip-ean" in urls
    assert "https://allegro.cz/nabidka/ip-cheap" not in urls
    assert urls[0] != urls[1]
    cats = {accept_feed_item(p.name, p.category, p.price, url=p.url) for p in picked[:2]}
    assert cats == {"telefony", "vysavace"}
