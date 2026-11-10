"""Týdenní přehled ve vypnutém stavu a dořešení variant, které 0002 nesloučila."""
import base64

from lovceny.models import PricePoint, Product, TriageDecision
from lovceny.part_codes import apply_part_code_merges
from lovceny.triage import approve
from lovceny.variant_merge import enqueue_variant_proposals, proposal_family


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


def test_title_near_matches_wait_for_approval(client_db, monkeypatch):
    client, db, make = client_db
    long_pods = ("Apple AirPods 4 s aktivním potlačením hluku bílá (2024) / "
                 "bezdrátová sluchátka / ANC / Bluetooth / výdrž až 30 hodin")
    pods_a = make([3599], name=long_pods, category="sluchatka")
    pods_b = make([3390], name="AirPods 4 s aktivním potlačováním hluku", category="sluchatka", shop="istyle")
    books = [
        make([209000], name=name, category="notebooky", shop="allegro")
        for name in (
            "MacBook Pro 14 128/4T/US/96/N Černý",
            "MacBook Pro 14 128/4T/N Černý",
            "MacBook Pro 14 128/4T/US/96W Černý",
        )
    ]
    slim = make([13834], name="PlayStation 5 Digital Edition (verze slim)", category="konzole")
    chassis = make([13900], name="PlayStation 5 Digital Edition 825GB (Slim/E-Chassis)", category="konzole")
    pro = make([20000], name="Apple iPhone 15 Pro 128GB", category="telefony")
    plain = make([16000], name="Apple iPhone 15 128 GB Black", category="telefony")
    db.commit()
    assert proposal_family(pods_a.name, "sluchatka") == proposal_family(pods_b.name, "sluchatka")
    assert proposal_family(pro.name, "telefony") != proposal_family(plain.name, "telefony")
    washer_a = proposal_family("Bosch WGE03200BY pračka", "pracky")
    washer_b = proposal_family("Bosch pračka WUU28T71", "pracky")
    assert washer_a and washer_b and washer_a != washer_b
    anc = proposal_family("Mcdodo Bezdrátová Bluetooth Sluchátka Do Uší S Mikrofonem Pouzdro Anc Enc", "sluchatka")
    plain_mcdodo = proposal_family("Mcdodo Sluchátka Do Uší S Mikrofonem Pouzdro", "sluchatka")
    assert anc != plain_mcdodo
    nano = "MacBook Pro 14 M5Max (18+40) – 128/8T/96/Nan Černý"
    plain_book = "MacBook Pro 14 M5Max (18+40)-128/4T/US/96/N Černý"
    assert proposal_family(nano, "notebooky") == proposal_family(plain_book, "notebooky")
    queued = enqueue_variant_proposals(db)
    db.commit()
    groups = {tuple(ids) for ids in queued["ids"]}
    assert (pods_a.id, pods_b.id) in groups or (pods_b.id, pods_a.id) in groups
    assert tuple(sorted(p.id for p in books)) in groups
    assert tuple(sorted((slim.id, chassis.id))) in groups
    assert all(pro.id not in ids for ids in queued["ids"])
    assert db.get(Product, pods_b.id).retired_at is None
    decision = db.query(TriageDecision).filter_by(kind="variant_merge", product_id=min(pods_a.id, pods_b.id)).one()
    assert str(pods_a.id) in (decision.payload or "") and str(pods_b.id) in (decision.payload or "")
    monkeypatch.setenv("ADMIN_TOKEN", "triage-admin-token1")
    token = base64.b64encode(b"karel:triage-admin-token1").decode()
    page = client.get("/admin/trideni", headers={"authorization": "Basic " + token})
    assert page.status_code == 200
    assert "Varianta modelu" in page.text
    assert f"#{pods_a.id}" in page.text and f"#{pods_b.id}" in page.text
    ok, _detail = approve(db, decision, "karel")
    db.commit()
    assert ok
    db.expire_all()
    canonical = min(pods_a.id, pods_b.id)
    other = max(pods_a.id, pods_b.id)
    assert db.get(Product, other).redirect_product_id == canonical
    page = client.get(f"/produkt/{other}", follow_redirects=False)
    assert page.status_code == 301
    assert page.headers["location"].startswith(f"/produkt/{canonical}-")
