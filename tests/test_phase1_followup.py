"""Týdenní přehled ve vypnutém stavu a dořešení variant, které 0002 nesloučila."""
import base64

from lovceny.auto_resolve import families_match, resolve_pending
from lovceny.models import PricePoint, Product, TriageDecision
from lovceny.part_codes import apply_part_code_merges
from lovceny.variant_merge import apply_variant_merges, enqueue_variant_proposals, proposal_family


def test_week_path_with_w_redirects_when_digest_is_off(client_db):
    client, _db, _make = client_db
    for path in ("/prehled/2026-w45", "/prehled/2026-45", "/prehled/neco/dalsiho"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 301, path
        assert response.headers["location"] == "/slevy"


def test_week_path_with_w_stays_missing_when_digest_is_on(client_db, monkeypatch):
    monkeypatch.setenv("LOVCENY_SIDE_FEATURES", "1")
    client, _db, _make = client_db
    assert client.get("/prehled/2026-w45", follow_redirects=False).status_code == 404


def test_part_code_merges_only_the_verified_iphone_pair(client_db):
    client, db, make = client_db
    black = make([16490], name="Apple iPhone 15 128 GB Black", category="telefony", shop="istyle")
    black.ean = "195949036033"
    black.offers[0].url = "https://istyle.cz/products/iphone-15-mtp03sx-a"
    blue = make([16490], name="iphone 15 mtp43sx a", category="telefony", shop="istyle")
    blue.needs_review = True
    blue.offers[0].url = "https://istyle.cz/products/iphone-15-mtp43sx-a"
    point_id = blue.offers[0].points[0].id
    pro = make([20000], name="Apple iPhone 15 Pro 128GB", category="telefony", shop="istyle")
    db.commit()
    stats = apply_part_code_merges(db, dry_run=False)
    db.commit()
    assert stats["ids"] == [[black.id, blue.id]]
    assert stats["retired"] == 1
    db.expire_all()
    loser = db.get(Product, blue.id)
    assert loser.retired_at is not None and loser.redirect_product_id == black.id
    assert db.get(Product, pro.id).retired_at is None
    assert db.get(PricePoint, point_id) is not None
    page = client.get(f"/produkt/{blue.id}", follow_redirects=False)
    assert page.status_code == 301
    assert page.headers["location"].startswith(f"/produkt/{black.id}-")
    again = apply_part_code_merges(db, dry_run=False)
    db.commit()
    assert again["groups"] == 0


def test_title_near_matches_stay_separate_unless_variant_tokens(client_db, monkeypatch):
    client, db, make = client_db
    long_pods = ("Apple AirPods 4 s aktivním potlačením hluku bílá (2024) / "
                 "bezdrátová sluchátka / ANC / Bluetooth / výdrž až 30 hodin")
    pods_a = make([3599], name=long_pods, category="sluchatka")
    pods_b = make([3390], name="AirPods 4 s aktivním potlačováním hluku", category="sluchatka", shop="istyle")
    book_names = (
        "MacBook Pro 14 M5Max (18+40)-128/4T/US/96/N Černý",
        "MacBook Pro 14 M5Max (18+40) – 128/4T/N Černý",
        "MacBook Pro 14 M5Max (18+40) – 128/4T/US/96W Černý",
    )
    books = [make([209000], name=name, category="notebooky", shop="allegro") for name in book_names]
    slim = make([13834], name="PlayStation 5 Digital Edition (verze slim)", category="konzole")
    chassis = make([13900], name="PlayStation 5 Digital Edition 825GB (Slim/E-Chassis)", category="konzole")
    black = make([16000], name="Apple iPhone 15 128 GB černá", category="telefony")
    white = make([17000], name="Apple iPhone 15 256 GB bílá", category="telefony")
    pro = make([20000], name="Apple iPhone 15 Pro 128GB", category="telefony")
    db.commit()
    assert not families_match([pods_a.name, pods_b.name], "sluchatka")
    assert not families_match([slim.name, chassis.name], "konzole")
    assert not families_match(book_names, "notebooky")
    assert families_match([black.name, white.name], "telefony")
    assert not families_match([black.name, pro.name], "telefony")
    assert proposal_family(pods_a.name, "sluchatka") == proposal_family(pods_b.name, "sluchatka")
    queued = enqueue_variant_proposals(db)
    db.commit()
    preview = resolve_pending(db, dry_run=True)
    assert preview["buckets"]["varianta"]["merged"] == 0
    assert preview["buckets"]["varianta"]["separate"] == len(queued["ids"])
    resolve_pending(db, dry_run=False)
    merged = apply_variant_merges(db, dry_run=False)
    db.commit()
    db.expire_all()
    assert db.get(Product, pods_b.id).retired_at is None
    assert db.get(Product, chassis.id).retired_at is None
    assert all(db.get(Product, book.id).retired_at is None for book in books)
    assert db.get(Product, pro.id).retired_at is None
    colour = db.get(Product, max(black.id, white.id))
    assert colour.retired_at is not None
    assert colour.redirect_product_id == min(black.id, white.id)
    assert merged["retired"] >= 1
    monkeypatch.setenv("ADMIN_TOKEN", "triage-admin-token1")
    token = base64.b64encode(b"karel:triage-admin-token1").decode()
    page = client.get("/admin/trideni", headers={"authorization": "Basic " + token})
    assert page.status_code == 200
    assert "Jen záznam" in page.text
    assert "Schválit" not in page.text
