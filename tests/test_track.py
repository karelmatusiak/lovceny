"""„Sledovat tento produkt“ z živého hledání: náhled, deduplikace, rate limit."""
from lovceny import main as m
from lovceny.models import Offer, TrackRequest
from lovceny.shops import search
from lovceny.shops.search import SearchHit, LiveResult


def _fake_add(calls):
    from lovceny.models import Product

    def add(db, url, fetch_now=True):
        calls.append(url)
        o = db.query(Offer).filter_by(url=url).first()
        if o:
            return o.product, ""
        p = Product(name="Nový výrobek " + url[-6:], slug="novy")
        db.add(Offer(product=p, shop="mironet", url=url, status="pending"))
        db.commit()
        return p, ""
    return add


def test_preview_offers_track_button(client_db):
    client, *_ = client_db
    url = "https://www.mironet.cz/samsung-galaxy-a55+dp123/"
    search._cache["galaxy"] = (1e12, LiveResult([SearchHit("mironet", "Samsung Galaxy A55 128GB", 8490, url, None)], []))
    html = client.get("/nahled", params={"url": url}).text
    assert "Samsung Galaxy A55 128GB" in html and "Sledovat tento produkt" in html and "8\xa0490" in html
    assert 'name="robots" content="noindex' in html
    assert client.get("/nahled", params={"url": "https://example.com/x"}).status_code == 404


def test_preview_redirects_if_already_tracked(client_db):
    client, db, make = client_db
    p = make([1000])
    r = client.get("/nahled", params={"url": p.offers[0].url}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/produkt/{p.id}-")


def test_track_dedup_and_rate_limit(client_db, monkeypatch):
    client, db, make = client_db
    db.query(TrackRequest).delete()
    db.commit()
    calls = []
    monkeypatch.setattr(m, "add_by_url", _fake_add(calls))
    monkeypatch.setattr(m, "TRACK_PER_IP_HOUR", 3)
    hdr = {"x-forwarded-for": "203.0.113.7"}
    for i in range(3):
        r = client.post("/sledovat", data={"url": f"https://www.mironet.cz/vyrobek-{i}+dp10{i}"}, headers=hdr, follow_redirects=False)
        assert r.status_code == 303
    # stejný produkt znovu = dedup, do limitu se nepočítá
    r = client.post("/sledovat", data={"url": "https://www.mironet.cz/vyrobek-0+dp100"}, headers=hdr, follow_redirects=False)
    assert r.status_code == 303
    assert db.query(Offer).filter(Offer.url == "https://www.mironet.cz/vyrobek-0+dp100").count() == 1
    r = client.post("/sledovat", data={"url": "https://www.mironet.cz/vyrobek-9+dp109"}, headers=hdr, follow_redirects=False)
    assert r.status_code == 429 and "Chvíli počkej" in r.text
    # jiná IP může dál
    r = client.post("/sledovat", data={"url": "https://www.mironet.cz/vyrobek-9+dp109"}, headers={"x-forwarded-for": "198.51.100.1"}, follow_redirects=False)
    assert r.status_code == 303
    assert all("203.0.113.7" not in t.ip_hash for t in db.query(TrackRequest).all())  # jen hash


def test_track_rejects_unknown_shop(client_db):
    client, *_ = client_db
    assert client.post("/sledovat", data={"url": "https://example.com/x"}).status_code == 422


def test_live_result_links_to_preview(client_db, monkeypatch):
    client, *_ = client_db
    hit = SearchHit("mironet", "Zcela nová pračka Test 7kg", 9990, "https://www.mironet.cz/pracka-test+dp9/", None)
    monkeypatch.setattr(m, "smart_offers", lambda db, q: {"offers": [(hit, None)], "statuses": [], "intent": None, "summary": "", "ai_used": False})
    html = client.get("/hledat/zive?q=pracka test").text
    assert "/nahled?url=https%3A//www.mironet.cz/pracka-test%2Bdp9/" in html or "/nahled?url=https%3A%2F%2Fwww.mironet.cz" in html
    assert "Sledovat tento produkt" in html
