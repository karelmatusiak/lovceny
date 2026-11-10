"""Allegro-only produkty bez známé značky se vyřadí, známé značky a ostatní obchody zůstávají."""
from lovceny.allegro_narrow import apply_allegro_narrow
from lovceny.matching import KNOWN_BRANDS
from lovceny.models import Offer, PricePoint, Product


def test_known_brand_list_is_the_curated_set():
    assert "apple" in KNOWN_BRANDS and "samsung" in KNOWN_BRANDS and "bosch" in KNOWN_BRANDS
    assert "wenom" not in KNOWN_BRANDS
    from lovceny.allegro_narrow import is_known_brand
    assert is_known_brand("De'Longhi Magnifica ECAM 22.110")
    assert not is_known_brand("Cubot Kingkong Dura")
    assert not is_known_brand("Kruger&Matz 65 palců Google TV")
    assert not is_known_brand("Baseus Inspire XH1 by Bose")
    assert not is_known_brand("SteelSeries Arctis GameBuds PS5")
    assert is_known_brand("Google Pixel 9 128GB")


def test_unknown_allegro_only_retires_to_category_and_keeps_history(client_db):
    client, db, make = client_db
    keep = make([5000], name="Samsung Galaxy S25 FE 128GB černá", category="telefony", shop="allegro")
    drop = make([900], name="Bezdrátová sluchátka Wenom AuraTouch ANC", category="sluchatka", shop="allegro")
    mixed = make([8000], name="Neznámá značka MixPhone 128GB", category="telefony", shop="allegro")
    db.add(Offer(product=mixed, shop="alza", url="https://www.alza.cz/mixphone", price=8100,
                 status="ok", in_stock=True))
    point_id = drop.offers[0].points[0].id
    db.commit()
    preview = apply_allegro_narrow(db, dry_run=True)
    db.rollback()
    assert preview["retire"] == 1 and preview["keep"] == 1
    assert db.get(Product, drop.id).retired_at is None

    applied = apply_allegro_narrow(db, dry_run=False)
    db.commit()
    assert applied["retire"] == 1 and applied["keep"] == 1
    db.expire_all()
    assert db.get(Product, keep.id).retired_at is None
    assert db.get(Product, mixed.id).retired_at is None
    retired = db.get(Product, drop.id)
    assert retired.retired_at is not None and retired.redirect_product_id is None
    assert db.get(PricePoint, point_id).price is not None
    page = client.get(f"/produkt/{drop.id}-{drop.slug}", follow_redirects=False)
    assert page.status_code == 301
    assert page.headers["location"] == "/kategorie/sluchatka"
    again = apply_allegro_narrow(db, dry_run=False)
    db.commit()
    assert again["retire"] == 0
