"""UX cleanup: nav, homepage category tiles, category intros, earnings copy per AFFILIATE_ON."""
from fastapi.testclient import TestClient

from lovceny.db import init_db
from lovceny.main import app


def test_nav_simplified_links():
    init_db()
    html = TestClient(app).get("/").text
    # Main nav links (panel)
    assert 'href="/kategorie"' in html
    assert 'href="/slevy"' in html
    assert 'href="/kdy-nakupovat"' in html
    assert 'href="/o-nas"' in html
    assert ">Prověřit cenu<" in html or ">Prověřit cenu</a>" in html
    # Ověř slevu not duplicated in nav panel as primary CTA
    assert 'class="btn btn-verify"' not in html
    assert 'class="nav-verify"' not in html
    # Still reachable from footer + hero alt link
    assert html.count('href="/over-slevu"') >= 2
    assert "nebo ověř slevu z odkazu" in html
    assert 'id="nav-toggle"' in html and "aria-expanded" in html


def test_home_category_tiles():
    init_db()
    html = TestClient(app).get("/").text
    assert 'class="home-cats"' in html or 'home-cats' in html
    assert 'id="home-cats-h"' in html
    assert 'href="/kategorie/telefony"' in html
    assert "Všechny kategorie" in html
    # Duplicate long how-section removed
    assert "Od odkazu k hlídání ceny" not in html or 'id="jak"' not in html
    assert 'id="jak"' not in html
    # Short 3-step kept
    assert "Jak to funguje" in html and "Tři kroky" in html
    # Slevy dne kept (item 2 not approved for change)
    assert "Slevy dne" in html


def test_category_intro_collapsible():
    init_db()
    html = TestClient(app).get("/kategorie/telefony").text
    assert "cat-intro" in html
    assert "Více o kategorii" in html
    assert "Sledujeme ceny mobilů" in html or "mobilů" in html
    assert 'href="/kdy-nakupovat#telefony"' in html
    # Not on filtered view
    html2 = TestClient(app).get("/kategorie/telefony/nejlevnejsi").text
    assert "cat-intro" not in html2 or "Více o kategorii" not in html2


def test_search_meta_description():
    init_db()
    html = TestClient(app).get("/hledat", params={"q": "iphone 16"}).text
    assert 'name="description" content="' in html
    assert "iphone 16" in html.lower() or "iPhone" in html or "iphone 16" in html
    assert "Ceny a historie" in html


def test_earnings_text_affiliate_off(monkeypatch):
    init_db()
    import lovceny.main as m
    monkeypatch.setattr(m, "affiliate_on", lambda: False)
    # Jinja global is the function object assigned at import – rebind
    m.tpl.env.globals["AFFILIATE_ON"] = lambda: False
    c = TestClient(app)
    home = c.get("/").text
    assert "Zatím nevyděláváme" in home or "zatím" in home.lower()
    assert "affiliate" in home.lower()
    about = c.get("/o-nas").text
    assert "nijak" in about.lower() or "Zatím" in about or "zatím" in about
    assert "affiliate" in about.lower()
    assert "nikdy neovlivní" in about
    # Footer
    assert "Zatím bez provizních odkazů" in home


def test_earnings_text_affiliate_on(monkeypatch):
    init_db()
    import lovceny.main as m
    monkeypatch.setattr(m, "affiliate_on", lambda: True)
    m.tpl.env.globals["AFFILIATE_ON"] = lambda: True
    c = TestClient(app)
    home = c.get("/").text
    assert "provizní" in home.lower() or "Affiliate" in home
    assert "Zatím nevyděláváme" not in home
    about = c.get("/o-nas").text
    assert "affiliate" in about.lower()
    assert "nikdy neovlivní" in about
    # FAQ JSON-LD should mention affiliate as live when on
    assert "Některé odkazy jsou affiliate" in about or "provizní" in about.lower()
    footer_snip = home[home.find("legal"):home.find("legal")+400] if "legal" in home else home[-800:]
    assert "provizní" in footer_snip.lower() or "Odkaz může být provizní" in home
