"""v7 – Black Friday: datum, banner (konfigurovatelné období), poctivé verdikty, nejnižší ceny, push přehled."""
from datetime import date, datetime, timedelta, timezone

import pytest

from lovceny import blackfriday as bf
from lovceny.models import PushTopicSubscription, TopicSend

BANNED = ("falešn", "falesn", "podvod", "klamav", "přifouk", "zdánliv")


def test_dates():
    assert bf.bf_date(2026) == date(2026, 11, 27) and bf.bf_date(2025) == date(2025, 11, 28) and bf.bf_date(2024) == date(2024, 11, 29)
    s = bf.season(datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert s.year == 2026 and s.cm == date(2026, 11, 30)
    assert bf.season(datetime(2026, 12, 20, tzinfo=timezone.utc)).year == 2027
    assert bf.countdown_text(datetime(2026, 10, 14, 12, tzinfo=timezone.utc)) == "Black Friday 2026 je za 44 dní – v pátek 27. listopadu."
    assert bf.countdown_text(datetime(2026, 11, 27, 9, tzinfo=timezone.utc)) == "Black Friday je dnes."


def test_banner_window(monkeypatch):
    oct14 = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
    assert not bf.banner_active(oct14)
    assert bf.banner_active(datetime(2026, 11, 1, 10, tzinfo=timezone.utc))
    assert bf.banner_active(datetime(2026, 12, 1, 10, tzinfo=timezone.utc))
    assert not bf.banner_active(datetime(2026, 12, 2, 10, tzinfo=timezone.utc))
    monkeypatch.setenv("BF_BANNER_FROM", "10-10")
    assert bf.banner_active(oct14)
    monkeypatch.setenv("BF_BANNER_FROM", "nesmysl")
    assert not bf.banner_active(oct14)


def test_home_banner_toggle(client_db, monkeypatch):
    client, db, make = client_db
    monkeypatch.setenv("BF_BANNER_FROM", "2000-01-01")
    monkeypatch.setenv("BF_BANNER_TO", "2100-12-31")
    h = client.get("/").text
    assert 'class="bf-banner"' in h and 'href="/black-friday"' in h
    inner = client.get("/kategorie").text
    assert 'class="bf-banner"' in inner and 'class="head-search"' in inner and 'id="home-q"' in inner
    monkeypatch.setenv("BF_BANNER_FROM", "2099-01-01")
    assert 'class="bf-banner"' not in client.get("/").text
    assert 'class="bf-banner"' not in client.get("/kategorie").text


def _deal(db, make, name, prices, original=None, cat="televize", shop="mironet"):
    p = make(prices, name=name, category=cat, original=original, shop=shop)
    p.offers[0].last_checked = datetime.now(timezone.utc)
    db.commit()
    return p


def test_rows_verdicts_lowest_and_filters(client_db, monkeypatch):
    monkeypatch.delenv("LOVCENY_INCLUDE_DEMO", raising=False)
    client, db, make = client_db
    low = _deal(db, make, "BF nejnizsi televize", [20000] * 40 + [16000], original=20000)
    fake = _deal(db, make, "BF kontrola 30 dni", [15000] * 25 + [19000] * 4 + [16990], original=21990, cat="sluchatka")
    plain = _deal(db, make, "BF bez slevy", [5000] * 20)
    rs = {r.product.id: r for r in bf.rows(db)}
    assert rs[low.id].is_lowest and rs[low.id].kind == "buy" and round(rs[low.id].claimed_pct) == 20
    assert rs[fake.id].kind == "fake" and not rs[fake.id].is_lowest and rs[fake.id].min30 == 15000
    assert plain.id not in rs
    h = client.get("/black-friday").text
    assert "BF nejnizsi televize" in h and "Nejnižší naměřená cena" in h and "Za 30 dní bylo levněji" in h
    assert "§ 12a" in h and '"@type":"FAQPage"' in h and 'rel="canonical" href="https://lovceny.cz/black-friday"' in h
    assert "27. listopadu 2026" in h or "Black Friday" in h
    assert all(b not in h.lower() for b in BANNED)
    f = client.get("/black-friday?kategorie=sluchatka").text
    assert "BF kontrola 30 dni" in f and "BF nejnizsi televize" not in f and 'content="noindex' in f
    assert client.get("/black-friday?kategorie=<x>&obchod=zzz").status_code == 200
    assert "/black-friday</loc>" in client.get("/sitemap.xml").text


def test_demo_excluded_by_default(client_db, monkeypatch):
    monkeypatch.delenv("LOVCENY_INCLUDE_DEMO", raising=False)
    client, db, make = client_db
    p = _deal(db, make, "BF jen demo historie", [9000] * 3, original=12000)
    for pt in p.offers[0].points:
        pt.is_demo = True
    db.commit()
    r = {x.product.id: x for x in bf.rows(db)}[p.id]
    assert r.kind == "none" and r.days == 0 and not r.is_lowest


@pytest.fixture
def vapid(monkeypatch):
    monkeypatch.setenv("LOVCENY_FEATURE_PUSH", "1")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLICKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "privatekeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")


def test_push_digest_only_on_bf_once(client_db, vapid):
    client, db, make = client_db
    _deal(db, make, "BF push televize", [20000] * 40 + [15000], original=20000)
    r = client.post("/api/push/tema", json={"topic": "black-friday", "subscription": {
        "endpoint": "https://fcm.googleapis.com/fcm/send/bf-1", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}})
    assert r.status_code == 200
    sent = []
    snd = lambda s, p: sent.append(p) or "ok"  # noqa: E731
    assert "skipped" in bf.run_digest(db, now=datetime(2026, 11, 20, 8, tzinfo=timezone.utc), sender=snd)
    assert "skipped" in bf.run_digest(db, now=datetime(2026, 11, 27, 4, tzinfo=timezone.utc), sender=snd)  # 5:00 Praha
    out = bf.run_digest(db, now=datetime.now(timezone.utc), sender=snd, force=True)
    assert out["sent"] >= 1 and sent[0]["url"].endswith("/black-friday?utm_source=push") and "/black-friday/odhlasit/" in sent[0]["unsubscribe"]
    assert all(b not in (sent[0]["title"] + sent[0]["body"]).lower() for b in BANNED)
    assert bf.run_digest(db, now=datetime.now(timezone.utc), sender=snd, force=True) == {"skipped": "už odesláno"}
    tok = db.query(PushTopicSubscription).filter_by(topic="black-friday").first().token
    assert "vypnut" in client.get(f"/black-friday/odhlasit/{tok}").text
    assert db.query(PushTopicSubscription).filter_by(token=tok).count() == 0
    db.query(TopicSend).delete()
    db.commit()


def test_page_optin_and_footer(client_db, vapid):
    client, db, make = client_db
    h = client.get("/black-friday").text
    assert 'data-topic="black-friday"' in h and "bf.js" in h and "privatekey" not in h
    assert 'href="/black-friday"' in client.get("/o-nas").text
