"""Pravidla verdiktu (veřejně popsaná na /jak-radime):
- Za 30 dní bylo levněji (kód „fake“, jen interní): deklarovaná „původní“ cena je o >5 % vyšší než
  nejnižší naměřená cena za 30 dní před akcí (logika § 12a zákona o ochraně spotřebitele), nebo cena
  během posledních ~3 týdnů vyrostla o ≥12 % a sleva ji vrátila zhruba na předchozí úroveň.
  Formulace je čistě faktická (žádné hodnocení obchodu), u štítku vždy ukazujeme data.
- Kupuj: aktuální nejnižší cena je do 3 % nad minimem za 90 dní.
- Počkej: jinak (typicky cena nad mediánem 90 dní).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from .util import czk

# Prahy (zobrazují se i na /jak-radime – měňte jen tady)
MIN_DAYS = 7            # min. počet dní s cenou pro verdikt
BUY_OVER_MIN = 0.03     # Kupuj: do 3 % nad 90denním minimem
REF_OVER_MIN30 = 0.05   # „původní“ cena > 105 % minima za 30 dní před akcí
JUMP = 0.12             # skok o ≥ 12 % během posledních 21 dní
JUMP_BACK = 0.03        # „sleva“ vrátila cenu na ≥ 97 % předchozí obvyklé ceny
FAKE_LABEL = "Za 30 dní bylo levněji"


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
    ref_min30: float | None = None   # nejnižší naměřená cena za 30 dní (u štítku „Za 30 dní bylo levněji“)
    ref_date: object = None          # date té ceny
    tip: str = ""  # volitelný tooltip (např. u „Sbíráme data“)

    @property
    def tooltip(self) -> str:
        if self.tip:
            return self.tip
        if self.code != "fake" or self.ref_min30 is None:
            return ""
        return (f"Nejnižší cena za posledních 30 dní byla {czk(self.ref_min30)} (dne {_cz_date(self.ref_date)}). "
                "Sleva se počítá z vyšší ceny.")

    @property
    def ref_text(self) -> str:
        if self.code != "fake" or self.ref_min30 is None:
            return ""
        return f"nejnižší za 30 dní: {czk(self.ref_min30)} ({_cz_date(self.ref_date)})"

    ICONS = {"buy": "✅", "wait": "⏳", "fake": "⚠️", "none": "📊", "new": "🆕"}
    HEADLINES = {"buy": "Kupuj – cena u minima", "wait": "Počkej – bývalo levněji", "fake": FAKE_LABEL}

    @property
    def icon(self) -> str:
        return self.ICONS.get(self.code, "📊")

    @property
    def headline(self) -> str:
        return self.HEADLINES.get(self.code, self.label)


def _cz_date(d) -> str:
    return f"{d.day}. {d.month}. {d.year}" if d else ""


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
        return Verdict("none", "Zatím bez ceny", "Cena zatím není k dispozici. Načteme ji při nejbližším stahování (3× denně) a pak ukážeme verdikt.",
                       reason="cenu teprve načítáme",
                       tip="Cenu zatím nemáme – produkt je v katalogu, ale žádný obchod ještě cenu nevrátil.")
    series = daily_best(points, now - timedelta(days=90))
    vals = list(series.values())
    if len(vals) < MIN_DAYS:
        from .util import cz_count
        days_txt = cz_count(len(vals), "den", "dny", "dní")
        tip = (f"Zatím máme historii za {days_txt}. Verdikt Kupuj/Počkej "
               f"dáváme až po {MIN_DAYS} dnech sledování.")
        return Verdict(
            "none", "Sbíráme data",
            f"Cenu sledujeme teprve {days_txt}. Verdikt dáme po týdnu.",
            current=current,
            reason=f"sledujeme {len(vals)} z {MIN_DAYS} potřebných dní",
            tip=tip,
        )
    mn, md, mx = min(vals), median(vals), max(vals)
    v = Verdict("wait", "Počkej", "", current, mn, md, mx, suggested_target=round(min(mn, current) / 10) * 10)

    # 1) deklarovaná původní cena vs. min. 30 dní před akcí
    days = list(series.items())
    before = [(d, p) for d, p in days if now.date() - timedelta(days=33) <= d < now.date() - timedelta(days=3)]
    last30 = [(d, p) for d, p in days if now.date() - timedelta(days=30) <= d < now.date()]

    def _flag(ref):
        d0, p0 = min(ref, key=lambda x: (x[1], x[0]))
        v.code, v.label, v.ref_min30, v.ref_date = "fake", FAKE_LABEL, p0, d0

    if claimed_original and before and claimed_original > min(p for _, p in before) * (1 + REF_OVER_MIN30) \
            and current < claimed_original:
        _flag(before)
        real = max(0, 1 - current / v.ref_min30) * 100
        v.text = (f"Obchod uvádí původní cenu {czk(claimed_original)}. Nejnižší cena, kterou jsme naměřili "
                  f"za 30 dní před akcí, byla {czk(v.ref_min30)} (dne {_cz_date(v.ref_date)}). "
                  f"Vůči ní je dnešní cena nižší o {real:.0f} %.")
        v.reason = f"nejnižší za 30 dní: {czk(v.ref_min30)} ({_cz_date(v.ref_date)}), vůči ní −{real:.0f} %"
        return v
    # 2) zdražení během posledních 3 týdnů, které sleva vrátila
    recent = [p for d, p in days if d >= now.date() - timedelta(days=21)]
    earlier = [p for d, p in days if now.date() - timedelta(days=60) <= d < now.date() - timedelta(days=21)]
    if recent and earlier:
        base = median(earlier)
        peak = max(recent)
        ref = last30 or before
        # štítek musí být pravdivý: za 30 dní opravdu bylo levněji než dnes
        if peak >= base * (1 + JUMP) and current >= base * (1 - JUMP_BACK) and current < peak \
                and ref and min(p for _, p in ref) < current:
            _flag(ref)
            v.text = (f"Během posledních 3 týdnů cena vzrostla na {czk(peak)} (o {(peak / base - 1) * 100:.0f} %) "
                      f"a teď je zpět u {czk(current)}; předtím byla obvykle {czk(base)}. "
                      f"Nejnižší cena za 30 dní byla {czk(v.ref_min30)} (dne {_cz_date(v.ref_date)}).")
            v.reason = f"nejnižší za 30 dní: {czk(v.ref_min30)} ({_cz_date(v.ref_date)}); před slevou +{(peak / base - 1) * 100:.0f} %"
            return v
    if current <= mn * (1 + BUY_OVER_MIN):
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
