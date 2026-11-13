"""Sestaví FEED_URLS JSON z výchozích adres ve starším registry.py.

Výstup jde jen do souboru zadaného argumentem, nikdy na stdout.
Výchozí revize je origin/main z doby, kdy tam ty adresy ještě byly.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

KEYS = (
    "ISTYLE_FEED_URL",
    "SENCOR_FEED_URL",
    "XM_FEED_URL",
    "EVOLVEO_FEED_URL",
    "TESLA_FEED_URL",
    "XUPE_FEED_URL",
    "TERA_FEED_URL",
    "AMAZESTORE_FEED_URL",
    "MISURA_FEED_URL",
    "DYSON_FEED_URL",
    "ALLEGRO_FEED_URL",
    "BSCOM_FEED_URL",
    "DRONPRO_FEED_URL",
    "IPHONEMARKET_FEED_URL",
)


def extract(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in text.split("Shop(")[1:]:
        env = re.search(r'feed_url_env="([A-Z0-9_]+)"', part)
        url = re.search(r'"feed_url_default":\s*"([^"]+)"', part)
        if not env or not url or env.group(1) not in KEYS:
            continue
        found[env.group(1)] = url.group(1)
    return found


def registry_at(ref: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "show", f"{ref}:lovceny/shops/registry.py"],
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or "").strip() or "git show selhal"
        raise SystemExit(err) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zapíše FEED_URLS JSON do souboru.")
    parser.add_argument("output", help="cesta k výstupnímu souboru")
    parser.add_argument("--ref", default="origin/main", help="git revize s původními defaulty")
    args = parser.parse_args(argv)
    if args.output in ("-", ""):
        print("Výstup nesmí být stdout.", file=sys.stderr)
        return 2
    path = Path(args.output)
    if path.exists() and path.is_dir():
        print("Výstup je adresář.", file=sys.stderr)
        return 2
    found = extract(registry_at(args.ref))
    missing = [key for key in KEYS if key not in found]
    if missing:
        print("Chybí klíče: " + ", ".join(missing), file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({key: found[key] for key in KEYS}, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    print(f"Zapsáno {len(KEYS)} klíčů.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
