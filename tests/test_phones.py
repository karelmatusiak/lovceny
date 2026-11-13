"""Veřejný web je specialista na telefony. Cizí kategorie se nemažou, jen se neukazuje."""
from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.models import Offer, PricePoint, Product
from lovceny.phones import group_phones, guide_models, storage_deals, GUIDE_BY_SLUG
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
