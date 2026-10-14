"""Pravidla verdiktu (viz koncept.md, kap. 3):
- Falešná sleva: deklarovaná „původní“ cena je o >5 % vyšší než nejnižší cena za 30 dní před akcí
  (duch pravidla Omnibus), nebo cena během posledních ~3 týdnů skokově vyrostla (≥12 %) a „sleva“
  ji jen vrátila na předchozí úroveň.
- Kupuj: aktuální nejnižší cena je do 3 % nad minimem za 90 dní.
- Počkej: jinak (typicky cena nad mediánem 90 dní).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from .util import czk


@dataclass
class Verdict:
    code: str  # buy | wait | fake | none
    label: str
    text: str
    current: float | None = None
    min90: float | None = None
    median90: float | None = None
    max90: float | None = None
    suggested_target: float | None = None
    reason: str = ""  # krátký jednořádkový důvod pod odznakem

    ICONS = {"buy": "✅", "wait": "⏳", "fake": "⚠️", "none": "📊", "new": "🆕"}
    HEADLINES = {"buy": "Kupuj – cena u minima", "wait": "Počkej – bývalo levněji", "fake": "Falešná sleva"}

    @property
    def icon(self) -> str:
        return self.ICONS.get(self.code, "📊")

    @property
    def headline(self) -> str:
        return self.HEADLINES.get(self.code, self.label)


def _pct_over(cur: float, base: float) -> str:
    d = (cur / base - 1) * 100 if base else 0
    return "na 90denním minimu" if d < 0.5 else f"o {d:.0f} % nad 90denním minimem"


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def daily_best(points, since: datetime) -> dict:
    """{date: nejnižší cena napříč obchody}"""
    out = {}
    for p in points:
        t = _aware(p.captured_at)
        if t < since:
            continue
        d = t.date()
        out[d] = min(out.get(d, float("inf")), float(p.price))
    return dict(sorted(out.items()))


def compute(points, current: float | None, claimed_original: float | None = None,
            now: datetime | None = None) -> Verdict:
    now = now or datetime.now(timezone.utc)
    if current is None:
        return Verdict("none", "Zatím bez ceny", "Cenu zatím nemáme. Jakmile ji načteme, ukážeme verdikt.",
                       reason="cenu teprve načítáme")
    series = daily_best(points, now - timedelta(days=90))
    vals = list(series.values())
    if len(vals) < 7:
        return Verdict("none", "Málo dat", f"Cenu sledujeme teprve {len(vals)} dní. Verdikt dáme po týdnu.",
                       current=current, reason=f"sledujeme {len(vals)} z 7 potřebných dní")
    mn, md, mx = min(vals), median(vals), max(vals)
    v = Verdict("wait", "Počkej", "", current, mn, md, mx, suggested_target=round(min(mn, current) / 10) * 10)

    # 1) deklarovaná původní cena vs. min. 30 dní před akcí
    days = list(series.items())
    before = [p for d, p in days if now.date() - timedelta(days=33) <= d < now.date() - timedelta(days=3)]
    if claimed_original and before and claimed_original > min(before) * 1.05 and current < claimed_original:
        v.code, v.label = "fake", "Falešná sleva"
        v.text = (f"Obchod uvádí „původní“ cenu {czk(claimed_original)}, ale za posledních 30 dní stál "
                  f"nejméně {czk(min(before))}. Skutečná sleva je {max(0, 1 - current / min(before)) * 100:.0f} %.")
        v.reason = f"„původní“ cena je přifouknutá, skutečná sleva {max(0, 1 - current / min(before)) * 100:.0f} %"
        return v
    # 2) skokové zdražení těsně před „slevou“
    recent = [p for d, p in days if d >= now.date() - timedelta(days=21)]
    earlier = [p for d, p in days if now.date() - timedelta(days=60) <= d < now.date() - timedelta(days=21)]
    if recent and earlier:
        base = median(earlier)
        peak = max(recent)
        if peak >= base * 1.12 and current >= base * 0.97 and current < peak:
            v.code, v.label = "fake", "Falešná sleva"
            v.text = (f"Cena před 3 týdny vyskočila na {czk(peak)} (o {(peak / base - 1) * 100:.0f} %) "
                      f"a „sleva“ ji jen vrátila na obvyklých {czk(base)}. Sleva je jen zdánlivá.")
            v.reason = f"před slevou zdražili o {(peak / base - 1) * 100:.0f} %"
            return v
    if current <= mn * 1.03:
        v.code, v.label = "buy", "Kupuj"
        v.text = f"Cena je u minima za 90 dní ({czk(mn)}). Lépe to teď nebude."
        v.reason = _pct_over(current, mn)
        return v
    v.reason = _pct_over(current, mn)
    diff = (current / md - 1) * 100
    if diff > 0:
        v.text = f"Cena je o {diff:.0f} % nad obvyklou úrovní ({czk(md)}). Minimum za 90 dní bylo {czk(mn)}."
    else:
        v.text = f"Cena je slušná, ale za 90 dní byla až {czk(mn)}. Nastav si hlídání ceny."
    return v
