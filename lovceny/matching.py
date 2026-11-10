"""Párování nabídek a produktů: značka, parametry z názvu, identita modelu.

Stejné EAN nesmí spojit jiný výrobek (jiná značka, příkon, objem, barva, generace).
Stejná značka a model (bez barvy, když ji má jen jedna strana) je kandidát na sloučení.
"""
from __future__ import annotations

import re
import unicodedata

from .specs import extract

# Slovo v názvu → kanonická značka. Delší fráze se hledají dřív.
_BRAND_WORDS: tuple[tuple[str, str], ...] = (
    ("apple", "apple"), ("iphone", "apple"), ("ipad", "apple"), ("macbook", "apple"),
    ("airpods", "apple"), ("imac", "apple"),
    ("playstation", "sony"), ("dualsense", "sony"), ("dualshock", "sony"), ("xperia", "sony"),
    ("sony", "sony"),
    ("samsung", "samsung"), ("galaxy", "samsung"),
    ("nintendo", "nintendo"),
    ("microsoft", "microsoft"), ("xbox", "microsoft"), ("surface", "microsoft"),
    ("klarstein", "klarstein"), ("aigostar", "aigostar"), ("libeica", "libeica"),
    ("modecom", "modecom"), ("delonghi", "delonghi"), ("krups", "krups"),
    ("bosch", "bosch"), ("siemens", "siemens"), ("philips", "philips"),
    ("lg", "lg"), ("dyson", "dyson"), ("roborock", "roborock"), ("rowenta", "rowenta"),
    ("jbl", "jbl"), ("bose", "bose"), ("garmin", "garmin"), ("xiaomi", "xiaomi"),
    ("redmi", "xiaomi"), ("poco", "xiaomi"), ("lenovo", "lenovo"), ("asus", "asus"),
    ("acer", "acer"), ("hp", "hp"), ("dell", "dell"), ("huawei", "huawei"),
    ("honor", "honor"), ("realme", "realme"), ("motorola", "motorola"), ("nokia", "nokia"),
    ("google", "google"), ("pixel", "google"),
    ("jabra", "jabra"), ("guess", "guess"), ("larma", "larma"),
    ("oneplus", "oneplus"), ("nothing", "nothing"), ("amazfit", "amazfit"),
    ("whirlpool", "whirlpool"), ("electrolux", "electrolux"), ("beko", "beko"),
    ("gorenje", "gorenje"), ("hisense", "hisense"), ("tcl", "tcl"), ("panasonic", "panasonic"),
    ("sencor", "sencor"), ("concept", "concept"), ("eta", "eta"), ("miele", "miele"),
    ("haier", "haier"), ("indesit", "indesit"), ("amica", "amica"), ("sharp", "sharp"),
    ("candy", "candy"), ("razer", "razer"), ("logitech", "logitech"),
    ("canon", "canon"), ("nikon", "nikon"), ("fujifilm", "fujifilm"), ("gopro", "gopro"),
)
# Z názvu identity smažeme jen samotný název značky, ne modelovou řadu (iphone, ps5, switch).
_BRAND_DROP = {
    "apple": {"apple"},
    "sony": {"sony", "playstation"},
    "samsung": {"samsung"},
    "nintendo": {"nintendo"},
    "microsoft": {"microsoft"},
    "klarstein": {"klarstein"},
    "aigostar": {"aigostar"},
    "libeica": {"libeica"},
    "modecom": {"modecom"},
}

# „pro“ / „max“ / „air“ tu nejsou – to jsou varianty modelu (15 ≠ 15 Pro).
_DROP = frozenset({
    "herni", "konzole", "console", "sluchatka", "sluchatko", "mikrofon", "mikrofonem",
    "mikrofonu", "usb", "led", "rgb", "tws", "anc", "enc", "bluetooth", "wifi",
    "wireless", "edition", "verze", "version", "vcetne", "espresso", "kavovar",
    "tlakovy", "automaticky", "pakovy", "mlynku", "mlynek", "originalni", "original",
    "sportovni", "sport", "panske", "damske", "pansky", "damsky", "hodinky",
    "chytre", "chytry", "chytra", "ssd", "hdd", "bluray", "blu", "ray", "gen",
    "set", "kit", "new", "novy", "nova", "nove", "bila", "bile", "bily",
    "s", "a", "i", "na", "do", "se", "bez",
})

_SPEC_KEYS = ("power", "volume", "pressure", "ram", "screen", "load", "storage")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
_GEN_RE = re.compile(
    r"\b(?:iphone|pixel|redmi)\s*(\d{1,2}[a-z]?)\b|\bgalaxy\s*[asz]\s*(\d{1,2})\b",
    re.I,
)
_PS_RE = re.compile(r"\bps\s*([45])\b")

_PROMO_TOKENS = frozenset({
    "panske", "pansky", "damske", "damsky", "unisex", "sportovni", "sport",
    "hovory", "volani", "menu", "polske", "polsky", "originalni", "luxusni",
    "super", "hit", "vyprodej", "akce",
})
_SPAM_PHRASES = ("polske menu", "polsky jazyk", "menu polske")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def prepare_offer_label(text: str) -> str:
    """Slug z URL srovná na text. „1-7-l“ → „1,7 l“, ať objem nespadne na 7 l."""
    s = (text or "").replace("_", " ").replace("+", " ")
    s = re.sub(r"\b(\d)-(\d)-l\b", r"\1,\2 l", s, flags=re.I)
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\b(\d)\s+(\d)\s+(l|litr|litru)\b", r"\1,\2 \3", s, flags=re.I)
    return s


def label_from_url(url: str) -> str:
    from urllib.parse import unquote, urlparse
    path = unquote(urlparse(url or "").path)
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.(html?|php|aspx)$", "", slug, flags=re.I)
    return prepare_offer_label(slug)


def brands(name: str) -> set[str]:
    n = " " + _fold(name) + " "
    found: set[str] = set()
    # Delší slova dřív, ať „playstation“ spadne na sony dřív než krátké shody.
    for word, canon in sorted(_BRAND_WORDS, key=lambda x: len(x[0]), reverse=True):
        if re.search(r"\b" + re.escape(word) + r"\b", n):
            found.add(canon)
    if _PS_RE.search(n):
        found.add("sony")
    if re.search(r"\bswitch\b", n) and "microsoft" not in found:
        found.add("nintendo")
    return found


def primary_brand(name: str) -> str | None:
    found = brands(name)
    if len(found) == 1:
        return next(iter(found))
    return None


def brand_and_mpn(name: str) -> tuple[str, frozenset[str]] | None:
    """Značka a silné kódy výrobce z názvu. Slabé tokeny (pro2, air15) sem nepatří."""
    brand = primary_brand(name)
    if not brand:
        return None
    from .shops.feed import _mpn_tokens, canonicalize_name
    raw = _mpn_tokens(canonicalize_name(name or ""))
    strong = frozenset(token for token in raw if len(token) >= 5 and any(ch.isdigit() for ch in token))
    if not strong:
        return None
    return brand, strong


def _num(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group().replace(",", "."))
    except ValueError:
        return None


def spec_map(name: str) -> dict[str, float]:
    raw = extract(name or "")
    out = {}
    for key in _SPEC_KEYS:
        n = _num(raw.get(key))
        if n is not None:
            out[key] = n
    return out


def spec_conflict(a: str, b: str) -> bool:
    """Obě strany uvádějí stejný parametr a liší se (1550 W ≠ 1360 W, 1,4 l ≠ 1,7 l)."""
    sa, sb = spec_map(a), spec_map(b)
    for key in _SPEC_KEYS:
        if key in sa and key in sb and abs(sa[key] - sb[key]) > 0.05:
            return True
    return False


def color_conflict(a: str, b: str) -> bool:
    ca, cb = extract(a or "").get("color"), extract(b or "").get("color")
    return bool(ca and cb and ca != cb)


def colors_agree(a: str, b: str) -> bool:
    """Sloučení jen když barva je stejná, nebo ji nemá ani jeden název.

    Jinak by „OLED“ bez barvy spojilo bílou a černou přes sebe.
    """
    ca, cb = extract(a or "").get("color"), extract(b or "").get("color")
    return ca == cb


def clear_model_conflict(product_name: str, other: str) -> bool:
    """Adresa nebo název zřetelně patří k jiné značce nebo jiné generaci.

    Číslo v adrese obchodu (t-12345, dp652173) samo o sobě konflikt není.
    """
    if not (product_name or "").strip() or not (other or "").strip():
        return False
    left, right = brands(product_name), brands(other)
    if left and right and left.isdisjoint(right):
        return True
    from .shops.feed import canonicalize_name
    gens_left, gens_right = _generations(canonicalize_name(product_name)), _generations(canonicalize_name(other))
    if gens_left and gens_right and gens_left != gens_right:
        return True
    return False


def titles_conflict(a: str, b: str) -> bool:
    """Jasný nesoulad dvou názvů. Volá se, až když names_match selže.

    Nevrací True jen proto, že názvy nejsou totožné – chybějící příkon na jedné
    straně konflikt není. Jiná značka, jiný příkon/objem/úložiště, jiná barva
    nebo jiná generace ano.
    """
    if not (a or "").strip() or not (b or "").strip():
        return False
    ba, bb = brands(a), brands(b)
    if ba and bb and ba.isdisjoint(bb):
        return True
    if spec_conflict(a, b) or color_conflict(a, b):
        return True
    from .shops.feed import _variants, canonicalize_name
    ca, cb = canonicalize_name(a), canonicalize_name(b)
    va, vb = _variants(ca), _variants(cb)
    if va != vb and (va or vb):
        return True
    ga, gb = _generations(ca), _generations(cb)
    if ga and gb and ga != gb:
        return True
    ta, tb = _bare_model_tokens(ca), _bare_model_tokens(cb)
    if ta and tb and ta.isdisjoint(tb):
        return True
    return False


def _bare_model_tokens(canon: str) -> set[str]:
    """Modelové kódy bez kapacity (825gb), ať zkrácený slug neshodí plný název."""
    from .shops.feed import _model_tokens
    cap = re.compile(r"^\d+(?:gb|tb|w|mah)$")
    return {t for t in _model_tokens(canon) if not cap.match(t)}


def _generations(canon: str) -> set[str]:
    out = set()
    for m in _GEN_RE.finditer(canon):
        out.add(next(g for g in m.groups() if g))
    return out


def _normalize_phrases(canon: str) -> str:
    s = canon
    s = re.sub(r"\bplay station\b", "playstation", s)
    s = re.sub(r"\bplaystation\s*5\b|\bps\s*5\b", "ps5", s)
    s = re.sub(r"\bplaystation\s*4\b|\bps\s*4\b", "ps4", s)
    s = re.sub(r"\bswitch\s+oled\b", "switcholed", s)
    s = re.sub(r"\bswitch\s+2\b", "switch2", s)
    s = re.sub(r"\bxbox\s+series\s+([xs])\b", r"xboxseries\1", s)
    # Balíček „+ Astro's Playroom“ a „bez mechaniky“ je pořád Digital Slim.
    s = re.sub(r"\bastro\w*\b", " ", s)
    s = re.sub(r"\bplayroom\b", " ", s)
    if re.search(r"\bbez\s+mechanik", s):
        s = re.sub(r"\bbez\s+mechanik\w*\b", " ", s)
        if not re.search(r"\bdigital\b", s):
            s += " digital"
    s = re.sub(r"\b\d+\s*(gb|tb)\b", " ", s)
    s = re.sub(r"\b\d+\s*w\b", " ", s)
    s = re.sub(r"\b\d+\s+\d+\s+litr\w*\b", " ", s)
    s = re.sub(r"\b\d+(?:[.,]\d+)?\s*litr\w*\b", " ", s)
    s = re.sub(r"\b\d+(?:[.,]\d+)?\s*l\b", " ", s)
    s = re.sub(r"\b\d+\s*bar\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def identity_tokens(name: str, brand: str | None) -> tuple[str, ...]:
    from .shops.feed import _VARIANT_TOKENS, canonicalize_name
    s = _normalize_phrases(canonicalize_name(name))
    drop_brand = _BRAND_DROP.get(brand or "", set())
    out = []
    for t in s.split():
        if t in _DROP or t in drop_brand:
            continue
        if t in _VARIANT_TOKENS or any(c.isdigit() for c in t) or len(t) >= 3:
            out.append(t)
    return tuple(sorted(set(out)))


def _tokens_strong(tokens: tuple[str, ...]) -> bool:
    if len(tokens) >= 2:
        return True
    if len(tokens) == 1 and (any(c.isdigit() for c in tokens[0]) or len(tokens[0]) >= 8):
        return True
    return False


def model_identity(name: str, category: str | None) -> tuple | None:
    """Klíč stejného modelu, nebo None když si nejsme jistí.

    Barva zůstává v tokenech, takže černá a bílá se nesloučí. Chybějící barva
    na jedné straně klíč změní – slučování „OLED“ a „OLED bílá“ řeší repair
    tím, že barvu do klíče dává jen když ji mají oba názvy.
    Úložiště u konzolí v klíči není (825 GB je jen dovětek k PS5 Slim).
    """
    from .categories import is_accessory_text
    if not category or is_accessory_text(name or ""):
        return None
    brand = primary_brand(name)
    if not brand:
        return None
    tokens = identity_tokens(name, brand)
    if not _tokens_strong(tokens):
        return None
    specs = spec_map(name)
    if category == "konzole":
        specs.pop("storage", None)
    sig = tuple(sorted(specs.items()))
    return (category, brand, tokens, sig)


def model_family(name: str, category: str | None) -> tuple | None:
    """Stejný model bez barvy, úložiště a operační paměti.

    iPhone 15 128GB černá a iPhone 15 256GB bílá jsou jedna rodina.
    iPhone 15 a iPhone 15 Pro ne. 55\" a 65\" televize ne. Příslušenství ne.
    """
    key = model_identity(name, category)
    if not key:
        return None
    cat, brand, tokens, sig = key
    from .shops.feed import _COLOR_CANON
    colors = set(_COLOR_CANON.values())
    kept = []
    for token in tokens:
        if token in colors or re.fullmatch(r"\d+(?:gb|tb)", token):
            continue
        kept.append(token)
    tokens = tuple(kept)
    if not _tokens_strong(tokens):
        return None
    sig = tuple((k, v) for k, v in sig if k not in ("storage", "ram"))
    return (cat, brand, tokens, sig)


def model_identity_loose_color(name: str, category: str | None) -> tuple | None:
    """Jako model_identity, ale bez barvy – na srovnání dvojice, kde barvu má jen jeden."""
    key = model_identity(name, category)
    if not key:
        return None
    category, brand, tokens, sig = key
    from .shops.feed import _COLOR_CANON
    colors = set(_COLOR_CANON.values())
    tokens = tuple(t for t in tokens if t not in colors)
    if not _tokens_strong(tokens):
        return None
    return (category, brand, tokens, sig)


def strong_listing_mismatch(product_name: str, label: str) -> bool:
    """Jiný výrobek, ne jen jinak zapsaný slug obchodu.

    Slug umí slepit 8/128 GB na „8128gb“, 6,6\" na „66“ a 1,8 l na „18 l“.
    To samo o sobě nabídku od produktu neodtrhne. Odtrhne jiná značka,
    jiný příkon, nebo objem, který není desetinný přepis.
    """
    from .shops.feed import _hard_conflict
    if _hard_conflict(product_name or "", label or ""):
        return True
    ba, bb = brands(product_name), brands(label)
    if ba and bb and ba.isdisjoint(bb):
        return True
    sa, sb = spec_map(product_name), spec_map(label)
    if "power" in sa and "power" in sb and abs(sa["power"] - sb["power"]) >= 50:
        return True
    if "volume" in sa and "volume" in sb and abs(sa["volume"] - sb["volume"]) > 0.05:
        hi, lo = max(sa["volume"], sb["volume"]), min(sa["volume"], sb["volume"])
        if lo <= 0 or not (hi <= 30 and abs(hi / 10.0 - lo) <= 0.2):
            return True
    return False


_OVERLAP_SKIP = frozenset({
    "android", "wireless", "bluetooth", "black", "white", "smart", "gaming",
    "digital", "edition", "slim", "version", "telefon", "smartphone",
})


def listing_overlap(product_name: str, label: str) -> bool:
    """Společný modelový token (a55, dualsense, magnifica), ne jen značka."""
    from .shops.feed import canonicalize_name
    def sig(name: str) -> set[str]:
        out = set()
        for t in canonicalize_name(name).split():
            if t in _OVERLAP_SKIP or t in _DROP:
                continue
            if re.fullmatch(r"\d+(?:gb|tb|w|mah)", t):
                continue
            if any(c.isdigit() for c in t) or len(t) >= 6:
                out.add(t)
        return out
    return bool(sig(product_name) & sig(label))


def same_model(a_name: str, a_cat: str | None, b_name: str, b_cat: str | None) -> bool:
    """Stejná značka a model. Rozdílná barva, příkon, objem nebo úložiště sloučení zakáže."""
    if not a_cat or a_cat != b_cat:
        return False
    ka = model_identity_loose_color(a_name, a_cat)
    kb = model_identity_loose_color(b_name, b_cat)
    if not ka or ka != kb:
        return False
    if not colors_agree(a_name, b_name) or spec_conflict(a_name, b_name):
        return False
    if titles_conflict(a_name, b_name):
        return False
    return True


def decent_title(name: str) -> bool:
    """Titulek, který smíme ukázat mezi slevami.

    Keyword salát z marketplace („pánské dámské polské menu“) ne. Běžný název
    modelu bez čísla (testovací „Slevy Lednice Velka“) ano.
    """
    from .categories import is_accessory_text
    if not (name or "").strip() or is_accessory_text(name):
        return False
    n = _fold(name)
    if any(p in n for p in _SPAM_PHRASES):
        return False
    words = [w for w in n.split() if w]
    if not words:
        return False
    promo = sum(1 for w in words if w in _PROMO_TOKENS)
    has_model = any(any(ch.isdigit() for ch in w) for w in words)
    if "panske" in words and "damske" in words and not has_model:
        return False
    if promo >= 3 and not has_model:
        return False
    return True
