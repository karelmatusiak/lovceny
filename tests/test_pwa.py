"""PWA: manifest, ikony, service worker (offline, cache bez API), výzva Přidat na plochu."""
import re
from pathlib import Path

from PIL import Image

STATIC = Path(__file__).parent.parent / "lovceny" / "static"


def test_manifest(client_db):
    client, _, _ = client_db
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["lang"] == "cs" and m["short_name"] == "Lovceny" and "hlídač cen" in m["name"]
    assert m["display"] == "standalone" and m["start_url"].startswith("/") and m["scope"] == "/"
    assert m["theme_color"] == "#0B6E4F" and m["background_color"] == "#FBF8F3"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for i in m["icons"]:
        path = STATIC / i["src"].split("?")[0].removeprefix("/static/")
        w, h = Image.open(path).size
        assert f"{w}x{h}" == i["sizes"]
        assert client.get(i["src"]).status_code == 200


def test_head_links(client_db):
    client, _, _ = client_db
    html = client.get("/").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html and "apple-touch-icon" in html
    assert "pwa.js" in html and 'id="pwa-install"' in html and "Přidat na plochu" in html
    assert "Sdílet" in html and "Teď ne" in html
    assert 'aria-label="Zavřít nabídku přidání na plochu"' in html
    assert 'data-pwa-open' in html
    css = (STATIC / "app.css").read_text()
    rule = css.split(".pwa-install{", 1)[1].split("}", 1)[0]
    assert "position:static" in rule and "position:fixed" not in rule


def test_service_worker(client_db):
    client, _, _ = client_db
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/" and r.headers["cache-control"] == "no-cache"
    js = r.text
    # push zůstal
    assert "addEventListener('push'" in js and "notificationclick" in js and "showNotification" in js
    # offline + precache verzovaných souborů
    assert "OFFLINE_URL = '/offline'" in js and re.search(r'"/static/style\.css\?v=[0-9a-f]{10}"', js)
    assert "req.mode === 'navigate'" in js and "caches.match(OFFLINE_URL)" in js
    # stránky a API se necachují (navigace = síť; /api/ ani /hledat nejsou v žádné cache větvi)
    assert "c.put(req" not in js.split("req.mode === 'navigate'")[1].split("return;")[0]
    assert "/api/" not in js.split("self.addEventListener('fetch'")[1].split("// /api/")[0]
    version = re.search(r"const VERSION = '([0-9a-f]{10})'", js).group(1)
    assert client.get("/sw.js").text.count(version) >= 1  # stabilní verze pro stejné soubory


def test_offline_page(client_db):
    client, _, _ = client_db
    r = client.get("/offline")
    assert r.status_code == 200 and "Jsi offline" in r.text and 'content="noindex' in r.text


def test_push_ios_hint_and_pwa_js(client_db, monkeypatch):
    monkeypatch.setenv("LOVCENY_FEATURE_PUSH", "1")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLICKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "privatekeyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    client, _, make = client_db
    html = client.get(f"/produkt/{make([1000]).id}").text
    assert "Na iPhonu fungují upozornění jen z plochy" in html and "data-pwa-open" in html
    js = (STATIC / "pwa.js").read_text()
    assert "beforeinstallprompt" in js and "navigator.serviceWorker.register('/sw.js'" in js
    assert "Přidat na plochu" in js and "standalone" in js
    assert "appinstalled" in js and "COOLDOWN_MS" in js and "ENGAGE_MS" in js
    push = (STATIC / "push.js").read_text()
    assert ".push-ios" in push
