"""AI hledání s mockovaným Gemini – žádná síť, žádný skutečný klíč."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lovceny import ai
from lovceny.db import SessionLocal, init_db
from lovceny.service import smart_offers
from lovceny.shops import SHOPS
from lovceny.shops.search import SearchHit


class FakeResp:
    def __init__(self, status, data=None):
        self.status_code, self._d = status, data

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(self._d)}]}}]}


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("LOVCENY_FEATURE_AI", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []
    answers = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "body": json, "timeout": timeout})
        kind = "rank" if "Nabídky:" in json["contents"][0]["parts"][0]["text"] else "intent"
        a = answers.get(kind)
        if isinstance(a, Exception):
            raise a
        if isinstance(a, int):
            return FakeResp(a)
        return FakeResp(200, a)

    monkeypatch.setattr(ai.httpx, "post", fake_post)
    return calls, answers


@pytest.fixture
def shops(monkeypatch):
    data = {
        "pracka": [SearchHit("mironet", "Pračka Bosch WAN28 tichá", 9490, "https://www.mironet.cz/a"),
                   SearchHit("mironet", "Pračka Samsung WW90", 12990, "https://www.mironet.cz/b"),
                   SearchHit("mironet", "Hadice k pračce 2 m", 290, "https://www.mironet.cz/c"),
                   SearchHit("mironet", "Pračka LG F4 inverter", 8990, "https://www.mironet.cz/d")],
    }
    seen = []

    def searcher(q):
        seen.append(q)
        return data["pracka"] if "pra" in q.lower() else []
    monkeypatch.setattr(SHOPS["mironet"], "searcher", searcher)
    return seen


def test_should_use_ai_skips_plain_short_queries():
    assert not ai.should_use_ai("garmin")
    assert not ai.should_use_ai("samsung galaxy s24")
    assert not ai.should_use_ai("sony wh1000xm5")
    assert not ai.should_use_ai("samsung galaxy")
    assert ai.should_use_ai("tichá pračka do 10 tisíc")
    assert ai.should_use_ai("sluchátka na běhání")


def test_rule_intent_parses_prices_without_ai():
    i = ai.rule_intent("pračka do 10 tisíc")
    assert i.max_price == 10000 and i.queries == ["pračka"] and i.source == "rules"
    i = ai.rule_intent("televize od 5 000 Kč do 12000 Kč")
    assert i.min_price == 5000 and i.max_price == 12000 and i.queries == ["televize"]


def test_scrub_removes_personal_data():
    assert "@" not in ai.scrub("pračka pro jan.novak@email.cz")
    assert "777" not in ai.scrub("lednice tel 777 123 456")


def test_no_key_falls_back_without_calls(monkeypatch, shops):
    called = []
    monkeypatch.setattr(ai.httpx, "post", lambda *a, **k: called.append(1))
    init_db()
    with SessionLocal() as db:
        r = smart_offers(db, "pračka do 10 tisíc")
    assert not called and not r["ai_used"] and r["summary"] == ""
    assert [h.price for h, _ in r["offers"]] == [8990, 9490]  # pravidla: cena ≤ 10 000, hadice vyřazena


def test_ai_intent_rank_and_summary(gemini, shops):
    calls, answers = gemini
    answers["intent"] = {"queries": ["pračka", "pračka tichá"], "max_price": 10000, "min_price": None,
                         "must_words": ["pračka"], "exclude_words": []}
    answers["rank"] = {"keep": [0, 1], "summary": "Našli jsme 2 tiché pračky, nejlevnější za 8 990 Kč."}
    init_db()
    with SessionLocal() as db:
        r = smart_offers(db, "tichá pračka do 10 tisíc, ať neruší")
    assert r["ai_used"] and r["intent"].source == "ai"
    assert set(shops) == {"pračka", "pračka tichá"}
    assert [h.price for h, _ in r["offers"]] == [8990, 9490]
    assert r["summary"].startswith("Našli jsme")
    assert calls[0]["timeout"] == 4.0 and calls[0]["headers"]["x-goog-api-key"] == "test-key"
    rank_text = calls[1]["body"]["contents"][0]["parts"][0]["text"]
    assert "Pračka LG F4 inverter | 8990 Kč | Mironet" in rank_text
    # cache: druhé stejné hledání už Gemini nevolá
    with SessionLocal() as db:
        smart_offers(db, "tichá pračka do 10 tisíc, ať neruší")
    assert len(calls) == 2


def test_ai_drops_irrelevant_but_keeps_price_order(gemini, shops):
    _, answers = gemini
    answers["intent"] = {"queries": ["pračka"]}
    answers["rank"] = {"keep": [2, 0], "summary": ""}  # pořadí od AI ignorujeme
    init_db()
    with SessionLocal() as db:
        r = smart_offers(db, "nejlepší pračka pro rodinu")
    assert [h.price for h, _ in r["offers"]] == [8990, 12990]


@pytest.mark.parametrize("failure", [429, 500, httpx.ReadTimeout("t"), "garbage"])
def test_errors_fall_back(gemini, shops, failure):
    _, answers = gemini
    answers["intent"] = failure if failure != "garbage" else None
    answers["rank"] = failure if failure != "garbage" else {"nope": 1}
    init_db()
    with SessionLocal() as db:
        r = smart_offers(db, "pračka do 10 tisíc")
    assert not r["ai_used"] and r["intent"].max_price == 10000
    assert [h.price for h, _ in r["offers"]] == [8990, 9490]


def test_429_triggers_cooldown(gemini):
    calls, answers = gemini
    answers["intent"] = 429
    assert ai.understand("levná televize do 8 tisíc") is None
    assert ai.understand("jiná levná televize do 9 tisíc") is None
    assert len(calls) == 1  # po 429 už nevoláme


def test_daily_cap(gemini, monkeypatch):
    calls, answers = gemini
    monkeypatch.setenv("GEMINI_DAILY_CAP", "2")
    answers["intent"] = {"queries": ["x"]}
    for i in range(4):
        ai.understand(f"levná televize do {i + 5} tisíc")
    assert len(calls) == 2


def test_plain_query_does_not_call_ai(gemini, shops):
    calls, _ = gemini
    init_db()
    with SessionLocal() as db:
        smart_offers(db, "pračka")
    assert calls == []


def test_ui_shows_hint_and_summary(gemini, shops):
    from lovceny.main import app
    _, answers = gemini
    answers["intent"] = {"queries": ["pračka"], "max_price": 10000}
    answers["rank"] = {"keep": [0], "summary": "Nejlevnější tichá pračka <b>stojí</b> 8 990 Kč."}
    c = TestClient(app)
    assert "Rozumíme i běžné řeči" in c.get("/").text
    html = c.get("/hledat/zive", params={"q": "tichá pračka do 10 tisíc"}).text
    assert "✨" in html and "&lt;b&gt;stojí" in html and "do 10 000" in html.replace("\xa0", " ")
    assert "Pračka LG F4" in html and "Samsung" not in html


def test_too_narrow_ai_terms_fall_back_to_product_type(gemini, shops):
    _, answers = gemini
    answers["intent"] = {"queries": ["xyz úzký výraz"], "must_words": ["pračka"]}
    answers["rank"] = 500
    init_db()
    with SessionLocal() as db:
        r = smart_offers(db, "nějaká speciální věc na praní prádla")
    assert "pračka" in shops and len(r["offers"]) == 3
