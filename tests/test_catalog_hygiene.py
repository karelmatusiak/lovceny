"""Párování nabídek, sloučení duplicit, kategorie a práh /slevy."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lovceny.catalog_repair import repair_catalog
from lovceny.categories import classify, is_accessory_text
from lovceny.db import SessionLocal, init_db
from lovceny.main import app
from lovceny.matching import decent_title, same_model
from lovceny.models import Offer, PricePoint, Product, Watch
from lovceny.shops.feed import FeedItem, offer_conflicts_product
from lovceny.service import apply_feed
from lovceny.triage import approve_pending
from lovceny.util import czk

COFFEE = "Elektronika > Audio, video a domácí spotřebiče > Malé domácí spotřebiče > Do kuchyně > Kávovary > Kávovary na espresso"
HEAD = "Elektronika > Audio, video a domácí spotřebiče > Sluchátka > Bezdrátová"


def test_spec_and_brand_conflict_blocks_ean_attach():
    init_db()
    db = SessionLocal()
    host = Product(
        name="Klarstein Bella Café espresso včetně mlýnku 1550 W 20 bar 1,4 litru",
        slug="klarstein-bella",
        ean="4060656524035",
        category_slug="kavovary",
        category="Kávovary",
    )
    db.add(host)
    db.commit()
    aigostar = FeedItem(
        "a1", "Tlakový kávovar Aigostar 1400 W 20 bar 1,7 l",
        "https://allegro.cz/nabidka/tlakovy-kavovar-1400-w-20-bar-1-7-l-ocelovy-aigostar-x",
        1979, ean="4060656524035", category=COFFEE, in_stock=True,
    )
    libeica = FeedItem(
        "l1", "Libeica pákový kávovar 1360 W 20 bar",
        "https://www.electronic-star.cz/libeica-1360w-20bar",
        2438, ean="4060656524035", category=COFFEE, in_stock=True,
    )
    assert offer_conflicts_product(host.name, aigostar.url, aigostar.name)
    assert offer_conflicts_product(host.name, libeica.url, libeica.name)
    st = apply_feed(db, "allegro", [aigostar], create_missing=True, max_create=5)
    st2 = apply_feed(db, "electronicstar", [libeica], create_missing=True, max_create=5)
    assert st["skipped_mismatch"] >= 1 or st["created"] == 1
    assert st2["skipped_mismatch"] >= 1 or st2["created"] == 1
    db.refresh(host)
    assert all("aigostar" not in (o.url or "") for o in host.offers)
    assert all("libeica" not in (o.url or "") for o in host.offers)
    # stejné EAN a stejný model se pořád připojí
    same = FeedItem(
        "k1", "Klarstein Bella Café espresso 1550 W 20 bar 1,4 litru",
        "https://allegro.cz/nabidka/klarstein-bella-cafe-1550-w-20-bar-1-4-l",
        2490, ean="4060656524035", category=COFFEE, in_stock=True,
    )
    st3 = apply_feed(db, "allegro", [same], create_missing=True, max_create=5)
    db.refresh(host)
    assert any(o.url.endswith("klarstein-bella-cafe-1550-w-20-bar-1-4-l") for o in host.offers), st3
    db.close()


def test_repair_detaches_wrong_offer_and_keeps_history():
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    p = Product(
        name="Klarstein Bella Café espresso včetně mlýnku 1550 W 20 bar 1,4 litru",
        slug="klarstein-bella-live", ean="4060656524035",
        category_slug="kavovary", category="Kávovary",
    )
    bad = Offer(
        product=p, shop="allegro", price=1979, in_stock=True, status="ok",
        url="https://allegro.cz/nabidka/tlakovy-kavovar-1400-w-20-bar-1-7-l-ocelovy-aigostar-x",
    )
    ok = Offer(
        product=p, shop="mironet", price=2438, in_stock=True, status="ok",
        url="https://www.mironet.cz/klarstein-bella-cafe-1550",
    )
    db.add(p)
    db.flush()
    db.add(PricePoint(offer=bad, price=1979, captured_at=now, source="feed"))
    db.add(PricePoint(offer=ok, price=2438, captured_at=now, source="scrape"))
    db.commit()
    pid = p.id
    bad_id = bad.id
    st = repair_catalog(db, dry_run=False)
    assert st["offers_detached"] >= 1
    db.expire_all()
    assert db.get(Offer, bad_id).product_id == pid
    approve_pending(db)
    db.expire_all()
    host = db.get(Product, pid)
    assert host.retired_at is None
    assert all(o.id != bad_id for o in host.offers)
    moved = db.get(Offer, bad_id)
    assert moved is not None and moved.product_id != pid
    assert len(moved.points) == 1 and float(moved.points[0].price) == 1979
    again = repair_catalog(db, dry_run=False)
    assert again["offers_detached"] == 0
    db.close()


def test_merge_duplicates_keeps_survivor_url_and_history():
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    a = Product(
        name="Herní sluchátka s mikrofonem Modecom Volcano Prometheus USB LED RGB",
        slug="modecom-a", category_slug="sluchatka", category="Sluchátka",
    )
    b = Product(
        name="Sluchátka s mikrofonem Modecom Volcano Prometheus USB LED RGB",
        slug="modecom-b", category_slug="sluchatka", category="Sluchátka",
    )
    db.add_all([a, b])
    db.flush()
    oa = Offer(product=a, shop="allegro", url="https://allegro.cz/nabidka/modecom-volcano-a",
               price=736, in_stock=True, status="ok")
    ob = Offer(product=b, shop="allegro", url="https://allegro.cz/nabidka/modecom-volcano-b",
               price=714, in_stock=True, status="ok")
    db.add_all([oa, ob])
    db.flush()
    db.add(PricePoint(offer=oa, price=736, captured_at=now - timedelta(days=1), source="feed"))
    db.add(PricePoint(offer=ob, price=714, captured_at=now, source="feed"))
    db.add(Watch(product=b, email="a@b.cz", target_price=700, token="merge-watch-token"))
    db.commit()
    assert same_model(a.name, a.category_slug, b.name, b.category_slug)
    survivor_slug = a.slug
    st = repair_catalog(db, dry_run=False)
    assert st["products_merged"] == 1
    db.expire_all()
    assert db.get(Product, b.id).retired_at is None
    approve_pending(db)
    db.expire_all()
    live = db.get(Product, a.id)
    gone = db.get(Product, b.id)
    assert live.slug == survivor_slug and live.retired_at is None
    assert gone.retired_at is not None and gone.redirect_product_id == live.id
    assert {o.id for o in live.offers} == {oa.id, ob.id}
    assert sum(len(o.points) for o in live.offers) == 2
    assert db.query(Watch).filter_by(token="merge-watch-token").one().product_id == live.id
    c = TestClient(app)
    r = c.get(f"/produkt/{gone.id}-{gone.slug}", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == f"/produkt/{live.id}-{live.slug}"
    # podruhé nic
    assert repair_catalog(db, dry_run=False)["products_merged"] == 0
    # jiné příkony se nesloučí
    hi = Product(name="Klarstein Bella Café 1550 W 1,4 litru", slug="bella-1550",
                 category_slug="kavovary", category="Kávovary")
    lo = Product(name="Klarstein Bella Café 1360 W 1,4 litru", slug="bella-1360",
                 category_slug="kavovary", category="Kávovary")
    db.add_all([hi, lo])
    db.flush()
    db.add(Offer(product=hi, shop="mironet", url="https://www.mironet.cz/bella-1550", price=3000, in_stock=True, status="ok"))
    db.add(Offer(product=lo, shop="mironet", url="https://www.mironet.cz/bella-1360", price=2800, in_stock=True, status="ok"))
    db.commit()
    assert not same_model(hi.name, "kavovary", lo.name, "kavovary")
    assert repair_catalog(db, dry_run=True)["products_merged"] == 0
    db.close()


def test_category_hygiene_controllers_and_surface():
    init_db()
    db = SessionLocal()
    pad = Product(name="Sony DualSense Edge Wireless Controller", slug="dualsense-edge",
                  category_slug="konzole", category="Herní konzole")
    pad2 = Product(name="PS5 DualSense Wireless Controller White", slug="dualsense-white",
                   category_slug="konzole", category="Herní konzole")
    surface = Product(name="Microsoft Surface Pro gen.10 1TB", slug="surface-pro",
                      category_slug="konzole", category="Herní konzole")
    ps5 = Product(name="PlayStation 5 Slim 825GB Digital", slug="ps5-slim-digital",
                  category_slug="konzole", category="Herní konzole")
    ps5b = Product(name="PlayStation 5 Digital Edition verze slim", slug="ps5-digital-edition",
                   category_slug="konzole", category="Herní konzole")
    for p in (pad, pad2, surface, ps5, ps5b):
        db.add(p)
        db.flush()
        db.add(Offer(product=p, shop="mironet", url=f"https://www.mironet.cz/{p.slug}",
                     price=5000, in_stock=True, status="ok"))
    db.commit()
    assert is_accessory_text(pad.name) and is_accessory_text(pad2.name)
    assert not is_accessory_text(ps5.name)
    assert classify(surface.name) == "notebooky"
    assert same_model(ps5.name, "konzole", ps5b.name, "konzole")
    # Ovladač je příslušenství, do výpisu nepatří. Surface má slug konzole, stránka produktu
    # ho tak ukáže, takže ve výpisu konzolí je – dokud úklid slug neopraví.
    html = TestClient(app).get("/kategorie/konzole").text
    assert "DualSense" not in html and "PlayStation 5" in html and "Surface" in html
    st = repair_catalog(db, dry_run=False)
    assert st["accessories_cleared"] >= 2 and st["recategorized"] >= 1 and st["products_merged"] == 1
    approve_pending(db)
    db.expire_all()
    assert db.get(Product, pad.id).category_slug is None
    assert db.get(Product, surface.id).category_slug == "notebooky"
    survivors = [p for p in (db.get(Product, ps5.id), db.get(Product, ps5b.id)) if p.retired_at is None]
    assert len(survivors) == 1
    loser = db.get(Product, ps5b.id if survivors[0].id == ps5.id else ps5.id)
    assert loser.redirect_product_id == survivors[0].id
    nb = TestClient(app).get("/kategorie/notebooky").text
    assert "Surface Pro" in nb
    db.close()


def test_chart_current_matches_hero_not_intraday_min(client_db):
    c, db, make = client_db
    p = make([2438], name="Klarstein Bella Café 1550 W")
    o = p.offers[0]
    now = datetime.now(timezone.utc)
    db.add(PricePoint(offer=o, price=1979, captured_at=now - timedelta(hours=6), source="feed"))
    o.price = 2438
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    assert f"Aktuálně</dt><dd>{czk(2438)}</dd>" in html
    assert f'class="price">{czk(2438)}</span>' in html
    assert f">{czk(2438)}</b>" in html


def test_slevy_skips_thin_history_spam_and_oos():
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    spam = Product(
        name="Chytré Hodinky Sportovní Pánské Hovory Dámské Polské Menu Sport Pomeranč",
        slug="spam-watch", category_slug="chytre-hodinky", category="Chytré hodinky",
    )
    thin = Product(
        name="Modecom Volcano Prometheus",
        slug="modecom-thin", category_slug="sluchatka", category="Sluchátka",
    )
    oos = Product(name="Sony WH-1000XM5 černá", slug="xm5-oos", category_slug="sluchatka")
    real = Product(name="Bosch WAN282ECO pračka", slug="bosch-wan", category_slug="pracky")
    db.add_all([spam, thin, oos, real])
    db.flush()

    def add_offer(prod, price, stock, days, shop="allegro"):
        o = Offer(product=prod, shop=shop, url=f"https://allegro.cz/nabidka/{prod.slug}",
                  price=price, in_stock=stock, status="ok")
        db.add(o)
        db.flush()
        for d in range(days, 0, -1):
            db.add(PricePoint(offer=o, price=price + 400, captured_at=now - timedelta(days=d), source="feed"))
        db.add(PricePoint(offer=o, price=price, captured_at=now, source="feed"))

    add_offer(spam, 824, True, 1)
    add_offer(thin, 714, True, 1)
    add_offer(oos, 5000, False, 30, shop="mironet")
    oos.offers[0].url = "https://www.mironet.cz/xm5-oos"
    add_offer(real, 8000, True, 30, shop="mironet")
    real.offers[0].url = "https://www.mironet.cz/bosch-wan"
    db.commit()
    assert not decent_title(spam.name)
    assert decent_title(thin.name)
    html = TestClient(app).get("/slevy").text
    assert "Polské Menu" not in html and "Pomeranč" not in html
    assert "Volcano Prometheus" not in html
    assert "WH-1000XM5" not in html
    assert "Bosch" in html or "WAN282" in html
    db.close()


def test_listing_keeps_products_classify_mislabels():
    """Produkt, jehož stránka ukazuje kategorii, musí být v jejím výpisu.

    classify() z 6,67\" / 16'' udělá televizi a z „for Xbox“ konzoli. Stránka produktu
    přitom správně říká telefony, notebooky a sluchátka a nabídka je skladem.
    """
    init_db()
    db = SessionLocal()
    rows = [
        ("Motorola EDGE 70 12+512 GB Pantone Gadget Grey / 6.67\" / 512GB / Android 16",
         "telefony", "Mobilní telefony", "edge-70"),
        ("Gigabyte GAMING/A16 PRO DYHG5CZCC4SH/7-240H/16''/2560x1600/32GB/1TB",
         "notebooky", "Notebooky", "a16-pro"),
        ("JBL Quantum 910X Wireless for Xbox barva Black",
         "sluchatka", "Sluchátka", "quantum-910x"),
    ]
    client = TestClient(app)
    for name, slug, label, url_slug in rows:
        assert classify(name) != slug
        p = Product(name=name, slug=url_slug, category_slug=slug, category=label)
        db.add(p)
        db.flush()
        db.add(Offer(
            product=p, shop="mironet", url=f"https://www.mironet.cz/{url_slug}",
            price=9990, in_stock=True, status="ok",
        ))
    accessory = Product(name="Sony DualSense Edge Wireless Controller", slug="dualsense-vypis",
                        category_slug="telefony", category="Mobilní telefony")
    retired = Product(name="Apple iPhone 15 128GB vyřazený", slug="iphone-vyrazeny",
                      category_slug="telefony", category="Mobilní telefony",
                      retired_at=datetime.now(timezone.utc))
    db.add_all([accessory, retired])
    db.flush()
    db.add(Offer(product=accessory, shop="mironet", url="https://www.mironet.cz/dualsense-vypis",
                 price=1800, in_stock=True, status="ok"))
    db.add(Offer(product=retired, shop="mironet", url="https://www.mironet.cz/iphone-vyrazeny",
                 price=15000, in_stock=True, status="ok"))
    db.commit()
    phones = client.get("/kategorie/telefony").text
    assert "EDGE 70" in phones and "DualSense" not in phones and "vyřazený" not in phones
    assert "A16 PRO" in client.get("/kategorie/notebooky").text
    assert "Quantum 910X" in client.get("/kategorie/sluchatka").text
    page = client.get("/produkt/" + str(db.query(Product).filter(Product.slug == "edge-70").one().id)).text
    assert "Mobilní telefony" in page and "Koupit za" in page
    assert "EDGE 70" in client.get("/hledat", params={"q": "Motorola EDGE 70"}).text
    db.close()


def test_recat_does_not_guess_tv_or_console():
    """6.67\" a 16'' nejsou úhlopříčka televize. Xbox v názvu sluchátek není konzole."""
    init_db()
    db = SessionLocal()
    rows = [
        ("Motorola EDGE 70 12+512 GB Pantone Gadget Grey / 6.67\" / 512GB / Android 16",
         "telefony", "edge"),
        ("Gigabyte GAMING/A16 PRO DYHG5CZCC4SH/7-240H/16''/2560x1600/32GB/1TB",
         "notebooky", "a16"),
        ("JBL Quantum 910X Wireless for Xbox barva Black", "sluchatka", "quantum"),
    ]
    made = []
    for name, cat, slug in rows:
        p = Product(name=name, slug=slug, category_slug="televize" if cat != "sluchatka" else "konzole",
                    category="špatně")
        db.add(p)
        db.flush()
        db.add(Offer(product=p, shop="mironet", url=f"https://www.mironet.cz/{slug}-ok",
                     price=10000, in_stock=True, status="ok"))
        made.append((p, cat))
    db.commit()
    # špatná kategorie se vrátí; správná se dalším během nemění
    repair_catalog(db, dry_run=False)
    approve_pending(db)
    from lovceny.one_off_fixes import apply_legacy_product_fixes
    apply_legacy_product_fixes(db, apply=True)
    db.expire_all()
    for p, cat in made:
        fresh = db.get(Product, p.id)
        assert fresh.category_slug == cat, (fresh.name, fresh.category_slug)
        assert fresh.slug == p.slug
    again = repair_catalog(db, dry_run=True)
    assert again["recategorized"] == 0
    db.close()


def test_surface_offer_comes_back_and_dualsense_redirects():
    init_db()
    db = SessionLocal()
    surface = Product(
        name="Microsoft Surface Pro gen.10 1TB (i7/16GB) PLATINUM",
        slug="surface-pro-gen-10", category_slug="notebooky", category="Notebooky",
        retired_at=datetime.now(timezone.utc),
    )
    pad = Product(
        name="Sony DualSense Edge Wireless Controller | Prémiový Ovladač pro PS5",
        slug="dualsense-edge", category_slug=None, category=None,
        retired_at=datetime.now(timezone.utc),
    )
    db.add_all([surface, pad])
    db.flush()
    shell = Product(name="microsoft xbox series s 1tb", slug="microsoft-xbox-series-s-1tb",
                    category_slug="konzole", category="Herní konzole")
    offer = Offer(product=shell, shop="svetpocitacu", price=76180, in_stock=False, status="ok",
                  url="https://www.svetpocitacu.cz/microsoft-xbox-series-s-1tb")
    db.add(shell)
    db.flush()
    db.add(PricePoint(offer=offer, price=76180, captured_at=datetime.now(timezone.utc), source="scrape"))
    # slug obchodu se nesmí znovu odtrhnout od telefonu (8128gb = 8/128 GB)
    phone = Product(
        name="Samsung Galaxy A55 5G 8/128GB modrá",
        slug="galaxy-a55", category_slug="telefony", category="Mobilní telefony",
    )
    db.add(phone)
    db.flush()
    db.add(Offer(
        product=phone, shop="mironet", price=8000, in_stock=True, status="ok",
        url="https://www.mironet.cz/samsung-galaxy-a55-5g-8128gb-modro-cerna-66-android-14-dp652173",
    ))
    db.commit()
    phone_slug = phone.slug
    st = repair_catalog(db, dry_run=False)
    approve_pending(db)
    from lovceny.one_off_fixes import apply_legacy_product_fixes
    apply_legacy_product_fixes(db, apply=True)
    db.expire_all()
    host = db.get(Product, surface.id)
    assert host.retired_at is None
    assert host.category_slug == "notebooky"
    assert host.slug == "surface-pro-gen-10"
    assert any(o.url.endswith("microsoft-xbox-series-s-1tb") for o in host.offers)
    assert len(host.offers[0].points) == 1
    gone = db.get(Product, shell.id)
    assert gone.retired_at is not None and gone.redirect_product_id == host.id
    pad_fresh = db.get(Product, pad.id)
    assert pad_fresh.retired_at is not None and pad_fresh.category_slug == "konzole"
    c = TestClient(app)
    r = c.get(f"/produkt/{pad.id}-x", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/kategorie/konzole"
    phone_fresh = db.get(Product, phone.id)
    assert phone_fresh.retired_at is None and phone_fresh.slug == phone_slug
    assert phone_fresh.offers and "8128gb" in phone_fresh.offers[0].url
    assert st["offers_detached"] == 0
    db.close()


def test_ps5_slim_digital_bundle_merges():
    init_db()
    db = SessionLocal()
    a = Product(name="PlayStation 5 Digital Edition (verze slim)", slug="ps5-72",
                category_slug="konzole", category="Herní konzole")
    b = Product(name="PlayStation 5 Slim 825GB Digital + Astro’s Playroom | Bez Mechaniky",
                slug="ps5-139", category_slug="konzole", category="Herní konzole")
    db.add_all([a, b])
    db.flush()
    db.add(Offer(product=a, shop="abcom", url="https://www.ab-com.cz/ps5-digital-slim",
                 price=13834, in_stock=True, status="ok"))
    db.add(Offer(product=b, shop="ellex", url="https://www.ellex.cz/ps5-slim-astro",
                 price=14790, in_stock=True, status="ok"))
    db.commit()
    assert same_model(a.name, "konzole", b.name, "konzole")
    st = repair_catalog(db, dry_run=False)
    assert st["products_merged"] == 1
    approve_pending(db)
    db.expire_all()
    live = [p for p in (db.get(Product, a.id), db.get(Product, b.id)) if p.retired_at is None]
    assert len(live) == 1 and live[0].slug in ("ps5-72", "ps5-139")
    db.close()


def test_repair_lock_skips_second_run():
    from lovceny import catalog_repair as cr
    init_db()
    db = SessionLocal()
    assert cr._LOCK.acquire(blocking=False)
    try:
        st = cr.repair_catalog_locked(db, dry_run=True)
        assert st.get("skipped") == "locked"
    finally:
        cr._LOCK.release()
    db.close()


def test_startup_does_not_run_catalog_repair():
    import inspect
    import fetch_prices
    import lovceny.main as m
    assert "repair_catalog" not in inspect.getsource(m._startup)
    assert "start_catalog_repair" not in inspect.getsource(m._startup)
    assert "repair_catalog" not in inspect.getsource(fetch_prices.main)


def test_offer_label_conflict_alias():
    # sanity: URL slug bez mezer pořád vidí 1,7 l a značku
    assert offer_conflicts_product(
        "Klarstein Bella Café 1550 W 1,4 litru",
        "https://allegro.cz/nabidka/tlakovy-kavovar-1400-w-20-bar-1-7-l-aigostar",
        None,
    )
