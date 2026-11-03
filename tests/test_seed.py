import seed
from lovceny.categories import BY_SLUG
from lovceny.models import Offer, Product
from lovceny.shops import shop_for_url


def test_catalog_is_valid_and_broad():
    items = seed.load_catalog()
    assert 60 <= len(items) <= 200
    assert len({i["url"] for i in items}) == len(items)
    cats = {i["cat"] for i in items}
    assert cats == set(BY_SLUG)  # každá kategorie má produkty
    for i in items:
        s = shop_for_url(i["url"])
        assert s and s.can_scrape, i  # jen obchody, které čtení dovolují
        assert i["name"].strip()
        assert "vestavne-spotrebice.cz" not in i["url"], i  # GH Actions ConnectTimeout


def test_seed_catalog_idempotent(client_db, monkeypatch):
    client, db, make = client_db
    added = []

    def fake_add(db_, url, fetch_now=True):
        added.append(url)
        p = Product(name="Seed " + url[-12:], slug="seed")
        db_.add(Offer(product=p, shop=shop_for_url(url).slug, url=url, status="ok", price=1000))
        db_.commit()
        return p, ""
    monkeypatch.setattr(seed, "add_by_url", fake_add)
    items = seed.load_catalog()[:5]
    st1 = seed.seed_catalog(db, items)
    assert st1["new"] == 5 and st1["priced"] == 5 and len(added) == 5
    st2 = seed.seed_catalog(db, items)
    assert st2 == {"new": 0, "existing": 5, "priced": 0, "failed": 0} and len(added) == 5
    for i in items:
        o = db.query(Offer).filter_by(url=shop_for_url(i["url"]).canonical_url(i["url"])).one()
        assert o.product.category_slug == i["cat"]


def test_seed_dry_run_writes_nothing(client_db, monkeypatch):
    client, db, make = client_db
    monkeypatch.setattr(seed, "add_by_url", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nesmí stahovat")))
    items = seed.load_catalog()[-3:]
    st = seed.seed_catalog(db, items, dry_run=True)
    assert st["new"] + st["existing"] == 3


def test_retire_vestavne_removes_dead_offers(client_db):
    _, db, make = client_db
    p = make([1000], name="Vestavne only")
    o = p.offers[0]
    o.shop, o.url, o.price, o.status = "vestavne", "https://www.vestavne-spotrebice.cz/x/", None, "error"
    db.commit()
    mixed = make([2000], name="Mixed shop")
    db.add(Offer(product=mixed, shop="vestavne", url="https://www.vestavne-spotrebice.cz/y/", status="error"))
    db.commit()
    st = seed.retire_vestavne(db)
    assert st["offers"] == 2 and st["products"] == 1  # pure vestavne soft-retired
    retired = db.get(Product, p.id)
    assert retired is not None and retired.retired_at is not None
    assert db.get(Product, mixed.id) is not None
    assert db.get(Product, mixed.id).retired_at is None
    assert not any(o.shop == "vestavne" for o in db.get(Product, mixed.id).offers)
