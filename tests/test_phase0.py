"""Fáze 0: špatné ceny se neukazují a úklid katalogu neběží sám."""
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.matching import titles_conflict
from lovceny.models import Offer, PricePoint, Product, ShopRunStat, TriageDecision
from lovceny.shops.feed import FeedItem, offer_conflicts_product
from lovceny.service import apply_feed

TOKEN = "phase0-admin-token1"
PHONE = "Elektronika > Telefony a příslušenství > Smartphony a mobilní telefony"


def _basic(token):
    raw = base64.b64encode(f"karel:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def test_titles_reject_the_five_foreign_offers():
    pairs = [
        ("Google Pixel 10 Pro XL 256GB", "Motorola Signature 512GB"),
        ("Electrolux 900 vysavač", "Larma vysavač tyčový"),
        ("Jabra Evolve2 85", "Guess sluchátka bezdrátová"),
        ("Apple iPhone 16e 128GB bílá", "Apple iPhone 15 128GB"),
    ]
    for product, foreign in pairs:
        assert titles_conflict(product, foreign), foreign
        assert offer_conflicts_product(product, "https://www.example.cz/" + foreign.replace(" ", "-"), foreign)
    assert not titles_conflict("Apple iPhone 16e 128GB bílá", "iPhone 16e 128GB white")


def test_mismatched_offer_goes_to_triage_and_is_not_attached():
    init_db()
    db = SessionLocal()
    host = Product(
        name="Google Pixel 10 Pro XL 256GB", slug="pixel-10", ean="0195949035838",
        category_slug="telefony", category="Mobilní telefony",
    )
    db.add(host)
    db.commit()
    item = FeedItem(
        "m1", "Motorola Signature 512 GB",
        "https://www.example.cz/motorola-signature-512gb",
        21618, ean="0195949035838", category=PHONE, in_stock=True,
    )
    stats = apply_feed(db, "allegro", [item], create_missing=True, max_create=5)
    db.refresh(host)
    assert stats["skipped_mismatch"] >= 1
    assert host.offers == []
    row = db.query(TriageDecision).filter_by(kind="model_mismatch", status="pending").one()
    assert row.product_id == host.id
    assert "motorola-signature" in (row.payload or "")
    db.close()


def test_known_bad_offers_detach_once_and_keep_history():
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    pixel = Product(id=328, name="Google Pixel 10 Pro XL 256GB", slug="pixel-10-pro-xl",
                    category_slug="telefony", category="Mobilní telefony")
    electrolux = Product(id=420, name="Electrolux 900 vysavač", slug="electrolux-900",
                         category_slug="vysavace", category="Vysavače")
    jabra = Product(id=452, name="Jabra Evolve2 85", slug="jabra-evolve2-85",
                    category_slug="sluchatka", category="Sluchátka")
    iphone = Product(id=2, name="Apple iPhone 16e 128GB bílá", slug="iphone-16e-bila",
                     category_slug="telefony", category="Mobilní telefony")
    db.add_all([pixel, electrolux, jabra, iphone])
    db.flush()
    bad = [
        Offer(product=pixel, shop="allegro", url="https://allegro.cz/nabidka/motorola-signature-512gb",
              price=21618, in_stock=True, status="ok"),
        Offer(product=electrolux, shop="allegro", url="https://allegro.cz/nabidka/larma-vysavac-tycovy",
              price=4499, in_stock=True, status="ok"),
        Offer(product=jabra, shop="allegro", url="https://allegro.cz/nabidka/guess-sluchatka-bila",
              price=1013, in_stock=True, status="ok"),
        Offer(product=jabra, shop="allegro", url="https://allegro.cz/nabidka/guess-sluchatka-cerna",
              price=1100, in_stock=True, status="ok"),
        Offer(product=iphone, shop="istyle", url="https://istyle.cz/iphone-15-128gb-cerna",
              price=14000, in_stock=True, status="ok"),
    ]
    keep = Offer(product=iphone, shop="istyle", url="https://istyle.cz/iphone-16e-128gb-bila",
                 price=15990, in_stock=True, status="ok")
    db.add_all(bad + [keep])
    db.flush()
    for offer in bad:
        db.add(PricePoint(offer=offer, price=offer.price, captured_at=now, source="feed"))
    db.commit()
    from lovceny.phase0_data import detach_known_bad_offers
    preview = detach_known_bad_offers(db, apply=False)
    assert preview["dry_run"] is True and preview["detached"] == 5
    db.expire_all()
    assert db.get(Offer, bad[0].id).product_id == 328
    applied = detach_known_bad_offers(db, apply=True)
    assert applied["detached"] == 5
    db.expire_all()
    assert db.get(Product, 328).slug == "pixel-10-pro-xl"
    assert db.get(Product, 2).offers[0].url.endswith("iphone-16e-128gb-bila")
    moved = db.get(Offer, bad[0].id)
    assert moved.product_id != 328
    assert moved.product.needs_review is True
    assert len(moved.points) == 1 and float(moved.points[0].price) == 21618
    again = detach_known_bad_offers(db, apply=True)
    assert again["detached"] == 0
    db.close()


def test_stale_and_blocked_are_not_cheapest_and_have_no_buy(client_db):
    client, db, make = client_db
    now = datetime.now(timezone.utc)
    p = make([20000], name="Pixel ověřený", category="telefony", shop="alza")
    fresh = p.offers[0]
    fresh.last_checked = now
    stale = Offer(product=p, shop="mironet", url="https://www.mironet.cz/pixel-stale",
                  price=1000, in_stock=True, status="ok", last_checked=now - timedelta(days=5))
    blocked = Offer(product=p, shop="softcom", url="https://www.softcom.cz/pixel-block",
                    price=500, in_stock=True, status="blocked", last_checked=now,
                    last_error="captcha")
    db.add_all([
        stale, blocked,
        ShopRunStat(run_id="r1", shop="mironet", run_at=now - timedelta(hours=18), ok=4, attempts=4),
        ShopRunStat(run_id="r2", shop="mironet", run_at=now - timedelta(hours=8), ok=4, attempts=4),
    ])
    db.commit()
    html = client.get(f"/produkt/{p.id}-{p.slug}").text
    assert "Koupit za" in html and "20" in html
    assert "nejlevnější" in html
    assert html.index("Koupit za") < html.index("Cena neověřena")
    assert "Cenu se nepodařilo ověřit" in html
    assert "offer-unverified" in html
    assert html.count("btn-shop") == 1
    home = client.get("/").text
    deals = client.get("/slevy").text
    assert "1 000" not in home and "1 000" not in deals
    assert "500" not in html.split("Koupit za", 1)[0]


def test_unreviewed_products_stay_on_their_page_but_not_in_listings(client_db):
    client, db, make = client_db
    named = make([9000], name="xiaomi watch 5 z136650", category="chytre-hodinky", shop="mironet")
    named.offers[0].url = "https://www.mironet.cz/xiaomi-watch-5-z136650"
    bare = make([8000], name="Monitor bez kategorie", shop="mironet")
    ok = make([7000], name="Apple iPhone 16e 128GB bílá", category="telefony", shop="istyle")
    db.commit()
    from lovceny.listing_review import queue_listing_review
    from lovceny.triage import approve
    stats = queue_listing_review(db)
    assert stats["queued"] >= 2
    db.expire_all()
    assert named.needs_review and bare.needs_review and not ok.needs_review
    home = client.get("/").text
    cat = client.get("/kategorie/chytre-hodinky").text
    sm = client.get("/sitemap.xml").text
    assert "xiaomi watch" not in home.lower() and "Monitor bez kategorie" not in home
    assert "xiaomi watch" not in cat.lower()
    assert f"/produkt/{named.id}-" not in sm and f"/produkt/{bare.id}-" not in sm
    assert f"/produkt/{ok.id}-" in sm
    page = client.get(f"/produkt/{named.id}")
    assert page.status_code == 200
    assert 'name="robots" content="noindex' in page.text
    decision = db.query(TriageDecision).filter_by(product_id=named.id, kind="confirm_listing").one()
    approve(db, decision, "karel", category="chytre-hodinky")
    db.commit()
    db.expire_all()
    assert named.needs_review is False and named.category_slug == "chytre-hodinky"
    assert "xiaomi" in client.get("/kategorie/chytre-hodinky").text.lower()


def test_repair_catalog_previews_unless_apply_and_admin_can_run_it(monkeypatch):
    init_db()
    db = SessionLocal()
    p = Product(name="Sony USB-C kabel 1 m", slug="usb-kabel", category_slug="telefony", category="Telefony")
    db.add(p)
    db.commit()
    pid = p.id
    db.close()
    import seed
    seed.main(["--repair-catalog"])
    db = SessionLocal()
    assert db.query(TriageDecision).count() == 0
    assert db.get(Product, pid).category_slug == "telefony"
    db.close()
    seed.main(["--repair-catalog", "--apply"])
    db = SessionLocal()
    assert db.query(TriageDecision).filter_by(status="pending").count() >= 1
    assert db.get(Product, pid).category_slug == "telefony"
    db.close()
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    client = TestClient(app)
    preview = client.post("/admin/katalog-uklid", data={"mode": "preview"}, headers=_basic(TOKEN))
    assert preview.status_code == 200 and "Náhled" in preview.text
    assert 'id="pwa-install"' not in client.get("/admin", headers=_basic(TOKEN)).text
    assert 'id="pwa-install"' in client.get("/").text
    assert "Přidej si Lovceny na plochu" not in client.get("/admin", headers=_basic(TOKEN)).text


def test_fetch_workflow_is_triggerable_without_the_full_suite():
    fetch = Path(".github/workflows/fetch.yml").read_text()
    ci = Path(".github/workflows/ci.yml").read_text()
    readme = Path("README.md").read_text()
    assert "repository_dispatch:" in fetch and "fetch-prices" in fetch
    assert "workflow_dispatch" in fetch
    assert "group: fetch-prices" in fetch
    assert "tests/test_pwa" not in fetch
    assert "tests/test_feed.py" in fetch
    assert "python -m pytest -q\n" not in fetch
    assert "python -m pytest -q" in ci
    assert "https://api.github.com/repos/karelmatusiak/lovceny/dispatches" in readme
    assert '{"event_type":"fetch-prices"}' in readme
    assert "contents:write" in readme or "Contents: Read and write" in readme
    assert "0 6,12,18 * * *" in readme


def test_legacy_product_fixes_are_not_part_of_general_repair():
    init_db()
    db = SessionLocal()
    p = Product(
        name="Motorola EDGE 70 12+512 GB", slug="edge-70",
        category_slug="televize", category="Televize",
    )
    db.add(p)
    db.commit()
    from lovceny.catalog_repair import repair_catalog
    from lovceny.one_off_fixes import apply_legacy_product_fixes
    repair_catalog(db, dry_run=False)
    db.expire_all()
    assert db.get(Product, p.id).category_slug == "televize"
    preview = apply_legacy_product_fixes(db, apply=False)
    assert preview["dry_run"] is True and preview["recategorized"] == 1
    db.expire_all()
    assert db.get(Product, p.id).category_slug == "televize"
    apply_legacy_product_fixes(db, apply=True)
    db.expire_all()
    assert db.get(Product, p.id).category_slug == "telefony"
    db.close()
