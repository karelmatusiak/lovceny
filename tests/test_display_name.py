"""Zobrazovací short_name – bez spec výčtů, unikátní."""
import html
import re
from lovceny.names import short_name, uniquify_short_names


def test_short_tv_and_robot():
    assert short_name(
        'Samsung QE43LS03FAUXXH 43" TV QLED, 4K (3 840 × 2 160), 50 Hz, Tizen Smart TV, HDMI, USB, Wi-Fi, BT'
    ) == 'Samsung QE43LS03FAUXXH 43" QLED'
    assert short_name(
        'Roborock Q7 BF černá / Robotický vysavač / 40 W / vysávání a mopování / 8000 Pa / 3200 mAh / 65 dB'
    ) == 'Roborock Q7 BF černá'


def test_short_notebook_keeps_caps():
    s = short_name(
        'Apple MacBook Air 15" M3 2024 CZ Silver / Apple M3 4.0GHz / 16GB / 512GB / Apple 10-jádrová iGPu / macOS'
    )
    assert 'MacBook Air 15"' in s and '16GB' in s and '512GB' in s
    assert 'GHz' not in s and 'iGPu' not in s


def test_uniquify_watch_bands():
    names = [
        'Apple Watch Series 11 GPS + Cellular 46mm zlaté titanové pouzdro se zlatým milánským tahem - M/L',
        'Apple Watch Series 11 GPS + Cellular 46mm zlaté titanové pouzdro se zlatým milánským tahem - S/M',
    ]
    a, b = uniquify_short_names(names)
    assert a != b
    assert 'zlaté' in a and 'zlaté' in b


def test_no_zero_axis_for_high_prices():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from lovceny.chart import build_chart
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    pts = []
    for i, price in enumerate([36002, 36100, 35950, 36002]):
        pts.append(SimpleNamespace(
            price=price, captured_at=now - timedelta(days=3 - i), is_demo=False))
    ranges = build_chart(pts, now=now)
    svg = ranges[0].svg
    # osa Y – popisky class="ax" s cenou (ne datumy)
    y_labels = [html.unescape(x).replace("\xa0", " ").replace("\u00a0", " ")
                for x in re.findall(r'class="ax">([^<]*Kč)</text>', svg)]
    assert y_labels, svg[:200]
    assert not any(lbl.strip() in ("0 Kč", "0 Kc") for lbl in y_labels), y_labels
    assert any("36" in lbl for lbl in y_labels)


def test_daily_best_skips_zero():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from lovceny.verdict import daily_best
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    pts = [
        SimpleNamespace(price=0, captured_at=now),
        SimpleNamespace(price=None, captured_at=now),
        SimpleNamespace(price=1000, captured_at=now),
    ]
    series = daily_best(pts, now.replace(year=2020))
    assert list(series.values()) == [1000.0]


def test_daily_best_drops_glitch_low():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from lovceny.verdict import daily_best
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    pts = [
        SimpleNamespace(price=36002, captured_at=now - timedelta(days=1), is_demo=False),
        SimpleNamespace(price=999, captured_at=now, is_demo=False),
        SimpleNamespace(price=35950, captured_at=now, is_demo=False),
    ]
    series = daily_best(pts, now.replace(year=2020))
    assert 999 not in series.values()
    assert series[now.date()] == 35950.0


def test_chart_ignores_glitch_no_zero_axis():
    import html as H
    import re
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from lovceny.chart import build_chart
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    pts = [
        SimpleNamespace(price=36002, captured_at=now - timedelta(days=2), is_demo=False),
        SimpleNamespace(price=999, captured_at=now - timedelta(days=1), is_demo=False),
        SimpleNamespace(price=36100, captured_at=now, is_demo=False),
    ]
    ranges = build_chart(pts, now=now)
    svg = ranges[0].svg
    y_labels = [H.unescape(x).replace("\xa0", " ").replace("\u00a0", " ")
                for x in re.findall(r'class="ax">([^<]*Kč)</text>', svg)]
    assert y_labels
    assert not any(lbl.strip() in ("0 Kč", "0 Kc") for lbl in y_labels), y_labels
    assert ranges[0].min and ranges[0].min > 1000


def test_strips_inline_category_word():
    assert short_name("LG televize 43QNED84A3C") == "LG 43QNED84A3C"
