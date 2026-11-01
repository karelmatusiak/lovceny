"""Shop buy CTAs are prominent and keep affiliate link attributes."""
from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.models import Offer, Product


def _seed_product(suffix="a"):
    init_db()
    with SessionLocal() as db:
        slug = f"cta-test-phone-{suffix}"
        existing = db.query(Product).filter_by(slug=slug).one_or_none()
        if existing:
            return existing.id, existing.slug
        p = Product(name="CTA Test Phone", slug=slug, category="Telefony", category_slug="telefony")
        db.add(p)
        db.flush()
        db.add(Offer(
            product_id=p.id,
            shop="mironet",
            url=f"https://www.mironet.cz/cta-test-{suffix}/",
            price=9999,
            status="ok",
            in_stock=True,
        ))
        db.commit()
        return p.id, p.slug


def test_product_page_primary_buy_cta():
    pid, slug = _seed_product("prod")
    html = TestClient(app).get(f"/produkt/{pid}-{slug}").text
    assert 'class="btn btn-primary btn-buy"' in html
    assert "Koupit v Mironet za" in html
    assert 'class="btn btn-shop"' in html
    assert "Do obchodu" in html
    assert 'rel="nofollow sponsored noopener"' in html
    assert 'target="_blank"' in html
    assert "offer-best" in html


def test_pcard_has_shop_cta():
    _seed_product("card")
    html = TestClient(app).get("/kategorie/telefony").text
    assert 'class="btn btn-shop-sm"' in html
    assert "Do obchodu" in html
    assert 'rel="nofollow sponsored noopener"' in html
