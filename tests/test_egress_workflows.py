"""Egress: pull request nesmí dostat produkční databázi a fetch nemá druhý budík."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FETCH = (ROOT / ".github/workflows/fetch.yml").read_text(encoding="utf-8")
STATIC = (ROOT / ".github/workflows/static.yml").read_text(encoding="utf-8")


def test_fetch_has_one_clock_and_no_chained_static_build():
    assert "schedule:" not in FETCH
    assert "cron:" not in FETCH
    assert "client_payload.force" not in FETCH.split("if:", 1)[1].split("\n", 1)[0]
    assert "uses: ./.github/workflows/static.yml" not in FETCH
    assert "repository_dispatch:" in FETCH
    assert "fetch-prices" in FETCH


def test_static_build_reads_r2_and_never_the_prod_database():
    assert "secrets.DATABASE_URL" not in STATIC
    assert "secrets.DATABASE_URL" not in FETCH
    assert "sqlite:///db/lovceny.db" in FETCH
    assert "lovceny.r2store publish" in FETCH
    assert "group: fetch-prices" in FETCH
    assert "needs: fetch" in FETCH
    assert "timeout-minutes: 15" in FETCH
    assert "sqlite:///snapshot/lovceny.sqlite" in STATIC
    assert "lovceny.r2store download" in STATIC
    assert "Neon se nevolá" in STATIC
    assert "schedule:" not in STATIC
    snapshot = (ROOT / ".github/workflows/snapshot.yml").read_text(encoding="utf-8")
    migrate = (ROOT / ".github/workflows/migrate-neon.yml").read_text(encoding="utf-8")
    assert "pull_request" not in snapshot
    assert "schedule:" not in snapshot
    assert "secrets.DATABASE_URL" not in snapshot
    assert "Neon se nevolá" in snapshot
    assert "secrets.DATABASE_URL" in migrate
    assert 'cron: "0 4 * * *"' in migrate
    assert "workflow disable" in migrate
    assert "pages deploy" not in migrate
