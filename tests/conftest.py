import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# Jedna lokální SQLite na běh pytestu. Nastaví se dřív, než se naimportuje lovceny.db,
# takže engine nemůže mířit na Neon ani na ./lovceny.db.
_TEST_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="lovceny-pytest-"), "test.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _TEST_DB_PATH
# eHUB default AID by zapnul AFFILIATE_ON ve všech testech – vypneme, dokud test výslovně nepovolí
os.environ["EHUB_AID"] = ""
# Dognet chid by stejně zapnul AFFILIATE_ON – výchozí kód v dognet.py, prázdné = vypnuto.
os.environ["DOGNET_CHID"] = ""

import pytest

from lovceny.db import DATABASE_URL, Base, engine

if engine.dialect.name != "sqlite" or engine.url.database != _TEST_DB_PATH:
    raise RuntimeError("pytest refused to start: tests may only use the temporary sqlite file")
if "neon.tech" in DATABASE_URL or DATABASE_URL.startswith("postgres"):
    raise RuntimeError("pytest refused to start against a hosted database")

from lovceny.shops import SHOPS as _SHOPS
ORIGINAL_SEARCHERS = {k: s.searcher for k, s in _SHOPS.items()}


def _empty_test_tables() -> None:
    """Každý test začíná s prázdnými tabulkami. Smazání jde jen do sqlite enginu z tohoto souboru."""
    if engine.dialect.name != "sqlite" or engine.url.database != _TEST_DB_PATH:
        raise RuntimeError("refusing to delete rows from a database that is not the pytest sqlite file")
    from lovceny.db import init_db
    init_db()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.fixture(autouse=True)
def _isolate_test_db():
    _empty_test_tables()
    yield


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
    from lovceny import verify, seasons, hero, catcache, deals
    from lovceny.main import clear_display_names_cache
    verify.clear_cache()
    seasons.clear_cache()
    hero.clear_cache()
    catcache.clear()
    deals.clear_cache()
    clear_display_names_cache()
    yield
    search.clear_cache()
    verify.clear_cache()
    seasons.clear_cache()
    hero.clear_cache()
    catcache.clear()
    deals.clear_cache()
    clear_display_names_cache()


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
