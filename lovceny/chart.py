"""Historie ceny jako inline SVG vykreslené na serveru (žádná JS knihovna).

Řada = nejnižší cena dne napříč obchody. Pro každé období (30 / 90 dní / celá historie) vrací
hotové SVG + statistiky (minimum, průměr, aktuální), stránka mezi nimi přepíná bez JS (radio + CSS).
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape

from .util import czk
from .verdict import daily_best

RANGES = [("30", "30 dní", 30), ("90", "90 dní", 90), ("all", "Celá historie", None)]
W, H = 720, 280
PAD_L, PAD_R, PAD_T, PAD_B = 64, 20, 34, 34
COL_LINE, COL_MIN, COL_AVG, COL_GRID = "#0B6E4F", "#C2410C", "#5B6B63", "#E4EAE7"


@dataclass
class ChartRange:
    key: str
    label: str
    days: int | None
    n: int = 0
    first: date | None = None
    min: float | None = None
    min_date: date | None = None
    avg: float | None = None
    current: float | None = None
    svg: str = ""
    svg_m: str = ""  # užší varianta pro mobil (čitelné popisky)
    summary: str = ""   # textová alternativa grafu pro čtečky
    has_demo: bool = False
    points: list = field(default_factory=list)


def _d(d: date) -> str:
    return f"{d.day}. {d.month}."


def _nice_step(span: float) -> float:
    raw = span / 4 or 1
    mag = 10 ** (len(str(int(raw))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _kc(v: float) -> str:
    return czk(v).replace("\u00a0Kč", " Kč")


def _y_bounds(lo: float, hi: float) -> tuple[float, float, float]:
    """Nice Y range that does not drag the axis to 0 Kč when prices are far above zero."""
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < max(hi * 0.02, 1):
        lo, hi = lo * 0.97, hi * 1.03
    step = _nice_step(hi - lo)
    y0 = (lo // step) * step
    y1 = -(-hi // step) * step
    if y1 <= y0:
        y1 = y0 + step
    if (lo - y0) / (y1 - y0) < 0.15:
        candidate = y0 - step
        # Keep 0 only when the series actually lives near zero
        if candidate > 0 or lo <= step:
            y0 = max(0.0, candidate)
    # Flooring onto 0 while the series midpoint is clearly above zero → zoom
    mid = (lo + hi) / 2
    if y0 <= 0 and mid > step * 2:
        pad = max((hi - lo) * 0.12, mid * 0.03, 1)
        lo2, hi2 = lo - pad, hi + pad
        step = _nice_step(hi2 - lo2)
        y0 = (lo2 // step) * step
        y1 = -(-hi2 // step) * step
        if y1 <= y0:
            y1 = y0 + step
        if y0 < 0:
            # Data still far from 0: start at first positive nice tick ≤ lo
            y0 = max(step, (lo // step) * step) if lo >= step else max(0.0, lo - pad)
            if y0 >= y1:
                y1 = y0 + step
    if y0 < 0:
        y0 = 0.0
    return y0, y1, step



def build_range(points, days: int | None, key: str, label: str, now: datetime | None = None,
                verdict=None) -> ChartRange:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else datetime(1970, 1, 1, tzinfo=timezone.utc)
    series = daily_best(points, since)
    demo_days = {(p.captured_at if p.captured_at.tzinfo else p.captured_at.replace(tzinfo=timezone.utc)).date()
                 for p in points if getattr(p, "is_demo", False)}
    r = ChartRange(key, label, days, n=len(series))
    if not series:
        r.summary = "Za toto období zatím nemáme žádné ceny."
        return r
    items = list(series.items())
    vals = [v for _, v in items]
    r.first = items[0][0]
    r.min = min(vals)
    r.min_date = next(d for d, v in items if v == r.min)
    r.avg = sum(vals) / len(vals)
    r.current = vals[-1]
    r.has_demo = any(d in demo_days for d, _ in items)
    r.points = items
    r.summary = (f"Za {label.lower() if days else 'celou dobu sledování'}: nejnižší cena {czk(r.min)} "
                 f"({_d(r.min_date)}), průměr {czk(r.avg)}, aktuálně {czk(r.current)}.")
    r.svg = _svg(r, verdict)
    r.svg_m = _svg(r, verdict, 380, 260, "m")
    return r


def _avg_label_y(pts, ya: float, x_max: float, top: float, bottom: float) -> float:
    """Popisek průměru dáme nad čáru, nebo pod ni – podle toho, kde mu méně překáží křivka ceny."""
    near = [y for x, y in pts if x <= x_max] or [pts[0][1]]
    above = sum(1 for y in near if ya - 20 <= y <= ya)
    below = sum(1 for y in near if ya <= y <= ya + 22)
    if above <= below and ya - 7 > top + 4:
        return ya - 7
    return min(ya + 17, bottom - 4)


def _svg(r: ChartRange, verdict, W: int = W, H: int = H, sfx: str = "") -> str:
    PAD_L = 64 if W > 500 else 58
    items = r.points
    lo, hi = min(v for _, v in items), max(v for _, v in items)
    y0, y1, step = _y_bounds(lo, hi)
    d0 = items[0][0]
    span_days = max((items[-1][0] - d0).days, 1)
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    X = lambda d: PAD_L + ((d - d0).days / span_days) * iw if len(items) > 1 else PAD_L + iw / 2  # noqa: E731
    Y = lambda v: PAD_T + (1 - (v - y0) / (y1 - y0)) * ih  # noqa: E731
    o = [f'<svg class="chart" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="ctt-{r.key}{sfx} cd-{r.key}{sfx}" '
         f'preserveAspectRatio="xMidYMid meet"><title id="ctt-{r.key}{sfx}">Historie ceny – {escape(r.label)}</title>'
         f'<desc id="cd-{r.key}{sfx}">{escape(r.summary)}</desc>',
         '<defs><linearGradient id="cg-' + r.key + sfx + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0B6E4F" stop-opacity=".22"/>'
         '<stop offset="1" stop-color="#0B6E4F" stop-opacity="0"/></linearGradient></defs>']
    # mřížka + osa Y
    v = y0
    while v <= y1 + 1e-6:
        y = Y(v)
        o.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{COL_GRID}"/>'
                 f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end" class="ax">{escape(_kc(v))}</text>')
        v += step
    # osa X: ~5 popisků
    n_lab = min(5 if W > 500 else 3, len(items))
    idxs = sorted({round(i * (len(items) - 1) / max(n_lab - 1, 1)) for i in range(n_lab)})
    for i in idxs:
        d = items[i][0]
        anchor = "start" if i == 0 and len(items) > 1 else ("end" if i == len(items) - 1 and len(items) > 1 else "middle")
        o.append(f'<text x="{X(d):.1f}" y="{H - 10}" text-anchor="{anchor}" class="ax">{_d(d)}</text>')
    pts = [(X(d), Y(val)) for d, val in items]
    if len(pts) > 1:
        # schodovitá čára: cena platí, dokud se nezmění
        path = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        for (x, y), (px, py) in zip(pts[1:], pts[:-1]):
            path += f" H{x:.1f} V{y:.1f}"
        base = PAD_T + ih
        o.append(f'<path d="{path} V{base:.1f} H{pts[0][0]:.1f} Z" fill="url(#cg-{r.key}{sfx})"/>')
        o.append(f'<path d="{path}" fill="none" stroke="{COL_LINE}" stroke-width="2.5" stroke-linejoin="round"/>')
    # průměr
    ya = Y(r.avg)
    o.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{ya:.1f}" y2="{ya:.1f}" stroke="{COL_AVG}" stroke-dasharray="6 5" stroke-width="1.5"/>'
             f'<text x="{PAD_L + 6}" y="{_avg_label_y(pts, ya, PAD_L + 150, PAD_T, PAD_T + ih):.1f}" text-anchor="start" class="lbl avg">průměr {escape(_kc(r.avg))}</text>')
    # minimum (zvýrazněné)
    xm, ym = X(r.min_date), Y(r.min)
    lx = xm
    if xm > W - PAD_R - 110:      # minimum u pravého okraje (typicky = dnešní cena): popisek vlevo od bodu
        anchor, lx, ly = "end", xm - 16, ym + 4
        if ly > PAD_T + ih - 4:
            ly = ym - 10
    else:
        anchor = "start" if xm < PAD_L + 70 else "middle"
        ly = ym + 22 if ym < PAD_T + ih - 24 else ym - 14
    o.append(f'<circle cx="{xm:.1f}" cy="{ym:.1f}" r="9" fill="#FF7A45" fill-opacity=".25"/>'
             f'<circle cx="{xm:.1f}" cy="{ym:.1f}" r="5" fill="{COL_MIN}" stroke="#fff" stroke-width="2"/>'
             f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" class="lbl min">minimum {escape(_kc(r.min))}</text>')
    # aktuální cena + verdikt
    xc, yc = pts[-1]
    o.append(f'<circle cx="{xc:.1f}" cy="{yc:.1f}" r="5.5" fill="{COL_LINE}" stroke="#fff" stroke-width="2"/>')
    if verdict is not None and verdict.code in ("buy", "wait", "fake"):
        txt = {"buy": "Kupuj", "wait": "Počkej", "fake": "Za 30 dní bylo levněji"}[verdict.code]
        cls = verdict.code
        tw = 14 + 7.6 * len(txt)
        bx = min(max(xc - tw / 2, PAD_L), W - PAD_R - tw)
        by = yc - 34 if yc - 34 > 2 else yc + 12
        o.append(f'<g class="vflag v-{cls}"><rect x="{bx:.1f}" y="{by:.1f}" width="{tw:.1f}" height="22" rx="11"/>'
                 f'<text x="{bx + tw / 2:.1f}" y="{by + 15:.1f}" text-anchor="middle">{txt}</text></g>')
    o.append("</svg>")
    return "".join(o)


def build_chart(points, now: datetime | None = None, verdict=None) -> list[ChartRange]:
    return [build_range(points, days, key, label, now, verdict) for key, label, days in RANGES]


def verdict_note(v, ranges: list[ChartRange]) -> str:
    """Krátké vysvětlení k verdiktu pod grafem."""
    r90 = next((r for r in ranges if r.key == "90"), None)
    if v.code == "buy" and r90 and r90.min:
        return f"Kupuj: dnešní cena je {v.reason}. Levněji za posledních 90 dní nebylo."
    if v.code == "wait" and r90 and r90.min:
        return (f"Počkej: cena je {v.reason}; {_d(r90.min_date)} stál {czk(r90.min)}. "
                "Nastav si hlídání ceny, dáme vědět.")
    if v.code == "fake":
        return f"{v.tooltip} Podrobnosti v metodice Jak řadíme a hodnotíme."
    return v.text


# ---------- v7: Srovnání produktů – více čar v jednom grafu ----------
DASHES = ["", "7 5", "2 4"]   # barva + vzor čáry (rozlišitelné i bez barev / při barvosleposti)


def _multi_svg(series: list[dict], key: str, label: str, W: int, H: int, sfx: str, summary: str) -> str:
    PAD_L = 64 if W > 500 else 58
    allv = [v for s in series for _, v in s["items"]]
    alld = [d for s in series for d, _ in s["items"]]
    lo, hi = min(allv), max(allv)
    y0, y1, step = _y_bounds(lo, hi)
    d0, d1 = min(alld), max(alld)
    span = max((d1 - d0).days, 1)
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    X = lambda d: PAD_L + ((d - d0).days / span) * iw  # noqa: E731
    Y = lambda v: PAD_T + (1 - (v - y0) / (y1 - y0)) * ih  # noqa: E731
    o = [f'<svg class="chart chart-multi" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
         f'aria-labelledby="mt-{key}{sfx} md-{key}{sfx}" preserveAspectRatio="xMidYMid meet">'
         f'<title id="mt-{key}{sfx}">Historie nejnižší ceny – {escape(label)}</title><desc id="md-{key}{sfx}">{escape(summary)}</desc>']
    v = y0
    while v <= y1 + 1e-6:
        y = Y(v)
        o.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{COL_GRID}"/>'
                 f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" text-anchor="end" class="ax">{escape(_kc(v))}</text>')
        v += step
    n_lab = 5 if W > 500 else 3
    for i in range(n_lab):
        d = d0 + timedelta(days=round(i * span / (n_lab - 1)))
        anchor = "start" if i == 0 else ("end" if i == n_lab - 1 else "middle")
        o.append(f'<text x="{X(d):.1f}" y="{H - 10}" text-anchor="{anchor}" class="ax">{_d(d)}</text>')
    for s in series:
        pts = [(X(d), Y(val)) for d, val in s["items"]]
        if not pts:
            continue
        dash = f' stroke-dasharray="{s["dash"]}"' if s["dash"] else ""
        if len(pts) > 1:
            path = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}" + "".join(f" H{x:.1f} V{y:.1f}" for x, y in pts[1:])
            o.append(f'<path d="{path}" fill="none" stroke="{s["color"]}" stroke-width="2.75" stroke-linejoin="round"{dash}/>')
        xc, yc = pts[-1]
        o.append(f'<circle cx="{xc:.1f}" cy="{yc:.1f}" r="5" fill="{s["color"]}" stroke="#fff" stroke-width="2"/>')
    o.append("</svg>")
    return "".join(o)


def build_multi(products: list[dict], now: datetime | None = None) -> list[dict]:
    """products: [{"name", "color", "dash", "points"}] → pro 30 / 90 / 365 dní {key, label, svg, svg_m, n, summary}."""
    now = now or datetime.now(timezone.utc)
    out = []
    for key, label, days in (("30", "30 dní", 30), ("90", "90 dní", 90), ("365", "Rok", 365)):
        series = []
        for p in products:
            items = list(daily_best(p["points"], now - timedelta(days=days)).items())
            series.append({**p, "items": items})
        have = [s for s in series if s["items"]]
        r = {"key": key, "label": label, "n": sum(len(s["items"]) for s in series), "svg": "", "svg_m": "",
             "enough": any(len(s["items"]) >= 2 for s in series)}
        parts = []
        for s in have:
            vals = [v for _, v in s["items"]]
            parts.append(f"{s['name']}: nejnižší {czk(min(vals))}, aktuálně {czk(vals[-1])}")
        r["summary"] = (f"Za {label.lower()}: " + "; ".join(parts) + ".") if parts else "Za toto období zatím nemáme ceny."
        if r["enough"]:
            r["svg"] = _multi_svg(have, key, label, W, H + 20, "", r["summary"])
            r["svg_m"] = _multi_svg(have, key, label, 380, 280, "m", r["summary"])
        out.append(r)
    return out
