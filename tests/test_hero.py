"""Hero showcase: výběr produktů s cenou a obrázkem, štítky jen když platí."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny.db import SessionLocal, init_db
from lovceny.hero import hero_showcase, clear_cache, _badge_for, _family_key
from lovceny.main import app
from lovceny.models import Product, Offer, PricePoint, Watch
from lovceny.util import slugify
from lovceny.verdict import Verdict, MIN_DAYS


def _seed(db, *, name, prices, image="https://img.example/x.jpg", shop="mironet", watches=0,
          category_slug=None, category=None, in_stock=True, created_at=None, shops=None, commit=True):
    p = Product(name=name, slug=slugify(name)[:70] + "-" + str(abs(hash(name + (category_slug or ""))) % 1_000_000),
                image_url=image, category_slug=category_slug, category=category)
    if created_at is not None:
        p.created_at = created_at
    db.add(p)
    db.flush()
    now = datetime.now(timezone.utc)
    for si, sh in enumerate(shops or (shop,)):
        o = Offer(product=p, shop=sh, url=f"https://www.{sh}.cz/p/{p.id}-{si}",
                  price=prices[-1], status="ok", in_stock=in_stock)
        for i, pr in enumerate(prices):
            o.points.append(PricePoint(price=pr, captured_at=now - timedelta(days=len(prices) - 1 - i)))
        db.add(o)
    for i in range(watches):
        db.add(Watch(product_id=p.id, email=f"w{i}-{p.id}@ex.cz", target_price=1,
                     token=f"tok-{p.id}-{i}"))
    if commit:
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
    # Silná sleva vs medián (~2000 → 1200), jiná kategorie než plochá cena
    disc = _seed(db, name="Velká sleva hero", prices=[2000] * 8 + [1200], watches=0,
                 category_slug="notebooky", category="Notebooky")
    # Stabilní cena, hodně hlídání
    flat = _seed(db, name="Stabilní hlídaný", prices=[1500] * 10, watches=5,
                 category_slug="vysavace", category="Vysavače")
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
    # Jen 2 dny historie – málo pro verdikt, ale s obrázkem a cenou. Dvě kategorie.
    a = _seed(db, name="Novinka A", prices=[999, 999], watches=3,
              category_slug="notebooky", category="Notebooky")
    b = _seed(db, name="Novinka B", prices=[1999], watches=1,
              category_slug="vysavace", category="Vysavače")
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
    _seed(db, name="Apple iPhone 16 128GB", prices=[3000] * 9 + [2100],
          category_slug="telefony", category="Mobilní telefony", shops=("mironet", "alza"))
    html = TestClient(app).get("/").text
    assert "Aspoň dva obchody, stejné úložiště" in html
    assert "phone-card" in html
    assert "Apple iPhone 16" in html
    assert "Notebooky" not in html
    assert html.find('action="/hledat"') < html.find("phone-grid")
    assert "Naposledy přidané" not in html
    db.close()


def test_hero_pick_img_css_scales_whole_product():
    """Teď stojí za to: fixed-height box + max-* + object-fit contain (no cover-crop / overflow clip)."""
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "lovceny" / "static" / "style.css").read_text()
    # Locate the base .hero-pick-img rule (not media-query overrides alone)
    assert ".hero-pick-img{" in css
    # Must not use the buggy pattern that clips tall shop photos
    assert "aspect-ratio:1;max-height:" not in css.replace(" ", "")
    block = css.split(".hero-pick-img{")[1].split("}")[0]
    assert "height:" in block
    img = css.split(".hero-pick-img img{")[1].split("}")[0]
    assert "object-fit:contain" in img.replace(" ", "") or "object-fit: contain" in img
    assert "max-width:100%" in img.replace(" ", "") or "max-width: 100%" in img
    assert "max-height:100%" in img.replace(" ", "") or "max-height: 100%" in img
    # Never cover-crop in hero picks
    assert "object-fit:cover" not in css.split(".hero-picks")[1].split("@media (min-width:960px)")[0].replace(" ", "")
    hero = css.split("/* hero product showcase */", 1)[1]
    compact = hero.replace(" ", "")
    assert "overflow-x:auto" not in compact
    assert "scroll-snap" not in hero
    assert "150px" not in hero
    assert "repeat(2,minmax(0,1fr))" in compact
    assert "repeat(4,minmax(0,1fr))" in compact
    assert "aspect-ratio:1" in block.replace(" ", "")


def test_family_key_collapses_colour_and_parenthetical_variant():
    assert _family_key("Modecom Volcano Prometheus") == _family_key(
        "Modecom Volcano Prometheus (gaming variant)")
    assert _family_key("Modecom Volcano Prometheus černá") == _family_key(
        "Modecom Volcano Prometheus bílá")
    assert _family_key("Sony WH-1000XM5") != _family_key("Sony WH-1000XM4")
    assert _family_key("iPhone 15 128GB") != _family_key("iPhone 15 Pro 128GB")
    assert _family_key("iPhone 15 128GB černá") != _family_key("iPhone 15 256GB černá")


def test_hero_one_per_category_and_no_near_duplicate_family():
    clear_cache()
    init_db()
    db = SessionLocal()
    for name, price in (
        ("Sony WH-1000XM5 černá", 7990),
        ("Sony WH-1000XM5 bílá", 7890),
        ("Sony WH-1000XM5 (gaming variant)", 7790),
    ):
        _seed(db, name=name, prices=[price], category_slug="sluchatka", category="Sluchátka",
              shops=("mironet", "datart"))
    _seed(db, name="JBL Tune 770NC", prices=[1990], category_slug="sluchatka", category="Sluchátka")
    _seed(db, name="Dyson V15 Detect", prices=[12990], category_slug="vysavace", category="Vysavače")
    picks = hero_showcase(db, limit=6)
    slugs = [h.category_slug for h in picks]
    assert len(slugs) == len(set(slugs))
    assert slugs.count("sluchatka") == 1
    sonys = [h for h in picks if "WH-1000XM5" in h.product.name]
    assert len(sonys) == 1
    assert any(h.product.name.startswith("Dyson") for h in picks)
    assert all(h.badge is None or h.badge == "Nejnižší cena" or h.badge.startswith("−") for h in picks)
    db.close()


def test_hero_thin_history_mixes_categories_past_newest_dump():
    """Čerstvý dump sluchátek nesmí vytlačit starší výrobky z jiných kategorií."""
    clear_cache()
    init_db()
    db = SessionLocal()
    old = datetime.now(timezone.utc) - timedelta(days=40)
    mains = [
        ("Sony WH-1000XM5", "sluchatka", "Sluchátka", 6990),
        ("Lenovo IdeaPad 5", "notebooky", "Notebooky", 15990),
        ("Dyson V15 Detect", "vysavace", "Vysavače", 12990),
        ("DeLonghi Magnifica S", "kavovary", "Kávovary", 8990),
        ("Samsung Galaxy A55", "telefony", "Mobilní telefony", 9990),
        ("Apple Watch SE", "chytre-hodinky", "Chytré hodinky a náramky", 6490),
    ]
    for name, slug, label, price in mains:
        _seed(db, name=name, prices=[price], category_slug=slug, category=label,
              created_at=old, commit=False)
    # Příslušenství omylem zařazené jako sluchátka do výběru nepatří.
    _seed(db, name="Náhradní kabel k Sony WH", prices=[299], category_slug="sluchatka",
          category="Sluchátka", created_at=datetime.now(timezone.utc), commit=False)
    for i in range(130):
        _seed(db, name=f"Modecom Volcano Prometheus {i}", prices=[700 + (i % 30)],
              category_slug="sluchatka", category="Sluchátka",
              created_at=datetime.now(timezone.utc), commit=False)
    db.commit()
    picks = hero_showcase(db, limit=6)
    slugs = [h.category_slug for h in picks]
    assert len(picks) == 6
    assert len(set(slugs)) == 6
    assert set(slugs) == {slug for _, slug, _, _ in mains}
    hp = next(h for h in picks if h.category_slug == "sluchatka")
    assert hp.product.name.startswith("Sony")
    assert all("kabel" not in h.product.name.lower() for h in picks)
    assert all(not h.product.name.startswith("Modecom") for h in picks)
    assert all(h.badge != "Nejnižší cena" for h in picks)
    assert all(h.best.in_stock is not False and h.best.price is not None for h in picks)
    db.close()


def test_hero_real_discounts_stay_varied_and_honest():
    clear_cache()
    init_db()
    db = SessionLocal()
    _seed(db, name="Lenovo IdeaPad sleva", prices=[20000] * 9 + [14000],
          category_slug="notebooky", category="Notebooky")
    _seed(db, name="Dyson V15 sleva", prices=[15000] * 9 + [11000],
          category_slug="vysavace", category="Vysavače")
    _seed(db, name="Sony WH-1000XM5 sleva", prices=[9000] * 9 + [6000],
          category_slug="sluchatka", category="Sluchátka")
    _seed(db, name="JBL Tune 770NC sleva", prices=[4000] * 9 + [2500],
          category_slug="sluchatka", category="Sluchátka")
    picks = hero_showcase(db, limit=6)
    by_cat = {h.category_slug: h for h in picks}
    assert set(by_cat) == {"notebooky", "vysavace", "sluchatka"}
    for h in picks:
        assert h.badge == "Nejnižší cena" or (h.badge or "").startswith("−")
    assert sum(1 for h in picks if h.category_slug == "sluchatka") == 1
    db.close()


def test_hero_oos_not_buyable_and_accessory_not_filed_as_main():
    clear_cache()
    init_db()
    db = SessionLocal()
    oos = _seed(db, name="Lenovo IdeaPad mimo prodej", prices=[9000],
                category_slug="notebooky", category="Notebooky", in_stock=False)
    ok = _seed(db, name="Lenovo IdeaPad koupit", prices=[12000],
               category_slug="notebooky", category="Notebooky", in_stock=True)
    phone = _seed(db, name="Samsung Galaxy A55", prices=[8000],
                  category_slug="telefony", category="Mobilní telefony", in_stock=True,
                  shops=("mironet", "datart"))
    cheap = Offer(product=phone, shop="alza", url=f"https://www.alza.cz/oos-{phone.id}",
                  price=1000, status="ok", in_stock=False)
    db.add(cheap)
    _seed(db, name="USB-C kabel k telefonu", prices=[199],
          category_slug="telefony", category="Mobilní telefony")
    db.commit()
    picks = hero_showcase(db, limit=6)
    names = {h.product.name for h in picks}
    assert oos.name not in names
    assert "USB-C kabel k telefonu" not in names
    assert ok.name in names
    phone_pick = next(h for h in picks if h.product.id == phone.id)
    assert float(phone_pick.best.price) == 8000
    assert phone_pick.best.in_stock is not False
    assert phone_pick.best.shop != "alza"
    html = TestClient(app).get("/").text
    assert cheap.url not in html
    assert "USB-C kabel" not in html
    assert "Galaxy A55" in html
    assert "Potvrzená cena" in html
    page = TestClient(app).get(f"/produkt/{phone.id}").text
    assert f'href="/go/{phone_pick.best.id}"' in page
    hop = TestClient(app).get(f"/go/{phone_pick.best.id}", follow_redirects=False)
    assert hop.status_code == 302 and hop.headers["location"] == phone_pick.best.url
    assert 'rel="nofollow noopener"' in page
    assert all("kabel" not in n.lower() for n in names)
    db.close()
