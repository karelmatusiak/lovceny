"""TTL cache: real_deals (homepage) a assign_all_display_names (product)."""
import time

from lovceny import deals
from lovceny.main import assign_all_display_names, clear_display_names_cache


def test_real_deals_cache_avoids_second_product_rows(client_db, monkeypatch):
    client, db, make = client_db
    # historie → verdikt Kupuj (cena u minima, medián vyšší)
    make([2000, 2000, 2000, 2000, 1000], name="Deal Phone A")
    make([3000, 3000, 3000, 3000, 1500], name="Deal Phone B")
    deals.clear_cache()
    calls = {"n": 0}
    real = deals.product_rows

    def wrapped(db, **kw):
        calls["n"] += 1
        return real(db, **kw)

    monkeypatch.setattr(deals, "product_rows", wrapped)
    r1 = deals.real_deals(db, top=3)
    r2 = deals.real_deals(db, top=3)
    assert calls["n"] == 1, calls
    assert [x.product.id for x in r1["buys"]] == [x.product.id for x in r2["buys"]]
    assert [x.product.id for x in r1["fakes"]] == [x.product.id for x in r2["fakes"]]
    # rehydrate: atributy z aktuální session
    for row in r2["buys"] + r2["fakes"]:
        assert row.product.name
        assert row.best.price is not None
    deals.clear_cache()
    deals.real_deals(db, top=3)
    assert calls["n"] == 2


def test_assign_all_display_names_cached(client_db):
    _, db, make = client_db
    make([100], name="Samsung Galaxy S24 Ultra 256GB")
    make([100], name="Samsung Galaxy S24 Ultra 512GB")
    clear_display_names_cache()
    m1 = assign_all_display_names(db)
    m2 = assign_all_display_names(db)
    assert m1 is m2  # stejný dict z cache
    assert len(m1) >= 2
    clear_display_names_cache()
    m3 = assign_all_display_names(db)
    assert m3 is not m1
    assert m3 == m1


def test_assign_all_display_names_ttl_expires(client_db, monkeypatch):
    _, db, make = client_db
    make([100], name="TTL Phone X")
    clear_display_names_cache()
    monkeypatch.setattr("lovceny.main._DISPLAY_NAMES_TTL_S", 0.05)
    m1 = assign_all_display_names(db)
    time.sleep(0.08)
    m2 = assign_all_display_names(db)
    assert m1 == m2 and m1 is not m2
