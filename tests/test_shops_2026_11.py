"""Dognet TESLA/Xupe/Tera/Amazestore/Misura a eHUB BScom/DronPro/iPhoneMarket/ProMobily.

De'Longhi content kampaň se nenapojuje (zákaz shopping CSS / srovnávače).
"""
from urllib.parse import parse_qs, unquote, urlparse

from lovceny.categories import classify_feed_item
from lovceny.shops import feed, get_shop, shop_for_url
from lovceny.shops.feed import FeedItem, _ChunkReader


def _slug(shop, name, category, price, url="https://example.test/p"):
    slug, reason = classify_feed_item(shop, name, category, price, url)
    return slug, reason


def test_new_shops_registry_and_tracking(monkeypatch):
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    monkeypatch.delenv("EHUB_AID", raising=False)
    for key in (
        "TESLA_FEED_URL", "XUPE_FEED_URL", "TERA_FEED_URL", "AMAZESTORE_FEED_URL",
        "MISURA_FEED_URL", "BSCOM_FEED_URL", "DRONPRO_FEED_URL",
        "IPHONEMARKET_FEED_URL", "PROMOBILY_FEED_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    dognet = {
        "tesla": ("TESLA", "tesla-electronics.eu", "mergado.com", "1215"),
        "xupe": ("Xupe", "xupe.cz", "xupe.cz/api/v3/feeds", "5649"),
        "tera": ("Tera", "tera.cz", "shopItemFeed.asp", "2207"),
        "amazestore": ("Amazestore", "amazestore.cz", "feedyio.com", "6425"),
        "misura": ("Misura", "misura.shop", "misura.shop/feed-google", "6271"),
    }
    for slug, (name, host, feed_bit, campaign) in dognet.items():
        shop = get_shop(slug)
        assert shop is not None and shop.name == name and shop.source == "feed"
        assert shop.dognet_chid == "9CFqgvpQ" and shop.ehub_bid is None
        assert shop.extra["dognet_campaign"] == campaign
        assert feed_bit in shop.feed_url
        assert shop_for_url(f"https://www.{host}/produkt") is shop
        dest = f"https://www.{host}/produkt?x=1"
        wrapped = shop.affiliate_link(dest)
        assert wrapped.startswith("https://go.dognet.com/?chid=9CFqgvpQ&url=")
        assert unquote(parse_qs(urlparse(wrapped).query)["url"][0]) == dest
        st, label = shop.status_label()
        assert st == "ok" and label == "oficiální feed"

    ehub = {
        "bscom": ("BScom", "bscom.cz", "f4a2088a", "ehub.xml"),
        "dronpro": ("DronPro", "dronpro.cz", "5889edbd", "dronpro"),
        "iphonemarket": ("iPhoneMarket", "iphonemarket.cz", "2bcd6f9d", "heureka/export"),
        "promobily": ("ProMobily", "promobily.cz", "9530f758", "heureka_cz.xml"),
    }
    for slug, (name, host, bid, feed_bit) in ehub.items():
        shop = get_shop(slug)
        assert shop.name == name and shop.ehub_bid == bid and shop.dognet_chid is None
        assert shop.source == "feed" and feed_bit in shop.feed_url
        assert shop_for_url(f"https://www.{host}/p") is shop
        dest = f"https://www.{host}/p"
        wrapped = shop.affiliate_link(dest)
        q = parse_qs(urlparse(wrapped).query)
        assert q["a_aid"] == ["667205fc"] and q["a_bid"] == [bid]
        assert unquote(q["desturl"][0]) == dest

    promo = get_shop("promobily")
    sources = promo.feed_sources()
    assert sources[0].endswith("heureka_cz.xml")
    assert "google-nakupy" in sources[1]
    monkeypatch.setenv("PROMOBILY_FEED_URL", "https://example.test/promo.xml")
    assert get_shop("promobily").feed_sources() == ["https://example.test/promo.xml"]


def test_feed_byte_cap_covers_bscom():
    """BScom XML mělo 472 MB. Strop musí být nad tím, jinak se feed zahodí."""
    assert feed.FEED_MAX_BYTES >= 520 * 1024 * 1024


def test_delonghi_content_campaign_stays_unwired():
    shop = get_shop("delonghi")
    assert shop.source == "scrape" and shop.dognet_chid is None and shop.ehub_bid is None
    assert "tradedoubler" not in (shop.feed_url or "")
    assert shop.affiliate_link("https://www.delonghi.com/cs-cz/kavovar") == "https://www.delonghi.com/cs-cz/kavovar"


def test_catalog_rules_keep_devices_and_drop_accessories():
    assert _slug("tesla", "TESLA Sound EB10 sluchátka", "Audio-video > Sluchátka", 1990) == ("sluchatka", "ok")
    assert _slug("tesla", "TESLA AeroStar T500 vysavač", "Domov a úklid > Vysavače > Tyčové vysavače", 4990) == ("vysavace", "ok")
    assert _slug("tesla", "TESLA CoffeeMaster ES400", "Kuchyně a vaření > Kávovary", 7990) == ("kavovary", "ok")
    assert _slug("tesla", "TESLA Smart Plug SP300", "Chytrá domácnost > Chytré zásuvky", 690) == ("chytra-domacnost", "ok")
    assert _slug("tesla", "TESLA EasyCook fritéza", "Kuchyně a vaření > Fritézy", 2490)[0] is None
    assert _slug("tesla", "TESLA RoboStar čistič oken", "Domov a úklid > Robotické čističe oken", 4990)[0] is None

    assert _slug(
        "xupe", "Samsung Galaxy A55 128GB",
        "Mobilní telefony a příslušenství -> Telefony -> Mobilní telefony", 8990,
    ) == ("telefony", "ok")
    assert _slug(
        "xupe", "Ochranné sklo Gigapack",
        "Mobilní telefony a příslušenství -> Příslušenství k mobilním telefonům -> Chrániče obrazovky", 47,
    )[1] == "accessory"
    assert _slug(
        "xupe", "Řemínek 3MK pro Apple",
        "Chytrá zařízení -> Chytré hodinky a příslušenství -> Chytré hodinky", 890,
    )[1] == "accessory"
    assert _slug("xupe", "Moka konvička Lamart", "Kuchyně -> Moka konvička", 430)[0] is None
    assert _slug("xupe", "Tesla CoffeeMaster ES400", "Kuchyně -> Kávovar na espresso", 2919) == ("kavovary", "ok")

    assert _slug("tera", "Apple iPhone 15 128GB", "MOBILY | Nové mobily | Apple iPhone", 16820) == ("telefony", "ok")
    assert _slug("tera", "Lenovo ThinkPad T14s", "Notebooky | Repasované notebooky | Dle výrobce", 12990) == ("notebooky", "ok")
    assert _slug("tera", "Lenovo ThinkCentre M75s", "Počítače | Repasované počítače | Dle výrobce", 5290) == ("pocitace", "ok")
    assert _slug("tera", "Trust sluchátka", "Doplňky | Audio, video, foto | Sluchátka", 165)[1] == "price"
    assert _slug("tera", "Marvo herní sluchátka", "Doplňky | Herní zóna | Herní sluchátka", 890) == ("sluchatka", "ok")
    assert _slug("tera", "Aligator kryt iPhone", "MOBILY | Příslušenství | Pouzdra a kryty", 199)[1] == "accessory"
    assert _slug("tera", "Razer Base Station", "Doplňky | Herní zóna | Herní konzole", 2490)[0] is None
    assert _slug("tera", "Gigabyte AORUS MASTER 16", "Notebooky | Nové notebooky | Dle výrobce", 119017)[1] == "excluded"
    assert _slug("tera", "Karl Lagerfeld kryt", "MOBILY | Nové mobily | Apple iPhone", 1290)[1] == "excluded"

    assert _slug("amazestore", "Noise Master Buds", "Bezdrátová sluchátka do uší", 1990) == ("sluchatka", "ok")
    assert _slug("amazestore", "Luna Ring", "Chytrý prsten", 4990)[0] is None
    assert _slug("amazestore", "Příslušenství lopata", "Příslušenství pro pec na pizzu", 900)[1] == "accessory"

    assert _slug("misura", "Parfém SANTINI", "SANTINI > PARFEMY", 890)[0] is None
    assert _slug("misura", "Monitor MISURA", "MONITORY > Kancelářské monitory", 5990)[0] is None

    assert _slug(
        "bscom", "Sennheiser RS 175",
        "E-shop > TV, audio a foto > Sluchátka, repro & Hi-Fi > Sluchátka > Bezdrátová sluchátka > Přes hlavu",
        6673,
    ) == ("sluchatka", "ok")
    assert _slug(
        "bscom", "Mikrofon Rode",
        "E-shop > TV, audio a foto > Sluchátka, repro & Hi-Fi > HiFi > Mikrofony", 1505,
    )[0] is None
    assert _slug(
        "bscom", "Soundbar Sony",
        "E-shop > TV, audio a foto > Sluchátka, repro & Hi-Fi > Reproduktory > Soundbary", 3069,
    )[0] is None
    assert _slug(
        "bscom", "Rowenta RH6543 tyčový vysavač",
        "E-shop > Domácí spotřebiče > Vysavače a úklid > Tyčové vysavače", 3009,
    ) == ("vysavace", "ok")
    assert _slug(
        "bscom", "Parní čistič Sencor",
        "E-shop > Domácí spotřebiče > Vysavače a úklid > Parní čističe", 1499,
    )[0] is None
    assert _slug(
        "bscom", "Vysavač na listí Gardena",
        "E-shop > Dům, zahrada a hobby > Zahrada > Vysavače a fukary > Vysavače listí", 3343,
    )[0] is None
    assert _slug(
        "bscom", "Dell Latitude 5420",
        "E-shop > Počítače a notebooky > Repasované a zánovní > Repasované notebooky", 12210,
    ) == ("notebooky", "ok")
    assert _slug(
        "bscom", "Kryt na mobil Xiaomi",
        "E-shop > Mobily a chytrá zařízení > Příslušenství k mobilům > Pouzdra a kryty > Xiaomi", 158,
    )[1] == "accessory"
    assert _slug(
        "bscom", "HAL3000 stolní počítač",
        "E-shop > Počítače a notebooky > Stolní počítače > Stolní počítače", 19990,
    ) == ("pocitace", "ok")
    assert _slug("bscom", "Puzzle 1000 dílků", "E-shop > Děti a hračky > Puzzle", 399)[0] is None

    assert _slug(
        "dronpro", "DJI Osmo Action 4",
        "Heureka.cz | Elektronika | TV, video, audio | Digitální kamery", 8990,
    ) == ("fotoaparaty", "ok")
    assert _slug(
        "dronpro", "Držák na kameru Insta360",
        "Heureka.cz | Elektronika | TV, video, audio | TV a video příslušenství | Příslušenství k dronům", 890,
    )[1] == "accessory"
    assert _slug("dronpro", "DJI Mini 4 Pro", "Heureka.cz | Elektronika | TV, video, audio | Drony", 24990)[0] is None

    assert _slug(
        "iphonemarket", "Apple iPhone 15 128GB",
        "Zánovní a použité iPhony | iPhone 15 | iPhone 15 128 GB", 12990,
    ) == ("telefony", "ok")
    assert _slug(
        "iphonemarket", "Pouzdro pro iPhone 15",
        "Příslušenství pro iPhone | Kryty a obaly pro iPhone | Kryty pro iPhone 15", 290,
    )[1] == "accessory"

    assert _slug(
        "promobily", "Guess Pearl TWS sluchátka",
        "Elektronika > TV, video, audio > Audio > Sluchátka", 630,
    ) == ("sluchatka", "ok")
    assert _slug(
        "promobily", "Tvrzené sklo Sony",
        "Elektronika > Mobily > Mobilní příslušenství > Tvrzená skla pro mobilní telefony", 249,
    )[1] == "accessory"


def test_parse_xupe_and_streamed_google():
    xml = """<?xml version="1.0"?>
<products>
  <product>
    <Identifier>abc</Identifier>
    <Name>Samsung Galaxy A55</Name>
    <EanCode>8806095498540</EanCode>
    <Category>Mobilní telefony a příslušenství -&gt; Telefony -&gt; Mobilní telefony</Category>
    <ImageUrl>https://cdn.xupe.hu/ProductImage/abc/a.png</ImageUrl>
    <Price>8990.00</Price>
    <ProductUrl>https://www.xupe.cz/api/click/feed/abc</ProductUrl>
    <DeliveryTime>0</DeliveryTime>
  </product>
  <product>
    <Name>Ochranné sklo</Name>
    <Category>Mobilní telefony a příslušenství -&gt; Příslušenství -&gt; Chrániče obrazovky</Category>
    <Price>46.70</Price>
    <ProductUrl>https://www.xupe.cz/api/click/feed/sklo</ProductUrl>
    <DeliveryTime>20</DeliveryTime>
  </product>
</products>"""
    items = feed.parse_bytes(xml.encode())
    assert len(items) == 2
    phone, glass = items
    assert phone.name.startswith("Samsung") and phone.price == 8990 and phone.in_stock is True
    assert phone.ean == "8806095498540"
    assert phone.url == "https://www.xupe.cz/api/click/feed/abc"
    assert "Mobilní telefony" in (phone.category or "") and "->" not in (phone.category or "")
    assert glass.in_stock is False and glass.price == 46.7
    from lovceny import images
    assert images.is_allowed(phone.image)

    google = """<?xml version="1.0"?>
<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0"><channel>
<item>
  <g:id>1</g:id><title>TESLA Sound EB10</title>
  <link>https://eshop.tesla-electronics.eu/eb10?utm_source=google</link>
  <g:price>1990 CZK</g:price><g:availability>in_stock</g:availability>
  <g:product_type>Audio-video &gt; Sluchátka</g:product_type>
  <g:image_link>https://data-b2b.inter-sat.cz/data/TESLA/eb10.jpg</g:image_link>
</item>
</channel></rss>""".encode()
    parts = [google[i:i + 40] for i in range(0, len(google), 40)]
    reader = _ChunkReader(b"", iter(parts), deadline=10**12, max_bytes=10_000_000)
    parsed = feed.parse_google_xml(reader)
    assert len(parsed) == 1
    assert parsed[0].price == 1990 and "utm_source" not in parsed[0].url
    assert "Sluchátka" in (parsed[0].category or "")
    assert images.is_allowed(parsed[0].image)

    kept = feed.parse_google_xml(google, keep=lambda it: "Sluchátka" in (it.category or ""))
    assert len(kept) == 1


def test_apply_feed_creates_phone_hides_case_and_page_links(client_db, monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    monkeypatch.delenv("DOGNET_CHID", raising=False)
    from lovceny.service import apply_feed
    c, db, make = client_db
    phone = FeedItem(
        "ip15", "Apple iPhone 15 128GB Black",
        "https://www.iphonemarket.cz/iphone-15-128-black",
        12990, ean="0194253773494",
        category="Zánovní a použité iPhony | iPhone 15 | iPhone 15 128 GB",
        in_stock=True,
    )
    case = FeedItem(
        "case", "Pouzdro pro iPhone 15",
        "https://www.iphonemarket.cz/pouzdro-15",
        290, category="Příslušenství pro iPhone | Kryty a obaly pro iPhone",
        in_stock=True,
    )
    st = apply_feed(db, "iphonemarket", [phone, case], create_missing=True, max_create=5)
    assert st["created"] == 1 and st["skipped_accessory"] >= 1
    db.commit()
    from lovceny.models import Product
    rows = db.query(Product).filter(Product.name.contains("iPhone 15")).all()
    assert len(rows) == 1 and rows[0].category_slug == "telefony" and rows[0].catalog_hidden is not True
    html = c.get(f"/produkt/{rows[0].id}").text
    assert "iPhoneMarket" in html
    assert "ehub.cz/system/scripts/click.php" in html
    assert "a_bid=2bcd6f9d" in html
    assert "nofollow sponsored noopener" in html
    assert 'class="btn btn-shop"' in html

    p = make([1990], name="TESLA Sound EB10", shop="tesla", category="sluchatka")
    o = p.offers[0]
    o.url = "https://eshop.tesla-electronics.eu/eb10-page"
    o.in_stock = True
    db.commit()
    page = c.get(f"/produkt/{p.id}").text
    assert "TESLA" in page and "go.dognet.com" in page and "chid=9CFqgvpQ" in page
    assert "nofollow sponsored noopener" in page
    o.in_stock = False
    db.commit()
    oos = c.get(f"/produkt/{p.id}").text
    assert "Není skladem" in oos
    assert 'class="btn btn-shop"' not in oos
    assert "go.dognet.com" in oos and "nofollow sponsored" in oos
