"""Živý audit: použité nabídky, kód modelu, kupón, příslušenství, řazení, počty."""
from lovceny.availability import sort_offers_for_display, visible_offer_count
from lovceny.categories import is_accessory_text, is_non_new, is_station_accessory
from lovceny.matching import model_codes_conflict
from lovceny.shops import feed
from lovceny.shops.feed import FeedItem


def test_non_new_from_condition_grade_and_category():
    assert is_non_new("Apple iPhone 15 128GB", condition="used")
    assert is_non_new("Apple iPhone 15 128GB", condition="bazar Výborný")
    assert is_non_new("Apple iPhone 15 128GB", item_type="bazar")
    assert is_non_new("Apple iPhone 14 Midnight Stav produktu: Výborný")
    assert is_non_new("iPhone 13", shop_category="Zánovní a použité iPhony | iPhone 13")
    assert is_non_new("Dell Latitude", shop_category="Repasované notebooky")
    assert is_non_new("Sluchátka rozbalené")
    assert not is_non_new("Apple iPhone 15 128GB", condition="new")
    assert not is_non_new("TESLA CoffeeMaster ES400", condition="NOVÉ")
    assert not is_non_new("JBL Tune 520BT")


def test_heureka_bazar_grade_is_not_created(client_db):
    xml = """<?xml version="1.0"?><SHOP><SHOPITEM>
      <ITEM_ID>1</ITEM_ID>
      <PRODUCTNAME>Apple iPhone 15 128GB Modrá Stav produktu: Výborný</PRODUCTNAME>
      <URL>https://www.iphonemarket.cz/iphone-15-vyborny</URL>
      <PRICE_VAT>11990</PRICE_VAT>
      <ITEM_TYPE>bazar</ITEM_TYPE>
      <PARAM><PARAM_NAME>Stav produktu</PARAM_NAME><VAL>Výborný</VAL></PARAM>
      <CATEGORYTEXT>Zánovní a použité iPhony | iPhone 15</CATEGORYTEXT>
      <DELIVERY_DATE>0</DELIVERY_DATE>
    </SHOPITEM></SHOP>"""
    item = feed.parse_xml(xml)[0]
    assert "bazar" in (item.condition or "").lower()
    assert "výborný" in (item.condition or "").lower()
    from lovceny.service import apply_feed
    _c, db, _make = client_db
    stats = apply_feed(db, "iphonemarket", [item], create_missing=True, max_create=5)
    assert stats["created"] == 0 and stats["skipped_used"] == 1


def test_model_codes_with_different_digits_do_not_match():
    left = "DeLonghi ECAM 220.80.SB Magnifica Start"
    right = "DeLonghi ECAM 220.30.SB Magnifica Start"
    url = "https://www.bscom.cz/automaticky-kavovar-de-longhi-magnifica-start-ecam-220-30-sb"
    assert model_codes_conflict(left, right)
    assert model_codes_conflict(left, url)
    assert not model_codes_conflict(left, "DeLonghi ECAM 220.80.SB černý")
    assert not model_codes_conflict("Apple iPhone 15 128GB", "Apple iPhone 15 256GB")
    assert not feed.names_match(left, right)
    assert feed.names_match("Sencor SVC 7811WH", "Sencor SVC 7811WH bílý")


def test_different_model_digits_detach_even_with_same_ean(client_db):
    from lovceny.live_audit import apply_live_audit
    from lovceny.models import Offer
    _c, db, make = client_db
    product = make(
        [8197],
        name="DeLonghi ECAM 220.80.SB Magnifica Start",
        shop="softcom",
        category="kavovary",
    )
    product.ean = "8004399021234"
    same = Offer(
        product=product, shop="xupe",
        url="https://www.xupe.cz/ecam-220-80-sb",
        price=9900, status="ok", in_stock=True, gtin="8004399021234",
    )
    wrong = Offer(
        product=product, shop="bscom",
        url="https://www.bscom.cz/automaticky-kavovar-de-longhi-magnifica-start-ecam-220-30-sb",
        price=8197, status="ok", in_stock=True, gtin="8004399021234",
    )
    db.add_all([same, wrong])
    db.commit()
    preview = apply_live_audit(db, dry_run=True)
    assert preview["offers_withheld_model"] == 1
    apply_live_audit(db, dry_run=False)
    db.commit()
    assert wrong.withheld is True and same.withheld is False
    # Stejné EAN nespasí jiný kód. 220.30 zůstane schované.
    html = _c.get(f"/produkt/{product.id}").text
    assert "BScom" not in html and "8" in html


def test_google_sale_price_is_not_the_compared_price():
    xml = """<?xml version="1.0"?>
    <rss xmlns:g="http://base.google.com/ns/1.0"><channel><item>
      <title>TESLA CoffeeMaster ES400</title>
      <link>https://eshop.tesla-electronics.eu/es400</link>
      <g:price>2489 CZK</g:price>
      <g:sale_price>1867 CZK</g:sale_price>
      <g:availability>in_stock</g:availability>
      <g:condition>new</g:condition>
    </item></channel></rss>"""
    item = feed.parse_google_xml(xml.encode())[0]
    assert item.price == 2489
    assert item.original_price is None
    assert item.condition == "new"


def test_tesla_stored_coupon_price_restores_regular(client_db):
    from lovceny.live_audit import apply_live_audit
    _c, db, make = client_db
    product = make([1867], name="TESLA CoffeeMaster ES400", shop="tesla", category="kavovary", original=2489)
    offer = product.offers[0]
    offer.shop = "tesla"
    db.commit()
    stats = apply_live_audit(db, dry_run=False)
    assert stats["tesla_prices_restored"] == 1
    assert float(offer.price) == 2489 and offer.original_price is None


def test_camera_accessories_and_vacuum_station():
    assert is_accessory_text("Dvojsměrná rukojeť pro akční kameru DJI Osmo Action 6")
    assert is_accessory_text("Makro objektiv pro akční kameru DJI Osmo Action 6")
    assert is_accessory_text("DJI Osmo Action 3 multifunkční nabíjecí stanice")
    assert is_accessory_text("Sada na potápění pro DJI Osmo Action 3")
    assert is_accessory_text("Freewell ND4 filtr na DJI Osmo Action 3")
    assert is_accessory_text("DJI Osmo Action 3 extrémní baterie")
    assert not is_accessory_text("Sony Alpha 7 Mark IV tělo / Fotoaparát / Bez objektivu")
    assert not is_accessory_text("Akční kamera DJI Osmo Action 5 Pro Standard Combo")
    assert is_station_accessory("Xiaomi Mi Robot Vacuum-Mop 2 Ultimate Autoempty Station")
    assert is_station_accessory("Vysypávací stanice pro Xiaomi Mi Robot Vacuum")
    assert not is_station_accessory("Roborock S8 Pro Ultra se samovysypávací stanicí")
    assert not is_accessory_text("Roborock S8 Pro Ultra se samovysypávací stanicí")


def test_accessory_product_is_hidden_and_station_offer_detached(client_db):
    from lovceny.live_audit import apply_live_audit
    from lovceny.models import Offer
    c, db, make = client_db
    grip = make(
        [2490],
        name="Dvojsměrná rukojeť pro akční kameru DJI Osmo Action 6",
        shop="dronpro",
        category="fotoaparaty",
    )
    vacuum = make(
        [8990],
        name="Xiaomi Mi Robot Vacuum-Mop 2",
        shop="alza",
        category="vysavace",
    )
    station = Offer(
        product=vacuum, shop="softcom",
        url="https://www.softcom.cz/eshop/vysypavaci-stanice-pro-xiaomi-robot-vacuum_d262051.html",
        price=2490, status="ok", in_stock=True,
    )
    db.add(station)
    db.commit()
    stats = apply_live_audit(db, dry_run=False)
    db.commit()
    assert stats["products_hidden_accessory"] >= 1
    assert stats["offers_withheld_accessory"] == 1
    assert station.withheld is True
    assert c.get(f"/produkt/{grip.id}", follow_redirects=False).status_code == 404
    page = c.get(f"/produkt/{vacuum.id}").text
    assert "Softcom" not in page


def test_offers_sort_by_price_and_card_ignores_hidden(client_db):
    from lovceny.models import Offer
    c, db, make = client_db
    product = make([799], name="Swissten ANC Focus černá", shop="bscom", category="sluchatka")
    cheap = Offer(
        product=product, shop="allegro",
        url="https://allegro.cz/nabidka/swissten-focus-759",
        price=759, status="ok", in_stock=True,
    )
    hidden = Offer(
        product=product, shop="mironet",
        url="https://www.mironet.cz/swissten-hidden",
        price=100, status="ok", in_stock=True, withheld=True,
    )
    db.add_all([cheap, hidden])
    db.commit()
    ordered = sort_offers_for_display(product.offers)
    assert [float(o.price) for o in ordered] == [759, 799]
    assert visible_offer_count(product.offers) == 2
    html = c.get("/kategorie/sluchatka").text
    assert "2 obchodů" in html
    page = c.get(f"/produkt/{product.id}").text
    assert page.index("759") < page.index("799")
    assert "Seřazeno výhradně podle ceny" in page


def test_shop_name_links_to_status_block_when_it_exists(client_db):
    c, db, make = client_db
    make([1000], name="TESLA Sound EB10", shop="tesla", category="sluchatka")
    html = c.get("/obchody").text
    assert 'href="/stav#obchod-tesla"' in html
    assert ">TESLA</b></a>" in html or ">TESLA</b></a>" in html.replace("\n", "")
    status = c.get("/stav").text
    assert 'id="obchod-tesla"' in status


def test_price_fetch_does_not_republish_a_used_phone(client_db):
    from lovceny.auto_resolve import apply_listing
    c, db, make = client_db
    phone = make(
        [20390],
        name="Apple iPhone 15 256GB Black Stav produktu: Zánovní",
        shop="tera",
        category="telefony",
    )
    phone.catalog_hidden = True
    phone.needs_review = False
    phone.offers[0].in_stock = True
    phone.offers[0].status = "ok"
    db.commit()
    assert apply_listing(db, phone) == "hidden"
    assert phone.catalog_hidden is True
    assert phone.needs_review is False
    assert c.get(f"/produkt/{phone.id}", follow_redirects=False).status_code == 404


def test_feed_item_roundtrip_types():
    assert isinstance(FeedItem("1", "A", "https://example.test/a", 10), FeedItem)
