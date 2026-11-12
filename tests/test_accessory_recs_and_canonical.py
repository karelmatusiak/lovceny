"""Příslušenství mimo doporučení/hero/deals; kanonická doména lovceny.cz."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny.categories import is_accessory_text
from lovceny.db import SessionLocal, init_db
from lovceny.hero import clear_cache as clear_hero, hero_showcase
from lovceny.main import app, priced_product_cards
from lovceny.models import Offer, PricePoint, Product
from lovceny import deals as dealsmod
from lovceny.web import public_url, CANONICAL_PUBLIC_URL


def test_accessory_name_extensions():
    assert is_accessory_text("Sada na čištění AirPods Belkin")
    assert is_accessory_text("Hliníkový podstavec pod MacBook")
    assert is_accessory_text("Držák na telefon do auta")
    assert is_accessory_text("USB-C kabel 2 m")
    assert is_accessory_text("HEPA filtr pro vysavač")
    assert not is_accessory_text("JBL Tune 520BT bílá")
    assert not is_accessory_text("Sony MDR-ZX310APR, červená")


def test_public_url_canonical_rewrites_onrender(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert public_url() == "https://lovceny.cz"
    monkeypatch.setenv("PUBLIC_URL", "https://lovceny.onrender.com")
    assert public_url() == CANONICAL_PUBLIC_URL
    monkeypatch.setenv("PUBLIC_URL", "https://lovceny.cz")
    assert public_url() == "https://lovceny.cz"


def test_homepage_ld_and_canonical_use_lovceny_cz(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://lovceny.onrender.com")
    init_db()
    html = TestClient(app).get("/").text
    assert "lovceny.onrender.com" not in html
    assert "https://lovceny.cz" in html
    assert '"@type": "WebSite"' in html or '"@type":"WebSite"' in html
    assert '"url":"https://lovceny.cz/"' in html or '"url": "https://lovceny.cz/"' in html
    assert "SearchAction" not in html
    assert 'rel="canonical" href="https://lovceny.cz/"' in html or 'href="https://lovceny.cz/"' in html


def _seed(name, price=1990, shops=("mironet",), image=True):
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    p = Product(
        name=name,
        slug=name.lower().replace(" ", "-")[:60],
        category_slug="sluchatka",
        category="Sluchátka",
        image_url="https://example.com/x.jpg" if image else None,
    )
    db.add(p)
    db.flush()
    for i, shop in enumerate(shops):
        o = Offer(
            product=p, shop=shop, url=f"https://www.mironet.cz/{p.slug}-{i}",
            price=price + i, in_stock=True, status="ok",
        )
        db.add(o)
        db.flush()
        db.add(PricePoint(offer=o, price=price + i, captured_at=now, source="scrape"))
    db.commit()
    pid = p.id
    db.close()
    return pid


def test_hero_and_cards_skip_accessories():
    init_db()
    clear_hero()
    dealsmod.clear_cache()
    _seed("Sada na čištění AirPods Belkin", price=399, shops=("datart",))
    _seed("Hliníkový podstavec pod MacBook", price=599, shops=("datart",))
    _seed("Sony MDR-ZX310APR hlavní", price=528, shops=("mironet", "abcom"))
    _seed("JBL Tune 520BT hlavní", price=757, shops=("mironet", "datart"))
    db = SessionLocal()
    picks = hero_showcase(db, limit=4)
    names = [h.product.name for h in picks]
    assert all("čištění" not in n.lower() and "podstavec" not in n.lower() for n in names)
    assert any("Sony" in n or "JBL" in n for n in names)
    # multi-shop preferred when present
    if len(picks) >= 1:
        top = picks[0]
        n_shops = len({o.shop for o in top.product.offers if o.price is not None})
        assert n_shops >= 1
    cards = priced_product_cards(db, limit=6, order="new")
    card_names = [p.name for p, _, _ in cards]
    assert all(not is_accessory_text(n) for n in card_names)
    db.close()


def test_near_minima_skips_accessories(monkeypatch):
    init_db()
    dealsmod.clear_cache()
    monkeypatch.setattr("lovceny.main.real_deals", lambda db, **k: {"buys": [], "fakes": [], "include_demo": False})
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    # accessory near min
    p = Product(name="Podstavec Near Min Acc", slug="podstavec-near", category_slug="notebooky",
                category="Notebooky", image_url="https://example.com/a.jpg")
    o = Offer(product=p, shop="mironet", url="https://www.mironet.cz/podstavec-near",
              price=1050, status="ok", in_stock=True)
    db.add(p)
    db.flush()
    for d in range(30, 0, -1):
        db.add(PricePoint(offer=o, price=1000, captured_at=now - timedelta(days=d), source="scrape"))
    db.add(PricePoint(offer=o, price=1050, captured_at=now, source="scrape"))
    # main product near min
    p2 = Product(name="Sony Near Min Phone", slug="sony-near-min", category_slug="sluchatka",
                 category="Sluchátka", image_url="https://example.com/b.jpg")
    o2 = Offer(product=p2, shop="mironet", url="https://www.mironet.cz/sony-near",
               price=1050, status="ok", in_stock=True)
    db.add(p2)
    db.flush()
    for d in range(30, 0, -1):
        db.add(PricePoint(offer=o2, price=1000, captured_at=now - timedelta(days=d), source="scrape"))
    db.add(PricePoint(offer=o2, price=1050, captured_at=now, source="scrape"))
    db.commit()
    db.close()
    near = dealsmod.near_minima(SessionLocal(), limit=12)
    names = [r.product.name for r in near]
    assert "Podstavec Near Min Acc" not in names
    assert any("Sony Near Min" in n for n in names)
    html = TestClient(app).get("/slevy").text
    assert "Podstavec Near Min Acc" not in html
