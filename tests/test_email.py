"""E-maily (Brevo mockované), upozornění bez duplicit, úklid hlídání, lehké /healthz."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from lovceny import notify
from lovceny.db import SessionLocal, init_db
from lovceny.emails import alert_email, confirm_email
from lovceny.main import app
from lovceny.models import Offer, Product, Watch
from lovceny.service import check_watches, cleanup_watches, create_watch


class R:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


@pytest.fixture
def brevo(monkeypatch):
    monkeypatch.setenv("EMAIL_BACKEND", "brevo")
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-test")
    monkeypatch.setenv("EMAIL_FROM", "upozorneni@lovceny.cz")
    monkeypatch.setenv("PUBLIC_URL", "https://lovceny.example")
    calls, state = [], {"code": 201}

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(state["code"], Exception):
            raise state["code"]
        return R(state["code"], "chyba")
    monkeypatch.setattr(notify.httpx, "post", post)
    return calls, state


_n = [0]


def _watch(db, price, target, confirmed=True, **kw):
    _n[0] += 1
    p = Product(name=f"Mail Pracka {_n[0]}", slug=f"mail-pracka-{_n[0]}")
    o = Offer(product=p, shop="mironet", url=f"https://www.mironet.cz/mail-{_n[0]}-{datetime.now().timestamp()}",
              price=price, status="ok", in_stock=True)
    w = Watch(product=p, email=f"kupec{_n[0]}@example.cz", target_price=target, only_in_stock=False,
              token=f"t-{_n[0]}-{datetime.now().timestamp()}", confirmed=confirmed, **kw)
    db.add_all([p, o, w])
    db.commit()
    return w, o


def _mine(calls, w):
    return [c for c in calls if c["json"]["to"][0]["email"] == w.email]


def test_brevo_payload_and_confirm_email(brevo):
    calls, _ = brevo
    init_db()
    with SessionLocal() as db:
        p = Product(name="Mail Confirm TV", slug="mail-confirm-tv")
        db.add(p)
        db.commit()
        w = create_watch(db, p, "Novak@Example.cz", 9990, False)
    c = calls[-1]
    assert c["url"] == "https://api.brevo.com/v3/smtp/email"
    assert c["headers"]["api-key"] == "xkeysib-test"
    j = c["json"]
    assert j["sender"] == {"email": "upozorneni@lovceny.cz", "name": "Lovceny"}
    assert j["to"] == [{"email": "novak@example.cz"}]
    assert j["subject"] == "Potvrď hlídání ceny: Mail Confirm TV"
    for body in (j["textContent"], j["htmlContent"]):
        assert f"https://lovceny.example/hlidani/potvrdit/{w.token}" in body
        assert f"https://lovceny.example/hlidani/zrusit/{w.token}" in body
        assert "9\xa0990\xa0Kč" in body
    assert 'src="https://lovceny.example/static/brand/logo-email.png"' in j["htmlContent"]


def test_html_escapes_product_name(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://x.cz")
    p = Product(id=1, name="<script>x</script> TV", slug="tv")
    w = Watch(product=p, email="a@b.cz", target_price=1, token="t", only_in_stock=False)
    m = confirm_email(w)
    assert "<script>" not in m.html and "&lt;script&gt;" in m.html
    assert "<script>x</script> TV" in m.text  # text e-mail nic neescapuje


def test_missing_key_falls_back_to_log(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_BACKEND", "brevo")
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    assert isinstance(notify.get_sender(), notify.LogEmailSender)
    monkeypatch.setenv("EMAIL_BACKEND", "neco")
    assert isinstance(notify.get_sender(), notify.LogEmailSender)
    monkeypatch.delenv("EMAIL_BACKEND")
    assert notify.get_sender().name == "log"


@pytest.mark.parametrize("err", [500, 401, httpx.ConnectTimeout("t")])
def test_brevo_errors_return_false(brevo, err):
    _, state = brevo
    state["code"] = err
    assert notify.get_sender().send("a@b.cz", "S", "T", "<p>T</p>") is False


def test_alert_sent_once_per_drop_and_again_after_rebound(brevo):
    calls, _ = brevo
    init_db()
    with SessionLocal() as db:
        w, o = _watch(db, 9000, 10000)
        check_watches(db)
        assert len(_mine(calls, w)) == 1
        m = _mine(calls, w)[0]["json"]
        assert m["subject"].startswith("Cena klesla: ") and "9\xa0000\xa0Kč" in m["subject"]
        assert "/hlidani/zrusit/" in m["textContent"] and "/hlidani/zrusit/" in m["htmlContent"]
        check_watches(db)                      # stejná cena → nic
        o.price = 8900; db.commit()            # pokles jen o ~1 % → nic
        check_watches(db)
        assert len(_mine(calls, w)) == 1
        o.price = 8500; db.commit()            # další pokles ≥ 3 % → znovu
        check_watches(db)
        assert len(_mine(calls, w)) == 2
        o.price = 10500; db.commit()           # zdražení nad cíl → reset
        check_watches(db)
        db.refresh(w)
        assert w.notified_price is None
        o.price = 9900; db.commit()            # znovu pod cíl → nové upozornění
        check_watches(db)
        assert len(_mine(calls, w)) == 3


def test_unconfirmed_or_above_target_not_alerted(brevo):
    calls, _ = brevo
    init_db()
    with SessionLocal() as db:
        a, _ = _watch(db, 9000, 10000, confirmed=False)
        b, _ = _watch(db, 12000, 10000)
        check_watches(db)
    assert not _mine(calls, a) and not _mine(calls, b)


def test_failed_send_is_retried_next_run(brevo):
    calls, state = brevo
    init_db()
    with SessionLocal() as db:
        w, _ = _watch(db, 9000, 10000)
        state["code"] = 500
        check_watches(db)
        db.refresh(w)
        assert w.notified_price is None and w.notified_at is None
        state["code"] = 201
        check_watches(db)
        db.refresh(w)
        assert float(w.notified_price) == 9000
    assert len(_mine(calls, w)) == 2


def test_cleanup_watches():
    init_db()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        fresh, _ = _watch(db, 1, 1)
        old, _ = _watch(db, 1, 1, created_at=now - timedelta(days=800))
        old_but_recent_alert, _ = _watch(db, 1, 1, created_at=now - timedelta(days=800),
                                         notified_at=now - timedelta(days=30))
        unconf, _ = _watch(db, 1, 1, confirmed=False, created_at=now - timedelta(days=8))
        unconf_new, _ = _watch(db, 1, 1, confirmed=False, created_at=now - timedelta(days=1))
        off, _ = _watch(db, 1, 1, active=False)
        ids = {k: v.id for k, v in dict(fresh=fresh, old=old, recent=old_but_recent_alert, unconf=unconf,
                                       unconf_new=unconf_new, off=off).items()}
        st = cleanup_watches(db, now=now)
        left = {i for (i,) in db.query(Watch.id).all()}
    assert st["expired"] >= 1 and st["unconfirmed"] >= 1 and st["inactive"] >= 1
    assert ids["fresh"] in left and ids["recent"] in left and ids["unconf_new"] in left
    assert ids["old"] not in left and ids["unconf"] not in left and ids["off"] not in left


def test_healthz_is_light_and_db_diag(monkeypatch):
    import lovceny.main as m
    c = TestClient(app)

    def boom():
        raise AssertionError("ping nesmí sahat do DB")
    monkeypatch.setattr(m, "SessionLocal", boom)
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True and "products" not in r.json()
    assert c.head("/healthz").status_code == 200
    monkeypatch.undo()
    m._health_cache.clear()
    d = c.get("/healthz?db=1").json()
    assert d["ok"] and d["db_ok"] and isinstance(d["products"], int) and "watches" in d


def test_healthz_db_failure_returns_503(monkeypatch):
    import lovceny.main as m

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(m, "SessionLocal", boom)
    r = TestClient(app).get("/healthz?db=1")
    assert r.status_code == 503 and r.json()["db_ok"] is False


def test_migration_adds_notified_price(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text
    from lovceny import db as dbmod
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE watches (id INTEGER PRIMARY KEY, email VARCHAR)"))
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod._migrate()
    dbmod._migrate()  # idempotentní
    assert "notified_price" in {c["name"] for c in inspect(eng).get_columns("watches")}
