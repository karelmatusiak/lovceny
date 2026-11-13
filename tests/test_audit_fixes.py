"""Audit lovceny.cz: špatné párování, vyloučené značky, verdikt skladem, počty kategorií."""
import re

from lovceny.audit_data import detach_mismatched_offers, hide_excluded_brands
from lovceny.categories import accept_feed_item
from lovceny.exclusions import is_excluded_brand
from lovceny.matching import titles_conflict
from lovceny.models import Offer, Product
from lovceny.service import apply_feed
from lovceny.shops.feed import FeedItem, offer_conflicts_product

PHONE = "Elektronika > Telefony a příslušenství > Smartphony a mobilní telefony"
NB = "Elektronika > Počítače > Notebooky"
VAC = "Elektronika > Audio, video a domácí spotřebiče > Malé domácí spotřebiče > Domácí spotřebiče > Vysavače > Tyčové vysavače"
TV = "Elektronika > Audio, video a domácí spotřebiče > Televize a video > Televizory"
EAN = "0195949035838"


def test_distinguishing_tokens_block_the_audited_pairs():
    cyclone = "Dyson V8 Cyclone"
    assert titles_conflict(cyclone, "Dyson V8 Origin fialový")
    assert titles_conflict(cyclone, "Akumulátorový vysavač Dyson V8 Absolute")
    assert titles_conflict("Dyson V8", "Dyson V15 Detect Absolute")
    assert titles_conflict("Samsung Galaxy S25", "Samsung Galaxy S25 FE")
    assert titles_conflict("Samsung Galaxy S24", "Samsung Galaxy S24 Ultra")
    assert titles_conflict("Samsung OM55N-S", "Samsung Galaxy S25 FE 8/256GB")
    assert not titles_conflict(cyclone, "Tyčový vysavač Dyson V8 Cyclone")
    assert not titles_conflict("Apple iPhone 16e 128GB bílá", "iPhone 16e 128GB white")
    assert offer_conflicts_product(
        "Samsung OM55N-S",
        "https://www.mironet.cz/samsung-galaxy-s25-fe-8-256gb",
        category_slug="televize",
    )
    assert offer_conflicts_product(
        cyclone,
        "https://allegro.cz/nabidka/dyson-v8-origin-fialovy",
        category_slug="vysavace",
    )
    assert not offer_conflicts_product(
        cyclone,
        "https://www.alza.cz/dyson-v8-cyclone-d123",
        category_slug="vysavace",
    )


def test_feed_does_not_attach_a_different_model(client_db):
    _client, db, _make = client_db
    host = Product(
        name="Dyson V8 Cyclone", slug="dyson-v8-cyclone", ean=EAN,
        category_slug="vysavace", category="Vysavače",
    )
    db.add(host)
    db.commit()
    item = FeedItem(
        "o1", "Dyson V8 Origin fialový",
        "https://allegro.cz/nabidka/dyson-v8-origin-fialovy",
        8139, ean=EAN, category=VAC, in_stock=True,
    )
    stats = apply_feed(db, "allegro", [item], create_missing=True, max_create=5)
    db.refresh(host)
    assert stats["skipped_mismatch"] >= 1
    assert host.offers == []


def test_migration_withholds_mismatched_offers_and_keeps_the_right_one(client_db):
    _client, db, _make = client_db
    vacuum = Product(
        name="Dyson V8 Cyclone", slug="dyson-v8-cyclone",
        category_slug="vysavace", category="Vysavače",
    )
    screen = Product(
        name="Samsung OM55N-S", slug="samsung-om55n-s",
        category_slug="televize", category="Televize",
    )
    db.add_all([vacuum, screen])
    db.flush()
    origin = Offer(
        product=vacuum, shop="allegro", price=8139, in_stock=True, status="ok",
        url="https://allegro.cz/nabidka/dyson-v8-origin-fialovy",
    )
    absolute = Offer(
        product=vacuum, shop="xm", price=9990, in_stock=True, status="ok",
        url="https://www.xm.cz/dyson-v8-absolute",
    )
    same = Offer(
        product=vacuum, shop="alza", price=10990, in_stock=True, status="ok",
        url="https://www.alza.cz/dyson-v8-cyclone",
    )
    phone = Offer(
        product=screen, shop="mironet", price=13490, in_stock=True, status="ok",
        url="https://www.mironet.cz/samsung-galaxy-s25-fe-8-256gb",
    )
    db.add_all([origin, absolute, same, phone])
    db.commit()
    stats = detach_mismatched_offers(db)
    db.refresh(origin)
    db.refresh(absolute)
    db.refresh(same)
    db.refresh(phone)
    assert origin.withheld and absolute.withheld and phone.withheld
    assert same.withheld is False
    assert stats["offers_withheld"] == 3
    assert stats["products_with_detached_offers"] == 2


def test_excluded_brands_stay_out_of_import_listing_and_sitemap(client_db):
    client, db, make = client_db
    assert is_excluded_brand("Gigabyte GAMING A16 PRO")
    assert is_excluded_brand("Karl Lagerfeld parfém")
    assert not is_excluded_brand("Lenovo IdeaPad 15")
    assert accept_feed_item("Gigabyte GAMING A16 PRO notebook 16GB", NB, 24990) is None
    assert accept_feed_item("Karl Lagerfeld parfém", PHONE, 1290) is None
    assert accept_feed_item("Notebook Lenovo IdeaPad 15 16GB", NB, 14990) == "notebooky"
    stats = apply_feed(db, "alza", [
        FeedItem("g1", "Gigabyte GAMING A16 PRO notebook", "https://www.alza.cz/gigabyte-a16",
                 24990, category=NB, in_stock=True),
    ], create_missing=True, max_create=5)
    assert stats["created"] == 0
    assert db.query(Product).count() == 0

    phone = make([8990], name="Samsung Galaxy A55 128GB", category="telefony")
    gigabyte = make([24990], name="Gigabyte GAMING A16 PRO notebook 16GB", category="notebooky")
    lagerfeld = make([890], name="Karl Lagerfeld parfém", category="telefony")
    counts = hide_excluded_brands(db)
    db.refresh(gigabyte)
    db.refresh(lagerfeld)
    assert gigabyte.catalog_hidden and gigabyte.retired_at is not None
    assert lagerfeld.catalog_hidden and lagerfeld.retired_at is not None
    assert counts["excluded_removed_from_public"] == 2
    home = client.get("/").text
    listing = client.get("/kategorie/notebooky").text
    phones = client.get("/kategorie/telefony").text
    assert "Gigabyte" not in listing and "A16" not in listing
    assert "Lagerfeld" not in phones
    assert "Galaxy A55" in phones
    assert "Gigabyte" not in home and "Lagerfeld" not in home
    sitemap = client.get("/sitemap.xml").text
    assert f"/produkt/{phone.id}-" in sitemap
    assert f"/produkt/{gigabyte.id}-" not in sitemap
    assert f"/produkt/{lagerfeld.id}-" not in sitemap
    gone = client.get(f"/produkt/{gigabyte.id}-{gigabyte.slug}", follow_redirects=False)
    assert gone.status_code == 301 and gone.headers["location"] == "/"


def test_category_counts_use_the_listing_filter(client_db):
    client, db, make = client_db
    a55 = make([8990], name="Samsung Galaxy A55 128GB", category="telefony")
    db.add(Offer(product=a55, shop="alza", url="https://www.alza.cz/a55-count", price=9100, status="ok", in_stock=True))
    db.commit()
    make([299], name="Samsung Galaxy A55 silikonový kryt", category="telefony")
    make([], name="Samsung Galaxy A35 bez ceny", category="telefony")
    make([24990], name="Gigabyte GAMING A16 PRO", category="notebooky")
    home = client.get("/").text
    index = client.get("/kategorie", follow_redirects=False)
    listing = client.get("/kategorie/telefony").text
    total = re.search(r"z (\d+)\s", listing.replace("\xa0", " "))
    assert total and total.group(1) == "1"
    assert "Potvrzená cena" in home
    assert "Galaxy A55" in home
    assert "1\xa0produkt" not in home
    assert index.status_code == 301 and index.headers["location"] == "/telefony"
    assert "Gigabyte" not in client.get("/kategorie/notebooky").text


def test_out_of_stock_has_no_buy_verdict(client_db):
    client, db, make = client_db
    p = make([9000] * 10, name="Samsung Galaxy A55 Pro", category="telefony")
    p.offers[0].in_stock = False
    p.offers[0].url = "https://www.mironet.cz/samsung-galaxy-a55-pro"
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert "v-buy" not in html and "Kupuj" not in html
    assert "Není skladem" in html
    card = client.get("/kategorie/telefony").text
    start = card.find('class="card pcard"')
    assert start != -1
    snippet = card[start:start + 2500]
    intro = snippet.find('class="cat-intro"')
    if intro != -1:
        snippet = snippet[:intro]
    assert "v-buy" not in snippet and "Kupuj" not in snippet
    assert "Není skladem" in snippet
