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


def test_static_build_reads_the_snapshot_and_never_the_prod_database():
    assert "secrets.DATABASE_URL" not in STATIC
    assert "sqlite:///snapshot/lovceny.sqlite" in STATIC
    assert "lovceny-snapshot" in STATIC
    assert "Neon se nevolá" in STATIC
    assert 'cron: "30 6 * * *"' in STATIC
    snapshot = (ROOT / ".github/workflows/snapshot.yml").read_text(encoding="utf-8")
    assert "pull_request" not in snapshot
    assert 'cron: "15 6 * * *"' in snapshot
    assert "name: lovceny-snapshot" in snapshot
    assert "secrets.DATABASE_URL" in snapshot
    assert "github.ref == 'refs/heads/main'" in snapshot
