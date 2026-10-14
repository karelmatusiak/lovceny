#!/usr/bin/env python3
"""Sestaví rozšíření Lovceny pro Chrome (MV3 service worker) a Firefox (MV3 background scripts).

  python extension/build.py                 # → extension/dist/{chrome,firefox}/ + lovceny-{chrome,firefox}-<verze>.zip
  python extension/build.py --api http://localhost:8020 --extra-host http://localhost/*   # testovací build

Seznam obchodů (host permissions) se bere z lovceny/shops/registry.py: jen obchody se stavem „ok“.
Ikony se zmenší z lovceny/static/brand/icon-512.png, logo v odznaku je ikona.svg.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(APP))

VERSION = "0.1.0"
DEFAULT_API = "https://lovceny.onrender.com"
LOVCENY_HOSTS = ["https://lovceny.onrender.com/*", "https://lovceny.cz/*", "https://www.lovceny.cz/*"]
GECKO_ID = "rozsireni@lovceny.cz"
ICON_SIZES = (16, 32, 48, 128)
NAME = "Lovceny – ověř slevu"
DESCRIPTION = ("Na stránce produktu v podporovaném e-shopu ukáže verdikt Lovceny, nejnižší cenu za 30 dní "
               "a nejlevnější nabídku jinde. Bez sledování.")


def shop_matches() -> list[str]:
    from lovceny.shops import SHOPS
    doms = sorted({d for s in SHOPS.values() if s.status == "ok" for d in s.domains})
    # oficiální e-shop De'Longhi je jen část globálního webu → jen česká sekce
    narrow = {"delonghi.com": ["https://www.delonghi.com/cs-cz/*"]}
    return [m for d in doms for m in narrow.get(d, [f"https://*.{d}/*"])]


def manifest(target: str, api: str = DEFAULT_API, extra_hosts: list[str] | None = None) -> dict:
    shops = shop_matches() + list(extra_hosts or [])
    api_host = api.rstrip("/") + "/*"
    hosts = shops + [h for h in LOVCENY_HOSTS + [api_host] if h not in shops]
    hosts = list(dict.fromkeys(hosts))
    m = {
        "manifest_version": 3,
        "name": NAME,
        "short_name": "Lovceny",
        "version": VERSION,
        "description": DESCRIPTION,
        "default_locale": None,
        "icons": {str(s): f"icons/icon-{s}.png" for s in ICON_SIZES},
        "action": {"default_title": "Lovceny – ověř slevu", "default_icon": {str(s): f"icons/icon-{s}.png" for s in (16, 32)}},
        "host_permissions": hosts,
        "permissions": ["activeTab"],  # jen po kliknutí na ikonu: otevřít ověření aktuální stránky
        "content_scripts": [{"matches": shops, "js": ["content.js"], "run_at": "document_idle"}],
        "homepage_url": "https://lovceny.onrender.com/rozsireni",
    }
    del m["default_locale"]
    if target == "chrome":
        m["background"] = {"service_worker": "background.js"}
        m["minimum_chrome_version"] = "116"
    else:
        m["background"] = {"scripts": ["config.js", "background.js"]}
        m["browser_specific_settings"] = {"gecko": {
            "id": GECKO_ID, "strict_min_version": "140.0",
            # URL produktu a název+cena ze stránky odcházejí na API Lovceny (nic víc)
            "data_collection_permissions": {"required": ["browsingActivity", "websiteContent"]}}}
    return m


def icon_svg() -> str:
    svg = (APP / "lovceny/static/brand/ikona.svg").read_text().strip()
    return svg.replace("<svg ", '<svg aria-hidden="true" focusable="false" ', 1)


def icons() -> dict[str, bytes]:
    from PIL import Image
    src = Image.open(APP / "lovceny/static/brand/icon-512.png").convert("RGBA")
    out = {}
    for s in ICON_SIZES:
        b = io.BytesIO()
        src.resize((s, s), Image.LANCZOS).save(b, "PNG", optimize=True)
        out[f"icons/icon-{s}.png"] = b.getvalue()
    return out


def files(target: str, api: str = DEFAULT_API, extra_hosts: list[str] | None = None) -> dict[str, bytes]:
    src = HERE / "src"
    content = (src / "content.js").read_text().replace("'__LOVCENY_ICON_SVG__'", json.dumps(icon_svg()))
    out = {
        "manifest.json": json.dumps(manifest(target, api, extra_hosts), ensure_ascii=False, indent=2).encode(),
        "config.js": f"/* vygenerováno build.py */\nvar LOVCENY_API = {json.dumps(api.rstrip('/'))};\n".encode(),
        "background.js": (src / "background.js").read_bytes(),
        "content.js": content.encode(),
        "PRIVACY.md": (HERE / "PRIVACY.md").read_bytes(),
    }
    out.update(icons())
    return out


def zip_bytes(target: str, **kw) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files(target, **kw).items()):
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # reprodukovatelný zip
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    return b.getvalue()


def build(out: Path, api: str = DEFAULT_API, extra_hosts: list[str] | None = None) -> list[Path]:
    made = []
    for target in ("chrome", "firefox"):
        d = out / target
        d.mkdir(parents=True, exist_ok=True)
        for name, data in files(target, api, extra_hosts).items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_bytes(data)
        z = out / f"lovceny-{target}-{VERSION}.zip"
        z.write_bytes(zip_bytes(target, api=api, extra_hosts=extra_hosts))
        made += [d, z]
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--extra-host", action="append", default=[], help="další match pattern (jen pro testy)")
    ap.add_argument("--out", default=str(HERE / "dist"))
    a = ap.parse_args()
    for p in build(Path(a.out), a.api, a.extra_host):
        print(p)
