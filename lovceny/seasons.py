"""Kdy nakupovat: kalendář typických slevových období v Česku (OBECNÉ VZORCE, ne naše data)
a datová doporučení podle vlastní historie cen – jen když je dost dat (prahy níže).

Zásada: žádné vymyšlené statistiky. Obecné vzorce jsou formulované jako „často / bývá“
a jsou viditelně označené. Čísla („o X % levněji v měsíci Y“) počítáme výhradně z DB
(price_points bez demo dat) a ukazujeme je, jen když projdou všemi prahy.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .models import Product, Offer

MONTHS = ["leden", "únor", "březen", "duben", "květen", "červen", "červenec", "srpen", "září", "říjen",
          "listopad", "prosinec"]
MONTHS_LOC = ["v lednu", "v únoru", "v březnu", "v dubnu", "v květnu", "v červnu", "v červenci", "v srpnu",
              "v září", "v říjnu", "v listopadu", "v prosinci"]

# ---------- prahy pro datová tvrzení (měňte jen tady, zobrazují se na /kdy-nakupovat) ----------
MIN_PRODUCTS = 5             # produktů v kategorii s dostatkem dat
MIN_MONTHS = 6               # různých kalendářních měsíců s daty
MIN_PRODUCTS_PER_MONTH = 3   # produktů s daty v každém započteném měsíci
MIN_DAYS_PER_PRODUCT_MONTH = 7  # dní s cenou, aby se produkt v měsíci započítal
MIN_DIFF_PCT = 2.0           # nejlevnější měsíc musí být aspoň o 2 % pod mediánem, jinak „bez výrazného rozdílu“
CACHE_S = 3600


def black_friday(year: int) -> date:
    """Pátek po čtvrtém čtvrtku v listopadu (Den díkůvzdání v USA)."""
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=21 + 1)


def cyber_monday(year: int) -> date:
    return black_friday(year) + timedelta(days=3)


@dataclass(frozen=True)
class Period:
    key: str
    name: str
    months: tuple[int, ...]   # 1–12, kdy obvykle probíhá
    when: str                 # lidsky
    text: str                 # co se obvykle děje (obecný vzorec)
    cats: tuple[str, ...] = ()  # kategorie, kterých se to typicky týká
    icon: str = "🗓️"


def periods(year: int | None = None) -> list[Period]:
    y = year or datetime.now(timezone.utc).year
    bf, cm = black_friday(y), cyber_monday(y)
    return [
        Period("povanocni", "Povánoční výprodeje", (12, 1), "od 26. 12. zhruba do konce ledna",
               "Obchody doprodávají zboží po vánoční sezóně. Často se zlevňují televize, spotřebiče a vystavené kusy.",
               ("televize", "pracky", "lednice", "kavovary", "vysavace"), "🎁"),
        Period("jaro-modely", "Nové modelové řady televizí", (3, 4, 5, 6),
               "jaro (novinky představí výrobci v lednu na veletrhu CES)",
               "S příchodem nových řad obvykle zlevňují loňské modely – často se vyplatí sáhnout po loňské řadě.",
               ("televize",), "📺"),
        Period("galaxy", "Nové telefony Samsung Galaxy S", (1, 2), "obvykle konec ledna / únor",
               "Po představení nové řady Galaxy S bývají starší modely levnější.", ("telefony",), "📱"),
        Period("leto", "Letní výprodeje a akce typu Prime Day", (7,), "obvykle červenec",
               "Řada obchodů pořádá letní akce (inspirované Amazon Prime Day). Často jde o sluchátka, chytrou domácnost a menší elektroniku.",
               ("sluchatka", "chytra-domacnost", "vysavace", "chytre-hodinky"), "☀️"),
        Period("skola", "Zpátky do školy (back-to-school)", (8, 9), "srpen a začátek září",
               "Obchody cílí na studenty – akce bývají na notebooky, tablety a sluchátka.",
               ("notebooky", "sluchatka"), "🎒"),
        Period("iphone", "Představení nových iPhonů", (9,), "obvykle září",
               "Apple představuje nové iPhony a Apple Watch typicky v září. Starší modely pak v obchodech často zlevňují "
               "a některé se doprodávají.", ("telefony", "chytre-hodinky"), "🍏"),
        Period("singles", "Singles Day (11. 11.)", (11,), "11. listopadu",
               "Původně čínský svátek nákupů; v Česku ho připomínají hlavně obchody s elektronikou a marketplace.",
               (), "🛍️"),
        Period("black-friday", "Black Friday", (11,),
               f"letos v pátek {bf.day}. {bf.month}. {bf.year}; akce často trvají celý listopad",
               "Největší slevová akce roku napříč kategoriemi. Pozor: některé „slevy“ se počítají z dříve zvýšené ceny – "
               "porovnej s nejnižší cenou za posledních 30 dní.",
               ("televize", "notebooky", "sluchatka", "konzole", "vysavace", "kavovary", "chytra-domacnost",
                "fotoaparaty", "chytre-hodinky", "telefony", "pracky", "lednice"), "🏷️"),
        Period("cyber-monday", "Cyber Monday", (11, 12), f"letos v pondělí {cm.day}. {cm.month}. {cm.year}",
               "Pondělí po Black Friday – pokračování akcí, hlavně v e-shopech s elektronikou.",
               ("notebooky", "sluchatka", "chytra-domacnost"), "💻"),
        Period("predvanocni", "Předvánoční nákupy", (12,), "prosinec do Vánoc",
               "Poptávka je nejvyšší, ceny proto bývají spíš běžné. Kdo může, často ušetří, když počká na povánoční výprodeje.",
               (), "🎄"),
    ]


# krátké tipy pro kategorii (obecné vzorce, ne naše data)
CATEGORY_TIPS = {
    "telefony": "U telefonů se často vyplatí počkat na zářijové představení nových modelů (iPhone) nebo únorové (Samsung Galaxy S) – starší modely pak bývají levnější.",
    "televize": "Televize bývají nejlevnější kolem Black Friday a na jaře, kdy přicházejí nové řady a loňské modely zlevňují.",
    "notebooky": "U notebooků se často vyplatí akce „zpátky do školy“ (srpen–září) a Black Friday.",
    "sluchatka": "Sluchátka často zlevňují na Black Friday a při letních akcích typu Prime Day (červenec).",
    "pracky": "U praček se často vyplatí Black Friday a povánoční výprodeje (leden).",
    "lednice": "U lednic se často vyplatí Black Friday a povánoční výprodeje (leden).",
    "kavovary": "Kávovary bývají v akci hlavně na Black Friday; před Vánoci je poptávka vysoká a ceny spíš běžné.",
    "vysavace": "Vysavače včetně robotických často zlevňují na Black Friday a při letních akcích.",
    "konzole": "Herní konzole a balíčky s hrami bývají nejvýhodnější kolem Black Friday.",
    "chytre-hodinky": "Chytré hodinky často zlevňují po zářijovém představení nových Apple Watch a na Black Friday.",
    "fotoaparaty": "Fotoaparáty bývají v akci kolem Black Friday a při akcích výrobců (např. cashback).",
    "chytra-domacnost": "Chytrá domácnost často zlevňuje na Black Friday a při letních akcích typu Prime Day.",
}


def category_tip(slug: str | None) -> str:
    return CATEGORY_TIPS.get(slug or "", "")


# ---------- data z vlastní historie ----------
@dataclass
class Insight:
    slug: str
    enough: bool
    products: int = 0           # produktů započtených do analýzy
    months: int = 0             # měsíců s dostatkem dat
    points: int = 0             # denních cen (produkt × den)
    since: date | None = None
    best_month: int | None = None   # 1–12
    best_diff_pct: float | None = None  # o kolik % je medián nejlevnějšího měsíce pod celkovým (kladné = levněji)
    by_month: dict = field(default_factory=dict)  # měsíc → (index, počet produktů)
    missing: str = ""           # co chybí (lidsky)

    @property
    def best_month_loc(self) -> str:
        return MONTHS_LOC[self.best_month - 1] if self.best_month else ""


def _daily(points) -> dict[date, float]:
    out: dict[date, float] = {}
    for pt in points:
        if pt.is_demo:
            continue  # demo data do tvrzení nikdy nezapočítáváme
        t = pt.captured_at if pt.captured_at.tzinfo else pt.captured_at.replace(tzinfo=timezone.utc)
        d = t.date()
        out[d] = min(out.get(d, float("inf")), float(pt.price))
    return out


def analyze(products, slug: str) -> Insight:
    """Pro každý produkt: denní nejlevnější cena / medián produktu = relativní index (1,0 = obvyklá cena).
    Pro měsíc: medián indexů produktů, které v měsíci mají ≥ MIN_DAYS_PER_PRODUCT_MONTH dní.
    Index odstraní rozdíly v cenové hladině produktů (televize za 30 tis. nepřebije sluchátka za 2 tis.)."""
    per_month: dict[int, list[float]] = {}
    used, npts, since = set(), 0, None
    for p in products:
        daily = _daily(pt for o in p.offers for pt in o.points)
        if len(daily) < MIN_DAYS_PER_PRODUCT_MONTH:
            continue
        base = median(daily.values())
        if base <= 0:
            continue
        months: dict[int, list[float]] = {}
        for d, v in daily.items():
            months.setdefault(d.month, []).append(v / base)
        for m, vals in months.items():
            if len(vals) >= MIN_DAYS_PER_PRODUCT_MONTH:
                per_month.setdefault(m, []).append(median(vals))
                used.add(p.id)
                npts += len(vals)
                first = min(daily)
                since = first if since is None or first < since else since
    ok = {m: v for m, v in per_month.items() if len(v) >= MIN_PRODUCTS_PER_MONTH}
    ins = Insight(slug, False, len(used), len(ok), npts, since,
                  by_month={m: (median(v), len(v)) for m, v in sorted(ok.items())})
    if len(used) < MIN_PRODUCTS or len(ok) < MIN_MONTHS:
        need = []
        if len(used) < MIN_PRODUCTS:
            need.append(f"aspoň {MIN_PRODUCTS} produktů s historií (máme {len(used)})")
        if len(ok) < MIN_MONTHS:
            need.append(f"data z aspoň {MIN_MONTHS} různých měsíců (máme {len(ok)})")
        ins.missing = " a ".join(need)
        return ins
    overall = median([x for v in ok.values() for x in v])
    m_best = min(ok, key=lambda m: ins.by_month[m][0])
    ins.enough = True
    diff = (1 - ins.by_month[m_best][0] / overall) * 100
    if diff >= MIN_DIFF_PCT:
        ins.best_month, ins.best_diff_pct = m_best, round(diff, 1)
    return ins


_cache: dict[str, tuple[float, dict]] = {}


def insights(db, now: float | None = None) -> dict[str, Insight]:
    """{slug: Insight} pro všechny kategorie, cache 1 h (stránka se počítá ze všech cen)."""
    from .categories import CATEGORIES
    now = now if now is not None else time.monotonic()
    c = _cache.get("all")
    if c and now - c[0] < CACHE_S:
        return c[1]
    products = db.scalars(select(Product).where(Product.category_slug.is_not(None), Product.retired_at.is_(None))
                          .options(selectinload(Product.offers).selectinload(Offer.points))).all()
    by: dict[str, list] = {}
    for p in products:
        by.setdefault(p.category_slug, []).append(p)
    out = {c.slug: analyze(by.get(c.slug, []), c.slug) for c in CATEGORIES}
    _cache["all"] = (now, out)
    return out


def clear_cache():
    _cache.clear()
