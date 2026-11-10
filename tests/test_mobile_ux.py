"""Mobilní UX: obchody, hero, kategorie, hledání v hlavičce, stránkování, cíle klepnutí, prázdné /slevy."""
from pathlib import Path

from fastapi.testclient import TestClient

from lovceny.db import init_db
from lovceny.main import _pager_numbers, app

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "lovceny/static/app.css").read_text(encoding="utf-8")
STYLE = (ROOT / "lovceny/static/style.css").read_text(encoding="utf-8")


def test_pager_window_stays_compact():
    assert _pager_numbers(1, 3) == [1, 2, 3]
    assert _pager_numbers(1, 9) == [1, 2, 3, None, 9]
    assert _pager_numbers(5, 9) == [1, None, 5, None, 9]
    assert _pager_numbers(9, 9) == [1, None, 7, 8, 9]
    for pages in range(1, 12):
        for page in range(1, pages + 1):
            nums = [n for n in _pager_numbers(page, pages) if n is not None]
            assert len(nums) <= 4
            assert nums[0] == 1 or pages <= 4
            assert page in nums
            assert nums[-1] == pages


def test_shops_table_has_stacked_labels():
    init_db()
    html = TestClient(app).get("/obchody").text
    assert "tbl-shops" in html and "shops-card" in html
    for label in ("Obchod", "Zdroj cen", "Stav", "Poznámka"):
        assert f'data-label="{label}"' in html
    assert "@media (max-width:600px)" in CSS and ".tbl-shops td::before{content:attr(data-label)" in CSS


def test_header_search_on_inner_pages_and_short_home_placeholder():
    init_db()
    c = TestClient(app)
    home = c.get("/").text
    assert 'class="nav-search"' not in home
    assert 'placeholder="Název nebo odkaz z e-shopu"' in home
    assert "sluchátka na běhání nebo odkaz" not in home
    shops = c.get("/obchody").text
    assert 'class="nav-search"' in shops and 'action="/hledat"' in shops
    assert 'name="q"' in shops.split('class="nav-search"', 1)[1].split("</form>", 1)[0]
    assert ".nav-search{display:none}" in CSS
    assert ".nav-search{display:flex" in CSS


def test_category_header_collapses_tip_and_filters():
    init_db()
    html = TestClient(app).get("/kategorie/sluchatka").text
    assert 'class="filters-summary"' in html and ">Filtry" in html
    assert 'class="filters-check"' in html
    assert 'class="tip-box"' in html and "Tip, kdy nakupovat" in html
    assert 'class="filters cat-filters"' in html
    assert ".cat-filters-wrap > .cat-filters{display:none" in CSS
    assert ".cat-hero .cat-lead{" in CSS and "-webkit-line-clamp:2" in CSS


def test_product_hero_is_large_on_mobile_and_desktop_slot_stays():
    assert "width:min(320px,100%);max-width:320px;height:auto;aspect-ratio:1" in CSS
    assert ".product .p-head .p-img{width:120px;height:120px;" in CSS


def test_tap_targets_and_minimum_text():
    assert ".foot ul a,.legal a{display:inline-flex" in CSS and "min-height:40px" in CSS
    assert ".rank-link{display:inline-flex" in CSS and "min-height:40px" in CSS.split(".rank-link{")[1].split("}")[0]
    assert ".expect-badge{display:inline-block;font-size:12px" in CSS
    assert ".offer-best-tag{display:inline-block;margin-left:6px;font-size:12px" in CSS
    assert ".bf-facts small,.bf-dates small{font-size:12px}" in CSS
    assert "font-size:11px" not in STYLE.split(".hero-pick-cat{font-size:12px}")[-1].split("@media (min-width:800px)")[0]
    assert ".pager-list{display:flex;flex-wrap:nowrap" in CSS
    assert "min-width:44px;min-height:44px" in CSS


def test_slevy_empty_state_separates_fallback(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from lovceny.db import SessionLocal
    from lovceny.models import Offer, PricePoint, Product

    init_db()
    db = SessionLocal()
    p = Product(name="Sony WH-1000XM5", slug="sony-wh-1000xm5", category_slug="sluchatka", category="Sluchátka")
    o = Offer(product=p, shop="mironet", url="https://www.mironet.cz/sony-wh-1000xm5",
              price=7990, status="ok", in_stock=True)
    db.add(p)
    db.flush()
    now = datetime.now(timezone.utc)
    for day in range(8):
        db.add(PricePoint(offer=o, price=7990, captured_at=now - timedelta(days=day), source="scrape"))
    db.commit()
    db.close()
    import lovceny.main as m
    monkeypatch.setattr(m, "real_deals", lambda db, **k: {"buys": [], "fakes": [], "include_demo": False})
    html = TestClient(app).get("/slevy").text
    assert "Dnes tu žádná skutečná sleva není" in html
    assert "Zatím tu žádné skutečné slevy nejsou" not in html
    assert 'class="empty-fallback"' in html
    assert "Dnes žádná skutečná sleva — zatím sledujte tyto produkty" in html
    assert "Níže nejsou slevy" in html
    assert html.index("empty-state-honest") < html.index("empty-fallback")
    assert "Sony WH-1000XM5" in html.split("empty-fallback", 1)[1]
    assert "žádnou takovou slevu" in html
