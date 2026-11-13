"""v7 – Hlídání podle rozpočtu: model, push/e-mail, deduplikace, denní strop, správa, napojení na fetch_prices."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lovceny import budget
from lovceny.models import BudgetWatch, BudgetHit

ROOT = Path(__file__).resolve().parents[1]
SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/rozpocet-1", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}


@pytest.fixture(autouse=True)
def _budget_feature_on(monkeypatch):
    """Tyhle testy popisují obnovené hlídání rozpočtu. Ve výchozím nasazení je vypnuté."""
    monkeypatch.setenv("LOVCENY_SIDE_FEATURES", "1")


@pytest.fixture
def vapid(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLICKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "privatekeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    from lovceny import verify
    verify.clear_cache()


def _fresh(db, p, price=None):
    o = p.offers[0]
    o.last_checked = datetime.now(timezone.utc)
    if price is not None:
        o.price = price
    db.commit()
    return p


def test_subscribe_baseline_then_only_new(client_db, vapid):
    client, db, make = client_db
    old = _fresh(db, make([11000] * 3, name="Pracka Rozpoctova Bosch A", category="pracky"))
    make([15000] * 3, name="Pracka Rozpoctova Bosch drahá", category="pracky")
    r = client.post("/api/rozpocet", json={"category": "pracky", "keyword": "rozpoctova", "max_price": 12000, "subscription": SUB})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["now_matching"] == 1 and j["label"] == "Pračky – „rozpoctova“ do 12\xa0000\xa0Kč"
    sent = []
    snd = lambda s, payload: sent.append(payload) or "ok"  # noqa: E731
    assert budget.check_budgets(db, push_sender=snd)["sent"] == 0  # už v rozpočtu při založení → nehlásíme
    new = _fresh(db, make([12500, 11900], name="Pracka Rozpoctova Bosch B", category="pracky"))
    assert budget.check_budgets(db, push_sender=snd)["sent"] == 1
    assert sent[-1]["title"].startswith("V rozpočtu: Pracka Rozpoctova Bosch B") and "/hlidat-rozpocet/zrusit/" in sent[-1]["unsubscribe"]
    assert budget.check_budgets(db, push_sender=snd)["sent"] == 0      # stejná cena → znovu ne
    _fresh(db, new, 11800)                                              # pokles < 3 % → ne
    assert budget.check_budgets(db, push_sender=snd)["sent"] == 0
    _fresh(db, old, 10000)                                              # starý produkt zlevnil o 9 % → ano
    assert budget.check_budgets(db, push_sender=snd)["sent"] == 1 and "Bosch A" in sent[-1]["title"]


def test_dedup_same_browser_limits_and_validation(client_db, vapid):
    client, db, make = client_db
    sub = {**SUB, "endpoint": SUB["endpoint"] + "-dedup"}
    t1 = client.post("/api/rozpocet", json={"category": "televize", "keyword": "OLED", "max_price": 20000, "subscription": sub}).json()["token"]
    t2 = client.post("/api/rozpocet", json={"category": "televize", "keyword": "oled", "max_price": 18000, "subscription": sub}).json()["token"]
    assert t1 == t2 and float(db.query(BudgetWatch).filter_by(token=t1).one().max_price) == 18000
    assert client.post("/api/rozpocet", json={"category": "neexistuje", "max_price": 5000, "subscription": sub}).status_code == 422
    assert client.post("/api/rozpocet", json={"category": "televize", "max_price": 5, "subscription": sub}).status_code == 422
    bad = {**sub, "endpoint": "https://evil.example.com/push"}
    assert client.post("/api/rozpocet", json={"category": "televize", "max_price": 5000, "subscription": bad}).status_code == 422
    mine = client.post("/api/rozpocet/moje", json={"endpoint": sub["endpoint"]}).json()["items"]
    assert [m["token"] for m in mine] == [t1]


def test_daily_cap_and_gone(client_db, vapid):
    client, db, make = client_db
    b = budget.subscribe_push(db, "kavovary", "capkavovar", 5000, {**SUB, "endpoint": SUB["endpoint"] + "-cap"})
    snd = lambda s, payload: "ok"  # noqa: E731
    for i in range(budget.MAX_PER_DAY):
        _fresh(db, make([4000], name=f"Kavovar capkavovar {i}", category="kavovary"))
        assert budget.check_budgets(db, push_sender=snd)["sent"] == 1
    _fresh(db, make([4000], name="Kavovar capkavovar navic", category="kavovary"))
    assert budget.check_budgets(db, push_sender=snd)["capped"] == 1
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    out = budget.check_budgets(db, push_sender=lambda s, p: "gone", now=tomorrow.replace(hour=10))
    assert out["gone"] == 1 and db.query(BudgetWatch).filter_by(id=b.id).count() == 0
    assert db.query(BudgetHit).filter_by(budget_id=b.id).count() == 0


def test_stale_prices_are_ignored(client_db, vapid):
    client, db, make = client_db
    budget.subscribe_push(db, "vysavace", "stalevysavac", 3000, {**SUB, "endpoint": SUB["endpoint"] + "-stale"})
    p = make([2500], name="Vysavac stalevysavac", category="vysavace")
    p.offers[0].last_checked = datetime.now(timezone.utc) - timedelta(days=5)
    db.commit()
    assert budget.check_budgets(db, push_sender=lambda s, x: "ok")["sent"] == 0


def test_pages_manage_edit_delete(client_db, vapid):
    client, db, make = client_db
    _fresh(db, make([900], name="Hodinky spravahodinky", category="chytre-hodinky"))
    h = client.get("/hlidat-rozpocet").text
    assert "Hlídání podle rozpočtu" in h and 'id="bw-form"' in h and "budget.js" in h and 'rel="canonical"' in h
    assert "privatekey" not in h
    pv = client.get("/hlidat-rozpocet?kategorie=chytre-hodinky&hledat=spravahodinky&max=1000").text
    assert "Teď v rozpočtu" in pv and "Hodinky spravahodinky" in pv and 'content="noindex' in pv
    cat = client.get("/kategorie/telefony").text
    assert 'id="hlidat-rozpocet"' in cat and 'name="category" value="telefony"' in cat
    tok = client.post("/api/rozpocet", json={"category": "chytre-hodinky", "keyword": "spravahodinky", "max_price": 1000,
                                             "subscription": {**SUB, "endpoint": SUB["endpoint"] + "-sprava"}}).json()["token"]
    m = client.get(f"/hlidat-rozpocet/{tok}").text
    assert "Teď v rozpočtu (1)" in m and 'content="noindex' in m and "Zrušit hlídání" in m
    r = client.post(f"/hlidat-rozpocet/{tok}/upravit", data={"max_price": "800"}, follow_redirects=False)
    assert r.status_code == 303 and "zmeneno=1" in r.headers["location"]
    assert "Teď v rozpočtu (0)" in client.get(f"/hlidat-rozpocet/{tok}").text
    assert "Rozpočet musí být" in client.post(f"/hlidat-rozpocet/{tok}/upravit", data={"max_price": "1"}).text
    assert "zrušeno" in client.get(f"/hlidat-rozpocet/zrusit/{tok}").text
    assert client.get(f"/hlidat-rozpocet/{tok}").status_code == 404
    assert "Disallow: /hlidat-rozpocet/" in client.get("/robots.txt").text
    assert "/hlidat-rozpocet</loc>" in client.get("/sitemap.xml").text


def test_push_off_hides_ui(client_db, monkeypatch):
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    client, db, make = client_db
    h = client.get("/hlidat-rozpocet").text
    assert "budget.js" not in h and "Upozornění v prohlížeči teď nejsou zapnutá" in h
    assert client.post("/api/rozpocet", json={"category": "televize", "max_price": 5000, "subscription": SUB}).status_code == 404


def test_email_prepared_but_disabled(client_db, monkeypatch):
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    client, db, make = client_db
    assert 'action="/hlidat-rozpocet/email"' not in client.get("/hlidat-rozpocet").text
    assert client.post("/hlidat-rozpocet/email", data={"category": "televize", "max_price": "5000", "email": "a@b.cz"}).status_code == 404


def test_email_flow_when_enabled(client_db, monkeypatch):
    monkeypatch.setenv("EMAIL_BACKEND", "brevo")
    from lovceny import verify
    verify.clear_cache()
    sent = []

    class S:
        def send(self, to, subject, text, html=None):
            sent.append((to, subject, text, html))
            return True
    import lovceny.notify as notify
    monkeypatch.setattr(notify, "get_sender", lambda: S())
    client, db, make = client_db
    assert 'action="/hlidat-rozpocet/email"' in client.get("/hlidat-rozpocet").text
    r = client.post("/hlidat-rozpocet/email", data={"category": "lednice", "keyword": "emailednice", "max_price": "9000",
                                                     "email": "Rozpocet@Example.cz"}, follow_redirects=False)
    assert r.status_code == 303 and "odeslano" in r.headers["location"]
    b = db.query(BudgetWatch).filter_by(email="rozpocet@example.cz").one()
    assert not b.confirmed and "/hlidat-rozpocet/potvrdit/" in sent[0][2]
    _fresh(db, make([8000], name="Lednice emailednice", category="lednice"))
    assert budget.check_budgets(db, push_sender=lambda s, p: "ok", email_sender=S())["email"] == 0  # nepotvrzené
    client.get(f"/hlidat-rozpocet/potvrdit/{b.token}")
    db.expire_all()
    assert budget.check_budgets(db, push_sender=lambda s, p: "ok", email_sender=S())["email"] == 1
    assert "Lednice emailednice" in sent[-1][2] and "/hlidat-rozpocet/zrusit/" in sent[-1][3]


def test_cleanup_and_fetch_hook():
    src = (ROOT / "fetch_prices.py").read_text()
    assert "budget.check_budgets(db)" in src and "blackfriday.run_digest(db)" in src
    assert (ROOT / "migrations/007_v7_rozpocet_black_friday.sql").read_text().count("CREATE TABLE IF NOT EXISTS") == 3


def test_cleanup_old(client_db):
    client, db, make = client_db
    old = BudgetWatch(token="old-unconfirmed", category_slug="televize", max_price=1000, channel="email",
                      email="x@example.cz", confirmed=False, created_at=datetime.now(timezone.utc) - timedelta(days=8))
    db.add(old)
    db.commit()
    assert budget.cleanup(db) >= 1 and db.query(BudgetWatch).filter_by(token="old-unconfirmed").count() == 0
