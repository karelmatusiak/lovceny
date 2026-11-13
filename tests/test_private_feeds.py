"""Soukromé feedy nemají adresu v gitu. Prázdný env je přeskočí."""
import json
import logging
import subprocess
import sys

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
    monkeypatch.delenv("FEED_URLS", raising=False)
    for slug, env in PRIVATE.items():
        monkeypatch.delenv(env, raising=False)
        assert get_shop(slug).feed_url == ""
        monkeypatch.setenv(env, "  ")
        assert get_shop(slug).feed_url == ""
        monkeypatch.setenv(env, "https://example.test/feed.xml")
        assert get_shop(slug).feed_url == "https://example.test/feed.xml"


def test_feed_urls_json_fills_in_and_env_wins(monkeypatch):
    monkeypatch.delenv("ISTYLE_FEED_URL", raising=False)
    monkeypatch.setenv("FEED_URLS", json.dumps({
        "ISTYLE_FEED_URL": "https://example.test/from-json.xml",
        "SENCOR_FEED_URL": "https://example.test/sencor.xml",
    }))
    assert get_shop("istyle").feed_url == "https://example.test/from-json.xml"
    assert get_shop("sencor").feed_url == "https://example.test/sencor.xml"
    monkeypatch.setenv("ISTYLE_FEED_URL", "https://example.test/from-env.xml")
    assert get_shop("istyle").feed_url == "https://example.test/from-env.xml"
    monkeypatch.setenv("ISTYLE_FEED_URL", "  ")
    assert get_shop("istyle").feed_url == "https://example.test/from-json.xml"


def test_invalid_feed_urls_logs_and_skips_without_the_value(monkeypatch, caplog):
    monkeypatch.delenv("DYSON_FEED_URL", raising=False)
    monkeypatch.setenv("FEED_URLS", "not-json SECRETTOKEN")
    with caplog.at_level(logging.ERROR, logger="lovceny.shops"):
        assert get_shop("dyson").feed_url == ""
        assert get_shop("jbl").feed_url == "https://www.jbl.cz/gmerchant-JBL-cz.xml"
    assert "není platný JSON" in caplog.text
    assert "SECRETTOKEN" not in caplog.text
    monkeypatch.setenv("FEED_URLS", "[1, 2]")
    with caplog.at_level(logging.ERROR, logger="lovceny.shops"):
        assert get_shop("dyson").feed_url == ""
    assert "není JSON objekt" in caplog.text


def test_workflows_pass_one_feed_urls_secret():
    for name in ("fetch.yml", "seed.yml", "static.yml"):
        text = open(f".github/workflows/{name}", encoding="utf-8").read()
        assert "FEED_URLS: ${{ secrets.FEED_URLS }}" in text
        for env in PRIVATE.values():
            assert f"secrets.{env}" not in text


def test_fetch_skips_a_shop_without_a_feed_url():
    text = open("fetch_prices.py", encoding="utf-8").read()
    assert 'if s.source == "feed" and not s.feed_url' in text
    assert "obchod přeskakuji" in text


def test_build_feed_urls_script_writes_keys_not_stdout(tmp_path):
    out = tmp_path / "feeds.json"
    proc = subprocess.run(
        [sys.executable, "scripts/build_feed_urls_json.py", str(out), "--ref", "origin/main"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "http" not in proc.stdout and "http" not in proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert list(data) == list(PRIVATE.values())
    missing = [key for key, value in data.items() if not (isinstance(value, str) and value.startswith("http"))]
    assert missing == []
    assert oct(out.stat().st_mode & 0o777) == "0o600"
