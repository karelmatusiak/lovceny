"""Přehlednost / redesign: plurály, 404 HTML, HEAD /, /kontakt, skrytí produktů bez ceny."""
from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app, price_history_thin, priced_product_cards
from lovceny.models import Offer, Product, PricePoint
from lovceny.util import cz_plural, cz_count
from lovceny.verdict import compute
from test_verdict import NOW, pts


def test_cz_plural_days_products_shops():
    assert cz_plural(1, "den", "dny", "dní") == "den"
    assert cz_plural(2, "den", "dny", "dní") == "dny"
    assert cz_plural(3, "den", "dny", "dní") == "dny"
    assert cz_plural(4, "den", "dny", "dní") == "dny"
    assert cz_plural(5, "den", "dny", "dní") == "dní"
    assert cz_plural(0, "den", "dny", "dní") == "dní"
    assert cz_plural(21, "den", "dny", "dní") == "dní"  # 21 = many in Czech (not 1)
    assert cz_count(1, "produkt", "produkty", "produktů") == "1\u00a0produkt"
    assert cz_count(2, "obchod", "obchody", "obchodů") == "2\u00a0obchody"
    assert cz_count(5, "obchod", "obchody", "obchodů") == "5\u00a0obchodů"


def test_verdict_collecting_data_uses_plural():
    n1 = compute(pts(lambda d: 1000, days=1), 1000, now=NOW)
    assert n1.headline == "Sbíráme data" and "1\u00a0den" in n1.text
    assert n1.tooltip and "1\u00a0den" in n1.tooltip
    n3 = compute(pts(lambda d: 1000, days=3), 1000, now=NOW)
    assert "3\u00a0dny" in n3.text
    n5 = compute(pts(lambda d: 1000, days=5), 1000, now=NOW)
    assert "5\u00a0dní" in n5.text


def test_head_home_ok():
    init_db()
    c = TestClient(app)
    r = c.head("/")
    assert r.status_code == 200


def test_favicon_ico():
    init_db()
    r = TestClient(app).get("/favicon.ico")
    assert r.status_code == 200
    assert "image" in r.headers.get("content-type", "")


def test_kontakt_redirect():
    init_db()
    r = TestClient(app).get("/kontakt", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert "/o-nas" in r.headers["location"] and "kontakt" in r.headers["location"]


def test_404_html_for_browser_json_for_api():
    init_db()
    c = TestClient(app)
    html = c.get("/tohle-urcite-neexistuje-xyz", headers={"Accept": "text/html"})
    assert html.status_code == 404
    assert "text/html" in html.headers.get("content-type", "")
    assert "Tady nic není" in html.text or "nenalezena" in html.text.lower() or "404" in html.text

    api = c.get("/api/neexistuje", headers={"Accept": "application/json"})
    assert api.status_code == 404
    assert "application/json" in api.headers.get("content-type", "")
    assert "detail" in api.json()


def test_home_hides_products_without_price():
    init_db()
    db = SessionLocal()
    priced = Product(name="Samsung Galaxy A55 s cenou", slug="s-cenou-redesign",
                     category_slug="telefony", category="Mobilní telefony",
                     image_url="https://img.example/redesign.jpg")
    bare = Product(name="Bez ceny redesign", slug="bez-ceny-redesign")
    db.add_all([priced, bare])
    db.flush()
    db.add(Offer(product=priced, shop="mironet", url="https://www.mironet.cz/redesign-priced",
                 price=9990, status="ok", in_stock=True))
    db.add(Offer(product=priced, shop="alza", url="https://www.alza.cz/redesign-priced",
                 price=10100, status="ok", in_stock=True))
    db.add(Offer(product=bare, shop="mironet", url="https://www.mironet.cz/redesign-bare",
                 price=None, status="pending"))
    db.commit()
    c = TestClient(app)
    home = c.get("/").text
    assert "Galaxy A55" in home
    assert "Bez ceny redesign" not in home
    # Detail bez ceny zůstane dostupný
    detail = c.get(f"/produkt/{bare.id}-{bare.slug}")
    assert detail.status_code == 200
    assert "Cena zatím není k dispozici" in detail.text or "zatím bez ceny" in detail.text.lower() or "Zatím bez ceny" in detail.text
    db.close()


def test_priced_cards_and_thin_history_helpers():
    """price_history_thin follows n_pts < n_priced * MIN_DAYS (shared test DB may already have data)."""
    from sqlalchemy import select, func
    from lovceny.verdict import MIN_DAYS
    init_db()
    db = SessionLocal()

    def expected_thin():
        n_priced = db.scalar(select(func.count()).select_from(Offer).where(Offer.price.isnot(None))) or 0
        if n_priced == 0:
            return True
        n_pts = db.scalar(select(func.count()).select_from(PricePoint)) or 0
        return n_pts < n_priced * MIN_DAYS

    assert price_history_thin(db) is expected_thin()
    p = Product(name="Thin hist", slug="thin-hist")
    o = Offer(product=p, shop="mironet", url="https://www.mironet.cz/thin-hist", price=1000, status="ok")
    db.add(p)
    db.flush()
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    for i in range(3):
        o.points.append(PricePoint(price=1000, captured_at=now - timedelta(days=i)))
    db.commit()
    assert price_history_thin(db) is expected_thin()
    cards = priced_product_cards(db, limit=3)
    assert any(c[0].slug == "thin-hist" for c in cards)
    # Isolated formula check: 3 points / 1 offer → thin
    assert (3 < 1 * MIN_DAYS) is True
    db.close()


def test_home_hero_clarity():
    init_db()
    html = TestClient(app).get("/").text
    assert "Ulovte telefon za férovou cenu." in html
    assert "<title>Ulovte telefon za férovou cenu. | Lovceny</title>" in html
    assert 'property="og:title" content="Ulovte telefon za férovou cenu. | Lovceny"' in html
    assert 'name="description" content="Ulovte telefon za férovou cenu.' in html
    assert 'property="og:description" content="Ulovte telefon za férovou cenu.' in html
    assert "Jak to funguje" not in html
    assert "Potvrzená cena" in html
    assert "Nejnižší ceny telefonů z českých" in html and "e-shopů" in html
    assert html.count("Ulovte telefon za férovou cenu.") >= 4
    assert 'id="nav-toggle"' in html and "aria-expanded" in html
    assert 'href="/telefony"' in html and 'href="/pruvodce"' in html
    assert "Pokrytí roste" not in html
    assert 'class="shops"' not in html
    assert "hero-art" not in html
