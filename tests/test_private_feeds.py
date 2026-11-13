"""Soukromé feedy nemají adresu v gitu. Prázdný env je přeskočí."""
from lovceny.shops import get_shop

PRIVATE = {
    "istyle": "ISTYLE_FEED_URL",
    "sencor": "SENCOR_FEED_URL",
    "xm": "XM_FEED_URL",
    "evolveo": "EVOLVEO_FEED_URL",
    "tesla": "TESLA_FEED_URL",
    "xupe": "XUPE_FEED_URL",
    "tera": "TERA_FEED_URL",
    "amazestore": "AMAZESTORE_FEED_URL",
    "misura": "MISURA_FEED_URL",
    "dyson": "DYSON_FEED_URL",
    "allegro": "ALLEGRO_FEED_URL",
    "bscom": "BSCOM_FEED_URL",
    "dronpro": "DRONPRO_FEED_URL",
    "iphonemarket": "IPHONEMARKET_FEED_URL",
}


def test_registry_has_no_private_feed_defaults():
    text = open("lovceny/shops/registry.py", encoding="utf-8").read()
    assert "api.tradedoubler.com" not in text
    assert "token=" not in text
    for slug, env in PRIVATE.items():
        shop = get_shop(slug)
        assert shop.feed_url_env == env
        assert "feed_url_default" not in shop.extra
        assert "feed_url_fallback" not in shop.extra


def test_empty_env_skips_private_feed(monkeypatch):
    for slug, env in PRIVATE.items():
        monkeypatch.delenv(env, raising=False)
        assert get_shop(slug).feed_url == ""
        monkeypatch.setenv(env, "  ")
        assert get_shop(slug).feed_url == ""
        monkeypatch.setenv(env, "https://example.test/feed.xml")
        assert get_shop(slug).feed_url == "https://example.test/feed.xml"


def test_fetch_skips_a_shop_without_a_feed_url():
    text = open("fetch_prices.py", encoding="utf-8").read()
    assert 'if s.source == "feed" and not s.feed_url' in text
    assert "obchod přeskakuji" in text
