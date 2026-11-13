"""Jedna SQLite databáze v Cloudflare R2. Tajemství se nelogují.

Objekt `lovceny.db` je aktuální soubor. `backups/lovceny-YYYY-MM-DD.db` drží
posledních 14 pražských dní. Zápis do R2 jde jen po kontrole, že počty řádků
prudce neklesly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")
CANONICAL_KEY = "lovceny.db"
BACKUP_PREFIX = "backups/lovceny-"
DONE_KEY = "migration-complete"
BACKUP_DAYS = 14
# Prudký pád: tabulka, která měla aspoň 50 řádků, nesmí klesnout pod 80 %.
SHARP_FLOOR = 50
SHARP_RATIO = 0.8
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class R2Error(RuntimeError):
    pass


class R2NotConfigured(R2Error):
    pass


class R2Missing(R2Error):
    pass


def settings() -> dict[str, str]:
    values = {
        "account": os.environ.get("R2_ACCOUNT_ID", "").strip(),
        "access_key": os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
        "secret": os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
        "bucket": (os.environ.get("R2_BUCKET") or "lovceny-data").strip(),
    }
    if not values["account"] or not values["access_key"] or not values["secret"]:
        raise R2NotConfigured(
            "Chybí R2_ACCOUNT_ID, R2_ACCESS_KEY_ID nebo R2_SECRET_ACCESS_KEY. Neon se nevolá."
        )
    if not _IDENT.match(values["bucket"].replace("-", "_")) and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", values["bucket"]):
        raise R2Error("Název bucketu R2 je neplatný.")
    return values


def configured() -> bool:
    try:
        settings()
    except R2NotConfigured:
        return False
    return True


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, day: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), day)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _encode_key(key: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in key.split("/"))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request(method: str, key: str, *, body: bytes | None = None, body_path: Path | None = None,
             payload_hash: str | None = None, query: dict[str, str] | None = None,
             now: dt.datetime | None = None, dest: Path | None = None) -> tuple[int, bytes]:
    cfg = settings()
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    if payload_hash is None:
        payload_hash = hashlib.sha256(body or b"").hexdigest()
    host = f"{cfg['account']}.r2.cloudflarestorage.com"
    canonical_uri = "/" + cfg["bucket"] + ("/" + _encode_key(key) if key else "")
    items = sorted((query or {}).items())
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}" for k, v in items
    )
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical = "\n".join([method, canonical_uri, canonical_query, canonical_headers, signed, payload_hash])
    scope = f"{day}/auto/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(_signing_key(cfg["secret"], day, "auto", "s3"), to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={cfg['access_key']}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    url = f"https://{host}{canonical_uri}"
    if canonical_query:
        url += "?" + canonical_query
    data = None
    handle = None
    if method not in {"GET", "HEAD"}:
        if body_path is not None:
            handle = body_path.open("rb")
            data = handle
        else:
            data = body if body is not None else b""
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Host", host)
    request.add_header("x-amz-content-sha256", payload_hash)
    request.add_header("x-amz-date", amz_date)
    request.add_header("Authorization", authorization)
    if body_path is not None:
        request.add_header("Content-Length", str(body_path.stat().st_size))
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if dest is None:
                return response.status, response.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as out:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    out.write(chunk)
            return response.status, b""
    except urllib.error.HTTPError as exc:
        detail = exc.read()
        if exc.code == 404:
            raise R2Missing(key) from None
        text = detail.decode("utf-8", "replace")
        text = re.sub(r"(?i)(authorization|signature|credential|secret)[=: ]+\S+", r"\1=…", text)
        raise R2Error(f"R2 {method} {key or '/'} vrátilo {exc.code}: {text[:300]}") from None
    finally:
        if handle is not None:
            handle.close()


def download(key: str, path: Path) -> None:
    status, _ = _request("GET", key, dest=path)
    if status != 200:
        raise R2Error(f"R2 GET {key} vrátilo {status}.")


def _is_sqlite_file(path: Path) -> bool:
    return path.suffix.lower() in {".db", ".sqlite"}


def upload(path: Path, key: str) -> None:
    # Značka migration-complete je text. Checkpoint na ní spadne: file is not a database.
    if _is_sqlite_file(path):
        _checkpoint(path)
    status, _ = _request("PUT", key, body_path=path, payload_hash=_hash_file(path))
    if status not in (200, 201):
        raise R2Error(f"R2 PUT {key} vrátilo {status}.")


def exists(key: str) -> bool:
    try:
        _request("GET", key)
    except R2Missing:
        return False
    return True


def list_keys(prefix: str) -> list[str]:
    keys: list[str] = []
    token = ""
    while True:
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        status, body = _request("GET", "", query=query)
        if status != 200:
            raise R2Error(f"Výpis R2 vrátil {status}.")
        root = ET.fromstring(body)
        keys.extend(_text(node) for node in root.iter() if _local(node) == "Key" and _text(node))
        truncated = next(((_text(node) or "").lower() for node in root.iter() if _local(node) == "IsTruncated"), "false")
        token = next((_text(node) or "" for node in root.iter() if _local(node) == "NextContinuationToken"), "")
        if truncated != "true" or not token:
            return keys


def delete(key: str) -> None:
    status, _ = _request("DELETE", key)
    if status not in (200, 204):
        raise R2Error(f"R2 DELETE {key} vrátilo {status}.")


def _local(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _text(node: ET.Element) -> str:
    return (node.text or "").strip()


def _checkpoint(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def table_counts(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        names = [
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        for name in names:
            if not _IDENT.match(name):
                continue
            counts[name] = int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        return counts
    finally:
        con.close()


def integrity_ok(path: Path) -> bool:
    con = sqlite3.connect(path)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    finally:
        con.close()


def sharp_drops(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Vrátí popis tabulek, jejichž počet řádků spadl pod 80 % (od 50 řádků)."""
    problems = []
    for name, old in sorted(before.items()):
        new = after.get(name, 0)
        if old >= SHARP_FLOOR and new < int(old * SHARP_RATIO):
            problems.append(f"{name}: {old} -> {new}")
    return problems


def write_counts(path: Path, counts: dict[str, int]) -> None:
    lines = [f"{name} {count}" for name, count in sorted(counts.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_counts(path: Path) -> dict[str, int]:
    counts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, count = line.partition(" ")
        if name and count.isdigit():
            counts[name] = int(count)
    return counts


def backup_key(day: dt.date | None = None) -> str:
    day = day or dt.datetime.now(PRAGUE).date()
    return f"{BACKUP_PREFIX}{day.isoformat()}.db"


def prune_backups(today: dt.date | None = None, keep_days: int = BACKUP_DAYS) -> list[str]:
    today = today or dt.datetime.now(PRAGUE).date()
    removed = []
    for key in list_keys(BACKUP_PREFIX):
        stamp = key.removeprefix(BACKUP_PREFIX).removesuffix(".db")
        try:
            day = dt.date.fromisoformat(stamp)
        except ValueError:
            continue
        if (today - day).days > keep_days:
            delete(key)
            removed.append(key)
    return removed


def publish(path: Path, before: dict[str, int]) -> dict[str, int]:
    if not integrity_ok(path):
        raise R2Error("SQLite integrity_check selhal, soubor se do R2 nenahrál.")
    after = table_counts(path)
    problems = sharp_drops(before, after)
    if problems:
        raise R2Error("Počty řádků prudce klesly, soubor se do R2 nenahrál: " + "; ".join(problems))
    upload(path, CANONICAL_KEY)
    upload(path, backup_key())
    prune_backups()
    return after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stáhne nebo nahraje lovceny.db v R2.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    down = sub.add_parser("download")
    down.add_argument("--out", required=True)
    down.add_argument("--counts", required=True)
    down.add_argument("--key", default=CANONICAL_KEY)
    pub = sub.add_parser("publish")
    pub.add_argument("--path", required=True)
    pub.add_argument("--before", required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "download":
            dest = Path(args.out)
            download(args.key, dest)
            write_counts(Path(args.counts), table_counts(dest))
            return 0
        after = publish(Path(args.path), read_counts(Path(args.before)))
        print("R2: " + ", ".join(f"{name} {count}" for name, count in sorted(after.items())))
        return 0
    except R2Missing:
        print(f"V R2 chybí objekt. Neon se nevolá a prázdná databáze se nenahrává.")
        return 2
    except R2NotConfigured as exc:
        print(exc)
        return 3
    except R2Error as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
