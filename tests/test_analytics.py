"""Vlastní analytika bez cookies (lovceny/analytics.py + middleware + /admin)."""
import base64
from datetime import datetime, timedelta, timezone

import pytest

from lovceny import analytics
from lovceny.models import AnalyticsEvent, AnalyticsSalt, AnalyticsDaily

TOKEN = "t" * 40


@pytest.fixture
def an(client_db):
    client, db, make = client_db
    for m in (AnalyticsEvent, AnalyticsSalt, AnalyticsDaily):
        db.query(m).delete()
    db.commit()
    analytics._cleanup_at["t"] = None
    return client, db, make


def events(db):
    db.expire_all()
    return db.query(AnalyticsEvent).order_by(AnalyticsEvent.id).all()


@pytest.mark.parametrize("q,out", [
    ("  Pračka   BOSCH ", "pračka bosch"),
    ("iphone 16 pro, karel@example.cz", "iphone 16 pro"),
    ("volejte +420 777 123 456 tv", "volejte tv"),
    ("https://www.alza.cz/neco", "[odkaz z obchodu]"),
    ("sony wh-1000xm5", "sony wh-1000xm5"),
    ("<script>alert(1)</script>", "script alert 1 /script"),
    ("", None),
    ("x" * 200, "x" * 60),
])
def test_normalize_query(q, out):
    assert analytics.normalize_query(q) == out


def test_ref_domain():
    assert analytics.ref_domain("https://www.google.com/search?q=secret", "lovceny.onrender.com") == "google.com"
    assert analytics.ref_domain("https://lovceny.onrender.com/x", "lovceny.onrender.com") is None
    assert analytics.ref_domain("http://192.168.1.1/", "x") is None
    assert analytics.ref_domain("", "x") is None


def test_pageview_recorded_without_cookies_or_pii(an):
    client, db, make = an
    p = make([1000])
    r = client.get(f"/produkt/{p.id}-{p.slug}?msg=ahoj", headers={"user-agent": "Mozilla/5.0 Firefox", "referer": "https://www.seznam.cz/hledani?q=tajne",
                                                                  "x-forwarded-for": "203.0.113.9"})
    assert r.status_code == 200 and "set-cookie" not in r.headers
    e = events(db)[-1]
    assert (e.kind, e.path, e.ref_domain, e.product_id) == ("view", f"/produkt/{p.id}", "seznam.cz", p.id)
    assert e.visitor and len(e.visitor) == 32 and "203.0.113.9" not in e.visitor
    cols = {c.name for c in AnalyticsEvent.__table__.columns}
    assert not cols & {"ip", "user_agent", "ua", "email"}


def test_dnt_gpc_bots_and_static_skipped(an):
    client, db, _ = an
    client.get("/", headers={"dnt": "1", "user-agent": "Mozilla/5.0"})
    client.get("/", headers={"sec-gpc": "1", "user-agent": "Mozilla/5.0"})
    client.get("/", headers={"user-agent": "Googlebot/2.1"})
    client.get("/", headers={"user-agent": "Mozilla/5.0 Chrome-Lighthouse"})
    client.get("/robots.txt", headers={"user-agent": "Mozilla/5.0"})
    client.get("/manifest.webmanifest", headers={"user-agent": "Mozilla/5.0"})
    client.get("/neexistuje", headers={"user-agent": "Mozilla/5.0"})
    assert events(db) == []
    client.get("/", headers={"user-agent": "Mozilla/5.0"})
    assert len(events(db)) == 1


def test_search_and_zero_results(an):
    client, db, make = an
    make([1000], name="Unikátní kávovar Zeta")
    ua = {"user-agent": "Mozilla/5.0"}
    client.get("/hledat", params={"q": "kávovar Zeta"}, headers=ua)
    client.get("/hledat", params={"q": "Neexistujici vec 123"}, headers=ua)
    client.get("/hledat/zive", params={"q": "Neexistujici vec 123"}, headers=ua)
    ev = events(db)
    assert [(e.kind, e.query, e.results) for e in ev] == [
        ("search", "kávovar zeta", 1), ("search", "neexistujici vec 123", 0), ("live", "neexistujici vec 123", 0)]
    rep = analytics.report(db, days=7)
    assert ("neexistujici vec 123", 1) in rep["zero_searches"]
    assert all(q != "kávovar zeta" for q, _ in rep["zero_searches"])
    assert rep["top_searches"][0][1] == 1


def test_unique_hash_rotates_daily_and_is_discarded(an):
    _, db, _ = an
    t0 = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
    for ip in ("1.1.1.1", "1.1.1.1", "2.2.2.2"):
        analytics.record(db, path="/", ip=ip, ua="UA", now=t0)
    analytics.record(db, path="/", ip="1.1.1.1", ua="UA", now=t0 + timedelta(days=1))
    ev = events(db)
    assert ev[0].visitor == ev[1].visitor != ev[2].visitor
    assert ev[3].visitor != ev[0].visitor  # jiný den = jiná sůl = nespojitelné
    res = analytics.cleanup(db, now=t0 + timedelta(days=1, hours=2))
    assert res["hashes_cleared"] == 3
    ev = events(db)
    assert [e.visitor is None for e in ev] == [True, True, True, False]
    assert db.get(AnalyticsDaily, "2026-10-10").uniques == 2
    assert db.get(AnalyticsDaily, "2026-10-10").views == 3
    assert [s.day for s in db.query(AnalyticsSalt).all()] == ["2026-10-11"]  # stará sůl smazaná


def _basic(pw):
    return {"authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def test_admin_auth(an, monkeypatch):
    client, _, _ = an
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get("/admin").status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", "kratky")
    assert client.get("/admin", headers=_basic("kratky")).status_code == 404  # slabý token = admin vypnutý
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    r = client.get("/admin")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/admin", headers=_basic("spatne")).status_code == 401
    assert client.get(f"/admin?token={TOKEN}").status_code == 401  # token v URL nebereme (logy, historie)
    r = client.get("/admin", headers=_basic(TOKEN))
    assert r.status_code == 200 and "Statistiky návštěvnosti" in r.text
    assert r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]
    assert client.get("/admin", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_admin_report_content(an, monkeypatch):
    client, db, make = an
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    p = make([1000], name="Populární sluchátka Omega")
    ua = {"user-agent": "Mozilla/5.0"}
    for _ in range(3):
        client.get(f"/produkt/{p.id}", headers=ua)
    client.get("/hledat", params={"q": "nic takového"}, headers=ua)
    html = client.get("/admin?dni=7", headers=_basic(TOKEN)).text
    for s in ("Populární sluchátka Omega", "nic takového", "Hledání bez výsledků", "Nejnavštěvovanější stránky", f"/produkt/{p.id}"):
        assert s in html
    assert len([e for e in events(db) if e.path == "/admin"]) == 0  # admin se nepočítá


def test_privacy_and_cookie_pages_updated(an):
    client, _, _ = an
    priv = client.get("/ochrana-osobnich-udaju").text
    assert "Anonymní statistika návštěvnosti" in priv and "Do Not Track" in priv and "24 hodin" in priv
    assert "cookie lišt" in priv
    cook = client.get("/cookies").text
    assert "cookie lištu" in cook and "lovceny-pwa" in cook and "service worker" in cook
