import re
from pathlib import Path

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.models import Offer, Product, Watch
from lovceny.service import record_price
from lovceny.verdict import compute
from test_verdict import NOW, pts

ROOT = Path(__file__).parent.parent


def test_verdict_headlines_and_reasons():
    b = compute(pts(lambda d: 1000), 1030, now=NOW)
    assert b.code == "buy" and b.icon == "✅" and b.headline == "Kupuj – cena u minima"
    assert b.reason == "o 3 % nad 90denním minimem"
    assert compute(pts(lambda d: 1000), 1000, now=NOW).reason == "na 90denním minimu"
    w = compute(pts(lambda d: 900 if d > 40 else 1000), 1000, now=NOW)
    assert w.headline == "Počkej – bývalo levněji" and w.icon == "⏳" and "nad 90denním minimem" in w.reason
    f = compute(pts(lambda d: 1200 if 3 <= d <= 18 else 1000), 1000, now=NOW)
    assert f.icon == "⚠️" and f.headline == "Falešná sleva" and "zdražili" in f.reason
    n = compute(pts(lambda d: 1000, days=3), 1000, now=NOW)
    assert n.headline == "Málo dat" and "7" in n.reason


def test_badges_rendered_with_text_on_cards_and_detail():
    init_db()
    db = SessionLocal()
    p = Product(name="Badge test pračka", slug="badge-test-pracka")
    o = Offer(product=p, shop="planeo", url="https://www.planeo.cz/badge-test.html")
    db.add(p)
    db.flush()
    record_price(db, o, 5000, None, True, "scrape")
    db.commit()
    c = TestClient(app)
    for url in [f"/produkt/{p.id}-{p.slug}", "/hledat?q=badge+test"]:
        html = c.get(url).text
        assert 'class="verdict v-none' in html and "Málo dat" in html and "Verdikt: " in html
        assert "verdict-reason" in html
    detail = c.get(f"/produkt/{p.id}-{p.slug}").text
    assert "verdict-lg" in detail and 'id="hlidani"' in detail and "🔔 Hlídat cenu" in detail
    db.close()


def test_live_results_show_neutral_badge(monkeypatch):
    from lovceny.shops import SHOPS
    from lovceny.shops.search import SearchHit
    monkeypatch.setattr(SHOPS["mironet"], "searcher",
                        lambda q: [SearchHit("mironet", "Zcela nova pracka XY", 9990, "https://www.mironet.cz/xy+dp9/")])
    html = TestClient(app).get("/hledat/zive?q=zcela nova pracka").text
    assert "Zatím bez historie" in html and "Klikni na Sledovat" in html and "Sledovat tento produkt" in html


def test_watch_routes_renamed_and_legacy_redirects():
    init_db()
    db = SessionLocal()
    p = Product(name="Watch route test", slug="watch-route-test")
    db.add(p)
    db.flush()
    w = Watch(product=p, email="a@b.cz", target_price=100, only_in_stock=False, token="tok-123")
    db.add(w)
    db.commit()
    c = TestClient(app)
    r = c.get("/pes/potvrdit/tok-123", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/hlidani/potvrdit/tok-123"
    r = c.get("/hlidani/potvrdit/tok-123")
    assert r.status_code == 200 and "Hlídání ceny potvrzeno" in r.text
    assert c.get("/pes/zrusit/tok-123", follow_redirects=False).headers["location"] == "/hlidani/zrusit/tok-123"
    assert "zrušené" in c.get("/hlidani/zrusit/tok-123").text
    assert "zrušené" in c.get("/hlidani/zrusit/tok-123").text  # opakované kliknutí nic nerozbije
    assert c.get("/hlidani/potvrdit/tok-123").status_code == 404  # e-mail smazán
    assert c.get("/pes/neco/tok-123", follow_redirects=False).status_code == 404
    db.close()


def test_watch_email_uses_new_routes(caplog):
    init_db()
    db = SessionLocal()
    from lovceny.service import create_watch
    p = Product(name="Email test", slug="email-test")
    db.add(p)
    db.commit()
    with caplog.at_level("WARNING"):
        create_watch(db, p, "x@y.cz", 500, False)
    log = caplog.text
    assert "/hlidani/potvrdit/" in log and "/hlidani/zrusit/" in log and "/pes/" not in log and not re.search(r"\bps(a|ovi)\b", log)
    db.close()


def test_no_dog_branding_left():
    bad = re.compile(r"🐶|🐕|\bHaf\b|hlídací(ho)? p(es|sa)|hlídací psi|#pes\b|\bdog-", re.I)
    files = [*ROOT.joinpath("lovceny").rglob("*.html"), *ROOT.joinpath("lovceny").rglob("*.css"),
             *ROOT.joinpath("lovceny").rglob("*.py"), ROOT / "README.md", ROOT / "fetch_prices.py"]
    hits = [f"{f.name}:{m.group(0)}" for f in files for m in bad.finditer(f.read_text("utf8"))]
    assert hits == []
