"""Monitory z Heureka listu „Sluchátka“ nesmí být sluchátka. Sluchátka s „monitor“ v názvu ano."""
from lovceny.categories import accept_feed_item, classify, is_computer_display
from lovceny.models import PricePoint
from lovceny.shops.feed import FeedItem

HEAD = "Heureka.cz | Elektronika | TV, audio, video | Sluchátka"
TV = "Heureka.cz | Elektronika | TV, audio, video | Televize"
MON = "Heureka.cz | Elektronika | Počítače a notebooky | Monitory"

# Názvy z feedu XM.cz (list Sluchátka) a ze stránky /kategorie/sluchatka.
MONITORS = [
    "Acer Monitor B248YGb (UM QB8EE G02) AcerQB8EE Acer QB8EE UM.QB8EE.G02",
    "LG Monitor 27GX700A-B 27GX700AB (27GX700A-B.AEU) 27GX700A-B.AEU",
    "ASUS Monitor ROG Strix OLED XG27ACDMS (90LM0B60-B01371) 90LM0B60-B01371",
    "ASUS ROG Strix/XG27AQDNG/26,5''/QD-OLED/QHD/360Hz/0,03ms/Černá/3R 90LM0CH0-B01971",
    "LENOVO LCD ThinkVision P34WD-40- 34\",IPS,mat,21:9",
    "Lenovo ThinkVision/P24Q-40/23,8''/IPS/QHD/120Hz/4ms/Černá/3R 64B2GAT1EU",
    "SAMSUNG MT LED LCD Monitor 40\" (G75F) - Prohnutý, VA",
    "Samsung Monitore Odyssey G9 S49FG912EU (LS49FG912EUXEN) LS49FG912EUXEN",
    "#SAMSUNG MT LCD 32\" Samsung Odyssey OLED G8 (G81SF) - 3840x2160, 240Hz, QD-OLED",
    "LG UltraGear/27G810A-B/27''/IPS/4K UHD/180Hz/1ms/Černá/2R 27G810A-B.AEU",
    "AOC MT VA 34\" CU34E4CW - VA panel, 3440x1440, 120Hz, HDMI, DP",
    "DELL LCD E2725HM - 27\"IPS/LED/1920x1080/16:9/100Hz/5ms",
    "Samsung Odyssey G7/LS27FG702EUXEN/27''/IPS/4K UHD/360Hz/1ms/Černá/2R LS27FG702EUXEN",
    "iiyama Monitor ProLite XUB2792UHSU-B6 XUB2792UHSUB6",
    "Samsung ViewFinity S7/LS37D700EAUXEN/37''/VA/4K UHD/60Hz/5ms/Black/3R",
    "Asus Monitor ZenScreen portable MB16FC (90LM0CB1-B01N71)",
    "ASUS ProArt/PA32UCDM/32''/QD-OLED/4K UHD/240Hz",
    "HP LCD 527pu 27\" 2560x1440, IPS w/LED micro-edge,350nic,1500:1,5ms",
    "Philips MT 24\" 24B2U3301D - 1920x1200,IPS,100Hz,1xHDMI,1xDP",
    "Philips Evnia/25M2N3200U/24,5''/IPS/FHD/300Hz/1ms/Šedá/3R 25M2N3200U/00",
]

# Kategorie smí zůstat důkazem, když název sám sluchátka neříká (Jabra, WH-CH).
HEADPHONES = [
    "Apple AirPods Max 2 (2026) barva Blue MHWM4ZM/A",
    "Sluchátka Sony WH-1000XM5 černá",
    "Sluchátka Logitech G k hernímu monitoru",
    "Sony WH-1000XM5 vhodné k monitoru, bezdrátová sluchátka",
    "Marshall Monitor II A.N.C. černá",
    "Shure SE846 in-ear monitor",
    "Herní headset s mikrofonem, vhodný k monitoru",
    "náhlavní souprava Poly Voyager k monitoru",
    "Sony WH-CH520 barva Beige WHCH520C.CE7",
    "Jabra Evolve2 65/Stereo/USB-C/BT/Bezdrát/MS/Černá 26599-999-899",
    "JBL Tune 500 barva Black",
    "Apple EarPods with Remote and Mic - Jack 3.5 mm White",
    "JBL Quantum 360 barva Black",
]


def test_display_names_are_not_headphones():
    for name in MONITORS:
        assert is_computer_display(name), name
        assert accept_feed_item(name, HEAD, 8000, url="https://xm.cz/monitor") is None, name
        assert classify(name, HEAD, url="https://xm.cz/monitor") is None, name


def test_headphone_identity_beats_monitor_mention():
    for name in HEADPHONES:
        assert not is_computer_display(name), name
        assert accept_feed_item(name, HEAD, 1990) == "sluchatka", name


def test_oled_monitor_does_not_fall_through_to_tv():
    name = "ASUS Monitor ROG Strix OLED XG27ACDMS"
    assert classify(name, HEAD) is None
    assert accept_feed_item("Televizor LG OLED55C45 55 palců", TV, 24990) == "televize"
    assert classify("Televizor LG OLED55C45 55 palců", TV) == "televize"
    assert classify('Samsung 43" QLED televize', TV) == "televize"
    assert not is_computer_display('Samsung 43" QLED televize')


def test_monitor_category_still_rejected():
    assert accept_feed_item("ASUS ProArt PA32UCDM", MON, 34146, url="https://xm.cz/asus-proart") is None


def test_seed_does_not_create_display_and_fetch_does_not_restore_category(client_db):
    from lovceny.service import apply_feed
    c, db, make = client_db
    phone = make([14990], name="Apple AirPods Max 2 (2026) barva Blue", shop="xm", category="sluchatka")
    phone.category = "Sluchátka"
    mon = make(
        [15249, 13646],
        name="LENOVO LCD ThinkVision P34WD-40- 34\",IPS",
        shop="xm",
        category="sluchatka",
    )
    mon.category = "Sluchátka"
    mon.image_url = "https://cdn.example/thinkvision.jpg"
    mon.ean = "0196543210987"
    mon_offer = mon.offers[0]
    mon_offer.url = "https://xm.cz/lenovo-lcd-thinkvision-p34wd-40"
    points_before = db.query(PricePoint).filter_by(offer_id=mon_offer.id).count()
    db.commit()

    fresh = FeedItem(
        "m1",
        "LG Monitor 27GX700A-B 27GX700AB",
        "https://xm.cz/lg-monitor-27gx700a-b",
        12490,
        category=HEAD,
        in_stock=True,
    )
    st = apply_feed(db, "xm", [fresh], create_missing=True, max_create=5)
    assert st["created"] == 0
    assert db.query(type(mon)).filter_by(name=fresh.name).count() == 0

    # existující řádek se opraví jen explicitně; fetch cenu dál píše, kategorii nevrací
    from seed import repair_display_headphones
    dry = repair_display_headphones(db, dry_run=True)
    assert dry["candidates"] == 1 and dry["updated"] == 0 and dry["headphones_kept"] == 1
    assert dry["offers_kept"] == 1 and dry["price_points_kept"] == points_before
    db.refresh(mon)
    assert mon.category_slug == "sluchatka"

    applied = repair_display_headphones(db, dry_run=False)
    assert applied["updated"] == 1 and applied["offers_kept"] == 1
    db.refresh(mon)
    db.refresh(phone)
    assert mon.category_slug is None and mon.category is None
    assert mon.retired_at is None
    assert mon.image_url == "https://cdn.example/thinkvision.jpg"
    assert mon.ean == "0196543210987"
    assert db.query(PricePoint).filter_by(offer_id=mon_offer.id).count() == points_before
    assert phone.category_slug == "sluchatka" and phone.category == "Sluchátka"

    again = FeedItem(
        "m2", mon.name, mon_offer.url, 12990, ean=mon.ean, category=HEAD, in_stock=True,
    )
    st2 = apply_feed(db, "xm", [again], create_missing=True)
    db.refresh(mon)
    assert mon.category_slug is None
    assert float(mon_offer.price) == 12990
    html = c.get("/kategorie/sluchatka").text
    assert "ThinkVision" not in html
    assert "AirPods Max" in html


def test_retire_does_not_delete_display_history(client_db, monkeypatch):
    from seed import retire_bad_feed_items
    c, db, make = client_db
    mon = make(
        [8000, 7900],
        name="SAMSUNG MT LED LCD Monitor 40\" (G75F)",
        shop="xm",
        category="sluchatka",
    )
    mon.category = "Sluchátka"
    mon.offers[0].url = "https://xm.cz/samsung-mt-led-lcd-monitor-40"
    filt = make([99], name="Sencor náhradní filtr", shop="sencor", category="vysavace")
    filt.offers[0].url = "https://www.sencor.cz/nahradni-filtr-svxd-0200"
    db.commit()
    mon_id = mon.id
    points = db.query(PricePoint).filter_by(offer_id=mon.offers[0].id).count()

    def fake_load(shop):
        return [FeedItem(
            "x", mon.name, mon.offers[0].url, 7900, category=HEAD, in_stock=True,
        )]

    monkeypatch.setattr("lovceny.shops.feed.load_shop", fake_load)
    st = retire_bad_feed_items(db, dry_run=False)
    db.refresh(mon)
    assert mon.retired_at is None
    assert mon.offers and mon.offers[0].url.endswith("monitor-40")
    assert db.query(PricePoint).filter_by(offer_id=mon.offers[0].id).count() == points
    assert db.get(type(mon), mon_id) is not None
    # příslušenství pořád mizí (URL), displej v důvodech retire není
    assert not any("Monitor 40" in r for r in st["reasons"])
    gone = db.get(type(filt), filt.id)
    assert gone is None or gone.retired_at is not None or not gone.offers
