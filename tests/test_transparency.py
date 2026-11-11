"""Právní úpravy: faktický štítek „Za 30 dní bylo levněji“, /jak-radime, /pro-obchody, /podminky, affiliate."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from lovceny.main import app
from lovceny.models import ContactMessage
from lovceny.db import SessionLocal, init_db
from lovceny.shops import SHOPS
from lovceny.shops.scrape import USER_AGENT
from lovceny.verdict import compute

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
BANNED = ("falešn", "falesn", "podvod", "klamav", "přifouk", "zdánliv")


def pts(fn, days=40):
    return [SimpleNamespace(price=fn(d), captured_at=NOW - timedelta(days=d)) for d in range(days, 0, -1)]


def test_label_shows_min30_and_date():
    v = compute(pts(lambda d: 900 if d == 10 else 1000), 950, claimed_original=1500, now=NOW)
    assert v.code == "fake" and v.headline == "Za 30 dní bylo levněji"
    assert v.ref_min30 == 900 and v.ref_date == (NOW - timedelta(days=10)).date()
    assert v.tooltip.replace("\xa0", " ") == "Nejnižší cena za posledních 30 dní byla 900 Kč (dne 4. 10. 2026). Sleva se počítá z vyšší ceny."
    assert all(b not in (v.text + v.reason + v.tooltip).lower() for b in BANNED)


def test_threshold_5_percent():
    assert compute(pts(lambda d: 1000), 990, claimed_original=1050, now=NOW).code != "fake"
    assert compute(pts(lambda d: 1000), 990, claimed_original=1051, now=NOW).code == "fake"


def test_jump_rule_has_data():
    v = compute(pts(lambda d: 1200 if 3 <= d <= 18 else 1000), 1020, now=NOW)
    assert v.code == "fake" and v.ref_min30 == 1000 and "Nejnižší cena za posledních 30 dní" in v.tooltip
    # dnešní cena = 30denní minimum → štítek by nebyl pravdivý, nedáváme ho
    assert compute(pts(lambda d: 1200 if 3 <= d <= 18 else 1000), 1000, now=NOW).code != "fake"


def test_pages_and_links():
    c = TestClient(app)
    r = c.get("/jak-radime")
    assert r.status_code == 200
    h = r.text
    hn = h.replace(" ", " ")
    for s in ("Jak řadíme a hodnotíme", "bez dopravy", "Placené umístění nemáme", "§ 12a", "3 %", "5 %", "12 %",
              "7 dní", "3× denně", "Ceny jsou orientační", "id=\"za-30-dni-levneji\""):
        assert s in hn, s
    for shop in SHOPS.values():
        if shop.retired:
            assert shop.name.replace("'", "&#39;") not in h
            continue
        assert shop.name.replace("'", "&#39;") in h
    assert "Odkaz může být provizní" not in h  # bez příznaku skryté
    p = c.get("/pro-obchody").text
    assert "LovcenyBot" in p and "robots.txt" in p and "7 dní" in p and "mailto:info@lovceny.cz" in p
    assert "tema=obchod-oprava" not in p
    t = c.get("/podminky").text
    assert "Karel Matusiak" in t and "IČO 30210453" in t and "info@lovceny.cz" in t
    assert "bude doplněn po založení živnosti" not in t
    home = c.get("/").text
    for href in ('href="/jak-radime"', 'href="/pro-obchody"', 'href="/podminky"'):
        assert href in home
    sm = c.get("/sitemap.xml").text
    assert "/jak-radime</loc>" in sm and "/pro-obchody</loc>" in sm and "/podminky</loc>" in sm
    r = c.get("/metodika", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/jak-radime"


def test_rank_link_in_category_and_search():
    c = TestClient(app)
    from lovceny.categories import CATEGORIES
    # stránka kategorie s produkty i bez nich – odkaz je u počtu výsledků (jen když jsou)
    h = c.get(f"/kategorie/{CATEGORIES[0].slug}").text
    assert ("Jak řadíme?" in h) or ("zatím nic nesledujeme" in h)
    from lovceny.main import tpl
    live = tpl.env.get_template("_live.html").render(offers=[], q="x", statuses=[], intent=None, summary=None)
    assert "Jak řadíme" not in live  # bez výsledků odkaz není potřeba
    from lovceny.shops.search import SearchHit
    hit = SearchHit(shop="mironet", name="Test TV", price=1000.0, url="https://www.mironet.cz/x", image=None)
    live = tpl.env.get_template("_live.html").render(offers=[(hit, None)], q="x", statuses=[], intent=None, summary=None)
    assert 'href="/jak-radime">Jak řadíme?' in live


def test_no_accusatory_words_on_site():
    c = TestClient(app)
    for path in ("/", "/slevy", "/o-nas", "/jak-radime", "/pro-obchody", "/podminky", "/kategorie"):
        h = c.get(path).text.lower()
        for b in BANNED:
            assert b not in h, (path, b)


def test_user_agent_links_pro_obchody():
    assert "https://lovceny.cz/pro-obchody" in USER_AGENT and "LovcenyBot" in USER_AGENT


def test_affiliate_note_flag(monkeypatch):
    c = TestClient(app)
    monkeypatch.setenv("LOVCENY_AFFILIATE", "1")
    assert "Odkaz může být provizní – na pořadí to nemá vliv." in c.get("/jak-radime").text
    monkeypatch.setenv("LOVCENY_AFFILIATE", "0")
    monkeypatch.setenv("EHUB_AID", "")  # eHUB by jinak držel AFFILIATE_ON
    assert "Odkaz může být provizní" not in c.get("/").text


def test_contact_topic_shop_correction():
    init_db()
    c = TestClient(app)
    h = c.get("/o-nas?tema=obchod-oprava").text
    assert "mailto:info@lovceny.cz" in h and "formulář na webu není" in h
    r = c.post("/kontakt", data={"message": "Prosím vyřaďte náš obchod.", "topic": "obchod-oprava"},
               headers={"X-Forwarded-For": "10.9.9.9"}, follow_redirects=False)
    assert r.status_code == 303 and "odeslano=1" in r.headers["location"]
    db = SessionLocal()
    assert db.query(ContactMessage).filter_by(topic="obchod-oprava").count() >= 1
    db.close()
