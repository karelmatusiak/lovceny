import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")

import pytest

from lovceny.shops import SHOPS as _SHOPS
ORIGINAL_SEARCHERS = {k: s.searcher for k, s in _SHOPS.items()}


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    """Testy nesmí volat skutečné obchody: všechny searchery vypneme, test si zapne, co potřebuje."""
    from lovceny.shops import SHOPS, search
    for s in SHOPS.values():
        monkeypatch.setattr(s, "searcher", None)
    search.clear_cache()
    from lovceny import ai
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # testy nikdy nevolají skutečné Gemini
    ai.client.clear()
    ai.client.calls_today = 0
    yield
    search.clear_cache()
