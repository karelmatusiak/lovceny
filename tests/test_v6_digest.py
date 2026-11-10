"""v6: Týdenní přehled slev – snímek týdne, Web Push téma, archiv, e-mail vypnutý bez EMAIL_BACKEND."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lovceny import digest
from lovceny.models import WeeklyDigest, PushTopicSubscription, DigestEmail

SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/tydenni-1", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _digest_feature_on(monkeypatch):
    """Tyhle testy popisují obnovený přehled. Ve výchozím nasazení je vypnutý."""
    monkeypatch.setenv("LOVCENY_SIDE_FEATURES", "1")


@pytest.fixture
def vapid(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLICKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "privatekeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


@pytest.fixture
def clean(client_db):
    client, db, make = client_db
    for M in (WeeklyDigest, PushTopicSubscription, DigestEmail):
        db.query(M).delete()
    db.commit()
    return client_db


def _deal(make, db, name, fresh=True, drop=0.2):
    # 30 dní kolem 1000, dnes o drop levněji → Kupuj (nové 90denní minimum), úspora vůči mediánu
    p = make([1000] * 30 + [round(1000 * (1 - drop))], name=name)
    o = p.offers[0]
    o.last_checked = datetime.now(timezone.utc) - (timedelta(hours=2) if fresh else timedelta(days=10))
    db.commit()
    return p


def test_week_and_send_window():
    assert digest.week_of(datetime(2026, 10, 16, 15, 17, tzinfo=timezone.utc)) == (2026, 42)
    assert digest.week_of(datetime(2027, 1, 3, 23, 30, tzinfo=timezone.utc)) == (2027, 1)  # už pondělí v Praze
    a, b = digest.week_range(2026, 42)
    assert (a.day, a.month, b.day, b.month) == (12, 10, 18, 10)


@pytest.mark.parametrize("when,runs", [
    (datetime(2026, 10, 16, 15, 17, tzinfo=timezone.utc), True),    # pátek 17:17 CEST
    (datetime(2026, 10, 16, 14, 17, tzinfo=timezone.utc), True),    # 16:17 Praha – uvnitř širšího okna
    (datetime(2026, 10, 16, 12, 17, tzinfo=timezone.utc), False),   # 14:17 Praha CEST – ještě brzy
    (datetime(2026, 10, 16, 13, 17, tzinfo=timezone.utc), True),    # 15:17 Praha CEST – začátek okna
    (datetime(2026, 10, 16, 19, 17, tzinfo=timezone.utc), True),    # 21:17 Praha CEST – poslední hodina okna
    (datetime(2026, 10, 16, 20, 17, tzinfo=timezone.utc), False),   # 22:17 Praha – po okně
    (datetime(2027, 1, 8, 16, 17, tzinfo=timezone.utc), True),      # pátek 17:17 CET
    (datetime(2027, 1, 8, 15, 17, tzinfo=timezone.utc), True),      # 16:17 Praha v zimě
    (datetime(2027, 1, 8, 14, 17, tzinfo=timezone.utc), True),      # 15:17 Praha v zimě (14 UTC cron)
    (datetime(2027, 1, 8, 13, 17, tzinfo=timezone.utc), False),     # 14:17 Praha v zimě – ještě brzy
    (datetime(2026, 10, 15, 15, 17, tzinfo=timezone.utc), False),   # čtvrtek
])
def test_run_only_friday_evening_prague(clean, when, runs):
    client, db, make = clean
    out = digest.run(db, now=when, push_sender=lambda s, p: "ok", email_sender=None)
    assert ("skipped" not in out) == runs


def test_run_dedupes_within_wide_window(clean, vapid):
    """Širší páteční okno + dvojitý cron nesmí poslat 2× (push_sent_at)."""
    client, db, make = clean
    _deal(make, db, "Dedupe přehled")
    client.post("/api/push/tema", json={"topic": "tydenni", "subscription": SUB})
    sent = []
    first = datetime(2026, 10, 16, 14, 17, tzinfo=timezone.utc)   # 16:17 Praha
    second = datetime(2026, 10, 16, 16, 17, tzinfo=timezone.utc)  # 18:17 Praha
    out1 = digest.run(db, now=first, push_sender=lambda s, p: sent.append(1) or "ok")
    out2 = digest.run(db, now=second, push_sender=lambda s, p: sent.append(1) or "ok")
    assert "skipped" not in out1 and out1["push"]["sent"] == 1
    assert out2.get("skipped") == "už odesláno tento týden"
    assert sent == [1]


def test_items_methodology_and_snapshot_idempotent(clean, monkeypatch):
    client, db, make = clean
    monkeypatch.delenv("LOVCENY_INCLUDE_DEMO", raising=False)
    big = _deal(make, db, "Přehled velká sleva", drop=0.25)
    small = _deal(make, db, "Přehled menší sleva", drop=0.10)
    stale = _deal(make, db, "Přehled stará cena", fresh=False)
    flat = make([1000] * 31, name="Přehled bez slevy")
    flat.offers[0].last_checked = datetime.now(timezone.utc)
    db.commit()
    d = digest.generate(db)
    items = digest.items_of(d)
    ids = [i["product_id"] for i in items]
    assert big.id in ids and small.id in ids and stale.id not in ids and flat.id not in ids
    assert ids.index(big.id) < ids.index(small.id)
    it = items[ids.index(big.id)]
    assert it["price"] == 750 and it["median90"] == 1000 and it["savings_pct"] == 25.0 and it["shop_name"] == "Mironet"
    _deal(make, db, "Přehled pozdě přidaná", drop=0.5)
    assert digest.items_of(digest.generate(db)) == items  # snímek se nemění


def test_push_send_once_and_gone(clean, vapid):
    client, db, make = clean
    _deal(make, db, "Push přehled produkt")
    r = client.post("/api/push/tema", json={"topic": "tydenni", "subscription": SUB})
    assert r.status_code == 200 and r.json()["token"]
    assert client.post("/api/push/tema", json={"topic": "tydenni", "subscription": SUB}).json()["token"] == r.json()["token"]
    sub2 = {**SUB, "endpoint": "https://updates.push.services.mozilla.com/wpush/v2/zaniklý"}
    client.post("/api/push/tema", json={"topic": "tydenni", "subscription": sub2})
    assert client.post("/api/push/tema", json={"topic": "jine", "subscription": SUB}).status_code == 422
    bad = {**SUB, "endpoint": "https://evil.example/push"}
    assert client.post("/api/push/tema", json={"topic": "tydenni", "subscription": bad}).status_code == 422
    sent = []

    def sender(s, payload):
        sent.append(payload)
        return "gone" if "mozilla" in s.endpoint else "ok"
    d = digest.generate(db)
    st = digest.send_push(db, d, sender)
    assert st == {"sent": 1, "gone": 1, "error": 0}
    p = sent[0]
    assert p["title"].startswith("Týdenní přehled:") and p["url"].endswith(f"/prehled/{digest.slug(d)}?utm_source=push")
    assert "/prehled/odhlasit/" in p["unsubscribe"] and p["stop_label"] == "Odhlásit přehled"
    assert db.query(PushTopicSubscription).count() == 1
    assert digest.send_push(db, d, sender) == {"sent": 0, "gone": 0, "error": 0} and len(sent) == 2  # už odesláno
    # hlídání cen produktu se tím nemění (oddělené téma)
    from lovceny.models import PushSubscription
    assert db.query(PushSubscription).filter(PushSubscription.endpoint == SUB["endpoint"]).count() == 0
    tok = db.query(PushTopicSubscription).one().token
    assert "vypnut" in client.get(f"/prehled/odhlasit/{tok}").text
    assert db.query(PushTopicSubscription).count() == 0


def test_empty_digest_sends_nothing(clean, monkeypatch):
    client, db, make = clean
    monkeypatch.setattr(digest, "build_items", lambda db, now=None, top=10: [])
    d = digest.generate(db)
    calls = []
    digest.send_push(db, d, lambda s, p: calls.append(1) or "ok")
    assert calls == [] and digest.items_of(d) == []


def test_pages_archive_and_week(clean, vapid):
    client, db, make = clean
    _deal(make, db, "Stránka přehled produkt")
    d = digest.generate(db)
    sl = digest.slug(d)
    html = client.get("/prehled").text
    assert "Skutečné slevy týdne" in html and f'href="/prehled/{sl}"' in html and "prehled-optin" in html
    assert "digest.js" in html and 'role="switch"' in html
    week = client.get(f"/prehled/{sl}").text
    assert "Stránka přehled produkt" in week and f"{d.week}. týden {d.year}" in week and "Jak řadíme?" in week
    assert "falešn" not in week.lower()
    assert client.get("/prehled/2001-01").status_code == 404
    assert "prehled-optin" in client.get("/slevy").text


def test_optin_hidden_without_vapid(clean, monkeypatch):
    client, db, make = clean
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    html = client.get("/prehled").text
    assert "dg-toggle" not in html and "nejsou zapnutá" in html
    assert client.post("/api/push/tema", json={"topic": "tydenni", "subscription": SUB}).status_code == 404


def test_email_disabled_until_backend(clean, monkeypatch):
    client, db, make = clean
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    assert 'action="/prehled/email"' not in client.get("/prehled").text
    assert client.post("/prehled/email", data={"email": "a@example.cz"}).status_code == 404
    _deal(make, db, "E-mail přehled produkt")
    d = digest.generate(db)
    assert digest.send_email(db, d) == 0 and d.email_sent_at is None


def test_email_flow_when_enabled(clean, monkeypatch):
    client, db, make = clean
    monkeypatch.setenv("EMAIL_BACKEND", "brevo")
    sent = []

    class S:
        def send(self, to, subject, text, html=None):
            sent.append((to, subject, text, html))
            return True
    import lovceny.notify as notify
    monkeypatch.setattr(notify, "get_sender", lambda: S())
    assert 'action="/prehled/email"' in client.get("/prehled").text
    r = client.post("/prehled/email", data={"email": "Odber@Example.cz"}, follow_redirects=False)
    assert r.status_code == 303 and "odeslano" in r.headers["location"]
    e = db.query(DigestEmail).one()
    assert e.email == "odber@example.cz" and not e.confirmed and "potvrdit" in sent[0][2]
    _deal(make, db, "E-mail přehled produkt 2")
    d = digest.generate(db)
    assert digest.send_email(db, d, S()) == 0  # nepotvrzený nic nedostane
    d.email_sent_at = None
    db.commit()
    client.get(f"/prehled/email/potvrdit/{e.token}")
    db.expire_all()
    assert digest.send_email(db, d, S()) == 1
    to, subj, text, html = sent[-1]
    assert subj.startswith("Týdenní přehled slev") and "E-mail přehled produkt 2" in text and "/prehled/email/zrusit/" in html
    client.get(f"/prehled/email/zrusit/{e.token}")
    db.expire_all()
    assert db.query(DigestEmail).count() == 0


def test_workflow_privacy_sw():
    wf = (ROOT / ".github/workflows/digest.yml").read_text()
    assert "schedule:" not in wf and "workflow_dispatch" in wf
    assert "python weekly_digest.py" in wf and "VAPID_PRIVATE_KEY" in wf
    assert "Fáze 1" in wf
    from fastapi.testclient import TestClient
    from lovceny.main import app
    c = TestClient(app)
    priv = c.get("/ochrana-osobnich-udaju").text
    for want in ('id="prehled"', 'id="rozsireni"', 'id="over-slevu"', "lovceny-prehled", "vypnutá"):
        assert want in priv
    assert "lovceny-prehled" in c.get("/cookies").text
    assert "stop_label" in c.get("/sw.js").text


def test_cli_dry_run(clean, capsys):
    import weekly_digest
    assert weekly_digest.main(["--dry-run"]) == 0
    assert '"items"' in capsys.readouterr().out
