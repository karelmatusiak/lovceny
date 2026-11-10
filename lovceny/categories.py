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
             ("notebook", "macbook", "ideapad", "thinkpad", "vivobook", "zenbook", "aspire", "inspiron", "latitude", "probook", "elitebook", "tuf gaming", "laptop", "surface pro", "surface laptop", "surface go"), "💻"),
    Category("pocitace", "Stolní počítače", "stolních počítačů",
             "Stolní počítače a repasované kancelářské sestavy. U každé konfigurace vidíš historii ceny a verdikt, jestli kupovat hned, nebo počkat.",
             ("thinkcentre", "optiplex", "prodesk", "elitedesk", "thinkstation", "pc24 gaming",
              "stolni pocitac", "stolni pocitace", "repasovane pocitce", "repasovane pocitace",
              "precision tower"), "🖥️"),
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
             "PlayStation 5, Xbox Series a Nintendo Switch. Kdy kupovat a kdy počkat na akci.",
             ("playstation", "xbox", "nintendo switch", "herni konzole", "ps5"), "🎮"),
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
          "konzole", "chytre-hodinky", "fotoaparaty", "chytra-domacnost", "pocitace"]
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
    'pocitace': (
        'Sledujeme stolní počítače a repasované kancelářské sestavy z českých e-shopů, které nám dávají oficiální feed. U každého kusu vidíš cenu, historii a verdikt.',
        'Sestavy se liší procesorem, pamětí a diskem – porovnávej konkrétní konfiguraci, ne jen řadu. Kabely, brašny a drobné příslušenství do téhle kategorie nebereme.',
        'Často se vyplatí Black Friday a akce zpátky do školy. Tip na sezóny: Kdy nakupovat.',
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
        'Sledujeme PlayStation, Xbox a Nintendo Switch včetně běžných balíčků. Ovladače, kabely a další příslušenství do výpisu konzolí nepatří.',
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


# Příslušenství / náhradní díly – nezařazujeme do hlavních kategorií.
# Stem prefixy (\b + stem): skl=sklo/skla, pouzdr=pouzdro/pouzdrem, …
_ACCESSORY_NAME = (
    "obal", "kryt", "sklo", "skla", "folie", "drzak", "kabel", "kabely",
    "nabijeck", "remin", "stojan", "podstavec", "filtr", "nahradn", "sacek", "sacky",
    "kartac", "adapter", "adaptor", "redukc", "cover", "strap",
    "naramek", "reminek", "powerbank", "baterie na", "magsafe bater",
    "prislusenstv", "nahradni dil", "cisteni", "sada na cisteni",
    "premiova sluzba", "istyle comfort", "sluzba istyle",
    "taska", "brasna", "batoh", "pouzdro na", "pouzdro pro",
    # anglické feedy (Dyson / Google): filtr, kabel – ne „HEPA“ v názvu vysavače
    "filter", "cable",
)
# Název vypadá jako „pouzdro/kryt/obal/sklo … na/pro …“ (ne AirPods s nabíjecím pouzdrem).
_ACCESSORY_NAME_RX = re.compile(
    r"(^|\s)(pouzdro|kryt|obal|sklo|folie|case)\s+(na|pro)\b"
    r"|(^|\s)(ochranny\s+kryt|ochranne\s+sklo|textilni\s+sacek)\b"
    r"|\b(pouzdro|kryt|obal)\s+pro\s+(iphone|ipad|macbook|airpods)\b"
    # stylus / dotykové pero jako výrobek (ne „se stylusem“ v názvu telefonu)
    r"|(^| )stylus( |$)"
    r"|(^| )dotykove pero( |$)",
    re.I,
)

# CATEGORYTEXT / shop path – příslušenství a nesouvisející větve
_ACCESSORY_CATEGORY = (
    "prislusenstv", "pouzdra", "kryty", "folie", "skla", "kabely a konektor",
    "baterie", "powerbank", "brasny", "batohy", "nahradni", "filtry",
    "sacky", "kabel", "nabijeck", "adapter",
)
# Whitelist: feed CATEGORYTEXT musí obsahovat aspoň jeden hint pro cílovou kategorii.
FEED_CATEGORY_WHITELIST: dict[str, tuple[str, ...]] = {
    "telefony": ("mobilni telefony", "mobilni telefon", "smartphony", "telefony"),
    "notebooky": ("notebooky", "notebook", "ultrabook", "repasovane notebooky"),
    # Počítače24 ve feedu píše „Repasované počítče“ (překlep). Ne „počítače“ samotné –
    # jinak by sedělo i na rodiče „Počítače a notebooky“.
    "pocitace": ("repasovane pocitce", "repasovane pocitace", "stolni pocitace", "stolni pocitac"),
    "sluchatka": ("sluchatk",),
    "pracky": ("pracky", "pracka"),
    "lednice": ("lednice", "chladnick", "mraznick", "mrazak"),
    "kavovary": ("kavovar",),
    "vysavace": ("vysavace", "vysavac", "roboticke vysavace"),
    "konzole": ("herni konzole", "konzole", "playstation", "xbox", "nintendo"),
    "chytre-hodinky": ("chytre hodinky", "smartwatch", "smart band", "fitness naramek"),
    "fotoaparaty": ("fotoapar", "digitalni kamery", "akcni kamery"),
    "televize": ("televize", "televizor", "led televize", "qled"),
    "chytra-domacnost": ("chytra domacnost", "smart home", "osvetleni", "zasuvk"),
}
# Minimální věrohodná cena (Kč) – levnější feed položky do kategorie nebereme.
MIN_PRICE_CZK: dict[str, float] = {
    "telefony": 2000,
    "notebooky": 5000,
    "pocitace": 1500,
    "sluchatka": 500,
    "pracky": 3000,
    "lednice": 3000,
    "kavovary": 800,
    "vysavace": 500,
    "konzole": 1000,
    "chytre-hodinky": 500,
    "fotoaparaty": 1500,
    "televize": 1500,
    "chytra-domacnost": 100,
}


def _stem_at_word(text: str, stem: str) -> bool:
    """Stem na začátku slova. „kabel“ nesedí na „bezkabelové“."""
    return re.search(r"(^| )"+re.escape(stem), text) is not None


def _path_segments(shop_category: str | None) -> list[str]:
    """Cesta kategorie bez diakritiky, segmenty oddělené „>“ / „|“."""
    raw = unicodedata.normalize("NFKD", shop_category or "").encode("ascii", "ignore").decode().lower()
    raw = raw.replace(">", "|")
    parts = []
    for seg in raw.split("|"):
        seg = re.sub(r"[^a-z0-9]+", " ", seg).strip()
        if seg:
            parts.append(seg)
    return parts


# Allegro (TradeDoubler) má v rodiči „Telefony a příslušenství“, ale list je telefon.
# Poznáme cestu podle těchto úseků a příslušenství soudíme jen z listu.
_ALLEGRO_MARKERS = (
    "telefony a prislusenstvi",
    "prislusenstvi gsm",
    "audio video a domaci spotrebice",
    "volne stojici domaci spotrebice",
    "male domaci spotrebice",
    "konzole a herni automaty",
    "chytre hodinky a prislusenstvi",
    "pocitace > notebooky",
    "pocitace > stolni pocitace",
    "pocitace > tablety",
    "pocitace > mikrofony a sluchatka",
    "pocitace > prislusenstvi",
)
# Konec cesty → slug Lovceny. Delší sufixy dřív (pračka se sušičkou před pračkou).
# Tablety (Počítače > Tablety > Zařízení) nemají vlastní kategorii, proto tu nejsou.
_ALLEGRO_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("volne stojici domaci spotrebice > pracky se susickou", "pracky"),
    ("volne stojici domaci spotrebice > pracky", "pracky"),
    ("volne stojici domaci spotrebice > lednicky", "lednice"),
    ("volne stojici domaci spotrebice > lednice", "lednice"),
    ("volne stojici domaci spotrebice > chladnicky", "lednice"),
    ("volne stojici domaci spotrebice > mraznicky", "lednice"),
    ("volne stojici domaci spotrebice > mrazaky", "lednice"),
    ("kavovary > kavovary na espresso", "kavovary"),
    ("kavovary > kapslove kavovary", "kavovary"),
    ("kavovary > automaticke kavovary", "kavovary"),
    ("vestavne spotrebice > kavovary", "kavovary"),
    ("vysavace > tycove vysavace", "vysavace"),
    ("vysavace > roboticke vysavace", "vysavace"),
    ("vysavace > podlahove vysavace", "vysavace"),
    ("vysavace > rucni vysavace", "vysavace"),
    ("vysavace > vysavace na mok", "vysavace"),
    ("sluchatka > bezdratova sluchatka", "sluchatka"),
    ("sluchatka > dratova sluchatka", "sluchatka"),
    ("sluchatka > bezdratova", "sluchatka"),
    ("sluchatka > bezdratove", "sluchatka"),
    ("sluchatka > dratova", "sluchatka"),
    ("sluchatka > dratove", "sluchatka"),
    ("mikrofony a sluchatka > bezdratova sluchatka", "sluchatka"),
    ("mikrofony a sluchatka > dratova sluchatka", "sluchatka"),
    ("chytre hodinky a prislusenstvi > chytre hodinky", "chytre-hodinky"),
    ("smartphony a mobilni telefony", "telefony"),
    ("pocitace > notebooky", "notebooky"),
    ("pocitace > stolni pocitace", "pocitace"),
    ("televize a video > televizory", "televize"),
    ("konzole a herni automaty > prenosne herni konzole", "konzole"),
    ("konzole a herni automaty > stacionarni herni konzole", "konzole"),
    ("nintendo switch 2 > konzole", "konzole"),
    ("nintendo switch > konzole", "konzole"),
    ("playstation 5 > konzole", "konzole"),
    ("playstation 4 > konzole", "konzole"),
    ("xbox series > konzole", "konzole"),
    ("xbox > konzole", "konzole"),
)
# Listy, které nejsou hlavní výrobek (držák, hadice, řemínek, displej, dálkový ovladač…).
_ALLEGRO_LEAF_ACCESSORY = _ACCESSORY_CATEGORY + (
    "drzak", "hadic", "trubk", "pasek", "remin", "displej", "dalkov", "ovladan",
    "servisn", "matric", "grafick", "obal", "kryt", "pouzdr", "folie", "kabel",
    "brasn", "sack", "nahradn", "prislusen", "filtry", "baterie", "nabijec",
)


def _allegro_path(shop_category: str | None) -> str:
    return " > ".join(_path_segments(shop_category))


def _is_allegro_path(shop_category: str | None) -> bool:
    path = _allegro_path(shop_category)
    if not path:
        return False
    return any(m in path for m in _ALLEGRO_MARKERS)


def _suffix_match(path: str, suffix: str) -> bool:
    if path == suffix or path.endswith(" > " + suffix):
        return True
    if " > " not in suffix:
        return False
    parent, leaf = suffix.rsplit(" > ", 1)
    marker = parent + " > "
    idx = path.rfind(marker)
    if idx == -1:
        return False
    rest = path[idx + len(marker):]
    if " > " in rest:
        return False
    return rest == leaf or rest.startswith(leaf + " ")


def _allegro_allowed_slug(shop_category: str | None) -> str | None:
    path = _allegro_path(shop_category)
    for suffix, slug in sorted(_ALLEGRO_SUFFIXES, key=lambda x: len(x[0]), reverse=True):
        if _suffix_match(path, suffix):
            return slug
    return None


def _allegro_leaf_accessory(shop_category: str | None) -> bool:
    segs = _path_segments(shop_category)
    if not segs:
        return False
    leaf = " " + segs[-1] + " "
    return any(_stem_at_word(leaf, a) for a in _ALLEGRO_LEAF_ACCESSORY)


def _standalone_remote(name_folded: str) -> bool:
    """Dálkový ovladač k televizi. Herní ovladač řeší _game_controller."""
    if not re.search(r"(^| )ovladac( |$)", name_folded):
        return False
    if re.search(r"dualsense|playstation|xbox|nintendo|joy-?con|gamepad", name_folded):
        return False
    return True


def _looks_like_console(name_folded: str) -> bool:
    """Samotná konzole (případně s ovladačem v balení), ne ovladač jako výrobek."""
    n = name_folded
    if re.match(r"\s*(ovladac|controller|gamepad|dualsense|dualshock|joy)", n):
        return False
    if re.search(r"\b(ovladac\w*|controller|gamepad|charging|nabijec)\b", n):
        # „s ovladačem“ / „včetně ovladače“ je pořád konzole
        if not re.search(r"\b(s|vcetne)\s+ovladac", n) and not re.search(r"ovladacem\b", n):
            return False
    if re.search(r"\b(konzole|console)\b", n):
        return True
    if re.search(r"\b(playstation\s*[45]|\bps[45]\b|xbox|nintendo switch)\b", n) and re.search(
        r"\b(slim|oled|digital|disk\w*|mechanik\w*|blu-?ray|\d+\s*tb|\d+\s*gb)\b", n
    ):
        return True
    return False


def _game_controller(name_folded: str) -> bool:
    """Ovladač, Joy-Con, DualSense. Konzole, která ovladač jen obsahuje, ne."""
    n = name_folded
    hit = re.search(r"\b(dualsense|dualshock|gamepad|controller)\b|\bjoy-?con\b|\bovladac", n)
    if not hit:
        return False
    # Konzole „s ovladačem“ / „včetně ovladače“, ne samotný ovladač.
    if re.search(r"\b(s|vcetne)\s+ovladac", n) or re.search(r"\bovladacem\b", n):
        return False
    if _looks_like_console(n):
        return False
    return True


def is_accessory_text(name: str, shop_category: str | None = None) -> bool:
    """True = příslušenství / náhradní díl / služba – ne hlavní produkt."""
    n = _fold(name)
    if _ACCESSORY_NAME_RX.search(n):
        return True
    if any(re.search(r"\b" + re.escape(a), n) for a in _ACCESSORY_NAME):
        return True
    if _standalone_remote(n):
        return True
    if _game_controller(n):
        return True
    if _is_allegro_path(shop_category):
        # Rodič „Telefony a příslušenství“ nesmí shodit samotný telefon.
        return _allegro_leaf_accessory(shop_category)
    c = _fold(shop_category or "")
    if c.strip() and any(_stem_at_word(c, a) for a in _ACCESSORY_CATEGORY):
        return True
    return False


def feed_category_slug(shop_category: str | None) -> str | None:
    """Mapuje Heureka CATEGORYTEXT na slug jen přes whitelist (bez tipování z názvu).

    Hint musí sedět na segment cesty (za `|`), ne jen jako podřetězec rodiče.
    Jinak „Počítače a notebooky | Monitory/GPU“ spadne pod notebooky.
    Allegro cesty jdou přes vlastní sufixy (rodič „… a příslušenství“ není list).
    """
    if _is_allegro_path(shop_category):
        if _allegro_leaf_accessory(shop_category):
            return None
        return _allegro_allowed_slug(shop_category)
    c = _fold(shop_category or "")
    if not c.strip():
        return None
    if any(_stem_at_word(c, a) for a in _ACCESSORY_CATEGORY):
        return None
    segments = [s.strip() for s in re.split(r"[|>]", c) if s.strip()]
    found = None
    for slug, hints in FEED_CATEGORY_WHITELIST.items():
        for h in hints:
            h2 = _fold(h).strip()
            if not h2:
                continue
            for seg in segments:
                # Prefix segmentu (sluchatk→sluchatka). Ne substring uprostřed
                # („notebooky“ ∈ „pocitace a notebooky“).
                # U vysavačů navíc celé slovo: „Bezkabelové vysavače“, „Tyčový vysavač“.
                word = h2 in ("vysavac", "vysavace") and re.search(
                    rf"(^| ){re.escape(h2)}( |$)", seg
                )
                if seg == h2 or seg.startswith(h2) or word:
                    found = slug
                    break
            if found:
                break
        if found:
            break
    # Rodič „Chladničky“ / „Lednice“ nesmí vtáhnout vinotéku, minibar ani zrací skříň.
    # „Vysavače listí“ není domácí vysavač.
    if found == "lednice" and re.search(r"vinotek|zrani|napoj|minibar|autochlad", c):
        return None
    if found == "vysavace" and re.search(r"listi|jezerkov", c):
        return None
    return found


# URL / název sanity: feed CATEGORYTEXT bývá špatně (čistič oken v „Robotické vysavače“).
_WINDOW_CLEANER_RX = re.compile(
    r"cistic[-_ ]?oken|cistici?\s+oken|window\s*clean|roboticky[-_ ]?cistic[-_ ]?oken",
    re.I,
)
_SCW_RX = re.compile(r"(^|[^a-z])scw(\s|-|_|\d)", re.I)

# Identita sluchátek v NÁZVU. Přebije zmínku monitoru (Marshall Monitor, „k monitoru“,
# in-ear monitor). Kategorie obchodu se sem nepočítá – ta u XM umí lhát.
_HEADPHONE_IDENTITY_RX = re.compile(
    r"\bsluchat"
    r"|\b(airpods|earpods)\b"
    r"|\b(headphones?|headsets?|earbuds?|earphones?)\b"
    r"|\bbuds\b"
    r"|\b(wh|wf)-?\d"
    r"|\bquietcomfort\b"
    r"|\bnahlavni souprava\b"
    r"|\bin[- ]ear monitors?\b"
    r"|\bmarshall monitor\b"
)
# Řady, které jsou počítačové displeje (ne televize, ne sluchátka).
_DISPLAY_LINE_RX = re.compile(r"\b(thinkvision|ultragear|viewfinity|zenscreen)\b")
_DISPLAY_WORD_RX = re.compile(
    r"\bmonitors?\b|\bmonitore\b|\bmonitory\b|\bmonitoru\b|\bmonitorem\b|\bmonitorech\b"
)
_DISPLAY_LCD_RX = re.compile(r"\blcd\b")
_DISPLAY_PANEL_RX = re.compile(
    r"\b(ips|va|qhd|fhd|wqhd|uwqhd|oled|uhd)\b|\d{3,4}\s*x\s*\d{3,4}|\d+\s*hz\b"
)
_DISPLAY_MT_RX = re.compile(r"\bmt\b")
_ODYSSEY_RX = re.compile(r"\bodyssey\b")
_ODYSSEY_MONITOR_RX = re.compile(
    r"\b(g[5789]\d|oled|qhd|uhd|ips|va)\b|\d+\s*hz\b|\d{2}(?:[.,]\d+)?\s*\""
)
# Modelový kód televize (OLED55, QE55…). Holé „oled“ / „qled“ je i u monitoru.
_TV_IDENTITY_RX = re.compile(r"\bteleviz|\b(qe|ue|kd-?|xr-?|oled|tq|tu)\d{2}\b")
_MONITOR_SPEC_RX = re.compile(r"\b(qhd|fhd|wqhd|uwqhd|qd-oled)\b")


def _display_name(name: str) -> str:
    """Název bez diakritiky; palce zapsané jako '' sjednotí na uvozovku."""
    return _fold(name).replace("''", "\"")


def _inch_size(folded: str) -> float | None:
    m = re.search(r"(\d{2}(?:[.,]\d+)?)\s*\"", folded)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def is_computer_display(name: str) -> bool:
    """True = název je počítačový displej.

    Rozhoduje jen název výrobku, ne CATEGORYTEXT a ne popis. Sluchátka, která se
    jmenují Monitor (Marshall) nebo v názvu mají „k monitoru“, displej nejsou.
    Televizní model (OLED55, „televizor“) taky ne.
    """
    n = _display_name(name)
    if not n.strip():
        return False
    if _HEADPHONE_IDENTITY_RX.search(n):
        return False
    if _TV_IDENTITY_RX.search(n):
        return False
    if _DISPLAY_LINE_RX.search(n) or _DISPLAY_WORD_RX.search(n):
        return True
    if _ODYSSEY_RX.search(n) and _ODYSSEY_MONITOR_RX.search(n):
        return True
    inch = _inch_size(n)
    if _DISPLAY_LCD_RX.search(n) and (_DISPLAY_PANEL_RX.search(n) or inch is not None):
        return True
    # XM ve feedu zkracuje monitor na „MT“ (AOC MT 27", SAMSUNG MT LED LCD).
    if _DISPLAY_MT_RX.search(n) and (
        _DISPLAY_LCD_RX.search(n) or inch is not None or _DISPLAY_PANEL_RX.search(n)
    ):
        return True
    # 26,5'' + QHD / QD-OLED (ASUS XG27…) – pod 50", ať se do toho nevejde 55" televize.
    if inch is not None and inch < 50 and _MONITOR_SPEC_RX.search(n):
        return True
    return False


def category_url_conflict(slug: str | None, name: str, url: str | None = None) -> bool:
    """True = název/URL nesedí na cílovou kategorii (např. čistič oken ≠ vysavač)."""
    if not slug:
        return False
    # Displej nesmí spadnout do sluchátek jen proto, že list Heureky je „Sluchátka“,
    # ani do televizí kvůli holému „OLED“ v názvu monitoru.
    if slug in ("sluchatka", "televize") and is_computer_display(name):
        return True
    hay = _fold(name) + " " + _fold(url or "")
    path = _fold((url or "").replace("https://", "").replace("http://", ""))
    if slug == "vysavace":
        if _WINDOW_CLEANER_RX.search(hay) or _WINDOW_CLEANER_RX.search(path):
            return True
        if _SCW_RX.search(name or "") or re.search(r"\bscw[-_]?\d", path) or "scw-" in path:
            return True
        if re.search(r"\bsrw\b", _fold(name)) and ("oken" in hay or "window" in hay):
            return True
        if "roboticky-cistic-oken" in path or "cistic-oken" in path:
            return True
    return False


def accept_feed_item(
    name: str,
    shop_category: str | None,
    price: float | None,
    url: str | None = None,
) -> str | None:
    """Vrátí category_slug, když cesta kategorie obchodu je ve whitelistu.

    O kategorii nerozhoduje název. Příslušenství, cena pod minimem a známý
    spor adresy (čistič oken ≠ vysavač, displej ≠ sluchátka) položku odmítnou.
    """
    if is_accessory_text(name, shop_category):
        return None
    if price is None or price <= 0:
        return None
    feed_slug = feed_category_slug(shop_category)
    if feed_slug is None:
        return None
    blob = _fold(name) + " " + _fold(shop_category or "")
    # Nápojové / zrací skříně nejsou domácí lednice; průmyslový vysavač není tyčový/robot.
    if feed_slug == "lednice" and re.search(r"vinotek|zrani|napoj|minibar|autochlad|obytn", blob):
        return None
    if feed_slug == "vysavace" and re.search(r"prumysl", blob):
        return None
    if category_url_conflict(feed_slug, name, url):
        return None
    min_p = MIN_PRICE_CZK.get(feed_slug, 0)
    if price < min_p:
        return None
    return feed_slug


def category_keywords_hit(name: str, slug: str | None) -> bool:
    """True = název nese klíčové slovo nebo vzor dané kategorie (ne shop path)."""
    if not slug or slug not in BY_SLUG:
        return False
    n = _fold(name)
    hay = n
    for k in BY_SLUG[slug].keywords:
        k2 = _fold(k).strip() if not str(k).startswith(" ") else _fold(k)[1:-1]
        if k2 and re.search(r"\b" + re.escape(k2), hay):
            return True
    for s, rx in _PATTERNS:
        if s == slug and rx.search(n.strip()):
            return True
    return False


def classify(name: str, shop_category: str | None = None, url: str | None = None) -> str | None:
    if is_accessory_text(name, shop_category):
        return None
    n = _fold(name)
    hay = n + _fold(shop_category or "")
    for c in _CLASSIFY:
        matched = False
        for k in c.keywords:
            k2 = _fold(k).strip() if not k.startswith(" ") else _fold(k)[1:-1]
            if (k.startswith(" ") and k2 in hay) or re.search(r"\b" + re.escape(k2), hay):
                matched = True
                break
        if matched and not category_url_conflict(c.slug, name, url):
            return c.slug
    for slug, rx in _PATTERNS:
        if rx.search(n) and not category_url_conflict(slug, name, url):
            return slug
    return None


# JBL.cz (Google Merchant) nemá product_type ani google_product_category.
# Sluchátka bereme z názvu a popisu. Reproduktory, soundbary, autoaudio a mikrofony
# vlastní kategorii nemají – do sluchátek je necpeme.
_JBL_BAZAR_RX = re.compile(r"\bbazar\b")
_JBL_ACCESSORY_RX = re.compile(
    r"\b(?:nahradn\w*|nausnik\w*|kabel\w*|hdmi|adapt[eo]r\w*|armband\w*|drzak\w*|"
    r"popruh\w*|poutk\w*|partycover\w*|cover\b|pouzdr\w*|obal\w*|kryt\w*|filtr\w*|"
    r"bateri\w*|battery\w*)"
)
# Řady, které nejsou sluchátka (repro, soundbar, auto, mikrofon, rádio, powerbanka…).
_JBL_NOT_HEADPHONE_RX = re.compile(
    r"\b(?:partybox\w*|partylight\w*|charge\b|flip\b|clip\b|boombox\w*|xtreme\b|pulse\b|"
    r"grip\b|authentics\b|tuner\b|horizon\w*|spinner\b|basspro\w*|cinema\b|citation\b|"
    r"onyx\b|club\b|stage(?:\b|\d)|stadium\b|concert\b|infinity\b|legend\b|celebrity\b|"
    r"cove\b|wind\b|easysing\w*|quantum stream\b|mikrofon\w*|microphone\w*|subwoofer\w*|"
    r"reproduktor\w*|soundbar\w*|zesilovac\w*|multibeam\b|go essential\b|go \+|go\d+|pbm\d+|"
    r"mics?\b|bar\b|smart tx\b)"
)
_JBL_TOUR_ONE_RX = re.compile(r"\btour one\b")
_JBL_HEADPHONE_LINE_RX = re.compile(
    r"\b(?:jbl tune\b|jbl live\b|jbl quantum\b|jbl tour\b|jbl wave\b|jbl endurance\b|"
    r"jbl vibe\b|jbl soundgear\b|jbl sense\b|jbl jr\d*|jbl junior\b|tour one\b|yurbuds\b|"
    r"t110\b|t205\b|t210\b|t290\b)"
)
_JBL_HEADPHONE_TEXT_RX = re.compile(
    r"\bsluchat|\bin[- ]ear\b|\bover[- ]ear\b|\bon[- ]ear\b|\bearbuds?\b|\bheadphones?\b|"
    r"\bearphones?\b|\bdo usi\b|\bna usi\b|\bpres usi\b|\bkolem usi\b"
)


def jbl_is_bazar(name: str, condition: str | None = None) -> bool:
    """Bazar / used z JBL feedu (g:condition nebo „Bazar“ v názvu)."""
    if (condition or "").strip().lower() == "used":
        return True
    return _JBL_BAZAR_RX.search(_fold(name)) is not None


def jbl_catalog_category(
    name: str,
    description: str | None = None,
    *,
    condition: str | None = None,
) -> str | None:
    """Slug pro katalog JBL.cz, nebo None.

    Tune, Live, Quantum (ne Stream), Tour, Wave, in-ear/over-ear a Yurbuds sluchátka ano.
    Příslušenství, bazar, reproduktory, soundbary, autoaudio a mikrofony ne.
    """
    if jbl_is_bazar(name, condition):
        return None
    n = _fold(name)
    if _JBL_ACCESSORY_RX.search(n):
        return None
    # „TOUR ONE M3 Smart TX“ jsou sluchátka s vysílačem, ne samotný Smart TX.
    if _JBL_NOT_HEADPHONE_RX.search(n) and not _JBL_TOUR_ONE_RX.search(n):
        return None
    if re.search(r"\bquantum stream\b", n):
        return None
    blob = n + _fold(description or "")
    if _JBL_HEADPHONE_LINE_RX.search(n) or _JBL_HEADPHONE_TEXT_RX.search(blob):
        return "sluchatka"
    return None


def annotate_jbl_items(items: list) -> list:
    """Z feedu JBL.cz vyhodí bazar a sluchátkům doplní kategorii pro whitelist seedu."""
    out = []
    for it in items:
        cond = getattr(it, "condition", None)
        if jbl_is_bazar(getattr(it, "name", ""), cond):
            continue
        if jbl_catalog_category(
            it.name, getattr(it, "description", None), condition=cond,
        ) == "sluchatka":
            it.category = "Sluchátka"
        out.append(it)
    return out
