"""Hard-delete tombstones restore → 301 on known retired ids."""
from lovceny.retire_tombstones import restore_tombstones, TOMBSTONES
from lovceny.models import Product


def test_restore_tombstones_redirects(client_db):
    c, db, make = client_db
    n = restore_tombstones(db)
    assert n == len(TOMBSTONES)
    assert restore_tombstones(db) == 0  # idempotent
    # representative phones
    r = c.get("/produkt/160-iphone-17e-256-gb-bily", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/kategorie/telefony"
    r = c.get("/produkt/167-apple-iphone-16-128gb-ruzovy", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/kategorie/telefony"
    # no category → home
    r = c.get("/produkt/300-mikrotik", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/"
    # curl -I / uvicorn HEAD (TestClient bez context manageru HEAD někdy 405)
    assert c.request("HEAD", "/produkt/160-x", follow_redirects=False).status_code == 301
