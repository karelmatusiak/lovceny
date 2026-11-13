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


def test_pull_request_static_build_does_not_receive_the_prod_database():
    pr_step = STATIC.split("Vygenerovat web ze snapshotu", 1)[1].split("- name:", 1)[0]
    neon_step = STATIC.split("Vygenerovat web z Neon", 1)[1].split("- name:", 1)[0]
    assert "github.event_name == 'pull_request'" in STATIC
    assert "secrets.DATABASE_URL" not in pr_step
    assert "sqlite:///snapshot/lovceny.sqlite" in pr_step
    assert "github.event_name != 'pull_request'" in neon_step
    assert "secrets.DATABASE_URL" in neon_step
    assert "Snapshot lovceny-snapshot" in STATIC or "lovceny-snapshot" in STATIC
    assert 'cron: "30 6 * * *"' in STATIC
