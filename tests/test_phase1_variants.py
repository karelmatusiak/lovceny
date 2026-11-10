"""Jedna stránka na model: barva a paměť jsou varianty, stará URL jde 301, historie zůstane."""
from lovceny.matching import model_family
from lovceny.models import Offer, PricePoint, Product, Watch
from lovceny.variant_merge import apply_variant_merges, option_label


def _pair(db, make, left, right, category="telefony"):
    a = make([10000, 9000], name=left, category=category, shop="alza")
    b = make([11000], name=right, category=category, shop="istyle")
    b.offers[0].url = "https://www.istyle.cz/" + str(b.id)
    db.commit()
    return a, b


def test_colour_and_storage_are_one_model_pro_is_not():
    assert model_family("Apple iPhone 15 128GB černá", "telefony") == model_family(
        "Apple iPhone 15 256GB bílá", "telefony")
    assert model_family("Apple iPhone 15 128GB", "telefony") != model_family(
        "Apple iPhone 15 Pro 128GB", "telefony")
    assert model_family("LG OLED 55 palců", "televize") != model_family("LG OLED 65 palců", "televize")


def test_merge_keeps_ids_history_and_redirects(client_db):
    client, db, make = client_db
    black, white = _pair(db, make, "Apple iPhone 15 128GB černá", "Apple iPhone 15 256GB bílá")
    point_ids = [pt.id for o in black.offers + white.offers for pt in o.points]
    offer_ids = [black.offers[0].id, white.offers[0].id]
    db.add(Watch(product_id=white.id, email="a@b.cz", target_price=8000, token="tok-varianta"))
    db.commit()
    preview = apply_variant_merges(db, dry_run=True)
    db.rollback()
    assert preview["dry_run"] is True and preview["groups"] == 1 and preview["retired"] == 1
    assert db.get(Product, white.id).retired_at is None

    applied = apply_variant_merges(db, dry_run=False)
    db.commit()
    assert applied["retired"] == 1
    db.expire_all()
    assert db.get(Product, black.id).retired_at is None
    loser = db.get(Product, white.id)
    assert loser.retired_at is not None and loser.redirect_product_id == black.id
    assert {o.product_id for o in db.query(Offer).filter(Offer.id.in_(offer_ids))} == {black.id}
    assert [pt.id for pt in db.query(PricePoint).filter(PricePoint.id.in_(point_ids))] == point_ids
    assert db.query(Watch).filter_by(token="tok-varianta").one().product_id == black.id
    labels = {option_label(n) for n in ("Apple iPhone 15 128GB černá", "Apple iPhone 15 256GB bílá")}
    page = client.get(f"/produkt/{white.id}-{white.slug}", follow_redirects=False)
    assert page.status_code == 301
    assert page.headers["location"] == f"/produkt/{black.id}-{black.slug}"
    html = client.get(f"/produkt/{black.id}-{black.slug}").text
    for label in labels:
        assert label in html
    again = apply_variant_merges(db, dry_run=False)
    db.commit()
    assert again["groups"] == 0


def test_different_models_stay_separate(client_db):
    _client, db, make = client_db
    _pair(db, make, "Apple iPhone 15 128GB", "Apple iPhone 16 128GB")
    stats = apply_variant_merges(db, dry_run=False)
    db.commit()
    assert stats["groups"] == 0
    assert db.query(Product).filter(Product.retired_at.is_(None)).count() == 2
