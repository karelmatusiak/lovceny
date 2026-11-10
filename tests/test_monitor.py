"""Monitoring blokací obchodů: statistiky běhu, vyhodnocení, /stav, GitHub issue."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from lovceny import monitor
from lovceny.models import ShopRunStat, Offer
from lovceny.shops.scrape import PoliteClient, Blocked


@pytest.fixture(autouse=True)
def _clean(client_db):
    _, db, _ = client_db
    db.query(ShopRunStat).delete()
    db.commit()
    yield


def _pc(handler):
    pc = PoliteClient()
    pc.client = httpx.Client(transport=httpx.MockTransport(handler))
    return pc


@pytest.mark.parametrize("status,body,kind", [
    (403, "Forbidden", "http"),
    (403, "<html>Please complete the captcha</html>", "captcha"),
    (429, "slow down", "http"),
    (200, "<html><script src='/challenge-platform/x'></script></html>", "captcha"),
])
def test_blocked_classification(status, body, kind, monkeypatch):
    monkeypatch.setattr("lovceny.shops.scrape.MIN_DELAY_S", 0)
    pc = _pc(lambda r: httpx.Response(404) if r.url.path == "/robots.txt" else httpx.Response(status, text=body))
    with pytest.raises(Blocked) as e:
        pc.get("https://www.mironet.cz/p")
    assert e.value.kind == kind and e.value.status == status


def test_robots_classification():
    pc = _pc(lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /") if r.url.path == "/robots.txt" else httpx.Response(200))
    with pytest.raises(Blocked) as e:
        pc.get("https://www.mironet.cz/p")
    assert e.value.kind == "robots"


def test_update_offer_feeds_stats(client_db, monkeypatch):
    from lovceny import service
    _, db, make = client_db
    p = make([1000])
    o = p.offers[0]

    class C:
        def __init__(self, exc=None, html=None):
            self.exc, self.html = exc, html

        def get(self, url):
            if self.exc:
                raise self.exc
            return self.html
    st = monitor.FetchStats("t1")
    monkeypatch.setattr(service, "client", lambda: C(Blocked("ochrana", kind="captcha", status=200)))
    assert service.update_offer(db, o, stats=st) == "blocked"
    monkeypatch.setattr(service, "client", lambda: C(Blocked("HTTP 403", kind="http", status=403)))
    service.update_offer(db, o, stats=st)
    req = httpx.Request("GET", "https://x")
    monkeypatch.setattr(service, "client", lambda: C(httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))))
    assert service.update_offer(db, o, stats=st) == "error"
    html = '<script type="application/ld+json">{"@type":"Product","name":"X","offers":{"price":"999"}}</script>'
    monkeypatch.setattr(service, "client", lambda: C(html=html))
    assert service.update_offer(db, o, stats=st) == "ok"
    a = st.shops["mironet"]
    assert dict(a.counts) == {"captcha": 1, "blocked": 1, "errors": 1, "ok": 1}
    assert dict(a.statuses) == {"200": 2, "403": 1, "404": 1}
    rows = st.save(db)
    r = rows[0]
    assert (r.shop, r.attempts, r.ok, r.failures) == ("mironet", 4, 1, 3)
    assert json.loads(r.statuses)["403"] == 1


def _run(db, shop, ok, blocked=0, captcha=0, errors=0, robots=0, when=None):
    r = ShopRunStat(run_id="r", run_at=when or datetime.now(timezone.utc), shop=shop, ok=ok, blocked=blocked,
                    captcha=captcha, errors=errors, robots=robots, attempts=ok + blocked + captcha + errors + robots)
    db.add(r)
    db.commit()
    return r


def test_evaluate_failing_stale_and_ok(client_db):
    _, db, make = client_db
    now = datetime.now(timezone.utc)
    # Mironet musí mít vlastní čerstvou nabídku, jinak ho vyhodnocení přeskočí.
    make([1000], shop="mironet")
    for o in db.query(Offer).all():  # čistý stav: všechny nabídky čerstvé
        o.status, o.last_checked = "ok", now
    db.commit()
    make([1000], shop="softcom")
    softcom = db.query(Offer).filter_by(shop="softcom").all()
    for o in softcom:
        o.status, o.last_checked = "ok", now - timedelta(days=3)
    db.commit()
    runs = [_run(db, "mironet", ok=2, blocked=5, captcha=1), _run(db, "softcom", ok=3)]
    h = monitor.evaluate(db, runs, now=now)["shops"]
    assert h["mironet"]["ok"] is False and [r["code"] for r in h["mironet"]["reasons"]] == ["failing"]
    assert "6 z 8" in h["mironet"]["reasons"][0]["text"]
    assert [r["code"] for r in h["softcom"]["reasons"]] == ["stale"]
    for o in softcom:
        o.last_checked = now
    db.commit()
    assert monitor.evaluate(db, runs, now=now)["shops"]["softcom"]["ok"] is True
    # málo pokusů (2) → „většina selhala“ se nehlásí (šum), řeší to až pravidlo stáří
    small = [_run(db, "mironet", ok=0, blocked=2)]
    assert monitor.evaluate(db, small, now=now)["shops"]["mironet"]["ok"] is True


def test_partial_block_is_not_called_ok(client_db):
    """26 blokací z 60 (34 OK, 9 přeskočeno) není „v pořádku“ – je to částečně blokováno."""
    client, db, make = client_db
    now = datetime.now(timezone.utc)
    make([1000], shop="mironet")
    for o in db.query(Offer).filter_by(shop="mironet").all():
        o.status, o.last_checked = "ok", now
    db.commit()
    run = _run(db, "mironet", ok=34, blocked=10, captcha=16, when=now)
    run.skipped = 9
    db.commit()
    h = monitor.evaluate(db, [run], now=now)["shops"]["mironet"]
    assert h["level"] == "partial" and h["ok"] is False
    assert [x["code"] for x in h["reasons"]] == ["partial"]
    assert "26 z 60" in h["reasons"][0]["text"] and "34" in h["reasons"][0]["text"]
    html = client.get("/stav").text
    card = html[html.find("Mironet"):html.find("Jak to vyhodnocujeme")]
    assert "částečně blokováno (26 z 60)" in card
    assert "st-mid" in card
    assert "34 OK z 60" in card and "10 HTTP" in card and "16 captcha" in card
    assert "9 přeskočeno" in card
    assert "v pořádku" not in card
    # pod prahem (2 ze 10) pořád v pořádku – šum neotevírá issue
    quiet = _run(db, "mironet", ok=8, blocked=2, when=now)
    q = monitor.evaluate(db, [quiet], now=now)["shops"]["mironet"]
    assert q["level"] == "ok" and q["ok"] is True


def test_partial_issue_title():
    import shop_health_issues as shi
    assert shi.title_for("Mironet", ["partial"]) == "⚠️ Mironet: část požadavků obchod blokuje"


def test_mironet_stale_first_cap_and_backoff(client_db, monkeypatch):
    """Nejstarší Mironet nabídky první, strop na běh, po 3 blokacích za sebou stop. Captcha se neobchází."""
    import fetch_prices
    _, db, make = client_db
    now = datetime.now(timezone.utc)
    made = []
    for age in (1, 40, 5, 20, 8, 15):
        p = make([1000 + age], name=f"Mironet age {age}")
        o = p.offers[0]
        o.last_checked = now - timedelta(hours=age)
        o.status = "ok"
        made.append(o)
    gone = make([100], name="Mironet vyřazený")
    gone.offers[0].status = "unavailable"
    gone.offers[0].last_checked = now - timedelta(days=30)
    planeo = make([500], name="Planeo vedle", shop="planeo")
    planeo.offers[0].last_checked = now - timedelta(hours=3)
    planeo.offers[0].status = "ok"
    db.commit()

    loaded = fetch_prices.load_scrape_offers(db, ["mironet"], 10)
    assert [o.id for o in loaded] == [o.id for o in sorted(made, key=lambda o: o.last_checked)]
    assert gone.offers[0].id not in {o.id for o in loaded}

    monkeypatch.setattr(fetch_prices, "MIRONET_MAX_PER_RUN", 6)
    monkeypatch.setattr(fetch_prices, "MIRONET_BACKOFF_AFTER", 3)
    called = []
    script = ["blocked", "ok", "blocked", "blocked", "blocked"]

    def fake(db, o, stats=None):
        if o.shop == "mironet":
            st = script[len([c for c in called if c[0] == "mironet"])]
            called.append((o.shop, o.id))
            stats.add(o.shop, "blocked" if st == "blocked" else "ok", 403 if st == "blocked" else 200)
            return st
        called.append((o.shop, o.id))
        stats.add(o.shop, "ok", 200)
        return "ok"

    monkeypatch.setattr(fetch_prices, "update_offer", fake)
    monkeypatch.setattr(fetch_prices, "check_watches", lambda db: 0)
    fetch_prices.main(["--min-age-hours", "0", "--limit", "50"])
    mir = [oid for shop, oid in called if shop == "mironet"]
    oldest = [o.id for o in sorted(made, key=lambda o: o.last_checked)]
    # blocked, ok (série se vynuluje), blocked, blocked, blocked → stop. Šestý nejstarší už ne.
    assert mir == oldest[:5]
    assert oldest[5] not in mir
    assert planeo.offers[0].id in [oid for shop, oid in called if shop == "planeo"]


def test_mironet_gap_is_slower_with_jitter_and_does_not_bypass(monkeypatch):
    import random
    from lovceny.shops import scrape
    from lovceny.shops.scrape import PoliteClient
    monkeypatch.setattr(scrape, "MIN_DELAY_S", 6.0)
    monkeypatch.setattr(scrape, "MIRONET_MIN_DELAY_S", 12.0)
    monkeypatch.setattr(scrape, "MIRONET_JITTER_S", 4.0)
    rng = random.Random(1)
    gaps = [scrape.min_gap("www.mironet.cz", rng) for _ in range(40)]
    assert min(gaps) >= 12 and max(gaps) <= 16
    assert max(gaps) - min(gaps) > 0.5
    assert scrape.min_gap("www.planeo.cz", rng) == 6
    assert scrape.SCRAPE_CONCURRENCY == 1
    monkeypatch.setattr(scrape, "MIN_DELAY_S", 0)
    assert scrape.min_gap("www.mironet.cz", rng) == 0
    pc = PoliteClient()
    assert "LovcenyBot" in pc.client.headers["User-Agent"]
    assert "cf_clearance" not in {k.lower() for k in pc.client.headers}


def test_feed_shop_without_url_not_monitored(client_db, monkeypatch):
    _, db, make = client_db
    make([], shop="alza")
    monkeypatch.delenv("ALZA_FEED_URL", raising=False)
    assert "alza" not in monitor.evaluate(db, [])["shops"]


def test_history_and_status_page(client_db):
    client, db, make = client_db
    make([1000])
    now = datetime.now(timezone.utc)
    _run(db, "mironet", ok=9, blocked=1, when=now - timedelta(days=1))
    _run(db, "mironet", ok=1, captcha=9, when=now)
    hist = monitor.history(db, days=14, now=now)
    assert hist["agg"]["mironet"][now.date()] == [1, 10]
    html = client.get("/stav").text
    assert "Stav obchodů" in html and "Mironet" in html and "blokováno (9 z 10)" in html
    assert 'name="robots" content="noindex' in html
    assert "/stav" in client.get("/").text  # odkaz v patičce


def test_fetch_prices_writes_health(client_db, tmp_path, monkeypatch):
    import fetch_prices
    _, db, make = client_db
    for _ in range(3):
        make([1000])
    monkeypatch.setattr(fetch_prices, "update_offer", lambda db, o, stats=None: (stats.add(o.shop, "blocked", 403), "blocked")[1])
    monkeypatch.setattr(fetch_prices, "check_watches", lambda db: 0)
    out = tmp_path / "health.json"
    fetch_prices.main(["--shop", "mironet", "--min-age-hours", "0", "--health-out", str(out), "--limit", "50"])
    h = json.loads(out.read_text())
    assert h["shops"]["mironet"]["ok"] is False and h["shops"]["mironet"]["run"]["blocked"] >= 3


# ---------- shop_health_issues.py ----------
class FakeGH:
    def __init__(self, issues=None):
        self.issues = issues or []
        self.calls = []

    def handler(self, req):
        path, m = req.url.path, req.method
        body = json.loads(req.content) if req.content else None
        self.calls.append((m, path, body))
        if m == "POST" and path.endswith("/labels"):
            return httpx.Response(422, json={})
        if m == "GET" and path.endswith("/issues"):
            return httpx.Response(200, json=[i for i in self.issues if i["state"] == "open"])
        if m == "POST" and path.endswith("/issues"):
            i = {"number": len(self.issues) + 1, "state": "open", **body}
            self.issues.append(i)
            return httpx.Response(201, json=i)
        if m == "PATCH":
            i = next(x for x in self.issues if x["number"] == int(path.rsplit("/", 1)[1]))
            i.update(body)
            return httpx.Response(200, json=i)
        if m == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={})
        return httpx.Response(404)

    def gh(self):
        import shop_health_issues as shi
        return shi.GitHub("karel/lovceny", "tok", client=httpx.Client(base_url="https://api.github.com",
                                                                       transport=httpx.MockTransport(self.handler)))


def _health(ok, codes=("failing",)):
    reasons = [] if ok else [{"code": c, "text": f"důvod {c}"} for c in codes]
    return {"shops": {"mironet": {"name": "Mironet", "ok": ok, "reasons": reasons, "offers": 3, "last_ok": None,
                                  "run": {"attempts": 10, "ok": 2, "blocked": 8, "captcha": 0, "robots": 0, "errors": 0,
                                          "skipped": 0, "statuses": {"403": 8}, "last_error": "HTTP 403"}}}}


def test_issue_lifecycle():
    import shop_health_issues as shi
    f = FakeGH()
    assert shi.sync(f.gh(), _health(False)) == ["create mironet"]
    i = f.issues[0]
    assert i["title"] == "⚠️ Mironet: obchod blokuje stahování cen" and i["labels"] == ["stav-obchodu"]
    assert "lovceny-health:mironet codes=failing" in i["body"] and "HTTP 403: 8×" in i["body"]
    # stejný problém znovu → jen aktualizace těla, žádný komentář (notifikace)
    assert shi.sync(f.gh(), _health(False)) == ["update mironet"]
    assert not any(p.endswith("/comments") for _, p, _ in f.calls)
    # přibyl důvod → komentář
    assert shi.sync(f.gh(), _health(False, ("failing", "stale"))) == ["update+comment mironet"]
    assert "ceny nejsou aktuální" in f.issues[0]["title"]
    # obnoveno → komentář + zavření
    assert shi.sync(f.gh(), _health(True)) == ["close mironet"]
    assert f.issues[0]["state"] == "closed" and f.issues[0]["state_reason"] == "completed"
    assert shi.sync(f.gh(), _health(True)) == []


def test_missing_health_opens_run_issue_then_closes():
    import shop_health_issues as shi
    f = FakeGH()
    assert shi.sync(f.gh(), None) == ["create _beh"]
    assert f.issues[0]["title"] == "⚠️ Stahování cen neproběhlo"
    assert shi.sync(f.gh(), None) == ["update _beh"]
    assert shi.sync(f.gh(), _health(True)) == ["close _beh"]


def test_main_without_token_is_dry_run(tmp_path, monkeypatch, capsys):
    import shop_health_issues as shi
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    p = tmp_path / "h.json"
    p.write_text(json.dumps(_health(False)))
    assert shi.main([str(p)]) == 0
    assert "náhled" in capsys.readouterr().out


def test_workflow_permissions_and_step():
    import pathlib
    wf = (pathlib.Path(__file__).parent.parent / ".github/workflows/fetch.yml").read_text()
    assert "issues: write" in wf and "contents: read" in wf
    assert "--health-out health.json" in wf and "shop_health_issues.py health.json" in wf and "if: always()" in wf


def test_unavailable_product_skipped_not_shop_failure(client_db, monkeypatch):
    """Vyřazený produkt → status unavailable, ve statistikách jako skipped (ne failing)."""
    from lovceny import service
    from lovceny.shops.scrape import unavailable_reason

    assert unavailable_reason("<p>Tento produkt je momentálně nedostupný nebo již není v naší nabídce.</p>")
    assert unavailable_reason("<div>Toto zboží nadále není možné koupit. Prohlédněte si podobné.</div>")
    assert unavailable_reason("<html>běžná stránka s cenou</html>") is None

    _, db, make = client_db
    p = make([1000])
    o = p.offers[0]
    html = """
    <html><h1>X</h1>
    <p>Tento produkt je momentálně nedostupný nebo již není v naší nabídce. Vyberte si jiný.</p>
    </html>
    """

    class C:
        def get(self, url):
            return html

    st = monitor.FetchStats("t-unavail")
    monkeypatch.setattr(service, "client", lambda: C())
    assert service.update_offer(db, o, stats=st) == "unavailable"
    assert o.status == "unavailable"
    assert o.in_stock is False
    assert "nedostupný" in (o.last_error or "")
    # skipped → nepočítá se do attempts/failures
    assert dict(st.shops["mironet"].counts) == {"skipped": 1}
    rows = st.save(db)
    assert rows[0].attempts == 0 and rows[0].skipped == 1 and rows[0].failures == 0


def test_fetch_skips_unavailable_offers(client_db, monkeypatch):
    """fetch_prices.py už nevolá update_offer pro status=unavailable."""
    import fetch_prices
    from lovceny.models import Offer

    _, db, make = client_db
    p = make([1000])
    o = p.offers[0]
    # Celý Mironet v test DB označ jako unavailable (mohou zbývat nabídky z dřívějších testů).
    for row in db.query(Offer).filter_by(shop="mironet").all():
        row.status = "unavailable"
        row.last_error = "produkt nedostupný / vyřazen z nabídky obchodu"
        row.last_checked = None
    db.commit()
    target_id = o.id

    called = []
    monkeypatch.setattr(fetch_prices, "update_offer",
                        lambda db, o, stats=None: called.append(o.id) or "ok")
    monkeypatch.setattr(fetch_prices, "check_watches", lambda db: 0)
    monkeypatch.setattr(fetch_prices, "cleanup_watches", lambda db: {})
    fetch_prices.main(["--shop", "mironet", "--min-age-hours", "0", "--limit", "50"])
    assert called == []
    assert db.get(Offer, target_id).status == "unavailable"
