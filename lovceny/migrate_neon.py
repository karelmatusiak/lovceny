"""Jednorázový přenos všech tabulek z Neon Postgres do jednoho SQLite souboru.

Každá tabulka se z Neonu čte jedním SELECT *. Druhý dotaz na stejnou tabulku
se neposílá. Kvóta přenosu se nezkouší znovu: proces skončí a další pokus
nechá až na zítřejší plán workflow.

Do Neonu se vůbec nevolá, dokud není nastavené R2. Jinak by se přenos načetl
a neměl kam uložit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from . import r2store
from .db import Base

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUOTA = (
    "data transfer",
    "transfer quota",
    "network transfer",
    "quota exceeded",
    "exceeded the data",
    "exceeded your",
    "consumption limit",
    "copy buffer",
)
COLUMNS_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""


class NeonQuotaExceeded(RuntimeError):
    pass


def is_quota_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _QUOTA)


def public_error(exc: BaseException) -> str:
    text_value = str(exc)
    text_value = re.sub(r"postgres(?:ql)?(?:\+\w+)?://\S+", "postgresql://…", text_value)
    text_value = re.sub(r"(?i)(password|secret|token)=\S+", r"\1=…", text_value)
    return text_value[:400]


def _sqlite_type(data_type: str) -> str:
    kind = (data_type or "").lower()
    if kind in {"smallint", "integer", "bigint"} or kind.endswith("int"):
        return "INTEGER"
    if "bool" in kind:
        return "INTEGER"
    if kind in {"bytea"} or "blob" in kind:
        return "BLOB"
    if kind in {"numeric", "decimal", "real", "double precision"} or "double" in kind:
        return "NUMERIC"
    return "TEXT"


def _quote(name: str) -> str:
    if not _IDENT.match(name):
        raise RuntimeError(f"Nepovolený název tabulky nebo sloupce: {name!r}")
    return f'"{name}"'


def _cell(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _columns(source: Connection) -> dict[str, list[tuple[str, str]]]:
    """Jeden katalogový dotaz. U SQLite (test) čte sqlite_master, ne information_schema."""
    found: dict[str, list[tuple[str, str]]] = {}
    if source.dialect.name == "sqlite":
        names = list(source.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).scalars())
        for name in names:
            if not _IDENT.match(name):
                continue
            rows = source.exec_driver_sql(f"PRAGMA table_info({_quote(name)})").all()
            found[name] = [(row[1], row[2] or "TEXT") for row in rows]
        return found
    for table_name, column_name, data_type in source.execute(text(COLUMNS_SQL)):
        if _IDENT.match(table_name) and _IDENT.match(column_name):
            found.setdefault(table_name, []).append((column_name, data_type))
    return found


def copy_all(source: Connection, dest: Engine) -> dict[str, int]:
    """Jeden SELECT na tabulku. Počty se ověří až v cílovém SQLite."""
    from . import models  # noqa: F401

    columns = _columns(source)
    Base.metadata.create_all(dest)
    known = {table.name: table for table in Base.metadata.sorted_tables}
    counts: dict[str, int] = {}
    with dest.begin() as out:
        out.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table_name, cols in columns.items():
            names = [name for name, _ in cols]
            if table_name in known:
                dest_cols = {column.name for column in known[table_name].columns}
                names = [name for name in names if name in dest_cols]
            else:
                defs = ", ".join(f"{_quote(name)} {_sqlite_type(kind)}" for name, kind in cols)
                out.exec_driver_sql(f"CREATE TABLE IF NOT EXISTS {_quote(table_name)} ({defs})")
            if not names:
                counts[table_name] = 0
                continue
            selected = ", ".join(_quote(name) for name in names)
            result = source.execution_options(stream_results=True, yield_per=1000).execute(
                text(f"SELECT {selected} FROM {_quote(table_name)}")
            )
            seen = 0
            batch: list[tuple] = []
            placeholders = ", ".join("?" for _ in names)
            insert_sql = f"INSERT INTO {_quote(table_name)} ({selected}) VALUES ({placeholders})"
            for row in result:
                batch.append(tuple(_cell(row._mapping[name]) for name in names))
                seen += 1
                if len(batch) >= 1000:
                    out.exec_driver_sql(insert_sql, batch)
                    batch.clear()
            if batch:
                out.exec_driver_sql(insert_sql, batch)
            counts[table_name] = seen
        out.exec_driver_sql("PRAGMA foreign_keys=ON")
    _verify_local(dest, counts)
    return counts


def _verify_local(dest: Engine, expected: dict[str, int]) -> None:
    with dest.connect() as conn:
        for name, seen in expected.items():
            stored = int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {_quote(name)}").scalar_one())
            if stored != seen:
                raise RuntimeError(f"Ověření počtu selhalo u {name}: načteno {seen}, v SQLite {stored}.")


def transfer(source_url: str, dest: Path) -> dict[str, int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    engine = create_engine(source_url, pool_pre_ping=False, future=True)
    target = create_engine(f"sqlite:///{dest}", future=True)
    try:
        try:
            with engine.connect() as source:
                return copy_all(source, target)
        except Exception as exc:
            if is_quota_error(exc):
                raise NeonQuotaExceeded(public_error(exc)) from None
            raise
    finally:
        engine.dispose()
        target.dispose()


def _source_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if not url.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL není Postgres. Přenos z Neonu se nespustil.")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zkopíruje Neon do SQLite a uloží ho do R2.")
    parser.add_argument("--out", default="db/lovceny.db")
    args = parser.parse_args(argv)
    if not r2store.configured():
        print("STATUS r2-missing")
        print("R2 není nastavené. Neon se nevolá, ať se kvóta nespálí nadarmo.")
        return 78
    try:
        if r2store.exists(r2store.DONE_KEY):
            print("STATUS already-done")
            print("Přenos už v R2 je. Neon se znovu nečte.")
            return 0
    except r2store.R2Error as exc:
        print("STATUS r2-error")
        print(exc)
        return 1
    dest = Path(args.out)
    try:
        counts = transfer(_source_url(), dest)
    except NeonQuotaExceeded as exc:
        print("STATUS quota")
        print("Neon odmítl přenos kvůli kvótě. Znovu se to v tomhle běhu nezkouší.")
        print(exc)
        return 75
    except Exception as exc:
        print("STATUS error")
        print(public_error(exc))
        return 1
    if not r2store.integrity_ok(dest):
        print("STATUS error")
        print("SQLite integrity_check po přenosu selhal. Do R2 se nic nenahrálo.")
        return 1
    try:
        r2store.upload(dest, r2store.CANONICAL_KEY)
        r2store.upload(dest, r2store.backup_key())
        r2store.prune_backups()
        summary = "\n".join(f"{name} {count}" for name, count in sorted(counts.items())) + "\n"
        # Značka bez tajemství: další denní běh Neon znovu neotevře.
        marker = dest.with_suffix(".done")
        marker.write_text(summary, encoding="utf-8")
        r2store.upload(marker, r2store.DONE_KEY)
    except r2store.R2Error as exc:
        print("STATUS r2-error")
        print(exc)
        return 1
    print("STATUS ok")
    for name, count in sorted(counts.items()):
        print(f"{name} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
