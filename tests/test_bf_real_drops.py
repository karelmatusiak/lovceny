"""Přehled skutečných slev na /black-friday: jen naše historie, ne tvrzení obchodu."""
from datetime import datetime, timedelta, timezone

from lovceny import blackfriday as bf
from lovceny.verdict import HistoryDrop, history_drop


class _Pt:
    def __init__(self, price, captured_at):
        self.price = price
        self.captured_at = captured_at


def _series(now, prices):
    """prices od nejstaršího dne; poslední hodnota je dnešek."""
    n = len(prices)
    return [_Pt(p, now - timedelta(days=n - 1 - i)) for i, p in enumerate(prices)]


def test_history_drop_needs_seven_prior_days_and_ignores_shop_claims():
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    assert history_drop(_series(now, [10000] * 6 + [8000]), 8000, now=now) is None
    flat = history_drop(_series(now, [5000] * 10), 5000, now=now)
    assert flat is None  # i kdyby obchod uváděl původní cenu 9 000
    drop = history_drop(_series(now, [10000] * 10 + [8000]), 8000, now=now)
    assert isinstance(drop, HistoryDrop)
    assert drop.days >= 7
    assert drop.drop_czk == 2000 and round(drop.drop_pct) == 20
    assert drop.at_min30 and drop.min30 == 10000
    # pod mediánem, ale ne pod minimem okna
    softer = history_drop(_series(now, [7000] + [10000] * 9 + [9000]), 9000, now=now)
    assert softer is not None and not softer.at_min30
    assert softer.min30 == 7000 and round(softer.drop_pct) == 10


def _stock(offer, when=None):
    offer.in_stock = True
    offer.status = "ok"
    offer.last_checked = when or datetime.now(timezone.utc)


def test_overview_groups_real_drops_and_skips_claims_oos_accessories(client_db):
    client, db, make = client_db
    now = datetime.now(timezone.utc)
    real = make([10000] * 10 + [8000], name="BF real telefon", category="telefony")
    _stock(real.offers[0], now)
    claimed = make([5000] * 10, name="BF jen puvodni cena", category="telefony", original=9000)
    _stock(claimed.offers[0], now)
    oos = make([10000] * 10 + [7000], name="BF vyprodany telefon", category="telefony")
    oos.offers[0].in_stock = False
    oos.offers[0].status = "ok"
    oos.offers[0].last_checked = now
    accessory = make([10000] * 10 + [6000], name="Kryt na iPhone 15", category="telefony")
    _stock(accessory.offers[0], now)
    short = make([10000] * 4 + [7000], name="BF kratka historie", category="televize")
    _stock(short.offers[0], now)
    db.commit()
    bf.clear_cache()

    drops = {d.product.id: d for d in bf.listing(db).drops}
    assert real.id in drops and drops[real.id].at_min30 and round(drops[real.id].drop_pct) == 20
    assert claimed.id not in drops and oos.id not in drops and accessory.id not in drops and short.id not in drops

    groups = dict((c.slug, items) for c, items in bf.group_by_category(list(drops.values())))
    assert [d.product.id for d in groups["telefony"]] == [real.id]
    assert groups["notebooky"] == []

    html = client.get("/black-friday").text
    block = html.split('id="prehled-skutecnych-slev"', 1)[1].split('id="bf-low-h"', 1)[0]
    phone = block.split("Mobilní telefony", 1)[1].split("bf-real-cat", 1)[0]
    assert f'href="/produkt/{real.id}-{real.slug}"' in phone
    assert "BF real telefon" in phone and "nejnižší za 30 dní" in phone
    assert "−20" in phone and "%" in phone
    assert 'rel="nofollow sponsored noopener"' in phone and "Do obchodu" in phone
    assert "BF jen puvodni cena" not in block
    assert "BF vyprodany telefon" not in block and "Kryt na iPhone 15" not in block
    notebooks = block.split("Notebooky", 1)[1].split("bf-real-cat", 1)[0]
    assert "žádný skutečný pokles" in notebooks
    assert "Do obchodu" not in notebooks


def test_real_drops_reuse_listing_cache(client_db, monkeypatch):
    from lovceny import deals
    _, db, make = client_db
    p = make([10000] * 10 + [8000], name="BF cache drop", category="telefony")
    _stock(p.offers[0])
    db.commit()
    bf.clear_cache()
    calls = {"n": 0}
    real = deals.load_points_for_offers

    def wrap(db, ids):
        calls["n"] += 1
        return real(db, ids)

    monkeypatch.setattr(deals, "load_points_for_offers", wrap)
    a = bf.listing(db)
    b = bf.listing(db)
    assert calls["n"] == 1
    assert [(d.product.id, round(d.drop_czk), d.at_min30) for d in a.drops] == [
        (d.product.id, round(d.drop_czk), d.at_min30) for d in b.drops]
    assert a.rows is not None and b.drops and b.drops[0].offer.in_stock is True
