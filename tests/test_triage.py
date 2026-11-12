"""Mapování kategorií, GTIN a fronta /admin/trideni."""
import base64

from fastapi.testclient import TestClient

from lovceny.categories import BY_SLUG
from lovceny.db import SessionLocal, init_db
from lovceny.gtin import checksum_ok, gtins_conflict, normalize_gtin
from lovceny.main import app
from lovceny.models import Offer, Product, ShopCategoryMapping, TriageDecision, TriageLog
from lovceny.shops.feed import FeedItem
from lovceny.catalog_repair import repair_catalog
from lovceny.service import apply_feed
from lovceny.triage import (
    FEED_SHOPS, approve_pending, ensure_mappings, lookup_mapping, map_shop_category,
)

TOKEN = "triage-admin-token1"
PHONE = "Elektronika > Telefony a příslušenství > Smartphony a mobilní telefony"
GRILL = "Zahrada | Grily | Plynové grily"


def _basic(password: str) -> dict:
    raw = base64.b64encode(f"karel:{password}".encode()).decode()
    return {"authorization": "Basic " + raw}


def test_gtin_normalise_and_checksum():
    assert checksum_ok("4006381333931")
    assert normalize_gtin("4006381333931") == "4006381333931"
    assert normalize_gtin(" 4006 3813 3393 1 ") == "4006381333931"
    assert normalize_gtin("04006381333931") == "4006381333931"
    assert normalize_gtin("96385074") == "96385074"
    assert normalize_gtin("036000291452") == "036000291452"
    assert normalize_gtin("4006381333932") is None
    assert normalize_gtin("123") is None
    assert normalize_gtin("8590669123456") is None  # špatný součet, do GTIN nepatří
    assert normalize_gtin(None) is None


def test_seed_mappings_prefix_accessory_and_shops():
    init_db()
    db = SessionLocal()
    n = ensure_mappings(db)
    again = ensure_mappings(db)
    db.commit()
    assert n == again and n >= 100
    shops = {row.shop for row in db.query(ShopCategoryMapping).all()}
    assert {"alza", "istyle", "sencor", "xm", "4home", "jbl", "allegro", "dyson"} <= shops
    assert set(FEED_SHOPS) <= shops
    assert lookup_mapping(db, "alza", "Elektronika | Mobilní telefony") == "telefony"
    assert lookup_mapping(db, "istyle", "Heureka.cz | Elektronika | Mobilní telefony") == "telefony"
    assert lookup_mapping(db, "4home", "Úklid > Vysavače > Příslušenství k vysavačům") == "accessory"
    assert lookup_mapping(db, "4home", "Úklid > Vysavače > Akumulátorové vysavače") == "vysavace"
    assert lookup_mapping(db, "dyson", "Bezkabelové vysavače") == "vysavace"
    assert lookup_mapping(db, "allegro", PHONE) == "telefony"
    assert lookup_mapping(db, "allegro", "Elektronika > Telefony a příslušenství > Příslušenství GSM > Obaly a kryty") == "accessory"
    db.close()


def test_gtin_attaches_same_product_and_stores_shop_category():
    init_db()
    db = SessionLocal()
    host = Product(name="Apple iPhone 15 128GB černá", slug="iphone-15-gtin", ean="4006381333931",
                   category_slug="telefony", category="Mobilní telefony")
    db.add(host)
    db.commit()
    item = FeedItem(
        "g1", "Apple iPhone 15 128GB černá", "https://istyle.cz/products/iphone-15-gtin-b",
        18990, ean="04006381333931", category="Heureka.cz | Elektronika | Mobilní telefony", in_stock=True,
    )
    st = apply_feed(db, "istyle", [item])
    assert st["attached"] == 1, st
    db.refresh(host)
    offer = host.offers[0]
    assert offer.shop == "istyle"
    assert offer.gtin == "4006381333931"
    assert offer.shop_category.endswith("Mobilní telefony")
    db.close()


def test_title_only_is_queued_and_conflicting_gtin_blocks_merge():
    init_db()
    db = SessionLocal()
    a = Product(name="Herní sluchátka Modecom Volcano Prometheus", slug="volcano-a",
                category_slug="sluchatka", category="Sluchátka", ean="1111111111116")
    b = Product(name="Sluchátka Modecom Volcano Prometheus", slug="volcano-b",
                category_slug="sluchatka", category="Sluchátka", ean="2222222222222")
    db.add_all([a, b])
    db.commit()
    assert gtins_conflict(a, b)
    st = repair_catalog(db, dry_run=False)
    assert st["products_merged"] == 0 and st["gtin_blocked"] >= 1
    assert db.query(TriageDecision).filter_by(kind="merge", status="pending").count() == 0
    plain = Product(name="Apple iPhone 15 128GB Black", slug="iphone-title",
                    category_slug="telefony", category="Mobilní telefony")
    db.add(plain)
    db.commit()
    item = FeedItem(
        "t1", "Apple iPhone 15 128GB černá", "https://istyle.cz/products/iphone-title-only",
        16490, category="Heureka.cz | Elektronika | Mobilní telefony", in_stock=True,
    )
    st2 = apply_feed(db, "istyle", [item])
    assert st2["attached"] == 0 and st2.get("hidden", 0) >= 1, st2
    assert db.query(Offer).filter_by(shop="istyle").count() == 0
    assert approve_pending(db) == 0
    db.expire_all()
    assert db.query(Offer).filter_by(product_id=plain.id, shop="istyle").count() == 0
    db.close()


def test_unmapped_stays_hidden_until_approve_or_reject():
    init_db()
    db = SessionLocal()
    item = FeedItem(
        "u1", "Krabice úložná 40 litrů", "https://www.4home.cz/krabice-ulozna-40/",
        1290, category=GRILL, in_stock=True,
    )
    st = apply_feed(db, "4home", [item], create_missing=True, max_create=5)
    assert st["created"] == 0 and st.get("hidden", 0) >= 1, st
    prod = db.query(Product).filter(Product.name == item.name).one()
    assert prod.catalog_hidden and prod.category_slug is None
    client = TestClient(app)
    hidden = client.get(f"/produkt/{prod.id}", follow_redirects=False)
    assert hidden.status_code == 301 and hidden.headers["location"] == "/"
    decision = db.query(TriageDecision).filter_by(product_id=prod.id, status="auto").one()
    import os
    os.environ["ADMIN_TOKEN"] = TOKEN
    try:
        r = client.post(f"/admin/trideni/{decision.id}/kategorie", data={"kategorie": "telefony"},
                        headers=_basic(TOKEN), follow_redirects=False)
        assert r.status_code == 303
    finally:
        os.environ.pop("ADMIN_TOKEN", None)
    db.expire_all()
    prod = db.get(Product, prod.id)
    assert prod.category_slug is None and prod.catalog_hidden is True
    db.close()


def test_bulk_map_is_remembered_for_the_next_item():
    init_db()
    db = SessionLocal()
    items = [
        FeedItem("b1", "Plynový gril Alfa", "https://www.4home.cz/gril-alfa/", 3990, category=GRILL, in_stock=True),
        FeedItem("b2", "Plynový gril Beta", "https://www.4home.cz/gril-beta/", 4590, category=GRILL, in_stock=True),
    ]
    apply_feed(db, "4home", items, create_missing=True, max_create=5)
    assert db.query(Product).filter(Product.catalog_hidden.is_(True)).count() == 2
    n = map_shop_category(db, "4home", GRILL, "kavovary", "karel")
    db.commit()
    assert n == 0
    saved = db.query(ShopCategoryMapping).filter_by(shop="4home", match_mode="exact", source="admin").one()
    assert saved.target == "kavovary"
    nxt = FeedItem("b3", "Plynový gril Gama", "https://www.4home.cz/gril-gama/", 4990, category=GRILL, in_stock=True)
    st = apply_feed(db, "4home", [nxt], create_missing=True, max_create=5)
    assert st["created"] == 1, st
    gama = db.query(Product).filter(Product.name == nxt.name).one()
    assert gama.category_slug == "kavovary" and gama.catalog_hidden is False
    again = apply_feed(db, "4home", items, create_missing=True, max_create=5)
    assert again["created"] == 0
    db.expire_all()
    assert db.query(Product).filter_by(category_slug="kavovary", catalog_hidden=False).count() == 3
    db.close()


def test_reject_leaves_product_hidden():
    init_db()
    db = SessionLocal()
    item = FeedItem("r1", "Krabice úložná 60 litrů", "https://www.4home.cz/krabice-60/",
                    1490, category=GRILL, in_stock=True)
    apply_feed(db, "4home", [item], create_missing=True, max_create=5)
    prod = db.query(Product).one()
    assert prod.catalog_hidden is True
    assert db.query(TriageDecision).filter_by(status="pending").count() == 0
    assert repair_catalog(db, dry_run=False)["queued"] == 0
    db.close()


def test_admin_trideni_auth_and_not_in_sitemap(monkeypatch):
    init_db()
    client = TestClient(app)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get("/admin/trideni").status_code == 404
    assert client.post("/admin/trideni/prihlaseni", data={"heslo": "x"}).status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", "kratky")
    assert client.get("/admin/trideni").status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    assert client.get("/admin/trideni").status_code == 401
    assert client.get("/admin/trideni", headers=_basic("spatne")).status_code == 401
    assert client.get(f"/admin/trideni?token={TOKEN}").status_code == 401
    ok = client.get("/admin/trideni", headers=_basic(TOKEN))
    assert ok.status_code == 200
    assert "Třídění katalogu" in ok.text and "Jen záznam" in ok.text
    assert "Schválit" not in ok.text
    assert "noindex" in ok.headers["x-robots-tag"]
    login = client.post("/admin/trideni/prihlaseni", data={"heslo": TOKEN},
                        headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert login.status_code == 303
    cookie = login.headers["set-cookie"].lower()
    assert "lovceny_admin=" in cookie and "httponly" in cookie and "secure" in cookie
    sm = client.get("/sitemap.xml").text
    assert "/admin/trideni" not in sm
    assert "Disallow: /admin" in client.get("/robots.txt").text
