"""Fáze 1: Alembic baseline, kategorie jen z mapování, párování EAN → kód výrobce."""
from lovceny.db import _head, _revision, init_db
from lovceny.matching import brand_and_mpn
from lovceny.models import Offer, Product, TriageDecision
from lovceny.service import add_by_url, apply_feed
from lovceny.shops.feed import FeedItem
from lovceny.triage import ensure_mappings, resolve_category


def test_alembic_baseline_is_idempotent():
    init_db()
    assert _revision() == "0001_baseline"
    assert _head() == "0001_baseline"
    init_db()
    assert _revision() == "0001_baseline"


def test_category_comes_from_mapping_not_the_title(client_db):
    _client, db, _make = client_db
    ensure_mappings(db)
    # Název sám o sobě by dřív spadl do telefonů. Bez cesty obchodu se produkt nezaloží.
    bare = resolve_category(db, "alza", "Apple iPhone 15 128GB", None, 18990)
    assert bare.action == "skip" and bare.slug is None
    mapped = resolve_category(db, "alza", "Apple iPhone 15 128GB", "Mobilní telefony", 18990)
    assert mapped.action == "high" and mapped.slug == "telefony"
    unknown = resolve_category(db, "alza", "Neznámý výrobek 9000", "Heureka | Něco jiného", 5000)
    assert unknown.action == "unmapped" and unknown.slug is None


def test_brand_and_manufacturer_code_attaches_title_does_not(client_db):
    _client, db, make = client_db
    p = make([8000], name="Sencor SVC 7811WH tyčový vysavač", category="vysavace", shop="mironet")
    assert brand_and_mpn(p.name) is not None
    item = FeedItem(
        item_id="svc-1", name="Sencor SVC 7811WH bílý", url="https://www.alza.cz/sencor-svc-7811wh",
        price=7600, ean=None, category="Úklid | Vysavače",
    )
    stats = apply_feed(db, "alza", [item], create_missing=True)
    db.expire_all()
    assert stats["attached"] == 1 and stats["created"] == 0
    assert db.query(Offer).filter_by(url="https://www.alza.cz/sencor-svc-7811wh").one().product_id == p.id

    titled = make([1000], name="Testovací výrobek Alfa", category="telefony", shop="mironet")
    by_title = FeedItem(
        item_id="jen-nazev", name="Testovací výrobek Alfa", url="https://www.alza.cz/jen-nazev",
        price=900, ean=None, category="Elektronika | Mobilní telefony",
    )
    queued = apply_feed(db, "alza", [by_title], create_missing=True)
    assert queued["attached"] == 0 and queued["created"] == 0
    assert db.query(Offer).filter_by(url="https://www.alza.cz/jen-nazev").count() == 0
    assert db.query(TriageDecision).filter_by(kind="title_match", product_id=titled.id).count() == 1


def test_page_url_goes_to_the_queue(client_db):
    _client, db, _make = client_db
    prod, msg = add_by_url(db, "https://www.alza.cz/nejaky-produkt-z-adresy.htm", fetch_now=False)
    assert prod is None and "adresy" in msg
    assert db.query(Product).count() == 0
    row = db.query(TriageDecision).filter_by(kind="track_url").one()
    assert row.product_id is None and "alza.cz" in (row.payload or "")
