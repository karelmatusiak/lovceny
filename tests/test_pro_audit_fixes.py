"""Professional-audit fixes: coverage copy, /obchody status, quiet empty states, cache, retired shops."""
import re
from datetime import datetime, timedelta, timezone

from lovceny.shops.registry import SHOPS
from lovceny import catcache, monitor
from lovceny.models import Offer, Product


def test_vestavne_retired_hidden_from_shops_and_status(client_db):
    c, db, make = client_db
    assert SHOPS["vestavne"].retired is True
    html = c.get("/obchody").text
    assert "Vestavné" not in html
    assert "st-bad" not in html
    assert "připravujeme oficiální feed" in html
    assert any(n in html for n in ("Smarty", "CZC", "Mall"))
    st = c.get("/stav").text
    assert "Vestavné" not in st


def test_obchody_meta_and_wait_badge(client_db):
    c, db, make = client_db
    r = c.get("/obchody")
    assert r.status_code == 200
    assert re.search(r"<title>Hlídané obchody", r.text)
    assert 'name="description"' in r.text
    assert "oficiální" in r.text.lower()
    assert ">blokuje<" not in r.text
    assert "st-wait" in r.text
    for name in ("Alza", "Datart", "CZC", "Smarty"):
        assert name in r.text


def test_footer_sidlo(client_db):
    c, db, make = client_db
    home = c.get("/").text
    assert "Brumovice" not in home and "Hřiště" not in home
    assert "IČO 30210453" in home and 'href="/kontakt"' in home
    about = c.get("/o-nas").text
    assert "U Hřiště 267/16" in about and "747 71 Brumovice" in about and "neplátce DPH" in about


def test_single_shop_coverage_copy(client_db):
    c, db, make = client_db
    p = make([4990], name="Samsung Galaxy A55 jediny obchod", shop="mironet", category="telefony")
    html = c.get(f"/produkt/{p.id}-{p.slug}").text
    assert "jednoho obchodu" in html
    assert "rozšiřujeme" in html
    assert "Affiliate partnerství" in html or "eHUB" in html
    about = c.get("/o-nas").text
    assert "Nepokrýváme celý trh" in about
    home = c.get("/").text
    assert "Nejoblíbenější telefony" in home
    assert "Pokrytí roste" not in home


def test_pcard_no_duplicate_sledujeme(client_db):
    c, db, make = client_db
    make([1990], name="Thin Hist Phone Card", shop="planeo", category="telefony")
    html = c.get("/kategorie/telefony").text
    assert "pc-tip" not in html
    css = open("lovceny/static/app.css", encoding="utf8").read()
    assert ".pcard .v-none .verdict-reason{display:none}" in css


def test_quiet_slevy_dne_kept(client_db):
    c, db, make = client_db
    home = c.get("/").text
    assert "Skutečné slevy" in home
    assert "Slevy dne" not in home
    assert "expect-badge" not in home and "teaser-empty-quiet" not in home
    css = open("lovceny/static/app.css", encoding="utf8").read()
    assert "expect-note-quiet" in css and "teaser-empty-quiet" in css
    index = open("lovceny/templates/index.html", encoding="utf8").read()
    assert "Nejoblíbenější telefony" in index
    assert "ukážeme nejlepší skutečné slevy" not in index


def test_stav_no_ok_without_metrics(client_db):
    c, db, make = client_db
    from lovceny.models import ShopRunStat
    # Use mobilfone + wipe its run stats so shared SQLite pollution cannot mark it OK.
    db.query(ShopRunStat).filter(ShopRunStat.shop == "mobilfone").delete()
    p = Product(name="Stav Probe Pending", slug="stav-probe-pending", category_slug="telefony")
    db.add(p)
    db.flush()
    db.add(Offer(product=p, shop="mobilfone", url="https://www.mobilfone.cz/stav-probe/", status="pending", price=None))
    db.commit()
    html = c.get("/stav").text
    assert "Mobilfone" in html
    idx = html.find("Mobilfone")
    card = html[idx:idx + 900]
    assert "v pořádku" not in card
    assert "zatím bez dat" in card


def test_catcache_roundtrip_and_clear():
    catcache.clear()
    assert catcache.get("telefony") is None
    catcache.set("telefony", [(1, 2, "v")])
    assert catcache.get("telefony") == [(1, 2, "v")]
    catcache.clear()
    assert catcache.get("telefony") is None


def test_category_uses_cache(client_db):
    c, db, make = client_db
    catcache.clear()
    make([3333], name="Cached Phone Pro", shop="planeo", category="telefony")
    r1 = c.get("/kategorie/telefony")
    assert r1.status_code == 200 and "Cached Phone Pro" in r1.text
    assert catcache.get("telefony") is not None
    r2 = c.get("/kategorie/telefony/nejlevnejsi")
    assert r2.status_code == 200 and "Cached Phone Pro" in r2.text


def test_retired_skipped_by_monitor_and_can_scrape():
    assert SHOPS["vestavne"].can_scrape is False
    assert all(s.slug != "vestavne" for s in monitor.monitored_shops())
