"""v6: veřejné API /api/v1/overit (CORS jen pro rozšíření, rate limit, cache) a rozšíření prohlížeče."""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lovceny import verify
from lovceny.main import app
from lovceny.shops import SHOPS

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
CHROME_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
FF_ORIGIN = "moz-extension://0b5c3f5e-1a2b-4c3d-9e8f-123456789abc"
PRODUCT = "https://www.mironet.cz/sluchatka-api-test"


@pytest.fixture(autouse=True)
def _extension_feature_on(monkeypatch):
    """Tyhle testy popisují obnovené rozšíření. Ve výchozím nasazení je vypnuté."""
    monkeypatch.setenv("LOVCENY_SIDE_FEATURES", "1")


@pytest.fixture
def c(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("API nesmí stahovat stránku, když rozšíření poslalo cenu")
    monkeypatch.setattr(verify, "safe_fetch", boom)
    from lovceny.db import init_db
    init_db()
    return TestClient(app)


def q(**kw):
    return {"url": PRODUCT, "name": "Sluchátka API Test", "price": 1990, **kw}


def test_cors_only_for_extensions(c, monkeypatch):
    monkeypatch.delenv("EXTENSION_ORIGINS", raising=False)
    for origin in (CHROME_ORIGIN, FF_ORIGIN):
        r = c.get("/api/v1/overit", params=q(), headers={"Origin": origin})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
        assert "Origin" in r.headers["vary"]
    for origin in ("https://evil.example", "null", "chrome-extension://x", "https://lovceny.onrender.com"):
        r = c.get("/api/v1/overit", params=q(), headers={"Origin": origin})
        assert "access-control-allow-origin" not in r.headers
    pf = c.options("/api/v1/overit", headers={"Origin": CHROME_ORIGIN, "Access-Control-Request-Method": "GET"})
    assert pf.status_code == 204 and pf.headers["access-control-allow-methods"] == "GET"
    monkeypatch.setenv("EXTENSION_ORIGINS", FF_ORIGIN)  # po zveřejnění: přesný seznam ID
    assert "access-control-allow-origin" not in c.get("/api/v1/overit", params=q(), headers={"Origin": CHROME_ORIGIN}).headers
    assert c.get("/api/v1/overit", params=q(), headers={"Origin": FF_ORIGIN}).headers["access-control-allow-origin"] == FF_ORIGIN


def test_json_shape_and_cache(c):
    r = c.get("/api/v1/overit", params=q())
    j = r.json()
    assert j["ok"] and j["status"] == "ok" and j["price"] == 1990 and j["name"] == "Sluchátka API Test"
    assert j["shop"] == {"slug": "mironet", "name": "Mironet", "supported": True}
    assert j["lovceny_url"].endswith("/over-slevu?url=https%3A%2F%2Fwww.mironet.cz%2Fsluchatka-api-test")
    assert j["tracked"] is None and j["check30"] is None and j["cached"] is False
    assert "max-age=900" in r.headers["cache-control"] and r.headers["x-robots-tag"] == "noindex"
    assert c.get("/api/v1/overit", params=q()).json()["cached"] is True


def test_tracked_product_check30(client_db, c):
    from datetime import datetime, timedelta, timezone
    from lovceny.models import Product, Offer, PricePoint
    client, db, make = client_db
    url = f"https://www.mironet.cz/api-30-{datetime.now().timestamp()}"
    p = Product(name="API 30", slug="api-30")
    o = Offer(product=p, shop="mironet", url=url, status="ok", price=1450)
    now = datetime.now(timezone.utc)
    for ago in range(40, 0, -1):
        o.points.append(PricePoint(price=900 if ago == 10 else 1000, captured_at=now - timedelta(days=ago)))
    db.add(p)
    db.commit()
    j = c.get("/api/v1/overit", params={"url": url, "price": 1450, "original": 2000, "name": "API 30"}).json()
    assert j["tracked"]["id"] == p.id and j["tracked"]["verdict"]["code"] == "fake"
    assert j["tracked"]["verdict"]["label"] == "Za 30 dní bylo levněji"
    assert j["check30"]["flag"] and j["check30"]["min30"] == 900 and j["check30"]["label"] == "Za 30 dní bylo levněji"


def test_bad_url_and_rate_limit(c, monkeypatch):
    r = c.get("/api/v1/overit", params={"url": "http://169.254.169.254/"})
    assert r.status_code == 422 and not r.json()["ok"] and r.headers["cache-control"] == "no-store"
    import lovceny.main as m
    monkeypatch.setattr(m, "API_PER_IP_HOUR", 3)
    codes = [c.get("/api/v1/overit", params=q(url=f"https://www.mironet.cz/rl-api-{i}")).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]
    assert c.get("/api/v1/overit", params=q(url="https://www.mironet.cz/rl-api-0")).status_code == 200  # cache


def test_api_not_in_analytics():
    from lovceny import analytics
    assert not analytics.should_track("/api/v1/overit", "GET", {})
    assert "Disallow: /api/" in TestClient(app).get("/robots.txt").text


# ---------- rozšíření ----------
sys.path.insert(0, str(EXT))
import build as extbuild  # noqa: E402


def _ok_domains():
    return sorted({d for s in SHOPS.values() if s.status == "ok" for d in s.domains})


@pytest.mark.parametrize("target", ["chrome", "firefox"])
def test_manifest_minimal_permissions(target):
    m = extbuild.manifest(target)
    assert m["manifest_version"] == 3 and m["permissions"] == ["activeTab"]
    text = json.dumps(m)
    assert "<all_urls>" not in text and "*://*/*" not in text and "http://" not in text
    shops = m["content_scripts"][0]["matches"]
    assert set(shops) <= set(m["host_permissions"])
    doms = {re.match(r"https://(?:\*\.|www\.)?([^/]+)/", p).group(1) for p in shops}
    assert doms == set(_ok_domains())  # přesně podporované obchody z registry, nic navíc
    assert not any("czc.cz" in p or "mall.cz" in p for p in m["host_permissions"])  # blokované obchody ne
    extra = set(m["host_permissions"]) - set(shops)
    assert extra == {"https://lovceny.onrender.com/*", "https://lovceny.cz/*", "https://www.lovceny.cz/*"}
    assert "https://www.delonghi.com/cs-cz/*" in shops
    if target == "chrome":
        assert m["background"] == {"service_worker": "background.js"}
    else:
        g = m["browser_specific_settings"]["gecko"]
        assert m["background"] == {"scripts": ["config.js", "background.js"]}
        assert g["id"] == "rozsireni@lovceny.cz" and g["data_collection_permissions"]["required"] == ["browsingActivity", "websiteContent"]


def test_zip_contents_icons_and_no_tracking():
    from PIL import Image
    z = zipfile.ZipFile(io.BytesIO(extbuild.zip_bytes("chrome")))
    names = set(z.namelist())
    assert {"manifest.json", "background.js", "content.js", "config.js", "PRIVACY.md"} <= names
    for s in extbuild.ICON_SIZES:
        assert Image.open(io.BytesIO(z.read(f"icons/icon-{s}.png"))).size == (s, s)
    content = z.read("content.js").decode()
    assert "__LOVCENY_ICON_SVG__" not in content and "#0B6E4F" in content and "#FF7A45" in content
    code = content + z.read("background.js").decode()
    for bad in ("eval(", "new Function", "google-analytics", "gtag", "document.cookie", "localStorage", "falešn", "<script src"):
        assert bad not in code, bad
    assert "credentials: 'omit'" in code
    assert 'LOVCENY_API = "https://lovceny.onrender.com"' in z.read("config.js").decode()
    assert extbuild.zip_bytes("chrome") == extbuild.zip_bytes("chrome")  # reprodukovatelné


def test_store_texts_privacy_and_no_private_email():
    store = (EXT / "STORE.md").read_text()
    for want in ("Chrome Web Store", "Firefox Add-ons", "Krátký popis", "Zdůvodnění oprávnění", "activeTab", "Za 30 dní bylo levněji"):
        assert want in store
    short = re.search(r"\*\*Krátký popis \(max\. 132 znaků\):\*\*\n(.+)", store).group(1)
    assert len(short) <= 132
    summary = re.search(r"\*\*Shrnutí \(max\. 250 znaků\):\*\*\n(.+)", store).group(1)
    assert len(summary) <= 250
    assert "falešn" not in store.lower()
    for f in list(EXT.rglob("*")) + list((ROOT / "lovceny" / "templates").rglob("*.html")):
        if f.is_file() and "dist" not in f.parts and f.suffix in (".md", ".js", ".py", ".html", ".json"):
            for addr in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", f.read_text(errors="ignore")):
                assert addr.endswith(("@lovceny.cz", "@example.cz")) or addr == "rozsireni@lovceny.cz", (f, addr)


def test_rozsireni_page_and_zip_download():
    c = TestClient(app)
    html = c.get("/rozsireni").text
    for want in ("chrome://extensions", "Načíst rozbalené", "about:debugging", "Načíst dočasný doplněk",
                 "/rozsireni/lovceny-chrome.zip", "/rozsireni/lovceny-firefox.zip", "Mironet", "/ochrana-osobnich-udaju#rozsireni"):
        assert want in html, want
    r = c.get("/rozsireni/lovceny-firefox.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    m = json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read("manifest.json"))
    assert m["browser_specific_settings"]["gecko"]["id"] == "rozsireni@lovceny.cz"
    assert c.get("/rozsireni/lovceny-safari.zip").status_code == 404


def _node_env():
    node = shutil.which("node")
    if not node:
        return None
    env = dict(os.environ)
    if os.environ.get("LOVCENY_E2E_NODE_PATH"):
        env["NODE_PATH"] = os.environ["LOVCENY_E2E_NODE_PATH"]
    ok = subprocess.run([node, "-e", "require('playwright')"], env=env, capture_output=True).returncode == 0
    return (node, env) if ok else None


@pytest.mark.skipif(_node_env() is None, reason="E2E potřebuje node + playwright (LOVCENY_E2E_NODE_PATH)")
def test_extension_loads_in_headless_chromium_and_renders_badge():
    node, env = _node_env()
    env["PYTHON"] = sys.executable
    r = subprocess.run([node, str(EXT / "tests" / "e2e.js")], env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK rozšíření" in r.stdout and "Za 30 dní bylo levněji" in r.stdout
