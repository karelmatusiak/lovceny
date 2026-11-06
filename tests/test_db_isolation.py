"""Testy sdílejí jeden engine, ale každý test musí vidět prázdnou databázi. Produkce se neotevírá."""
from lovceny.db import DATABASE_URL, SessionLocal, engine
from lovceny.models import Product


def test_engine_is_the_temporary_sqlite_file():
    path = engine.url.database or ""
    assert engine.dialect.name == "sqlite"
    assert path.startswith("/tmp/") and "lovceny-pytest-" in path and path.endswith("test.db")
    assert DATABASE_URL == "sqlite:///" + path
    assert "neon.tech" not in DATABASE_URL
    assert not DATABASE_URL.startswith("postgres")


def test_first_test_leaves_a_headphone_row(client_db):
    _, db, make = client_db
    make([1000], name="Izolace sluchátka která nesmí přežít další test", category="sluchatka")
    assert db.query(Product).filter(Product.category_slug == "sluchatka").count() == 1


def test_next_test_does_not_see_the_previous_headphone(client_db):
    _, db, _make = client_db
    names = [p.name for p in db.query(Product).filter(Product.category_slug == "sluchatka")]
    assert names == []
    fresh = SessionLocal()
    try:
        assert fresh.query(Product).count() == 0
    finally:
        fresh.close()
