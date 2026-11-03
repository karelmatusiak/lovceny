"""eHUB affiliate wrapping + Heureka feed matching for iStyle/Sencor/XM.cz."""
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, Product
from lovceny.service import apply_feed
from lovceny.shops import feed, get_shop
from lovceny.shops.ehub import ehub_aid, ehub_click_url
from lovceny.util import slugify

FIXTURE = Path(__file__).parent / "fixtures" / "ehub_heureka_mini.xml"


def test_ehub_aid_default_and_empty(monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    assert ehub_aid() == "667205fc"
    monkeypatch.setenv("EHUB_AID", "")
    assert ehub_aid() == ""
    monkeypatch.setenv("EHUB_AID", "  abc  ")
    assert ehub_aid() == "abc"


def test_ehub_click_url_encoding(monkeypatch):
    monkeypatch.setenv("EHUB_AID", "667205fc")
    dest = "https://istyle.cz/products/iphone?x=1&y=2"
    u = ehub_click_url("92ecd9a4", dest)
    p = urlparse(u)
    assert p.netloc == "ehub.cz"
    assert p.path.endswith("click.php")
    q = parse_qs(p.query)
    assert q["a_aid"] == ["667205fc"]
    assert q["a_bid"] == ["92ecd9a4"]
    assert unquote(q["desturl"][0]) == dest


def test_shop_affiliate_link_ehub(monkeypatch):
    monkeypatch.setenv("EHUB_AID", "667205fc")
    istyle = get_shop("istyle")
    sencor = get_shop("sencor")
    smarty = get_shop("smarty")
    xm = get_shop("xm")
    url = "https://istyle.cz/products/iphone-15"
    wrapped = istyle.affiliate_link(url)
    assert "ehub.cz" in wrapped and "a_bid=92ecd9a4" in wrapped and "desturl=" in wrapped
    assert "a_bid=67b5afc3" in sencor.affiliate_link("https://www.sencor.cz/x")
    assert "a_bid=1651aa06" in smarty.affiliate_link("https://www.smarty.cz/x")
    xm_u = "https://xm.cz/apple-iphone-15_z53756/?id_variation=49121"
    assert "a_bid=0ff74973" in xm.affiliate_link(xm_u)
    assert "desturl=" in xm.affiliate_link(xm_u)
    monkeypatch.setenv("EHUB_AID", "")
    assert istyle.affiliate_link(url) == url
    assert xm.affiliate_link(xm_u) == xm_u
    assert not istyle.affiliate_active


def test_status_labels_ehub_shops(monkeypatch):
    monkeypatch.delenv("ISTYLE_FEED_URL", raising=False)
    monkeypatch.delenv("SENCOR_FEED_URL", raising=False)
    monkeypatch.delenv("XM_FEED_URL", raising=False)
    st, label = get_shop("istyle").status_label()
    assert st == "ok" and "feed" in label
    st, label = get_shop("sencor").status_label()
    assert st == "ok" and "feed" in label
    st, label = get_shop("xm").status_label()
    assert st == "ok" and "feed" in label
    st, label = get_shop("smarty").status_label()
    assert st == "ok" and "affiliate" in label.lower()


def test_feed_url_empty_env_falls_back_to_default(monkeypatch):
    """GitHub Actions injects empty string for missing secrets; must not hide defaults."""
    monkeypatch.setenv("ISTYLE_FEED_URL", "")
    monkeypatch.setenv("SENCOR_FEED_URL", "")
    monkeypatch.setenv("XM_FEED_URL", "")
    assert get_shop("istyle").feed_url.startswith("https://")
    assert get_shop("sencor").feed_url.startswith("https://")
    assert get_shop("xm").feed_url.startswith("https://xm.cz/")
    monkeypatch.setenv("ISTYLE_FEED_URL", "https://example.test/istyle.xml")
    assert get_shop("istyle").feed_url == "https://example.test/istyle.xml"
    monkeypatch.setenv("XM_FEED_URL", "https://example.test/xm.xml")
    assert get_shop("xm").feed_url == "https://example.test/xm.xml"


def test_parse_stream_and_strip_tracking():
    items = feed.load(str(FIXTURE))
    assert len(items) == 2
    assert items[0].item_id == "IS1" and items[0].price == 19990
    assert "utm_source" not in items[0].url
    assert items[0].url.startswith("https://istyle.cz/")
    assert items[1].ean == "8590669123456" and items[1].in_stock is True


def test_names_match_conservative():
    assert feed.names_match("Sencor SVC 7811WH", "Sencor SVC 7811WH bílý")
    assert not feed.names_match("Sencor", "Sencor SVC 7811WH")
    assert not feed.names_match("Apple iPhone 15", "Apple iPhone 14")
    # CZ ↔ EN barva + kapacita / varianta
    assert feed.names_match("Apple iPhone 15 128GB - černý", "Apple iPhone 15 128GB Black")
    assert feed.names_match("Apple iPhone 15 128GB černá", "Apple iPhone 15 128GB Black")
    assert not feed.names_match("Apple iPhone 15 128GB Black", "Apple iPhone 15 Plus 128GB Black")
    assert not feed.names_match("Apple iPhone 15 128GB Black", "Apple iPhone 15 256GB Black")
    assert not feed.names_match("Apple iPhone 15 128GB Black", "Apple iPhone 15 128GB White")
    assert feed.names_match("Sencor SBL 7170", "Sencor SBL7170")


def test_apply_feed_attach_by_ean_and_create():
    init_db()
    db = SessionLocal()
    # existující produkt s EAN z iStyle feedu, bez istyle nabídky
    p = Product(name="Apple iPhone 15 128GB černá", slug=slugify("iphone-ehub"), ean="0195949035838")
    db.add(p)
    db.commit()
    items = feed.load(str(FIXTURE))
    st = apply_feed(db, "istyle", [items[0]])
    assert st["attached"] == 1 and st["matched"] == 0
    o = db.query(Offer).filter(Offer.shop == "istyle").one()
    assert float(o.price) == 19990 and o.product_id == p.id
    # druhé volání = match URL
    st2 = apply_feed(db, "istyle", [items[0]])
    assert st2["matched"] == 1 and st2["attached"] == 0
    # Sencor create_missing
    st3 = apply_feed(db, "sencor", [items[1]], create_missing=True, max_create=5)
    assert st3["created"] == 1
    assert db.query(Product).filter(Product.ean == "8590669123456").count() == 1
    db.close()


def test_obchody_shows_ehub_partners(client_db):
    c, db, make = client_db
    html = c.get("/obchody").text
    assert "iStyle" in html and "Sencor" in html and "Smarty" in html and "XM.cz" in html
    assert "oficiální feed" in html
    assert "affiliate odkazy" in html  # Smarty zdroj


def test_apply_feed_attach_by_name_color():
    """Feed s EAN připojí nabídku k existujícímu produktu bez EAN podle názvu (černý↔Black)."""
    from lovceny.db import Base, engine
    init_db()
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    p = Product(name="Apple iPhone 15 128GB Black", slug=slugify("iphone-15-black-color-match-x"))
    db.add(Offer(product=p, shop="mironet", url="https://www.mironet.cz/iphone-15-black-color-match-x",
                 status="ok", price=17990, in_stock=True))
    db.commit()
    pid = p.id
    item = feed.FeedItem(
        item_id="COLOR1",
        name="Apple iPhone 15 128GB - černý",
        url="https://istyle.cz/products/iphone-15-color-match-x",
        price=16490,
        ean="9990001112223",  # v katalogu není
        category="Heureka.cz | Elektronika | Mobilní telefony",
        in_stock=True,
    )
    st = apply_feed(db, "istyle", [item])
    assert st["attached"] == 1, st
    assert db.query(Offer).filter(Offer.shop == "istyle", Offer.product_id == pid).count() == 1
    # create_missing True nesmí vytvořit duplicitu – URL/název už sedí
    st2 = apply_feed(db, "istyle", [item], create_missing=True, max_create=5)
    assert st2["matched"] == 1 and st2["created"] == 0, st2
    assert db.query(Product).count() == 1
    db.refresh(p)
    assert p.category is None or "|" not in (p.category or "")
    db.close()


def test_product_page_clean_category_and_buy_label(client_db, monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    c, db, make = client_db
    p = make([16490], name="Apple iPhone 15 128GB Black", shop="mironet", category="telefony")
    p.category = "Heureka.cz | Elektronika | Mobilní telefony"
    p.ean = "195949036033"
    db.add(Offer(product=p, shop="istyle", url="https://istyle.cz/products/iphone-15-demo",
                 status="ok", price=16490, in_stock=True))
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    # viditelný podtitul i JSON-LD berou čistý název kategorie, ne Heureka path
    assert "Heureka.cz |" not in html
    assert "Mobilní telefony" in html
    assert "Koupit za" in html and ("· iStyle" in html or "· Mironet" in html)
    assert "v obchodě" not in html.split("btn-buy")[1][:120]


def test_names_match_rejects_shared_partial_mpn():
    """Filtr SVXD … SVC 3001 nesmí matchnout vysavač SVC 3001 Orca (dřív průnik MPN)."""
    assert not feed.names_match("Sencor SVC 3001 Orca", "Sencor SVXD 0200for SVC 3001")
    assert not feed.names_match(
        "MacBook Pro 16,2\", Apple M5 Pro 18jádrové CPU, 20jádrové GPU, 48GB, 1TB SSD, stříbrný",
        "Ochranný kryt pro MacBook Pro 16\" Epico Slim Shell - matný průhledný",
    )
    assert feed.names_match("Sencor SVC 7811WH", "Sencor SVC 7811WH bílý")



def test_names_match_rejects_cross_line_and_chip():
    """Slabé MPN (pro2/air15) nesmí spárovat AirPods↔iPhone ani M3↔M5."""
    assert not feed.names_match("Apple AirPods Pro 2", "Apple iPhone 18 Pro 256GB")
    assert not feed.names_match("Apple MacBook Air 15 M3", "Apple MacBook Air 15 M5")
    assert feed.names_match("Apple MacBook Air 15 M3", "Apple MacBook Air 15 M3 2024")
    assert not feed.names_match("LG OLED55B65LA", "ASUS PA278QV 27\" monitor")
    assert not feed.names_match("LG OLED55B65LA televize", "ASUS PA278QV monitor")


def test_apply_feed_detaches_mismatched_ehub_url(client_db):
    """Špatný desturl na nabídce (AirPods→iPhone) se při feedu odpojí / přesune."""
    from lovceny.shops.feed import FeedItem
    from lovceny.service import apply_feed
    c, db, make = client_db
    air = make([4990], name="Apple AirPods Pro 2", shop="istyle", category="sluchatka")
    o = air.offers[0]
    o.url = "https://www.istyle.cz/apple-iphone-18-pro-256gb.html"
    o.external_id = "bad-airpods"
    phone = make([29990], name="Apple iPhone 18 Pro 256GB", shop="alza", category="telefony")
    db.commit()
    items = [FeedItem(
        "iphone-feed", "Apple iPhone 18 Pro 256GB",
        "https://www.istyle.cz/apple-iphone-18-pro-256gb.html", 29990,
        category="Mobily", in_stock=True,
    )]
    st = apply_feed(db, "istyle", items, create_missing=False)
    assert st["detached"] + st["reattached"] >= 1, st
    db.refresh(air)
    # AirPods už nemá istyle URL na iPhone
    urls = [x.url for x in air.offers]
    assert not any("iphone-18-pro" in u for u in urls), urls


def test_accept_feed_item_orca_and_filter():
    from lovceny.categories import accept_feed_item, is_accessory_text
    vac_cat = "Heureka.cz | Bílé zboží | Malé spotřebiče | Vysavače"
    acc_cat = "Heureka.cz | Bílé zboží | Malé spotřebiče | Příslušenství k malým spotřebičům"
    assert accept_feed_item("Sencor SVC 3001 Orca", vac_cat, 3399) == "vysavace"
    assert accept_feed_item("Sencor SVC 3001 Orca", vac_cat, 99) is None  # pod min. cenou
    assert is_accessory_text("Sencor SVXD 0200for SVC 3001", acc_cat)
    assert accept_feed_item("Sencor SVXD 0200for SVC 3001", acc_cat, 99) is None
    assert accept_feed_item("Sencor SVC 190W", vac_cat, 499) is None  # < 500


def test_orca_filter_never_attaches():
    """Regrese: filtr pro Orca se nesmí připojit k vysavači ani přepsat cenu na 99 Kč."""
    from lovceny.db import Base, engine
    init_db()
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    vac = feed.FeedItem(
        item_id="ORCA1",
        name="Sencor SVC 3001 Orca",
        url="https://www.sencor.cz/vysavac-viceucelovy-svc-3001-orca",
        price=3399,
        ean="8590669090037",
        category="Heureka.cz | Bílé zboží | Malé spotřebiče | Vysavače",
        in_stock=True,
    )
    filtr = feed.FeedItem(
        item_id="FIL1",
        name="Sencor SVXD 0200for SVC 3001",
        url="https://www.sencor.cz/nahradni-filtr-svxd-0200",
        price=99,
        ean="8590669365340",
        category="Heureka.cz | Bílé zboží | Malé spotřebiče | Příslušenství k malým spotřebičům",
        in_stock=True,
    )
    st = apply_feed(db, "sencor", [vac], create_missing=True, max_create=5)
    assert st["created"] == 1, st
    p = db.query(Product).one()
    assert float(p.offers[0].price) == 3399
    st2 = apply_feed(db, "sencor", [filtr, vac], create_missing=True, max_create=5)
    assert st2["skipped_accessory"] >= 1, st2
    assert st2["created"] == 0
    db.refresh(p)
    o = p.offers[0]
    assert "filtr" not in o.url
    assert float(o.price) == 3399
    assert db.query(Product).count() == 1
    db.close()


def test_retire_bad_feed_items_removes_filter_offer(client_db):
    from seed import retire_bad_feed_items
    c, db, make = client_db
    p = make([99], name="Sencor SVC 3001 Orca", shop="sencor", category="vysavace")
    o = p.offers[0]
    o.url = "https://www.sencor.cz/nahradni-filtr-svxd-0200"
    db.commit()
    pid = p.id
    st = retire_bad_feed_items(db, dry_run=False)
    assert st["offers"] >= 1
    gone = db.get(Product, pid)
    # soft-retire: řádek zůstane bez nabídek, nebo je retired
    assert gone is None or (not gone.offers and gone.retired_at is not None) or not gone.offers
    if gone is not None and not gone.offers:
        assert gone.retired_at is not None


def test_scw_window_cleaner_rejected():
    """Čistič oken SCW nesmí do vysavačů – ani když feed CATEGORYTEXT lže."""
    from lovceny.categories import accept_feed_item, category_url_conflict, classify
    rob = "Heureka.cz | Bílé zboží | Malé spotřebiče | Robotické vysavače"
    url = "https://www.sencor.cz/cistic-oken-scw-3001yl"
    assert category_url_conflict("vysavace", "Sencor SCW 3001YL", url)
    assert accept_feed_item("Sencor SCW 3001YL", rob, 1099, url=url) is None
    assert classify("Sencor SCW 3001YL", rob, url=url) is None
    srw = "https://www.sencor.cz/roboticky-cistic-oken-srw-6010wh-aquino"
    assert accept_feed_item("Sencor SRW 6010WH Aquino", rob, 4999, url=srw) is None


def test_variant_family_key_collapses_colors():
    from lovceny.shops.feed import variant_family_key as vfk
    assert vfk("Sencor SVC 45RD-EUE3") == vfk("Sencor SVC 45BK-EUE3")
    assert vfk("Sencor SVC 730GR-EUE2") == vfk("Sencor SVC 730RD-EUE2")
    assert vfk("Sencor SVC 510GR-EUE2") != vfk("Sencor SVC 511TQ-EUE2")
    assert vfk("Apple iPhone 15 128GB Black") == vfk("Apple iPhone 15 128GB White")
    assert vfk("Apple iPhone 15 128GB Black") != vfk("Apple iPhone 15 256GB Black")


def test_pick_feed_create_items_caps_and_collapses():
    from lovceny.shops.feed import FeedItem
    from seed import pick_feed_create_items, FEED_MAX_PER_BRAND_CAT
    vac = "Heureka.cz | Bílé zboží | Malé spotřebiče | Vysavače"
    items = [
        FeedItem("1", "Sencor SVC 45RD-EUE3", "https://sencor.cz/vysavac-svc-45rd", 899, category=vac, in_stock=True),
        FeedItem("2", "Sencor SVC 45BK-EUE3", "https://sencor.cz/vysavac-svc-45bk", 849, category=vac, in_stock=True),
        FeedItem("3", "Sencor SVC 45WH-EUE3", "https://sencor.cz/vysavac-svc-45wh", 879, category=vac, in_stock=True),
        FeedItem("4", "Sencor SCW 3001YL", "https://sencor.cz/cistic-oken-scw-3001yl", 1099,
                 category="Heureka.cz | Bílé zboží | Malé spotřebiče | Robotické vysavače", in_stock=True),
    ]
    # 4 další různé modely (dohromady s 45* = 5 rodin < cap) + 3 drahé navíc → cap
    for i, price in enumerate([1999, 2499, 2999, 3499], start=5):
        items.append(FeedItem(
            str(i), f"Sencor SVC {1000+i}BK", f"https://sencor.cz/vysavac-svc-{1000+i}bk",
            float(price), category=vac, in_stock=True,
        ))
    picked = pick_feed_create_items(items, ("vysavace",))
    names = [p.name for p in picked]
    assert "Sencor SCW 3001YL" not in names
    assert sum(1 for n in names if "SVC 45" in n) == 1
    assert any("45BK" in n for n in names)  # nejlevnější skladem z barev
    assert len(picked) == 5  # pod capem

    # přes cap: přidej další drahé rodiny → zůstane max FEED_MAX_PER_BRAND_CAT a bez nejlevnějších
    for i, price in enumerate([4999, 5499, 5999, 6499], start=20):
        items.append(FeedItem(
            str(i), f"Sencor SVC {2000+i}TI", f"https://sencor.cz/vysavac-svc-{2000+i}ti",
            float(price), category=vac, in_stock=True,
        ))
    picked2 = pick_feed_create_items(items, ("vysavace",))
    assert len(picked2) == FEED_MAX_PER_BRAND_CAT
    assert min(float(p.price) for p in picked2) >= 2499


def test_xm_shop_registry_and_click_wrapper(monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    monkeypatch.delenv("XM_FEED_URL", raising=False)
    xm = get_shop("xm")
    assert xm is not None and xm.name == "XM.cz" and xm.ehub_bid == "0ff74973"
    assert xm.source == "feed" and xm.status == "ok"
    assert "xm.cz" in xm.domains
    assert xm.feed_url.startswith("https://xm.cz/feed/")
    dest = "https://xm.cz/apple-iphone-15_z53756/?id_variation=49121"
    wrapped = xm.affiliate_link(dest)
    assert wrapped.startswith("https://ehub.cz/system/scripts/click.php?")
    q = parse_qs(urlparse(wrapped).query)
    assert q["a_aid"] == ["667205fc"]
    assert q["a_bid"] == ["0ff74973"]
    assert unquote(q["desturl"][0]) == dest


def test_apply_feed_xm_attach_by_ean():
    init_db()
    from lovceny.db import Base, engine
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    p = Product(name="Apple iPhone 15 128GB Blue", slug=slugify("iphone-15-xm-ean"), ean="0195949036576")
    db.add(Offer(product=p, shop="mironet", url="https://www.mironet.cz/iphone-15-xm-ean",
                 status="ok", price=17990, in_stock=True))
    db.commit()
    item = feed.FeedItem(
        item_id="53756_49121",
        name="Apple iPhone 15 barva Blue paměť 128 GB",
        url="https://xm.cz/apple-iphone-15_z53756/?id_variation=49121",
        price=17483,
        ean="0195949036576",
        category="Heureka.cz | Elektronika | Mobily, tablety, nositelná elektronika | Mobilní telefony",
        in_stock=True,
    )
    st = apply_feed(db, "xm", [item])
    assert st["attached"] == 1, st
    o = db.query(Offer).filter(Offer.shop == "xm").one()
    assert float(o.price) == 17483 and o.product_id == p.id
    assert o.in_stock is True
    # accessory must never attach / create
    acc = feed.FeedItem(
        item_id="461_1987",
        name="Originální Xiaomi Redmi 4 16 GB flipcase barva Černá",
        url="https://xm.cz/originalni-xiaomi-redmi-4-16-gb-flipcase_z461/?id_variation=1987",
        price=319,
        ean="8595633310876",
        category="Heureka.cz | Elektronika | Mobily, tablety, nositelná elektronika | Příslušenství k mobilům | Pouzdra, skla, fólie | Pouzdra a kryty na mobilní telefony",
        in_stock=True,
    )
    st2 = apply_feed(db, "xm", [acc], create_missing=True, max_create=5)
    assert st2["skipped_accessory"] >= 1, st2
    assert st2["created"] == 0
    assert db.query(Offer).filter(Offer.shop == "xm").count() == 1
    db.close()


def test_xm_product_page_ehub_rel(client_db, monkeypatch):
    monkeypatch.delenv("EHUB_AID", raising=False)
    c, db, make = client_db
    p = make([17483], name="Apple iPhone 15 128GB Blue XM page", shop="xm", category="telefony")
    o = p.offers[0]
    # unique URL (offers.url is globally unique; prior tests may reuse XM sample URLs)
    o.url = "https://xm.cz/apple-iphone-15-page-test_z99901/?id_variation=99901"
    o.in_stock = True
    db.commit()
    html = c.get(f"/produkt/{p.id}").text
    assert "XM.cz" in html
    assert "ehub.cz/system/scripts/click.php" in html
    assert "a_bid=0ff74973" in html
    assert "nofollow sponsored noopener" in html


def test_product_line_oled_monitor_vs_tv():
    """QD-OLED ProArt je monitor, OLED55… je TV – hard conflict / offer_conflicts."""
    tv = "55\" LG OLED55B65LA černá televize"
    mon = "ASUS ProArt/PA32UCDM/32''/QD-OLED/4K UHD/240Hz"
    assert feed._product_line(feed.canonicalize_name(tv)) == "tv"
    assert feed._product_line(feed.canonicalize_name(mon)) == "monitor"
    assert feed.offer_conflicts_product(tv, "https://xm.cz/asus-proart-pa32ucdm-32-qd-oled_z1", mon)
    assert not feed.names_match(tv, mon)


def test_feed_category_slug_rejects_pc_parent_components():
    from lovceny.categories import feed_category_slug, accept_feed_item
    mon = "Heureka.cz | Elektronika | Počítače a notebooky | Monitory"
    gpu = "Heureka.cz | Elektronika | Počítače a notebooky | Grafické karty"
    nb = "Heureka.cz | Elektronika | Počítače a notebooky | Notebooky"
    assert feed_category_slug(mon) is None
    assert feed_category_slug(gpu) is None
    assert feed_category_slug(nb) == "notebooky"
    assert accept_feed_item("ASUS ProArt PA32UCDM", mon, 34146, url="https://xm.cz/asus-proart") is None
    assert accept_feed_item("Lenovo ThinkPad X1", nb, 35000, url="https://xm.cz/thinkpad") == "notebooky"


def test_apply_feed_skips_ean_mismatch_tv_monitor(client_db):
    """EAN shoda nestačí, když feed položka je jasně jiný produkt (TV↔monitor)."""
    from lovceny.shops.feed import FeedItem
    from lovceny.service import apply_feed
    from lovceny.models import Offer
    c, db, make = client_db
    tv = make([20637], name="55\" LG OLED55B65LA černá", shop="mironet", category="televize")
    tv.ean = "4711387564714"  # EAN z ASUS monitoru – otrávená data
    bad = Offer(
        product=tv, shop="xm",
        url="https://xm.cz/asus-proart-pa32ucdm-32-qd-oled-4k_z131267",
        external_id="asus1", price=34146, status="ok", in_stock=True,
    )
    db.add(bad)
    db.commit()
    items = [FeedItem(
        "asus1", "ASUS ProArt/PA32UCDM/32''/QD-OLED/4K UHD/240Hz",
        "https://xm.cz/asus-proart-pa32ucdm-32-qd-oled-4k_z131267", 34146,
        ean="4711387564714",
        category="Heureka.cz | Elektronika | Počítače a notebooky | Monitory",
        in_stock=True,
    )]
    st = apply_feed(db, "xm", items, create_missing=False)
    db.refresh(tv)
    urls = [o.url for o in tv.offers if o.shop == "xm"]
    assert not urls, (urls, st)
    assert st.get("detached", 0) >= 1
    assert tv.ean is None  # otrávené EAN pryč
