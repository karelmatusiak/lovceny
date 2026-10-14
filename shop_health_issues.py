#!/usr/bin/env python3
"""GitHub issue pro každý problémový obchod (spouští .github/workflows/fetch.yml po stažení cen).

  python shop_health_issues.py health.json

- problém (většina požadavků v běhu selhala / ceny starší než 2 dny) → založí issue, nebo aktualizuje
  otevřené (komentář jen při změně důvodu, ať nechodí notifikace 3× denně),
- obchod je zase v pořádku → komentář „obnoveno“ a zavření issue,
- chybí health.json (běh spadl dřív) → issue „Stahování cen neproběhlo“; zavře se po dalším úspěšném běhu.
Používá jen vestavěný GITHUB_TOKEN (permissions: issues: write) a REST API. Bez tokenu jen vypíše plán.
"""
import json
import os
import re
import sys

import httpx

LABEL = "stav-obchodu"
MARK = re.compile(r"<!-- lovceny-health:(?P<key>[\w-]+) codes=(?P<codes>[\w,]*) -->")
RUN_KEY = "_beh"
SHORT = {"failing": "obchod blokuje stahování cen", "stale": "ceny nejsou aktuální"}


class GitHub:
    def __init__(self, repo: str, token: str, client: httpx.Client | None = None):
        self.repo = repo
        self.c = client or httpx.Client(base_url="https://api.github.com", timeout=20)
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}

    def _req(self, method, path, **kw):
        r = self.c.request(method, f"/repos/{self.repo}{path}", headers=self.h, **kw)
        if r.status_code >= 400 and not (method == "POST" and path == "/labels" and r.status_code == 422):
            raise RuntimeError(f"GitHub {method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def ensure_label(self):
        self._req("POST", "/labels", json={"name": LABEL, "color": "FF7A45",
                                           "description": "Automatický monitoring obchodů (Lovceny)"})

    def open_issues(self) -> list[dict]:
        return [i for i in self._req("GET", "/issues", params={"labels": LABEL, "state": "open", "per_page": 100})
                if "pull_request" not in i]

    def create(self, title, body):
        return self._req("POST", "/issues", json={"title": title, "body": body, "labels": [LABEL]})

    def update(self, num, **fields):
        return self._req("PATCH", f"/issues/{num}", json=fields)

    def comment(self, num, body):
        return self._req("POST", f"/issues/{num}/comments", json={"body": body})


def run_link() -> str:
    srv, repo, rid = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{srv}/{repo}/actions/runs/{rid}" if srv and repo and rid else ""


def title_for(name: str, codes: list[str]) -> str:
    return f"⚠️ {name}: " + ", ".join(SHORT[c] for c in codes if c in SHORT)


def body_for(slug: str, info: dict, codes: list[str]) -> str:
    lines = [f"Automatický monitoring zjistil problém s obchodem **{info['name']}** (`{slug}`).", ""]
    lines += [f"- {r['text']}" for r in info["reasons"]]
    r = info.get("run")
    if r:
        st = ", ".join(f"HTTP {k}: {v}×" for k, v in sorted((r.get("statuses") or {}).items())) or "–"
        lines += ["", "| Pokusů | OK | Blokováno | Captcha/JS | robots.txt | Chyby |", "|---|---|---|---|---|---|",
                  f"| {r['attempts']} | {r['ok']} | {r['blocked']} | {r['captcha']} | {r['robots']} | {r['errors']} |",
                  "", f"Stavové kódy: {st}"]
        if r.get("last_error"):
            lines.append(f"Poslední chyba: `{r['last_error'][:200]}`")
    lines += ["", f"Sledovaných nabídek: {info.get('offers', 0)}. Poslední úspěšné stažení: {info.get('last_ok') or 'nikdy'}."]
    if run_link():
        lines.append(f"Běh: {run_link()}")
    lines += ["", "Ochranu proti botům **neobcházíme** – řešením je oficiální feed / affiliate export, "
              "nebo obchod dočasně vyřadit v `lovceny/shops/registry.py`. Přehled: `/stav`.",
              "Issue se samo zavře, až bude obchod zase v pořádku.", "",
              f"<!-- lovceny-health:{slug} codes={','.join(codes)} -->"]
    return "\n".join(lines)


def sync(gh: GitHub, health: dict | None) -> list[str]:
    """Vrací seznam provedených akcí (pro log a testy)."""
    gh.ensure_label()
    existing = {}
    for i in gh.open_issues():
        m = MARK.search(i.get("body") or "")
        if m:
            existing[m["key"]] = (i, [c for c in m["codes"].split(",") if c])
    actions = []
    if health is None:
        body = ("Stahování cen skončilo dřív, než vzniklo vyhodnocení obchodů (spadly testy, instalace "
                "nebo připojení k databázi). Podívej se do logu běhu." + (f"\n\nBěh: {run_link()}" if run_link() else "")
                + f"\n\n<!-- lovceny-health:{RUN_KEY} codes=run -->")
        if RUN_KEY in existing:
            gh.update(existing[RUN_KEY][0]["number"], body=body)
            actions.append(f"update {RUN_KEY}")
        else:
            gh.create("⚠️ Stahování cen neproběhlo", body)
            actions.append(f"create {RUN_KEY}")
        return actions
    if RUN_KEY in existing:
        num = existing[RUN_KEY][0]["number"]
        gh.comment(num, "✅ Stahování cen zase proběhlo.")
        gh.update(num, state="closed", state_reason="completed")
        actions.append(f"close {RUN_KEY}")
    for slug, info in sorted(health.get("shops", {}).items()):
        if info["ok"]:
            if slug in existing:
                num = existing[slug][0]["number"]
                gh.comment(num, f"✅ Obnoveno: {info['name']} zase funguje, ceny jsou aktuální."
                           + (f"\n\nBěh: {run_link()}" if run_link() else ""))
                gh.update(num, state="closed", state_reason="completed")
                actions.append(f"close {slug}")
            continue
        codes = [r["code"] for r in info["reasons"]]
        title, body = title_for(info["name"], codes), body_for(slug, info, codes)
        if slug in existing:
            issue, old = existing[slug]
            gh.update(issue["number"], title=title, body=body)
            if sorted(old) != sorted(codes):
                gh.comment(issue["number"], "Změna stavu: " + "; ".join(r["text"] for r in info["reasons"]))
                actions.append(f"update+comment {slug}")
            else:
                actions.append(f"update {slug}")
        else:
            gh.create(title, body)
            actions.append(f"create {slug}")
    return actions


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else "health.json"
    try:
        with open(path, encoding="utf-8") as f:
            health = json.load(f)
    except (OSError, json.JSONDecodeError):
        health = None
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        bad = None if health is None else {k: v["reasons"] for k, v in health["shops"].items() if not v["ok"]}
        print("GITHUB_TOKEN/GITHUB_REPOSITORY chybí – jen náhled:", "běh selhal" if health is None else (bad or "vše OK"))
        return 0
    for a in sync(GitHub(repo, token), health):
        print(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
