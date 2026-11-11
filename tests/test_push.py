import json

import pytest

from lovceny import push
from lovceny.models import PushSubscription

SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}


@pytest.fixture
def vapid(monkeypatch):
    monkeypatch.setenv("LOVCENY_FEATURE_PUSH", "1")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLICKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "privatekeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.cz")


def test_feature_hidden_without_keys(client_db, monkeypatch):
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    client, db, make = client_db
    p = make([1000, 990])
    html = client.get(f"/produkt/{p.id}").text
    assert "push-box" not in html and "push.js" not in html
    assert client.post("/api/push/odebirat", json={"product_id": p.id, "target_price": 900, "subscription": SUB}).status_code == 404


def test_optin_ui_and_subscribe_dedup_unsubscribe(client_db, vapid):
    client, db, make = client_db
    p = make([1000, 990])
    html = client.get(f"/produkt/{p.id}").text
    assert "Zapnout upozornění v prohlížeči" not in html and "push.js" not in html
    assert "privatekey" not in html
    r = client.post("/api/push/odebirat", json={"product_id": p.id, "target_price": 900, "subscription": SUB})
    assert r.status_code == 200 and r.json()["ok"]
    tok = r.json()["token"]
    r2 = client.post("/api/push/odebirat", json={"product_id": p.id, "target_price": 850, "subscription": SUB})
    assert r2.json()["token"] == tok  # stejný prohlížeč + produkt = aktualizace, ne duplicita
    db.expire_all()
    subs = db.query(PushSubscription).filter_by(product_id=p.id).all()
    assert len(subs) == 1 and float(subs[0].target_price) == 850
    assert client.post("/api/push/zrusit", json={"token": tok}).json()["deleted"] == 1
    assert db.query(PushSubscription).filter_by(product_id=p.id).count() == 0
    # odkaz z oznámení
    tok = client.post("/api/push/odebirat", json={"product_id": p.id, "target_price": 900, "subscription": SUB}).json()["token"]
    assert "Upozornění vypnuto" in client.get(f"/hlidani/push/zrusit/{tok}").text
    assert db.query(PushSubscription).filter_by(product_id=p.id).count() == 0


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/x", "https://evil.example.com/push",
                                      "https://169.254.169.254/latest", "https://fcm.googleapis.com.evil.cz/x"])
def test_rejects_foreign_endpoints(client_db, vapid, endpoint):
    client, db, make = client_db
    p = make([1000])
    r = client.post("/api/push/odebirat", json={"product_id": p.id, "target_price": 900,
                                                 "subscription": {**SUB, "endpoint": endpoint}})
    assert r.status_code == 422


def test_check_push_sends_once_and_drops_gone(client_db, vapid):
    client, db, make = client_db
    p = make([1200, 1100])
    s = push.subscribe(db, p, SUB, 1000)
    sent = []
    fake = lambda sub, payload: sent.append(payload) or "ok"  # noqa: E731
    assert push.check_push(db, sender=fake)["sent"] == 0  # 1100 > cíl 1000
    p.offers[0].price = 990
    db.commit()
    assert push.check_push(db, sender=fake)["sent"] == 1
    assert "Cena klesla" in sent[0]["title"] and "990" in sent[0]["body"] and f"/produkt/{p.id}-" in sent[0]["url"]
    assert sent[0]["unsubscribe"].endswith(s.token)
    assert push.check_push(db, sender=fake)["sent"] == 0  # stejný pokles jen jednou
    p.offers[0].price = 900  # další pokles o > 3 %
    db.commit()
    assert push.check_push(db, sender=fake)["sent"] == 1
    p.offers[0].price = 800
    db.commit()
    assert push.check_push(db, sender=lambda s, pl: "gone")["gone"] == 1
    assert db.query(PushSubscription).filter_by(product_id=p.id).count() == 0


def test_send_uses_pywebpush(monkeypatch, client_db, vapid):
    client, db, make = client_db
    p = make([1000])
    s = push.subscribe(db, p, SUB, 900)
    calls = {}
    import pywebpush
    monkeypatch.setattr(pywebpush, "webpush", lambda info, data, **kw: calls.update(info=info, data=data, kw=kw))
    assert push.send(s, {"title": "Cena klesla"}) == "ok"
    assert calls["info"]["endpoint"] == SUB["endpoint"] and json.loads(calls["data"])["title"] == "Cena klesla"
    assert calls["kw"]["vapid_claims"] == {"sub": "mailto:test@example.cz"}


def test_service_worker_served_from_root(client_db):
    client, *_ = client_db
    r = client.get("/sw.js")
    assert r.status_code == 200 and "showNotification" in r.text
    assert r.headers["service-worker-allowed"] == "/" and "no-cache" in r.headers["cache-control"]


def test_privacy_mentions_push(client_db):
    client, *_ = client_db
    html = client.get("/ochrana-osobnich-udaju").text
    assert "Web Push" in html and "lovceny-push" in html and "24 měsíců" in html


def test_vapid_keyfile_generated_locally():
    import os, stat
    f = "/workspace/lovceny/vapid-keys.txt"
    if not os.path.exists(f):
        pytest.skip("klíče jen na vývojovém stroji")
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_real_encryption_and_vapid_signing(monkeypatch, client_db):
    """Skutečný pywebpush (šifrování aes128gcm + VAPID JWT) s klíči ve stejném formátu jako vapid-keys.txt; síť mockujeme."""
    import base64
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()  # noqa: E731
    srv = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setenv("LOVCENY_FEATURE_PUSH", "1")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", b(srv.private_numbers().private_value.to_bytes(32, "big")))
    monkeypatch.setenv("VAPID_PUBLIC_KEY", b(srv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)))
    cli = ec.generate_private_key(ec.SECP256R1())
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/xyz",
           "keys": {"p256dh": b(cli.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
                    "auth": b(b"0123456789abcdef")}}
    client, db, make = client_db
    s = push.subscribe(db, make([1000]), sub, 900)
    seen = {}

    class R:
        status_code = 201
        text = ""
        headers: dict = {}

    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: seen.update(url=url, **kw) or R())
    assert push.send(s, {"title": "Cena klesla", "body": "Teď 899 Kč"}) == "ok"
    assert seen["url"] == sub["endpoint"]
    assert seen["headers"]["authorization"].startswith("vapid t=") and seen["headers"]["content-encoding"] == "aes128gcm"
    assert b"Cena klesla" not in seen["data"]  # obsah je šifrovaný
