"""Kategorie pro SEO landing stránky (/kategorie/<slug>) a třídění produktů.

Produkt má `category_slug`; u nových produktů ho odhadneme z názvu/kategorie obchodu (classify)."""
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    slug: str
    name: str        # „Telefony“
    gen: str         # 2. pád mn. č. pro titulky: „telefonů“
    lead: str        # úvodní odstavec stránky
    keywords: tuple  # hledá se bez diakritiky, na hranici slova (začátek)
    icon: str = "📦"


CATEGORIES: list[Category] = [
    Category("telefony", "Mobilní telefony", "mobilních telefonů",
             "Ceny iPhonů, Samsungů, Xiaomi a dalších telefonů z českých e-shopů. U každého modelu vidíš historii ceny a verdikt, jestli kupovat hned, nebo počkat.",
             ("iphone", "smartphone", "mobilni telefon", "galaxy s", "galaxy a", "galaxy z", "redmi", "pixel", "motorola moto", "xiaomi 1", "poco", "honor", "realme", "nothing phone"), "📱"),
    Category("notebooky", "Notebooky", "notebooků",
             "Notebooky pro práci, školu i hraní: MacBook, Lenovo, ASUS, HP, Acer a další. Řazeno jen podle ceny.",
             ("notebook", "macbook", "ideapad", "thinkpad", "vivobook", "zenbook", "aspire", "inspiron", "latitude", "probook", "elitebook", "tuf gaming", "laptop"), "💻"),
    Category("sluchatka", "Sluchátka", "sluchátek",
             "Bezdrátová i drátová sluchátka – AirPods, Sony, JBL, Bose, Samsung. Historie ceny ukáže, kdy byla nejlevnější.",
             ("sluchatk", "airpods", "jbl tune", "jbl live", "jbl tour", "buds", "headphone", "earbuds", "wh-1000", "wf-1000", "quietcomfort"), "🎧"),
    Category("pracky", "Pračky", "praček",
             "Pračky předem plněné, úzké i se sušičkou. Bosch, Samsung, Electrolux, AEG, Whirlpool, Gorenje a další.",
             ("prack", "pracka se susick"), "🫧"),
    Category("lednice", "Lednice a mrazáky", "lednic",
             "Kombinované chladničky, americké lednice i vestavné. Sledujeme ceny a upozorníme na pokles.",
             ("lednic", "chladnick", "mraznick", "mrazak", "chladici"), "🧊"),
    Category("kavovary", "Kávovary", "kávovarů",
             "Automatické, pákové i kapslové kávovary – DeLonghi, Philips, Krups, Siemens, Nespresso. Poctivé ceny bez placených pozic.",
             ("kavovar", "espresso", "magnifica", "dedica", "barista", "presovac", "nespresso", "dolce gusto"), "☕"),
    Category("vysavace", "Vysavače", "vysavačů",
             "Tyčové, robotické i klasické vysavače. Dyson, Roborock, Xiaomi, Rowenta, Electrolux, ETA a další.",
             ("vysavac", "robot vacuum", "dyson v", "roborock", "roomba", "tycovy"), "🧹"),
    Category("konzole", "Herní konzole", "herních konzolí",
             "PlayStation 5, Xbox Series a Nintendo Switch včetně ovladačů. Kdy kupovat a kdy počkat na akci.",
             ("playstation", "xbox", "nintendo switch", "herni konzole", "dualsense", "ps5"), "🎮"),
    Category("chytre-hodinky", "Chytré hodinky a náramky", "chytrých hodinek",
             "Apple Watch, Samsung Galaxy Watch, Garmin, Xiaomi Smart Band, Amazfit. Ceny a historie z českých obchodů.",
             ("apple watch", "galaxy watch", "chytre hodinky", "smart band", "smartwatch", "garmin", "amazfit", "fitness naramek", "chytry naramek"), "⌚"),
    Category("fotoaparaty", "Fotoaparáty a kamery", "fotoaparátů",
             "Bezzrcadlovky, zrcadlovky, akční kamery a gimbaly – Canon, Sony, Nikon, Fujifilm, GoPro, DJI.",
             ("fotoapar", "bezzrcadl", "zrcadlov", "eos", "alpha 7", "gopro", "osmo", "akcni kamer", "nikon z", "fujifilm"), "📷"),
    Category("televize", "Televize", "televizí",
             "Televize od 32 do 85 palců – OLED, QLED i levné LED. Porovnáváme ceny napříč obchody a u slev ukazujeme nejnižší cenu za 30 dní – hodí se před Black Friday.",
             ("televiz", "televizor", "oled", "qled", "smart tv", " tv "), "📺"),
    Category("chytra-domacnost", "Chytrá domácnost", "chytré domácnosti",
             "Chytré žárovky, zásuvky, kamery, zvonky a reproduktory – Philips Hue, Tapo, Google Nest, Echo, Shelly.",
             ("chytr", "smart plug", "philips hue", "tapo", "nest", "echo dot", "shelly", "ip kamer", "wifi zasuvk", "videozvonek", "zigbee"), "🏠"),
]
# pořadí v menu/na stránkách (klasifikace jde v pořadí CATEGORIES – specifičtější dřív)
_ORDER = ["telefony", "televize", "notebooky", "sluchatka", "pracky", "lednice", "kavovary", "vysavace",
          "konzole", "chytre-hodinky", "fotoaparaty", "chytra-domacnost"]
_CLASSIFY = list(CATEGORIES)
CATEGORIES = sorted(CATEGORIES, key=lambda c: _ORDER.index(c.slug))

# Úvodní texty na stránce kategorie (2–3 odstavce). Žádné vymyšlené statistiky.
CATEGORY_INTROS: dict[str, tuple[str, ...]] = {
    'telefony': (
        'Sledujeme ceny mobilů z českých e-shopů, které automatické čtení dovolují – iPhony, Samsungy, Xiaomi i další značky. U každého modelu ukážeme nejnižší cenu napříč obchody, historii a verdikt Kupuj/Počkej, až máme dost dní měření.',
        'Při výběru telefonu se vyplatí sledovat nejen dnešní cenu, ale i to, jestli obchod „slevu“ nepočítá z dříve zvýšené ceny. U nás u štítku Za 30 dní bylo levněji vždy ukážeme naměřenou cenu a datum.',
        'Nové iPhony bývají v září, nové Galaxy S obvykle na přelomu ledna a února – starší generace pak často zlevní. Obecný kalendář a tipy najdeš na stránce Kdy nakupovat.',
    ),
    'televize': (
        'Hlídáme televize napříč velikostmi a technologiemi (OLED, QLED, LED) u vybraných českých obchodů. Ceny řadíme jen podle částky – bez placených pozic.',
        'U TV je důležité porovnat nejen cenu, ale i dostupnost a dopravu; my ukazujeme cenu bez dopravy a historii, abys viděl(a), jestli je dnešek opravdu u minima.',
        'Televize bývají výhodnější kolem Black Friday a na jaře, kdy přicházejí nové řady a loňské zlevňují. Podrobnosti v kalendáři Kdy nakupovat.',
    ),
    'notebooky': (
        'Sledujeme notebooky pro práci, školu i hraní – od MacBooků po běžné Windowsové řady. U každého kusu vidíš ceny v obchodech a vývoj v čase.',
        'Notebooky se často liší konfigurací (RAM, disk, displej); proto u nás porovnávej konkrétní model, ne jen značku. Verdikt Kupuj dáme, až máme aspoň týden historie.',
        'Často se vyplatí akce „zpátky do školy“ (srpen–září) a Black Friday. Tip na sezóny: Kdy nakupovat.',
    ),
    'sluchatka': (
        'Hlídáme bezdrátová i drátová sluchátka – AirPods, Sony, JBL, Bose a další – a ukazujeme, kdy byla cena nejnižší.',
        'U sluchátek obchody často střídají krátké akce; historie ceny pomůže poznat, jestli „−30 %“ sedí s naměřeným minimem za 30 dní.',
        'Sluchátka často zlevňují na Black Friday a při letních akcích typu Prime Day. Viz Kdy nakupovat.',
    ),
    'pracky': (
        'Sledujeme pračky předem plněné, úzké i s vrchním plněním od Bosch, Samsung, Electrolux, AEG, Whirlpool, Gorenje a dalších značek v českých e-shopech.',
        'U praček rozhoduje kapacita, hlučnost a energetická třída – my ti neřekneme, který model je „nejlepší“, ale jestli je dnešní cena poctivá vůči historii.',
        'U praček se často vyplatí Black Friday a povánoční výprodeje v lednu. Kalendář: Kdy nakupovat.',
    ),
    'lednice': (
        'Hlídáme kombinované chladničky, americké lednice i vestavné mrazáky u obchodů, které čtení cen dovolují.',
        'Lednice se kupují na roky – počkat pár týdnů na skutečný pokles často ušetří víc než sleva z katalogové ceny. Verdikt a graf ti to ukážou na rovinu.',
        'Často se vyplatí Black Friday a povánoční výprodeje. Další tipy: Kdy nakupovat.',
    ),
    'kavovary': (
        'Sledujeme automatické, pákové i kapslové kávovary – DeLonghi, Philips, Krups, Siemens, Nespresso a další.',
        'Před Vánoci bývá poptávka vysoká a ceny spíš běžné; historie ceny pomůže neplatit zbytečně víc za „akční“ set.',
        'Kávovary bývají v akci hlavně na Black Friday. Sezónní přehled: Kdy nakupovat.',
    ),
    'vysavace': (
        'Hlídáme tyčové, robotické i klasické vysavače (Dyson, Roborock, Xiaomi, Rowenta, Electrolux, ETA…).',
        'U robotů a tyčovek se ceny hodně hýbou mezi generacemi modelů – porovnej konkrétní SKU a podívej se na minimum za 90 dní.',
        'Často zlevňují na Black Friday a při letních akcích. Viz Kdy nakupovat.',
    ),
    'konzole': (
        'Sledujeme PlayStation, Xbox a Nintendo Switch včetně běžných balíčků a ovladačů, kde to obchody dovolují číst.',
        'U konzolí pozor na „slevy“ na starší sklady a bundle – historie ceny ukáže, jestli je dnešek opravdu výhodný.',
        'Herní konzole a balíčky bývají nejvýhodnější kolem Black Friday. Kalendář: Kdy nakupovat.',
    ),
    'chytre-hodinky': (
        'Hlídáme Apple Watch, Galaxy Watch, Garmin, Xiaomi Smart Band, Amazfit a podobné náramky.',
        'Po představení nové generace starší řady často zlevní – verdikt Kupuj/Počkej ti pomůže načasovat nákup.',
        'Chytré hodinky často zlevňují po zářijovém představení Apple Watch a na Black Friday. Tip: Kdy nakupovat.',
    ),
    'fotoaparaty': (
        'Sledujeme bezzrcadlovky, zrcadlovky, akční kamery a gimbaly – Canon, Sony, Nikon, Fujifilm, GoPro, DJI.',
        'U foťáků a kamer se vyplatí ověřit, jestli sleva sedí s nejnižší cenou za 30 dní – marketingové „původní ceny“ bývají nadsazené.',
        'Fotoaparáty bývají v akci kolem Black Friday a při akcích výrobců. Viz Kdy nakupovat.',
    ),
    'chytra-domacnost': (
        'Hlídáme chytré žárovky, zásuvky, kamery, zvonky a reproduktory – Philips Hue, Tapo, Nest, Echo, Shelly a další.',
        'U chytré domácnosti obchody často střídají balíčky a 1–packs; porovnávej stejný model a sleduj historii, ať nekoupíš „slevu“ z vyšší ceny.',
        'Chytrá domácnost často zlevňuje na Black Friday a při letních akcích. Kalendář: Kdy nakupovat.',
    ),
}
BY_SLUG = {c.slug: c for c in CATEGORIES}
# typická označení modelů (TV: QE55…, UE50…, OLED55…, KD-55…, 55C6K, 55" …; notebooky HP 250/255 G…)
_PATTERNS = [("televize", re.compile(r"\b(qe|ue|kd-?|xr-?|oled|tq|tu)\d{2}[a-z]|\b\d{2}\s?(\"|''|palc)|\b(32|40|43|50|55|58|65|75|85)[a-z]{1,3}\d{1,4}[a-z]*\b")),
             ("notebooky", re.compile(r"\bhp (250|255|240|245)r?\b|\bhp (15s?|probook|elitebook|victus|omen|pavilion)\b")),
             ("pracky", re.compile(r"\b(wan|wgg|wgb|waj|ww\d|ew\d|l\d[a-z]{2,3}\d|wnei|wue|f\d[a-z]{2}\d)"))]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"\s+", " ", s) + " "


# Příslušenství nezařazujeme do hlavních kategorií (obal na iPhone není telefon).
_ACCESSORY = ("obal", "kryt", "pouzdro", "sklo", "folie", "drzak", "kabel", "nabijeck", "remin", "stojan", "filtr", "nahradn", "sacek")


def classify(name: str, shop_category: str | None = None) -> str | None:
    n = _fold(name)
    if any(re.search(r"\b" + a, n) for a in _ACCESSORY):
        return None
    hay = n + _fold(shop_category or "")
    for c in _CLASSIFY:
        for k in c.keywords:
            k2 = _fold(k).strip() if not k.startswith(" ") else _fold(k)[1:-1]
            if (k.startswith(" ") and k2 in hay) or re.search(r"\b" + re.escape(k2), hay):
                return c.slug
    for slug, rx in _PATTERNS:
        if rx.search(n):
            return slug
    return None
