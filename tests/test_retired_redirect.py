"""Soft-retired produkty: 301 na kategorii, aktivní kanonický slug, neznámé id = 404."""
from datetime import datetime, timezone

from lovceny.models import Product, Offer
from lovceny.seo import sitemap_xml
from lovceny.service import search_db
from seed import soft_retire_product


def test_retired_product_redirects_to_category(client_db):
    c, db, make = client_db
    p = make([10000], name="iPhone 17e Retired", category="telefony")
    # simuluj retire: smaž nabídky + soft-retire
    for o in list(p.offers):
        db.delete(o)
    db.flush()
    assert soft_retire_product(p)
    db.commit()
    r = c.get(f"/produkt/{p.id}-{p.slug}", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/kategorie/telefony"
    # kategorie žije
    assert c.get("/kategorie/telefony").status_code == 200


def test_retired_without_category_redirects_home(client_db):
    c, db, make = client_db
    p = make([5000], name="Orphan Cat Product")
    p.category_slug = None
    for o in list(p.offers):
        db.delete(o)
    db.flush()
    soft_retire_product(p)
    db.commit()
    r = c.get(f"/produkt/{p.id}-x", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/"


def test_unknown_product_still_404(client_db):
    c, db, make = client_db
    r = c.get("/produkt/999999-neexistuje", follow_redirects=False)
    assert r.status_code == 404


def test_active_wrong_slug_redirects_canonical(client_db):
    c, db, make = client_db
    p = make([8000], name="Canonical Phone", category="telefony")
    r = c.get(f"/produkt/{p.id}-spatny-slug", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == f"/produkt/{p.id}-{p.slug}"
    ok = c.get(f"/produkt/{p.id}-{p.slug}")
    assert ok.status_code == 200


def test_retired_excluded_from_sitemap_search_listings(client_db):
    c, db, make = client_db
    active = make([9000], name="Active List Phone", category="telefony")
    retired = make([9100], name="Retired List Phone", category="telefony")
    for o in list(retired.offers):
        db.delete(o)
    db.flush()
    soft_retire_product(retired)
    db.commit()

    sm = sitemap_xml(db)
    assert f"/produkt/{active.id}-" in sm
    assert f"/produkt/{retired.id}-" not in sm

    hits = search_db(db, "Retired List Phone")
    assert all(h.id != retired.id for h in hits)

    # kategorie výpis
    html = c.get("/kategorie/telefony").text
    assert "Active List Phone" in html or "Active List" in html
    assert "Retired List Phone" not in html
