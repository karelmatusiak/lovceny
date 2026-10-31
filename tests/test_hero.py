"""Hero showcase: výběr produktů s cenou a obrázkem, štítky jen když platí."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.hero import hero_showcase, clear_cache, _badge_for
from lovceny.main import app
from lovceny.models import Product, Offer, PricePoint, Watch
from lovceny.util import slugify
from lovceny.verdict import Verdict, MIN_DAYS


def _seed(db, *, name, prices, image="https://img.example/x.jpg", shop="mironet", watches=0):
    p = Product(name=name, slug=slugify(name) + "-" + str(abs(hash(name)) % 10_000),
                image_url=image)
    o = Offer(product=p, shop=shop, url=f"https://www.mironet.cz/{p.slug}",
              price=prices[-1], status="ok", in_stock=True)
    now = datetime.now(timezone.utc)
    for i, pr in enumerate(prices):
        o.points.append(PricePoint(price=pr, captured_at=now - timedelta(days=len(prices) - 1 - i)))
    db.add(p)
    db.flush()
    for i in range(watches):
        db.add(Watch(product_id=p.id, email=f"w{i}-{p.id}@ex.cz", target_price=1,
                     token=f"tok-{p.id}-{i}"))
    db.commit()
    return p


def test_hero_excludes_no_price_and_no_image():
    clear_cache()
    init_db()
    db = SessionLocal()
    _seed(db, name="Hero OK", prices=[1000] * 10)
    bare = Product(name="Bez obrázku", slug="bez-obr", image_url=None)
    db.add(bare)
    db.flush()
    db.add(Offer(product=bare, shop="mironet", url="https://www.mironet.cz/bez-obr",
                 price=500, status="ok"))
    no_price = Product(name="Bez ceny", slug="bez-ceny-hero", image_url="https://img.example/y.jpg")
    db.add(no_price)
    db.flush()
    db.add(Offer(product=no_price, shop="mironet", url="https://www.mironet.cz/bez-ceny-hero",
                 price=None, status="pending"))
    db.commit()

    picks = hero_showcase(db, limit=4)
    names = {h.product.name for h in picks}
    assert "Hero OK" in names
    assert "Bez obrázku" not in names
    assert "Bez ceny" not in names
    for h in picks:
        assert h.best.price is not None
        assert (h.product.image_url or "").strip()
    db.close()


def test_hero_prefers_real_discount_and_badge():
    clear_cache()
    init_db()
    db = SessionLocal()
    # Silná sleva vs medián (~2000 → 1200)
    disc = _seed(db, name="Velká sleva hero", prices=[2000] * 8 + [1200], watches=0)
    # Stabilní cena, hodně hlídání
    flat = _seed(db, name="Stabilní hlídaný", prices=[1500] * 10, watches=5)
    picks = hero_showcase(db, limit=40)
    assert picks
    pos = {h.product.id: i for i, h in enumerate(picks)}
    assert disc.id in pos and flat.id in pos
    assert pos[disc.id] < pos[flat.id]
    badge = picks[pos[disc.id]].badge
    assert badge in ("Nejnižší cena",) or (badge and badge.startswith("−"))
    assert all(h.badge is None or h.badge.startswith("−") or h.badge == "Nejnižší cena" for h in picks)
    db.close()


def test_hero_thin_history_fallback_no_fake_badge():
    clear_cache()
    init_db()
    db = SessionLocal()
    # Jen 2 dny historie – málo pro verdikt, ale s obrázkem a cenou
    a = _seed(db, name="Novinka A", prices=[999, 999], watches=3)
    b = _seed(db, name="Novinka B", prices=[1999], watches=1)
    picks = hero_showcase(db, limit=40)
    ids = {h.product.id for h in picks}
    assert a.id in ids
    assert b.id in ids
    # Při řídké historii bez výrazného poklesu žádný falešný „Nejnižší cena“
    for h in picks:
        if h.product.id in (a.id, b.id):
            assert h.badge != "Nejnižší cena" or (
                h.verdict.min90 is not None and float(h.best.price) <= float(h.verdict.min90) * 1.005
            )
    db.close()


def test_badge_for_rules():
    v = Verdict(code="buy", label="Kupuj", text="", current=900, min90=900, median90=1200, max90=1500)
    assert _badge_for(900, v, hist_n=MIN_DAYS + 2) == "Nejnižší cena"
    v2 = Verdict(code="buy", label="Kupuj", text="", current=1000, min90=900, median90=1200, max90=1500)
    b = _badge_for(1000, v2, hist_n=MIN_DAYS + 2)
    assert b and b.startswith("−")
    # Thin history, flat price → no badge
    v3 = Verdict(code="none", label="x", text="", current=1000, min90=1000, median90=None, max90=1000)
    assert _badge_for(1000, v3, hist_n=2) is None


def test_home_renders_hero_picks():
    clear_cache()
    init_db()
    db = SessionLocal()
    _seed(db, name="Hero na homepage", prices=[3000] * 9 + [2100])
    html = TestClient(app).get("/").text
    assert "Teď stojí za to" in html
    assert "hero-picks" in html
    assert "Hero na homepage" in html
    assert "hero-art" not in html
    db.close()
