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
    from lovceny import verify, seasons
    verify.clear_cache()
    seasons.clear_cache()
    yield
    search.clear_cache()
    verify.clear_cache()
    seasons.clear_cache()


@pytest.fixture
def client_db():
    """(TestClient, session, make(prices, name=…, shop=…, category=…)) – produkt s denní historií cen."""
    import itertools
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    from lovceny.main import app
    from lovceny.db import SessionLocal, init_db
    from lovceny.models import Product, Offer, PricePoint
    from lovceny.util import slugify
    init_db()
    db = SessionLocal()
    counter = itertools.count()

    def make(prices, name=None, shop="mironet", category=None, original=None):
        i = next(counter)
        name = name or f"Testovací výrobek {datetime.now().timestamp()}-{i}"
        p = Product(name=name, slug=slugify(name), category_slug=category)
        o = Offer(product=p, shop=shop, url=f"https://www.mironet.cz/t-{datetime.now().timestamp()}-{i}",
                  status="ok" if prices else "pending", price=prices[-1] if prices else None,
                  original_price=original, in_stock=True)
        now = datetime.now(timezone.utc)
        for d, pr in enumerate(prices):
            o.points.append(PricePoint(price=pr, captured_at=now - timedelta(days=len(prices) - 1 - d)))
        db.add(p)
        db.commit()
        return p
    yield TestClient(app), db, make
    db.close()
