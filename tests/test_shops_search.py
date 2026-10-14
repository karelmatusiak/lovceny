from pathlib import Path

import pytest

from lovceny.db import SessionLocal, init_db
from lovceny.service import offers_for_query
from lovceny.shops import SHOPS, search, shop_for_url
from lovceny.shops.search import (SearchHit, _price, live_search, parse_abcom, parse_softcom,
                                  parse_softcom_detail, parse_svetpocitacu)

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("raw,val", [("14&nbsp;509&nbsp;Kč", 14509), ("1 290.00 <em>Kč </em>", 1290),
                                     ("464.60 Kč", 464.6), ("1.290,50 Kč", 1290.5), ("16 784,- Kč", 16784),
                                     ("999 Kč", 999), ("bez ceny", None)])
def test_price_parsing(raw, val):
    assert _price(raw) == val


@pytest.mark.parametrize("fn,fix,shop,domain", [(parse_softcom, "softcom_garmin", "softcom", "softcom.cz"),
                                               (parse_svetpocitacu, "svetpocitacu_garmin", "svetpocitacu", "svetpocitacu.cz"),
                                               (parse_abcom, "abcom_garmin", "abcom", "ab-com.cz")])
def test_shop_parsers(fn, fix, shop, domain):
    hits = fn((FIX / f"{fix}.html").read_text("utf8"))
    assert len(hits) >= 3
    for h in hits:
        assert h.shop == shop and "garmin" in h.name.lower() and h.price > 50
        assert domain in h.url and h.image
        assert shop_for_url(h.url).slug == shop  # Hlídat → add_by_url pozná obchod
    assert SHOPS[shop].search_note


def test_softcom_detail_price():
    r = parse_softcom_detail((FIX / "softcom_detail.html").read_text("utf8"))
    assert r.price == 1290.0 and "Garmin" in r.name


def test_sort_by_current_price_not_original(monkeypatch):
    """„Rozbaleno“ za 5 999 (dříve 7 939) musí být za 5 581 a před 6 018 – řadí se jen aktuální cena."""
    init_db()
    db = SessionLocal()
    search.clear_cache()
    hits = [SearchHit("mironet", "Garmin DriveSmart 76", 6018, "https://www.mironet.cz/a+dp1/", original_price=23548),
            SearchHit("mironet", "Rozbaleno Garmin Instinct 2S", 5999, "https://www.mironet.cz/b+dp2/", original_price=7939),
            SearchHit("mironet", "Garmin Edge Explore 2", 5581, "https://www.mironet.cz/c+dp3/")]
    other = [SearchHit("softcom", "Garmin Forerunner", 5600, "https://www.softcom.cz/eshop/x_d1.html", original_price=5000)]
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: hits)
    monkeypatch.setattr(SHOPS["softcom"], "searcher", lambda q: other)
    offers, _ = offers_for_query(db, "garmin")
    assert [h.price for h, _ in offers if h.shop != "alza"] == [5581, 5600, 5999, 6018]
    db.close()


def test_accessories_hidden_unless_asked():
    from lovceny.shops.search import drop_accessories
    hits = [SearchHit("x", "Pouzdro CELLY Apple iPhone 15", 29, "u1"), SearchHit("x", "Apple iPhone 15 128GB", 16990, "u2"),
            SearchHit("x", "Ochranná fólie pro iPhone", 39, "u3")]
    assert [h.url for h in drop_accessories(hits, "iphone")] == ["u2"]
    assert len(drop_accessories(hits, "pouzdro iphone")) == 3
    only_acc = hits[:1]
    assert drop_accessories(only_acc, "iphone") == only_acc


def test_abcom_keeps_trailing_slash_others_unchanged():
    assert SHOPS["abcom"].canonical_url("https://www.ab-com.cz/garmin-camper-895/") == "https://www.ab-com.cz/garmin-camper-895/"
    assert SHOPS["abcom"].canonical_url("http://www.ab-com.cz/garmin-camper-895") == "https://www.ab-com.cz/garmin-camper-895/"
    assert SHOPS["mironet"].canonical_url("https://www.mironet.cz/x+dp1/") == "https://www.mironet.cz/x+dp1"
    assert SHOPS["planeo"].canonical_url("https://www.planeo.cz/a/b.html") == "https://www.planeo.cz/a/b.html"


def test_accessories_dropped_across_shops(monkeypatch):
    monkeypatch.setattr(SHOPS["softcom"], "searcher", lambda q: [SearchHit("softcom", "Pouzdro Apple iPhone XR", 29, "a")])
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: [SearchHit("mironet", "Apple iPhone 15", 16990, "b")])
    assert [h.url for h in live_search("iphone").hits] == ["b"]


from lovceny.shops.search import parse_4home, parse_vestavne, parse_eta, parse_eta_detail, _matches, is_accessory


@pytest.mark.parametrize("fn,fix,shop,word", [(parse_4home, "4home_kavovar", "4home", "kavovar"),
                                              (parse_vestavne, "vestavne_mycka", "vestavne", "mycka"),
                                              (parse_eta, "eta_vysavac", "eta", "vysavac")])
def test_appliance_shop_parsers(fn, fix, shop, word):
    hits = fn((FIX / f"{fix}.html").read_text("utf8"))
    assert len(hits) >= 3
    for h in hits:
        assert h.shop == shop and h.price > 100 and h.image and shop_for_url(h.url).slug == shop
        assert _matches(h.name + " " + search._slug_words(h.url), word)
        assert SHOPS[shop].canonical_url(h.url).endswith("/")


def test_eta_detail_price():
    r = parse_eta_detail((FIX / "eta_detail.html").read_text("utf8"))
    assert r.price == 6999 and "ETA Sonar" in r.name and r.image.startswith("https://www.eta.cz/")


@pytest.mark.parametrize("name,q,ok", [
    ("Hisense WFQP7012EVM pračka úzká", "pračka", True),
    ("Samsung RB34 kombinovaná chladnička", "lednice", True),
    ("Ukonic Mini-lednička", "lednice", True),
    ("ARMOR Fotopapír FOTOPOHLEDNICE 10x15", "lednice", False),
    ('LG 55" televizor OLED', "televize", True),
    ("Dyson V12 Origin tyčový vysavač", "vysavač dyson", True),
    ("Electrolux vysavač EL51", "vysavač dyson", False),
    ("Tyčový vysávač ETA", "vysavač", True),
])
def test_czech_matching(name, q, ok):
    assert _matches(name, q) is ok


@pytest.mark.parametrize("name,q", [("Nedis Odkapávací miska pod lednici", "lednice"),
                                    ("Stojan na televize Fiber Novelty", "televize"),
                                    ("AVACOM baterie pro Dyson V8", "vysavač dyson"),
                                    ("DeLonghi EcoDecalk Odvápňovač", "kávovar"),
                                    ("Dobíjecí stanice FIXED pro ovladače DualSense", "playstation")])
def test_appliance_accessories(name, q):
    assert is_accessory(name, q)


@pytest.mark.parametrize("name,q", [("Hario Mizudashi Kávovar na cold brew", "kávovar"),
                                    ("Dyson Cinetic Big Ball bezsáčkový vysavač", "vysavač dyson"),
                                    ("Philco Pračka s předním plněním", "pračka"),
                                    ("Sony PlayStation 5 Slim", "playstation")])
def test_appliances_not_hidden(name, q):
    assert not is_accessory(name, q)


@pytest.mark.parametrize("name,q", [("Televizní stolek Basic", "televize"), ("FINISH Sůl 1.5 kg", "myčka"),
                                    ("Bino Dětská kuchyňka s pračkou", "pračka")])
def test_more_appliance_accessories(name, q):
    assert is_accessory(name, q)


def test_slug_matching_only_where_enabled(monkeypatch):
    hit = SearchHit("mironet", "FINISH Sůl 1.5 kg", 74, "https://www.mironet.cz/finish-sul-do-mycky+dp1/")
    vs = SearchHit("vestavne", "mora IM 6435", 9990, "https://www.vestavne-spotrebice.cz/x-vestavne-mycky-integrovane/")
    monkeypatch.setattr(SHOPS["mironet"], "searcher", lambda q: [hit])
    monkeypatch.setattr(SHOPS["vestavne"], "searcher", lambda q: [vs])
    assert [h.shop for h in live_search("myčka").hits] == ["vestavne"]


@pytest.mark.parametrize("name,q", [("Skříň nad pračku Kamil White", "pračka"), ("Televizní stěna Duru", "televize"),
                                    ("Bigjigs Rail Vlaková myčka", "myčka")])
def test_furniture_and_toys_hidden(name, q):
    assert is_accessory(name, q)
