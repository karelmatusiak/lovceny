"""SQLite v R2: jeden SELECT na tabulku, kvóta se nezkouší znovu, prudký pád se nenahraje."""
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from lovceny.db import Base
from lovceny.migrate_neon import NeonQuotaExceeded, copy_all, is_quota_error, public_error, transfer
from lovceny.models import Offer, Product
from lovceny import r2store


def _source(tmp_path: Path):
    path = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    product = Product(name="Samsung Galaxy A55", slug="samsung-galaxy-a55")
    db.add(product)
    db.flush()
    db.add(Offer(product=product, shop="alza", url="https://www.alza.cz/a55.htm", price=8990, in_stock=True, status="ok"))
    db.commit()
    db.close()
    return engine


def test_copy_reads_each_table_once_and_checks_the_sqlite_count(tmp_path):
    source = _source(tmp_path)
    selects: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(source, "before_cursor_execute", capture)
    dest = tmp_path / "lovceny.db"
    try:
        with source.connect() as conn:
            counts = copy_all(conn, create_engine(f"sqlite:///{dest}"))
    finally:
        event.remove(source, "before_cursor_execute", capture)
        source.dispose()
    assert counts["products"] == 1
    assert counts["offers"] == 1
    product_selects = [sql for sql in selects if "FROM products" in sql or 'FROM "products"' in sql]
    offer_selects = [sql for sql in selects if "FROM offers" in sql or 'FROM "offers"' in sql]
    assert len(product_selects) == 1
    assert len(offer_selects) == 1
    check = sqlite3.connect(dest)
    try:
        assert check.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
        assert check.execute("SELECT name FROM products").fetchone()[0] == "Samsung Galaxy A55"
    finally:
        check.close()


def test_quota_stops_after_one_attempt(tmp_path, monkeypatch):
    calls = {"connect": 0}

    class Boom:
        def connect(self):
            calls["connect"] += 1
            raise RuntimeError("Your project has exceeded the data transfer quota")

        def dispose(self):
            pass

    real = create_engine

    def fake(url, *args, **kwargs):
        if str(url).startswith("postgresql"):
            return Boom()
        return real(url, *args, **kwargs)

    monkeypatch.setattr("lovceny.migrate_neon.create_engine", fake)
    try:
        transfer("postgresql+psycopg://lovceny:secret@db.example/lovceny", tmp_path / "out.db")
    except NeonQuotaExceeded as exc:
        assert "secret" not in str(exc)
        assert "postgresql://" not in str(exc)
    else:
        raise AssertionError("kvóta měla přenos zastavit")
    assert calls["connect"] == 1
    assert is_quota_error(RuntimeError("network transfer quota"))
    assert "password=…" in public_error(RuntimeError("password=hunter2"))


def test_sharp_drop_blocks_the_upload(tmp_path):
    before = {"products": 100, "offers": 40, "notes": 10}
    after = {"products": 79, "offers": 40}
    problems = r2store.sharp_drops(before, after)
    assert problems == ["products: 100 -> 79"]
    assert r2store.sharp_drops({"products": 100}, {"products": 80}) == []
    path = tmp_path / "ok.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("INSERT INTO products (name) VALUES ('a')")
    con.commit()
    con.close()
    assert r2store.integrity_ok(path)
    counts_path = tmp_path / "counts.txt"
    r2store.write_counts(counts_path, {"products": 1})
    assert r2store.read_counts(counts_path) == {"products": 1}


def test_r2_request_does_not_leak_the_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "super-secret-value")
    monkeypatch.setenv("R2_BUCKET", "lovceny-data")
    captured = {}

    class Resp:
        status = 200

        def read(self, n=-1):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout=0):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(r2store.urllib.request, "urlopen", fake_open)
    dest = tmp_path / "lovceny.db"
    r2store.download("lovceny.db", dest)
    assert captured["url"] == "https://acct.r2.cloudflarestorage.com/lovceny-data/lovceny.db"
    assert "AKIATEST" in captured["auth"]
    assert "super-secret-value" not in captured["auth"]
    assert dest.is_file()


def test_migration_does_not_open_neon_without_r2(tmp_path, monkeypatch, capsys):
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("Neon se neměl otevřít")

    monkeypatch.setattr("lovceny.migrate_neon.transfer", boom)
    code = __import__("lovceny.migrate_neon", fromlist=["main"]).main(["--out", str(tmp_path / "x.db")])
    assert code == 78
    assert "STATUS r2-missing" in capsys.readouterr().out
    assert called["n"] == 0


def test_marker_upload_does_not_checkpoint_a_text_file(tmp_path, monkeypatch):
    seen = {"checkpoint": []}

    def remember(path):
        seen["checkpoint"].append(path)

    monkeypatch.setattr(r2store, "_checkpoint", remember)
    monkeypatch.setattr(r2store, "_request", lambda *args, **kwargs: (200, b""))
    marker = tmp_path / "lovceny.done"
    marker.write_text("products 1\n", encoding="utf-8")
    r2store.upload(marker, "migration-complete")
    assert seen["checkpoint"] == []
    db = tmp_path / "lovceny.db"
    db.write_bytes(b"SQLite format 3\x00")
    r2store.upload(db, "lovceny.db")
    assert seen["checkpoint"] == [db]


def test_workflows_keep_one_writer_and_the_neon_job_can_stop():
    root = Path(__file__).resolve().parents[1]
    fetch = (root / ".github/workflows/fetch.yml").read_text(encoding="utf-8")
    migrate = (root / ".github/workflows/migrate-neon.yml").read_text(encoding="utf-8")
    seed = (root / ".github/workflows/seed.yml").read_text(encoding="utf-8")
    digest = (root / ".github/workflows/digest.yml").read_text(encoding="utf-8")
    assert "secrets.DATABASE_URL" not in fetch
    assert "secrets.DATABASE_URL" not in seed
    assert "secrets.DATABASE_URL" not in digest
    assert "group: fetch-prices" in seed
    assert "group: fetch-prices" in digest
    assert "STATUS" in (root / "lovceny/migrate_neon.py").read_text(encoding="utf-8")
    assert "migration-complete" in (root / "lovceny/r2store.py").read_text(encoding="utf-8")
    assert "pages deploy" not in migrate
