"""Veřejný web je specialista na telefony. Cizí kategorie se nemažou, jen se neukazuje."""
from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.models import Offer, PricePoint, Product
from sqlalchemy import event

from lovceny.phones import find_model, group_phones, guide_models, phone_models, storage_deals, GUIDE_BY_SLUG
from lovceny.service import apply_feed
from lovceny.shops.feed import FeedItem
from tests.test_feed_create import ean, item


def _phone(db, name, price, shop="alza", stock=True, slug=None):
    product = Product(name=name, slug=slug or name.lower().replace(" ", "-"), category_slug="telefony", category="Mobilní telefony")
    offer = Offer(product=product, shop=shop, url=f"https://www.{shop}.cz/{product.slug}.htm", price=price, in_stock=stock, status="ok")
    db.add(product)
    db.flush()
    db.add(PricePoint(offer=offer, price=price, in_stock=stock, source="feed"))
    db.commit()
    return product


def test_tablet_stays_off_the_phone_site_and_names_drop_specs():
    init_db()
    db = SessionLocal()
    try:
        tablet = _phone(db, 'Kruger&Matz Drive 11 6 56" 4/64 Gb Lte eSIM 6500 mAh IP68', 3990, slug="drive")
        _phone(db, "Chytrý telefon Apple iPhone 17 Pro 12 GB", 30000, slug="ip17")
        models = group_phones(db.query(Product).all())
        names = [model.name for model in models]
        assert names == ["Apple iPhone 17 Pro"]
        client = TestClient(app)
        assert "Kruger" not in client.get("/").text
        assert client.get(f"/produkt/{tablet.id}", follow_redirects=False).headers["location"] == "/telefony"
    finally:
        db.close()


def test_same_storage_only_and_a_far_outlier_stays_out_of_the_price():
    init_db()
    db = SessionLocal()
    try:
        from lovceny.phones import _fix_casing, _model_name, price_trust
        assert _model_name(["Samsung Galaxy Z FOLD8 256GB černá"]) == "Samsung Galaxy Z Fold8"
        assert _fix_casing("iPhone 16 Pro Max FE 5G") == "iPhone 16 Pro Max FE 5G"
        assert _model_name(["Nothing Phone (3a) Lite 8/128GB černá / Mobilní telefon / 8 GB RAM / 128 GB"]) == "Nothing Phone 3a Lite"
        assert _model_name(["Nothing Phone 3a Lite / Mobilní telefon / Ram"]) == "Nothing Phone 3a Lite"
        assert _model_name(["Apple iPhone 15 128GB modrá / 6.1\" / 128GB / iOS17.3"]) == "Apple iPhone 15"
        assert _model_name(["Mobilní telefon Samsung Galaxy A55 128GB"]) == "Samsung Galaxy A55"
        assert _model_name(["Smartphone Xiaomi Redmi Note 12 128GB"]) == "Xiaomi Redmi Note 12"
        assert _model_name(["Honor 600 Smart 5G Dual SIM barva Meteor Silver paměť 4GB/256GB"]) == "Honor 600 Smart"
        cheap = _phone(db, "Apple iPhone Air 256GB černá", 10000, shop="tera", slug="air-tera")
        _phone(db, "Apple iPhone Air 256GB bílá", 32000, shop="alza", slug="air-alza")
        _phone(db, "Apple iPhone Air 256GB modrá", 33000, shop="datart", slug="air-datart")
        _phone(db, "Apple iPhone Air 1TB černá", 40000, shop="istyle", slug="air-istyle")
        model = group_phones(db.query(Product).all())[0]
        assert model.name == "Apple iPhone Air"
        assert model.lowest == 32000
        assert "tera" not in {offer.shop for offer in model.offers if offer.product_id == cheap.id}
        kept, disputed = price_trust(model.products, [offer for product in model.products for offer in product.offers])
        assert cheap.offers[0].id not in {offer.id for offer in kept}
        assert "256 GB" not in disputed
        assert any(offer.shop == "istyle" for offer in kept)
    finally:
        db.close()


def test_two_shops_far_apart_do_not_claim_a_lowest_price():
    init_db()
    db = SessionLocal()
    try:
        _phone(db, "Samsung Galaxy Z Fold8 512GB krémová", 39653, shop="xm", slug="fold-xm")
        _phone(db, "Samsung Galaxy Z Fold8 512GB krémová", 73368, shop="xupe", slug="fold-xupe")
        model = group_phones(db.query(Product).all())[0]
        assert "512 GB" in model.disputed
        assert model.lowest is None
    finally:
        db.close()


def test_kruger_drive_11_is_a_tablet_everywhere():
    from lovceny.phones import GUIDE_BY_SLUG, guide_models, is_tablet_text
    name = "Smartphone Kruger&Matz Drive 11 6,56'' 4/64 Gb Lte Nfc eSIM 6500 mAh IP68"
    assert is_tablet_text(name)
    assert is_tablet_text('Kruger&Matz Drive 11 6 56" 4/64 Gb Lte eSIM 6500 mAh IP68')
    assert not is_tablet_text('Apple iPhone 16 6,1" 128GB černá')
    init_db()
    db = SessionLocal()
    try:
        tablet = _phone(db, name, 3351, shop="allegro", slug="drive-11")
        _phone(db, "Samsung Galaxy A16 128GB černá", 3930, shop="alza", slug="a16")
        models = group_phones(db.query(Product).all())
        assert all("Kruger" not in model.name and "Drive" not in model.name for model in models)
        guide = guide_models(GUIDE_BY_SLUG["nejlepsi-do-15-tisic"], models)
        assert all("Drive" not in model.name for model in guide)
        client = TestClient(app)
        home = client.get("/").text
        guide_html = client.get("/pruvodce/nejlepsi-do-15-tisic").text
        found = client.get("/hledat?q=Kruger").text
        assert "Kruger" not in home and "Drive 11" not in guide_html and "Drive 11" not in found
        assert "/telefon/" + "drive" not in client.get("/sitemap.xml").text
        assert client.get(f"/produkt/{tablet.id}", follow_redirects=False).headers["location"] == "/telefony"
    finally:
        db.close()


def test_offer_rows_keep_one_cheapest_per_shop():
    init_db()
    db = SessionLocal()
    try:
        from lovceny.phones import shop_offer_rows
        black = _phone(db, "Apple iPhone 16 128GB černá", 18000, shop="tera", slug="ip16-cerna")
        white = _phone(db, "Apple iPhone 16 128GB bílá", 18100, shop="tera", slug="ip16-bila")
        other = _phone(db, "Apple iPhone 16 128GB černá", 19000, shop="alza", slug="ip16-alza")
        model = group_phones([black, white, other])[0]
        rows = shop_offer_rows(model, model.offers)
        assert [row["offer"].shop for row in rows] == ["tera", "alza"]
        assert rows[0]["offer"].price == 18000
        assert rows[0]["color"] == ""
    finally:
        db.close()


def test_card_shop_count_matches_the_shown_price():
    init_db()
    db = SessionLocal()
    try:
        _phone(db, "Apple iPhone Air 256GB černá", 22227, shop="tera", slug="air-256")
        _phone(db, "Apple iPhone Air 512GB černá", 28341, shop="xm", slug="air-512")
        _phone(db, "Apple iPhone Air 1TB zlatá", 40490, shop="istyle", slug="air-1tb")
        model = group_phones(db.query(Product).all())[0]
        assert model.lowest == 22227
        assert model.price_shops == 1
        assert model.shops == 3
        client = TestClient(app)
        catalog = client.get("/telefony").text
        card = catalog.split("iPhone Air", 1)[1].split("</article>", 1)[0]
        assert "1\xa0obchod" in card
        assert "3\xa0obchody" not in card
        _phone(db, "Samsung Galaxy A55 128GB", 8990, shop="alza", slug="a55-alza")
        _phone(db, "Samsung Galaxy A55 128GB", 9100, shop="datart", slug="a55-datart")
        home = client.get("/").text
        assert "Nejnižší teď" not in home
        assert "Nejoblíbenější telefony" in home
        assert "Cenu kontrolujeme denně" in home
        assert "Nikdo neplatí za pořadí" in home
        assert home.find("Galaxy A55") < home.find("iPhone Air")
        air = home.split("iPhone Air", 1)[1].split("</article>", 1)[0]
        assert "22\xa0227" in air
        assert "1\xa0obchod" in air
        assert "Ověřeno 2+ obchody" not in air
        assert "Ověřeno 2+ obchody" in home.split("Galaxy A55", 1)[1].split("</article>", 1)[0]
    finally:
        db.close()


def test_colour_code_and_box_are_one_model_and_the_old_slug_redirects():
    init_db()
    db = SessionLocal()
    try:
        from lovceny.main import tpl
        from lovceny.matching import model_family
        from lovceny.phones import _model_name
        plain = "Apple iPhone 16 128GB černá"
        boxed = "Apple iPhone 16 ULTRAMARINE 128GB A3081 Krabička"
        teal = "Apple iPhone 16 Plus 128GB teal"
        assert _model_name([boxed]) == "Apple iPhone 16"
        assert _model_name(["Apple iPhone 16 berylove zelen 128GB"]) == "Apple iPhone 16"
        assert _model_name([teal]) == "Apple iPhone 16 Plus"
        assert model_family(plain, "telefony") == model_family(boxed, "telefony")
        assert model_family(plain, "telefony") != model_family(teal, "telefony")
        _phone(db, plain, 18000, shop="alza", slug="apple-iphone-16-128gb-cerna")
        _phone(db, "Apple iPhone 16 256GB bílá", 21000, shop="datart", slug="apple-iphone-16-256gb-bila")
        _phone(db, boxed, 19000, shop="xupe", slug="apple-iphone-16-ultramarine-128gb")
        _phone(db, teal, 22000, shop="tera", slug="apple-iphone-16-plus-128gb-teal")
        client = TestClient(app)
        gone = client.get("/telefon/apple-iphone-16-ultramarine-a3081-krabicka", follow_redirects=False)
        assert gone.status_code == 301
        assert gone.headers["location"] == "/telefon/apple-iphone-16"
        plus = client.get("/telefon/apple-iphone-16-plus-teal", follow_redirects=False)
        assert plus.status_code == 301
        assert plus.headers["location"] == "/telefon/apple-iphone-16-plus"
        tpl.env.globals["STATIC_SITE"] = True
        page = client.get("/telefon/apple-iphone-16")
        html = page.text
        assert page.status_code == 200
        assert 'rel="canonical" href="' in html
        assert "/telefon/apple-iphone-16\"" in html
        assert "?barva=" not in html.split('rel="canonical"', 1)[1].split(">", 1)[0]
        assert 'data-barva="ultramar' in html
        assert 'data-barva="černá"' in html or "data-barva=\"&#269;ern&#225;\"" in html
        assert 'data-pamet="128 GB"' in html
        assert 'data-pamet="256 GB"' in html
        assert 'data-phone-chip="barva"' in html
        assert "Tuhle kombinaci barvy a úložiště teď nemáme." in html
        assert "katalog.js" in html
        assert 'rel="nofollow sponsored noopener"' in html
        assert html.count("Do obchodu") == 3
    finally:
        tpl.env.globals["STATIC_SITE"] = False
        db.close()


def test_storage_variants_are_one_model():
    init_db()
    db = SessionLocal()
    try:
        _phone(db, "Apple iPhone 16 128GB černá", 18000, slug="iphone-16-128")
        _phone(db, "Apple iPhone 16 256GB bílá", 21000, shop="datart", slug="iphone-16-256")
        _phone(db, "Pouzdro pro iPhone 16", 400, slug="pouzdro")
        models = group_phones(db.query(Product).all())
        assert len(models) == 1
        assert models[0].lowest == 18000
        deals = storage_deals(models[0])
        assert [row["label"] for row in deals] == ["128 GB", "256 GB"]
        assert deals[0]["price"] == 18000 and deals[1]["price"] == 21000
        assert models[0].shops == 2
    finally:
        db.close()


def test_guide_uses_only_prices_we_have():
    init_db()
    db = SessionLocal()
    try:
        _phone(db, "Samsung Galaxy A55 128GB", 7000, slug="a55")
        _phone(db, "Apple iPhone 16 128GB", 19000, slug="ip16")
        models = group_phones(db.query(Product).all())
        cheap = guide_models(GUIDE_BY_SLUG["nejlepsi-do-10-tisic"], models)
        assert [model.name for model in cheap] == ["Samsung Galaxy A55"]
        versus = guide_models(GUIDE_BY_SLUG["iphone-nebo-samsung-do-15-tisic"], models)
        assert [model.brand for model in versus] == ["Samsung"]
    finally:
        db.close()


def test_thin_filter_explains_the_empty_grid():
    init_db()
    db = SessionLocal()
    try:
        _phone(db, "Samsung Galaxy A55 128GB", 8990, slug="a55")
        client = TestClient(app)
        missing = client.get("/telefony?znacka=Apple&pasmo=do-5").text
        assert "Značka Apple v pásmu do 5 000 Kč" in missing
        assert "jen Apple" in missing
        assert "phone-grid" not in missing
        one = client.get("/telefony?znacka=Samsung").text
        assert "jen 1 model" in one
        assert "celém katalogu" in one
        assert "Galaxy A55" in one
    finally:
        db.close()


def test_public_site_hides_other_categories_and_accessories():
    init_db()
    db = SessionLocal()
    try:
        phone = _phone(db, "Samsung Galaxy A55 128GB", 8990, slug="a55")
        laptop = Product(name="Lenovo IdeaPad 5", slug="ideapad", category_slug="notebooky", category="Notebooky")
        case = Product(name="Pouzdro pro iPhone 16", slug="pouzdro", category_slug="telefony")
        db.add_all([laptop, case])
        db.commit()
        client = TestClient(app)
        home = client.get("/")
        assert home.status_code == 200
        catalog = client.get("/telefony").text
        assert "Samsung Galaxy A55" in catalog
        assert "Samsung Galaxy A55" in home.text
        assert "IdeaPad" not in home.text
        assert "Pouzdro" not in home.text
        assert "Repas" not in home.text
        assert "do 5 000" in home.text
        assert client.get("/kategorie/notebooky", follow_redirects=False).status_code == 301
        assert client.get(f"/produkt/{laptop.id}", follow_redirects=False).headers["location"] == "/"
        model = client.get("/telefon/" + group_phones([phone])[0].slug)
        assert model.status_code == 200
        assert 'rel="nofollow sponsored noopener"' in model.text
        phone.offers[0].in_stock = False
        db.commit()
        oos = client.get("/telefon/" + group_phones([phone])[0].slug).text
        assert "Není skladem" in oos
        assert "Do obchodu" not in oos
        sitemap = client.get("/sitemap.xml").text
        assert "/kategorie/notebooky" not in sitemap
        assert "/telefon/" in sitemap
        assert "/pruvodce/nejlepsi-do-5-tisic" in sitemap
    finally:
        db.close()


def test_fetch_creates_only_phones():
    init_db()
    db = SessionLocal()
    try:
        tv = item(name="Samsung QE55Q80C", url="https://www.alza.cz/qe55.htm", ean=ean("590123412360"),
                  category="Televize", price=18000)
        case = item(name="Pouzdro pro iPhone 16", url="https://www.alza.cz/pouzdro.htm", ean=ean("590123412361"),
                    category="Mobilní telefony", price=400)
        phone = item()
        stats = apply_feed(db, "alza", [tv, case, phone], create_identified=True)
        assert stats["created"] == 1
        names = [row.name for row in db.query(Product).all()]
        assert any("Galaxy" in name or "Samsung" in name for name in names)
        assert not any("QE55" in name or "Pouzdro" in name for name in names)
    finally:
        db.close()


def test_phone_catalog_is_loaded_once_per_session():
    init_db()
    db = SessionLocal()
    try:
        _phone(db, "Apple iPhone 16 128GB černá", 19000, slug="ip16")
        _phone(db, "Samsung Galaxy A55 128GB", 8990, slug="a55")
        selects = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT") and "products" in statement:
                selects.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", capture)
        try:
            first = phone_models(db)
            again = phone_models(db)
            found = find_model(db, first[0].slug)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", capture)
        assert again is first
        assert found is first[0]
        assert len(selects) == 1
    finally:
        db.close()
