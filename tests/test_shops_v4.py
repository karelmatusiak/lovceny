"""v4: Onlineshop, De'Longhi, Quikset (živé hledání) a Mobilfone (jen sledování podle URL)."""
from pathlib import Path

from lovceny import images
from lovceny.shops import SHOPS, shop_for_url
from lovceny.shops.robots import Robots
from lovceny.shops.search import (SearchHit, _matches, drop_accessories, parse_delonghi, parse_mobilfone_detail,
                                  parse_onlineshop, parse_quikset, parse_quikset_detail)
from lovceny.shops.scrape import parse_product

FIX = Path(__file__).parent / "fixtures"


def _r(name):
    return (FIX / name).read_text("utf8", errors="ignore")


def test_onlineshop_regular_price_not_voucher():
    hits = parse_onlineshop(_r("onlineshop_pracka.html"))
    assert len(hits) >= 10
    by = {h.name: h for h in hits}
    # 6 490 Kč jen se slevovým kódem → bereme běžnou cenu 6 990 Kč
    assert by["Pračka Candy EY 29B8-S ProWash 100"].price == 6990
    assert by["Pračka se sušičkou Miele WTD160 WCS"].price == 48382  # ne cena bez DPH
    for h in hits:
        assert shop_for_url(h.url).slug == "onlineshop" and "/product/" in h.image


def test_onlineshop_detail_jsonld():
    r = parse_product(_r("onlineshop_detail.html"))
    assert r.price == 48382 and "Miele" in r.name


def test_delonghi_parser_decimals_and_original():
    hits = parse_delonghi(_r("delonghi_kavovar.html"))
    assert len(hits) >= 5 and len({h.url for h in hits}) == len(hits)
    h = next(h for h in hits if "ECAM630.75.TSM" in h.url)
    assert h.price == 34990.0 and h.original_price == 36999.0  # „34 990,00 Kč“
    for h in hits:
        assert h.name.startswith("De'Longhi") and shop_for_url(h.url).slug == "delonghi"
        assert h.image.startswith("https://dam.delonghi.com/")
    assert SHOPS["delonghi"].extra.get("match_slug")  # „kávovar“ je jen v URL
    assert _matches(hits[0].name + " " + hits[0].url.replace("-", " "), "kávovary")


def test_delonghi_detail_jsonld():
    assert parse_product(_r("delonghi_detail.html")).price == 36499


def test_quikset_parser_and_accessories():
    hits = parse_quikset(_r("quikset_iphone.html"))
    assert len(hits) >= 5
    assert hits[0].price == 1390 and hits[0].image.endswith("_v.jpeg")
    assert all(shop_for_url(h.url).slug == "quikset" for h in hits)
    phone = SearchHit("quikset", "Apple iPhone 15 128GB", 18990, "https://www.quikset.cz/x")
    kept = drop_accessories(hits + [phone], "iphone")
    assert kept == [phone]


def test_quikset_detail():
    r = parse_quikset_detail(_r("quikset_detail.html"))
    assert r.price == 1390 and "iPhone" in r.name


def test_mobilfone_detail_only_no_search():
    r = parse_mobilfone_detail(_r("mobilfone_detail.html"))
    assert r.price == 34500 and "iPhone" in r.name
    s = SHOPS["mobilfone"]
    assert s.searcher is None and s.can_scrape and s.page_parser


def test_robots_rules_v4():
    assert not Robots("User-agent: *\nDisallow: /search.php").can_fetch("LovcenyBot", "https://www.mobilfone.cz/search.php?q=x")
    assert not Robots("User-agent: *\nDisallow: /hledat*").can_fetch("LovcenyBot", "https://czc.cz/hledat?q=x")
    assert Robots("User-agent: *\nDisallow: /ajax/").can_fetch("LovcenyBot", "https://www.quikset.cz/hledani?q=iphone")


def test_blocked_shops_not_searched():
    for slug in ("czc", "conrad", "eberry", "bscom", "tsbohemia", "smarty"):
        assert SHOPS[slug].status == "blocked" and not SHOPS[slug].searcher and not SHOPS[slug].can_scrape


def test_image_hosts_include_new_shops():
    hosts = images.allowed_hosts()
    for d in ("onlineshop.cz", "delonghi.com", "quikset.cz", "mobilfone.cz"):
        assert d in hosts


def test_istores_mobilprovas_mobilyostrava():
    from lovceny.shops.search import parse_istores, parse_mobilprovas, parse_mobilyostrava
    h = parse_istores(_r("istores_iphone.html"))
    assert len(h) >= 10 and len({x.url for x in h}) == len(h)
    assert any(x.name == "iPhone 18 Pro 256GB Černá" and x.price == 34990 for x in h)
    assert all(shop_for_url(x.url).slug == "istores" for x in h)
    m = parse_mobilprovas(_r("mobilprovas_iphone.html"))
    assert len(m) >= 10 and all(x.price > 0 and shop_for_url(x.url).slug == "mobilprovas" for x in m)
    o = parse_mobilyostrava(_r("mobilyostrava_iphone.html"))
    assert len(o) >= 5 and o[0].price == 200 and shop_for_url(o[0].url).slug == "mobilyostrava"


def test_dot_thousands_price():
    from lovceny.shops.search import _price
    assert _price("20.222 Kč") == 20222 and _price("36\xa0499,00\xa0Kč") == 36499.0


def test_ellexgk_and_itex24():
    from lovceny.shops.search import parse_ellexgk, parse_itex24
    e = parse_ellexgk(_r("ellexgk_playstation.html"))
    assert len(e) >= 10 and e[0].price == 349 and all(shop_for_url(x.url).slug == "ellexgk" for x in e)
    assert images.is_allowed(e[0].image) if hasattr(images, "is_allowed") else "cdn-upgates" in e[0].image
    assert parse_product(_r("ellexgk_detail.html")).price == 16090
    i = parse_itex24(_r("itex24_iphone.html"))
    assert i[0].price == 70980 and all(x.price > 0 for x in i)  # 0 Kč (cena na dotaz) vynecháno
    assert all(shop_for_url(x.url).slug == "itex24" for x in i)


def test_spare_parts_hidden():
    from lovceny.shops.search import is_accessory
    assert is_accessory("IPS Liquid Retina LCD displej + sklíčko LCD + dotyková plocha Apple iPhone 11 černá", "iphone")
    assert not is_accessory("Apple iPhone 16 128GB černá", "iphone")
