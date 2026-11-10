"""v6: Kdy nakupovat – kalendář (obecné vzorce) a datová doporučení jen s dostatkem dat."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from lovceny import seasons
from lovceny.categories import CATEGORIES

BANNED = ("falešn", "falesn", "podvod")


def _prod(pid, daily: dict, demo=False):
    pts = [SimpleNamespace(price=v, captured_at=datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc), is_demo=demo)
           for d, v in daily.items()]
    return SimpleNamespace(id=pid, offers=[SimpleNamespace(points=pts)])


def _series(start: date, days: int, price_fn):
    return {start + timedelta(days=i): price_fn(start + timedelta(days=i)) for i in range(days)}


def test_black_friday_and_cyber_monday_dates():
    assert seasons.black_friday(2026) == date(2026, 11, 27)
    assert seasons.black_friday(2025) == date(2025, 11, 28)
    assert seasons.black_friday(2024) == date(2024, 11, 29)
    assert seasons.cyber_monday(2026) == date(2026, 11, 30)
    bf = next(p for p in seasons.periods(2026) if p.key == "black-friday")
    assert "27. 11. 2026" in bf.when


def test_every_category_has_general_tip_and_phone_hint():
    for c in CATEGORIES:
        assert seasons.category_tip(c.slug), c.slug
    assert seasons.category_tip("telefony").startswith(
        "U telefonů se často vyplatí počkat na zářijové představení nových modelů")
    assert seasons.category_tip("neexistuje") == ""
    for t in list(seasons.CATEGORY_TIPS.values()) + [p.text for p in seasons.periods(2026)]:
        assert not any(b in t.lower() for b in BANNED)
        assert "%" not in t  # obecné vzorce bez čísel – žádné vymyšlené statistiky


def test_insufficient_data_gives_no_claim():
    prods = [_prod(i, _series(date(2026, 9, 1), 40, lambda d: 1000)) for i in range(3)]
    ins = seasons.analyze(prods, "telefony")
    assert not ins.enough and ins.best_month is None
    assert "aspoň 5 produktů" in ins.missing and "6 různých měsíců" in ins.missing


def test_enough_data_finds_cheapest_month():
    # 6 produktů, 8 měsíců denních cen; v listopadu o 10 % levněji
    prods = [_prod(i, _series(date(2025, 6, 1), 240, lambda d, i=i: (900 if d.month == 11 else 1000) * (1 + i)))
             for i in range(6)]
    ins = seasons.analyze(prods, "televize")
    assert ins.enough and ins.products == 6 and ins.months >= 6
    assert ins.best_month == 11 and ins.best_month_loc == "v listopadu"
    assert 9.5 <= ins.best_diff_pct <= 10.5
    assert ins.since == date(2025, 6, 1)


def test_small_difference_is_not_claimed():
    prods = [_prod(i, _series(date(2025, 6, 1), 240, lambda d: 995 if d.month == 3 else 1000)) for i in range(6)]
    ins = seasons.analyze(prods, "televize")
    assert ins.enough and ins.best_month is None


def test_demo_points_never_count():
    prods = [_prod(i, _series(date(2025, 6, 1), 240, lambda d: 900 if d.month == 11 else 1000), demo=True) for i in range(6)]
    ins = seasons.analyze(prods, "televize")
    assert not ins.enough and ins.products == 0


def test_month_needs_enough_products():
    # jen 2 produkty mají data v prosinci → prosinec se nezapočítá
    prods = [_prod(i, _series(date(2025, 5, 1), 210, lambda d: 1000)) for i in range(6)]
    for i in range(2):
        prods.append(_prod(10 + i, _series(date(2025, 12, 1), 20, lambda d: 500)))
    ins = seasons.analyze(prods, "televize")
    assert 12 not in ins.by_month


def test_page_general_and_data_labels(client_db, monkeypatch):
    client, db, make = client_db
    html = client.get("/kdy-nakupovat").text
    for want in ("Kdy nakupovat", "Black Friday", "Cyber Monday", "Povánoční výprodeje", "Zpátky do školy",
                 "Prime Day", "Představení nových iPhonů", "Obecný vzorec", "Naše data", "Zatím málo dat",
                 'id="telefony"', "obecné vzorce trhu", "/over-slevu"):
        assert want in html, want
    assert not any(b in html.lower() for b in BANNED)
    # s dostatkem dat ukážeme číslo z DB
    fake = {c.slug: seasons.Insight(c.slug, False, missing="x") for c in CATEGORIES}
    fake["televize"] = seasons.Insight("televize", True, products=7, months=8, points=1500, since=date(2025, 3, 1),
                                       best_month=11, best_diff_pct=8.4)
    monkeypatch.setattr(seasons, "insights", lambda db: fake)
    html = client.get("/kdy-nakupovat").text
    assert "v listopadu" in html and "8,4 %" in html and "7 produktů" in html and "1500 denních cen" in html


def test_hint_on_category_and_product(client_db):
    client, db, make = client_db
    html = client.get("/kategorie/telefony").text
    assert "season-hint" in html and "zářijové představení" in html and "/kdy-nakupovat#telefony" in html
    p = make([1000] * 3, name="Apple iPhone 15 128GB testovaci", category="telefony")
    html = client.get(f"/produkt/{p.id}").text
    assert "season-hint" in html and "zářijové představení" in html
    p2 = make([1000] * 3, name="Bez kategorie xyz")
    assert "season-hint" not in client.get(f"/produkt/{p2.id}").text


def test_nav_footer_sitemap():
    from fastapi.testclient import TestClient
    from lovceny.main import app
    c = TestClient(app)
    html = c.get("/").text
    assert 'href="/kdy-nakupovat"' in html and 'href="/over-slevu"' in html
    sm = c.get("/sitemap.xml").text
    assert "/kdy-nakupovat</loc>" in sm and "/over-slevu</loc>" in sm
    assert "/rozsireni</loc>" not in sm and "/prehled</loc>" not in sm and "/hlidat-rozpocet</loc>" not in sm
