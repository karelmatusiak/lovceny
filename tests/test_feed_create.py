"""Nový produkt ze stažení cen jen při jisté kategorii a GTIN nebo jednom kódu výrobce."""
from lovceny.db import SessionLocal, init_db
from lovceny.gtin import check_digit
from lovceny.matching import brand_and_mpn
from lovceny.models import Offer, Product
from lovceny.offer_trust import offer_is_promoted
from lovceny.service import apply_feed, feed_display_name, single_part_code
from lovceny.shops.feed import FeedItem


def ean(body: str) -> str:
    return body + str(check_digit(body))


def item(**kw) -> FeedItem:
    data = dict(
        item_id="A1",
        name="Samsung Galaxy A55 8/128GB",
        url="https://www.alza.cz/samsung-galaxy-a55.htm",
        price=8990,
        ean=ean("590123412345"),
        category="Mobilní telefony",
        in_stock=True,
        image="https://cdn.alza.cz/img/samsung-a55.jpg",
    )
    data.update(kw)
    return FeedItem(**data)


def run(items, **kw):
    init_db()
    db = SessionLocal()
    stats = apply_feed(db, "alza", items, create_identified=True, **kw)
    return db, stats


def test_display_name_uses_names_cleanup():
    assert "Samsung" in feed_display_name("samsung galaxy a55 8/128gb")
    assert single_part_code("Samsung QE55Q80C") is not None
    assert single_part_code("Samsung QE55Q80C QE65Q80C") is None


def test_gtin_and_high_category_creates_product_with_image():
    db, stats = run([item()])
    assert stats["created"] == 1 and stats["eligible"] == 1
    prod = db.query(Product).one()
    assert prod.category_slug == "telefony"
    assert prod.catalog_hidden is False and prod.needs_review is False
    assert prod.image_url and "samsung-a55" in prod.image_url
    assert "Samsung" in prod.name
    assert prod.offers[0].in_stock is True


def test_each_gate_refuses_creation():
    accessory = item(name="Pouzdro pro iPhone 16", url="https://www.alza.cz/pouzdro.htm", ean=ean("590123412346"))
    used = item(name="Apple iPhone 16 128GB", url="https://www.alza.cz/pouzity.htm",
                ean=ean("590123412347"), condition="used")
    gigabyte = item(name="Gigabyte Notebook Aorus 16", url="https://www.alza.cz/gigabyte.htm",
                    ean=ean("590123412348"), category="Notebooky", price=25000)
    lagerfeld = item(name="Karl Lagerfeld sluchátka", url="https://www.alza.cz/kl.htm",
                     ean=ean("590123412349"), category="Sluchátka", price=2000)
    cheap = item(price=100, url="https://www.alza.cz/levny.htm", ean=ean("590123412350"))
    unmapped = item(category="Zahradní nábytek", url="https://www.alza.cz/zahrada.htm", ean=ean("590123412351"))
    no_identity = item(name="Sluchátka bílá", url="https://www.alza.cz/bila.htm", ean=None,
                       category="Sluchátka", price=2000)
    two_codes = item(name="Samsung QE55Q80C QE65Q80C", url="https://www.alza.cz/dva.htm", ean=None,
                     category="Televize", price=20000)
    assert brand_and_mpn(two_codes.name) is not None and len(brand_and_mpn(two_codes.name)[1]) != 1
    db, stats = run([accessory, used, gigabyte, lagerfeld, cheap, unmapped, no_identity, two_codes])
    assert stats["created"] == 0
    assert db.query(Product).count() == 0


def test_existing_gtin_attaches_instead_of_creating():
    code = ean("590123412345")
    db, first = run([item()])
    assert first["created"] == 1
    stats = apply_feed(db, "datart", [item(
        url="https://www.datart.cz/samsung-galaxy-a55.htm", item_id="D2",
    )], create_identified=True)
    assert stats["created"] == 0 and stats.get("second_offer") == 1
    assert db.query(Product).filter(Product.ean == code).count() == 1
    assert db.query(Offer).count() == 2


def test_part_code_attaches_before_create():
    db, first = run([item(
        name="Apple iPhone 16 A3287", url="https://www.alza.cz/iphone-16.htm", ean=None,
        category="Mobilní telefony", price=18990, image=None,
    )])
    assert first["created"] == 1
    stats = apply_feed(db, "datart", [item(
        name="Apple iPhone 16 A3287", url="https://www.datart.cz/iphone-16.htm", ean=None,
        category="Mobilní telefony", price=18490, item_id="IP2",
    )], create_identified=True)
    assert stats["created"] == 0 and stats.get("second_offer") == 1
    assert db.query(Product).count() == 1
    assert db.query(Offer).count() == 2


def test_title_only_does_not_create():
    db, _ = run([item()])
    named = db.query(Product).one().name
    stats = apply_feed(db, "alza", [item(
        name=named, url="https://www.alza.cz/jen-nazev.htm", ean=None, item_id="T",
    )], create_identified=True)
    assert stats["created"] == 0
    assert db.query(Product).count() == 1


def test_cap_stops_further_creates():
    items = [
        item(url="https://www.alza.cz/a.htm", ean=ean("590123412345"), item_id="1"),
        item(name="Samsung Galaxy A35 6/128GB", url="https://www.alza.cz/b.htm",
             ean=ean("590123412346"), item_id="2"),
    ]
    _db, stats = run(items, max_create=1)
    assert stats["eligible"] == 2 and stats["created"] == 1 and stats["capped"] == 1


def test_out_of_stock_is_created_but_not_promoted():
    db, stats = run([item(in_stock=False, url="https://www.alza.cz/neni.htm")])
    assert stats["created"] == 1
    offer = db.query(Offer).one()
    assert offer.in_stock is False
    assert offer_is_promoted(offer, product=offer.product) is False


def test_without_flag_feed_does_not_create():
    init_db()
    db = SessionLocal()
    stats = apply_feed(db, "alza", [item()])
    assert stats["created"] == 0
    assert db.query(Product).count() == 0


def test_dry_run_rolls_back():
    init_db()
    db = SessionLocal()
    apply_feed(db, "alza", [item()], create_identified=True, commit=False)
    db.rollback()
    assert db.query(Product).count() == 0
