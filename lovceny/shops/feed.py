"""Import oficiálního produktového feedu (Heureka XML, Shoptet „Ostatní“, případně CSV).

Heureka XML:
<SHOP><SHOPITEM><ITEM_ID/><PRODUCTNAME/><URL/><PRICE_VAT/><EAN/>…</SHOPITEM></SHOP>
Shoptet universal.xml (eHUB formát „Ostatní“, např. Počítače24): stejný kořen SHOP/SHOPITEM,
ale název je v PRODUCT (ne PRODUCTNAME), ITEM_ID chybí a EAN bývá GTIN-14 s úvodní nulou.
CSV: hlavička se stejnými názvy sloupců (ITEM_ID,PRODUCTNAME,URL,PRICE_VAT,EAN).

XML se parsuje streamovaně (iterparse) – feedy iStyle/Sencor/XM.cz/EVOLVEO mají i desítky MB.
"""
from __future__ import annotations

import csv
import html
import io
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

import httpx

from ..util import normalize_url
from ..availability import parse_delivery_date

log = logging.getLogger("lovceny.feed")

# Tracking parametry z feedů (Heureka UTM, FeedOptimise fo_*) – pro párování je odřízneme.
_TRACKING_KEYS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
    "fo_c", "fo_k", "fo_s", "fo_m", "fo_v",
})

# TradeDoubler products.xml je stránkovaný. Strop jednoho výpisu (Dyson a podobné malé feedy).
TD_MAX_PAGES = 25
# Allegro má ~385 tis. položek. load_shop proto bere jen krátké dotazy a součet stránek
# nesmí přerůst tenhle strop (ani kdyby registry omylem nastavilo víc).
TD_MULTI_TOTAL_CAP = 40
# Allegro fid 257027: krátké dotazy s minPrice. q je jen nápověda — po stažení
# se stejně nechá jen allowlist kategorií (q=notebook umí vrátit myš).
ALLEGRO_TD_PAGE_CAP = 3
ALLEGRO_TD_TOTAL_CAP = 30
# TradeDoubler vrací 429, když dotazy jdou hned za sebou. Mezera drží Allegro v limitu.
_TD_MIN_GAP_S = 1.2
_td_last_http = 0.0
ALLEGRO_TD_QUERIES = (
    "minPrice=2500;q=smartphone",
    "minPrice=4000;q=iphone",
    "minPrice=5000;q=notebook",
    "minPrice=8000;q=macbook",
    "minPrice=4000;q=televizor",
    "minPrice=3000;q=pračka",
    "minPrice=3000;q=chladnička",
    "minPrice=3000;q=lednička",
    "minPrice=3000;q=mraznička",
    "minPrice=1500;q=vysavač",
    "minPrice=2000;q=kávovar",
    "minPrice=8000;q=stolní počítač",
    "minPrice=700;q=sluchátka",
    "minPrice=800;q=chytré hodinky",
    "minPrice=4000;q=nintendo",
    "minPrice=5000;q=playstation",
)
_TD_PAGED = re.compile(r"api\.tradedoubler\.com/.+;page=\d+", re.I)
_TD_INNER_URL = re.compile(r"url\(([^)]+)\)", re.I)
_MONEY_RE = re.compile(r"[-+]?\d[\d\s\u00a0]*(?:[.,]\d+)?")


@dataclass
class FeedItem:
    item_id: str | None
    name: str
    url: str
    price: float
    ean: str | None = None
    image: str | None = None
    category: str | None = None
    original_price: float | None = None
    in_stock: bool | None = None
    # Google Merchant: popis a stav (new/used). Heureka je nechává prázdné.
    description: str | None = None
    condition: str | None = None


def strip_tracking(url: str) -> str:
    """normalize_url + odstranění běžných tracking parametrů (utm_*, gclid…)."""
    u = normalize_url(url)
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in _TRACKING_KEYS and not k.lower().startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(q)))


def normalize_name(name: str) -> str:
    """Pro konzervativní párování: bez diakritiky, lowercase, jen alfanumerika+mezery."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# CZ ↔ EN barvy (a běžné varianty) → kanonický anglický token
_COLOR_CANON: dict[str, str] = {}
for _canon, _alts in {
    "black": ("black", "cerna", "cerny", "cerne", "cernou", "cernych"),
    "white": ("white", "bila", "bily", "bile", "bilou", "bilych"),
    "blue": ("blue", "modra", "modry", "modre", "modrou"),
    "pink": ("pink", "ruzova", "ruzovy", "ruzove", "ruzovou", "jemne ruzova", "jemne ruzovy"),
    "green": ("green", "zelena", "zeleny", "zelene", "zelenou"),
    "yellow": ("yellow", "zluta", "zluty", "zlute", "zlutou"),
    "purple": ("purple", "violet", "fialova", "fialovy", "fialove", "fialovou"),
    "silver": ("silver", "stribrna", "stribrny", "stribrne", "stribrnou"),
    "gold": ("gold", "zlata", "zlaty", "zlate", "zlatou"),
    "gray": ("gray", "grey", "seda", "sedy", "sede", "sedou", "vesmirne seda", "vesmirne sedy",
             "space gray", "spacegrey", "spacegray", "space grey"),
    "orange": ("orange", "oranzova", "oranzovy"),
    "red": ("red", "cervena", "cerveny", "cervene"),
    "titanium": ("titanium", "titan", "titany", "prirodni titan", "natural titanium",
                 "desert titanium", "black titanium", "white titanium", "naturaltitanium",
                 "deserttitanium", "blacktitanium", "whitetitanium"),
    "starlight": ("starlight",),
    "midnight": ("midnight",),
}.items():
    for a in _alts:
        _COLOR_CANON[a] = _canon

_VARIANT_TOKENS = frozenset({
    "plus", "pro", "max", "mini", "air", "ultra", "se", "fe", "lite", "e",
})
_CAPACITY_RE = re.compile(r"\b(\d+)\s*gb\b")
# MPN / model kód: písmena+číslice (svc7811wh, sbl7170, mtp03sxa, qe55q60…)
_MPN_RE = re.compile(r"\b(?=[a-z]*\d)[a-z]{1,6}\d{2,6}[a-z0-9]{0,6}\b")


def canonicalize_name(name: str) -> str:
    """normalize_name + sjednocení barev (černý→black) a mezer u GB."""
    s = normalize_name(name)
    if not s:
        return ""
    # víceslovné barvy nejdřív (delší klíče)
    for src in sorted(_COLOR_CANON, key=len, reverse=True):
        if " " in src:
            s = s.replace(src, _COLOR_CANON[src])
    parts = []
    for t in s.split():
        parts.append(_COLOR_CANON.get(t, t))
    s = " ".join(parts)
    s = re.sub(r"\b(\d+)\s*gb\b", r"\1gb", s)
    return re.sub(r"\s+", " ", s).strip()


# Písmenné kódy barev na konci Sencor MPN (SVC 45RD, 730GR, 511TQ…)
_COLOR_CODE = "rd|bk|wh|bl|gr|tq|vt|ti|gd|yl|or|pk|sv|gn|cr|cb"
_MPN_COLOR_JOINED = re.compile(rf"\b([a-z]{{1,6}}\d{{2,5}})({_COLOR_CODE})\b", re.I)
_MPN_COLOR_NUM = re.compile(rf"\b(\d{{2,5}})({_COLOR_CODE})\b", re.I)


def variant_family_key(name: str) -> str:
    """Klíč rodiny variant: bez barvy (slova i kódu v MPN), kapacita zůstává.

    SVC 45RD-EUE3 a SVC 45BK-EUE3 → stejný klíč; 128GB Black ≠ 256GB Black.
    """
    c = canonicalize_name(name)
    if not c:
        return ""
    drop = set(_COLOR_CANON.values()) | {"eue1", "eue2", "eue3", "eue4", "eu"}
    toks = [t for t in c.split() if t not in drop]
    s = " ".join(toks)
    s = _MPN_COLOR_JOINED.sub(lambda m: m.group(1).lower(), s)
    s = _MPN_COLOR_NUM.sub(lambda m: m.group(1), s)
    return re.sub(r"\s+", " ", s).strip()


def _capacity(canon: str) -> str | None:
    m = _CAPACITY_RE.search(canon.replace("gb", " gb "))  # 128gb → 128 gb
    if m:
        return m.group(1)
    m = re.search(r"\b(\d+)gb\b", canon)
    return m.group(1) if m else None


def _colors(canon: str) -> set[str]:
    known = set(_COLOR_CANON.values())
    return {t for t in canon.split() if t in known}


def _variants(canon: str) -> set[str]:
    return {t for t in canon.split() if t in _VARIANT_TOKENS}


def _mpn_tokens(canon: str) -> set[str]:
    """Modelové kódy (Sencor SVC 7811WH → svc7811wh po normalize; i bez mezery).

    Nespojujeme variantní tokeny (pro/air/max/…) s číslem – jinak „Pro 2“ a „Air 15“
    dají slabé MPN pro2/air15 a spárují AirPods↔iPhone nebo M3↔M5.
    """
    toks = canon.split()
    out = set(_MPN_RE.findall(canon.replace(" ", "")))  # celý řetězec bez mezer
    out |= set(_MPN_RE.findall(canon))
    # Jednotky + číslo („20 bar 1,4 l“ → bar1) nejsou modelový kód.
    units = {"bar", "kg", "cm", "mm", "hz", "rpm", "wh", "mah", "ot", "w", "l", "litr", "litru"}
    # dvojice: písmenný prefix + číselný kód vedle sebe (svc + 7811wh), ne pro/air/…
    for i, t in enumerate(toks[:-1]):
        if t in units or t in _VARIANT_TOKENS:
            continue
        if t.isalpha() and len(t) <= 5 and any(c.isdigit() for c in toks[i + 1]):
            out.add(t + toks[i + 1])
    return {x for x in out if len(x) >= 4}


def _model_tokens(norm: str) -> set[str]:
    """Výrazné modelové tokeny (čísla/kódy typu svc7811wh, 128gb, iphone15, m3)."""
    out = {t for t in norm.split() if len(t) >= 3 and any(c.isdigit() for c in t)}
    # Apple Silicon m1–m5 má jen 2 znaky – jinak M3↔M3 2024 selže po zrušení slabého air15
    out |= {f"m{g}" for g in re.findall(r"\bm([1-5])\b", norm)}
    return out



_CHIP_RE = re.compile(r"\bm([1-5])\b")
# Vzájemně výlučné produktové řady – nesmí se párovat napříč.
_LINE_TOKENS = (
    "airpods", "iphone", "ipad", "macbook", "imac", "watch",
    "pixel", "galaxy", "playstation", "xbox", "switch",
)
_LINE_TV = frozenset({"televize", "tv", "television"})
_LINE_MONITOR = frozenset({"monitor", "monitoru"})


def _apple_chips(canon: str) -> set[str]:
    """Apple Silicon generace v názvu (m1–m5), prázdné když chybí."""
    return set(_CHIP_RE.findall(canon))


def _product_line(canon: str) -> str | None:
    """Hrubá produktová řada pro odmítnutí AirPods↔iPhone, TV↔monitor apod."""
    toks = set(canon.split())
    compact = canon.replace(" ", "")
    for line in _LINE_TOKENS:
        if line in toks or (line != "watch" and line in compact):
            return line
        if line == "watch" and ("watch" in toks or "applewatch" in compact):
            return "watch"
    # Monitory dřív než OLED panel (ASUS ProArt QD-OLED ≠ televize OLED55…)
    if toks & _LINE_MONITOR or "monitor" in compact or "proart" in toks:
        return "monitor"
    if toks & _LINE_TV:
        return "tv"
    # Jen modelové kódy OLED55 / QLED65… – holé „oled“ je i u monitorů
    if any(re.match(r"^(oled|qled)\d{2}", t) for t in toks):
        return "tv"
    return None


def offer_conflicts_product(product_name: str, offer_url: str, offer_title: str | None = None,
                            category_slug: str | None = None) -> bool:
    """True = nabídka zjevně patří k jinému produktu (špatný desturl / přepsané URL).

    Nestačí neshoda názvu. Konflikt je jiná řada, jiná značka, jiný příkon/objem/
    úložiště, jiná barva, jiná generace, jiný rozlišující token modelu
    (Origin/Absolute/Detect/Cyclone, FE/Ultra/Plus/Pro/Max), jiný modelový kód,
    nebo jiný druh (telefon na televizi). Stejné EAN pak nabídku nepřipojí.
    """
    other = offer_title or ""
    if not other:
        # hrubý název z URL path (istyle /products/foo-bar, allegro /nabidka/slug)
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(offer_url or "").path)
        slug = path.rstrip("/").split("/")[-1]
        other = slug
    from ..matching import prepare_offer_label, titles_conflict
    other = prepare_offer_label(other)
    if not other.strip():
        return False
    # Značka / příkon / objem / barva / generace přebíjí i shodu „bar1“ z „20 bar 1,4 l“.
    if _hard_conflict(product_name, other) or titles_conflict(product_name, other):
        return True
    from ..category_guard import category_against_offer, offer_type_conflict
    if offer_type_conflict(product_name, other):
        return True
    if category_against_offer(category_slug, other):
        return True
    return False


def _hard_conflict(a: str, b: str) -> bool:
    """Jasný konflikt řad/čipů (ne jen slabá neshoda názvu)."""
    ca, cb = canonicalize_name(a), canonicalize_name(b)
    la, lb = _product_line(ca), _product_line(cb)
    if la and lb and la != lb:
        return True
    cha, chb = _apple_chips(ca), _apple_chips(cb)
    if cha and chb and cha.isdisjoint(chb):
        return True
    return False


def names_match(a: str, b: str) -> bool:
    """Konzervativní shoda: kanonický název (barvy CZ↔EN), kapacita, varianta, MPN.
    Nikdy nepáruje jen podle značky. 15 ≠ 15 Plus, 128GB ≠ 256GB, černá ≠ bílá.
    M3 ≠ M5, AirPods ≠ iPhone, televize ≠ monitor."""
    ca, cb = canonicalize_name(a), canonicalize_name(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True

    # produktová řada a Apple Silicon – dřív než MPN (slabé air15/pro2 už ne)
    la, lb = _product_line(ca), _product_line(cb)
    if la and lb and la != lb:
        return False
    cha, chb = _apple_chips(ca), _apple_chips(cb)
    if cha and chb and cha.isdisjoint(chb):
        return False

    capa, capb = _capacity(ca), _capacity(cb)
    if capa and capb and capa != capb:
        return False
    cola, colb = _colors(ca), _colors(cb)
    if cola and colb and cola.isdisjoint(colb):
        return False
    va, vb = _variants(ca), _variants(cb)
    # varianta na jedné straně (plus/pro/…) musí být i na druhé, jinak jiné SKU
    if va != vb and (va or vb):
        # povolíme jen pokud kratší strana nemá žádnou variantu a delší má – to je nesoulad
        if va != vb:
            return False

    from ..matching import model_codes_conflict
    if model_codes_conflict(a, b):
        return False

    mpn_a, mpn_b = _mpn_tokens(ca), _mpn_tokens(cb)
    if mpn_a and mpn_b:
        # Jen přesná shoda množiny MPN – průnik (svc3001 ∩ filtr SVXD…svc3001) nestačí.
        if mpn_a == mpn_b:
            return True
        return False

    # jádro bez barev/kapacity: stejné významné tokeny
    def core(c: str) -> list[str]:
        drop = set(_COLOR_CANON.values()) | {"gb"}
        out = []
        for t in c.split():
            if t in drop or re.fullmatch(r"\d+gb", t):
                continue
            out.append(t)
        return out

    ca_c, cb_c = core(ca), core(cb)
    if not ca_c or not cb_c:
        return False
    # přesná shoda jádra
    if ca_c == cb_c:
        return True
    # kratší jádro je prefix/podmnožina delšího (stejné pořadí) + shodná kapacita/barva pokud jsou
    if len(ca_c) <= len(cb_c):
        shorter, longer = ca_c, cb_c
    else:
        shorter, longer = cb_c, ca_c
    if " ".join(shorter) in " ".join(longer):
        # vyžadujeme aspoň jeden modelový token s číslem společný
        ta, tb = _model_tokens(ca), _model_tokens(cb)
        if ta and tb and ta & tb:
            # kapacita: pokud ji má jen jedna strana, OK; barva stejně
            return True
    return False


def _f(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", "").replace("\xa0", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _ean13_checksum_ok(s: str) -> bool:
    if len(s) != 13 or not s.isdigit():
        return False
    digits = [int(c) for c in s]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - (total % 10)) % 10 == digits[-1]


def _clean_ean(raw: str | None) -> str | None:
    """EAN z feedu. Shoptet universal.xml posílá platné EAN-13 jako GTIN-14 s úvodní nulou."""
    s = re.sub(r"\s+", "", (raw or "").strip())
    if not s:
        return None
    if len(s) == 14 and s.startswith("0") and s.isdigit() and _ean13_checksum_ok(s[1:]):
        return s[1:]
    return s


def _feed_image(url: str | None) -> str | None:
    """Allegro /original/ a /s1024+/ uložíme jako /s512/. Ostatní URL beze změny."""
    if not url:
        return None
    from ..images import normalize_allegro_image
    return normalize_allegro_image(url)


def _item_id(r: dict) -> str | None:
    """ITEM_ID (Heureka). Shoptet „Ostatní“ ho nemá – když je vyplněné, použije se PRODUCTNO."""
    return (r.get("ITEM_ID") or r.get("PRODUCTNO") or "").strip() or None


def _grade_key(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", raw).strip()


def _google_detail_condition(el) -> str | None:
    """Google product_detail „stav produktu“ (NOVÉ / použité), když g:condition chybí."""
    for node in el.iter():
        if _local(node.tag) != "product_detail":
            continue
        label = value = ""
        for child in list(node):
            key = _local(child.tag)
            if key == "attribute_name":
                label = _grade_key(child.text)
            elif key == "attribute_value":
                value = (child.text or "").strip()
        if label in ("stav produktu", "stav", "condition") and value:
            return value
    return None


def _heureka_condition(el) -> str | None:
    """ITEM_TYPE (bazar) a parametr Stav produktu (Výborný, Zánovní, …)."""
    item_type = ""
    grade = ""
    for child in list(el):
        tag = child.tag.split("}")[-1]
        if tag == "ITEM_TYPE":
            item_type = (child.text or "").strip()
        elif tag == "PARAM":
            name = val = ""
            for field in list(child):
                key = field.tag.split("}")[-1]
                if key == "PARAM_NAME":
                    name = _grade_key(field.text)
                elif key == "VAL":
                    val = (field.text or "").strip()
            if name in ("stav produktu", "stav", "condition") and val:
                grade = val
    parts = [p for p in (item_type, grade) if p]
    return " ".join(parts) or None


def _from_row(r: dict) -> FeedItem | None:
    r = {k.upper(): (v or "").strip() for k, v in r.items() if k}
    price = _f(r.get("PRICE_VAT"))
    url = r.get("URL") or ""
    if not url or price is None:
        return None
    dd = r.get("DELIVERY_DATE", "")
    # DELIVERY_DATE: 0 = skladem, 1..MAX = na objednávku, chybí/záporné = nedostupné.
    # AVAILABILITY (Heureka): 1 = skladem, 0 = není – použijeme když DELIVERY_DATE chybí.
    in_stock = parse_delivery_date(dd) if str(dd).strip() != "" else None
    if in_stock is None:
        av = str(r.get("AVAILABILITY", "")).strip().lower()
        if av in ("1", "true", "yes", "skladem", "in stock", "instock"):
            in_stock = True
        elif av in ("0", "false", "no", "out of stock", "outofstock", "nedostupne", "nedostupné"):
            in_stock = False
        elif str(dd).strip() == "":
            in_stock = False  # chybějící DELIVERY_DATE i AVAILABILITY → nedostupné
    return FeedItem(
        item_id=_item_id(r),
        name=r.get("PRODUCTNAME") or r.get("PRODUCT") or url,
        url=strip_tracking(url),
        price=price,
        ean=_clean_ean(r.get("EAN")),
        image=_feed_image(r.get("IMGURL")),
        category=r.get("CATEGORYTEXT") or None,
        original_price=_f(r.get("PRICE_VAT_ORIGINAL")),
        in_stock=in_stock,
        condition=(r.get("ITEM_TYPE") or r.get("CONDITION") or None) or None,
    )


def parse_xml(data: bytes | str) -> list[FeedItem]:
    """Celý dokument v paměti (malé feedy / testovací fixtures)."""
    root = ET.fromstring(data.encode() if isinstance(data, str) else data)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] != "SHOPITEM":
            continue
        row = {c.tag.split("}")[-1]: (c.text or "") for c in el}
        it = _from_row(row)
        if it:
            it.condition = _heureka_condition(el) or it.condition
            out.append(it)
    return out


def parse_xml_stream(source, keep=None) -> list[FeedItem]:
    """Streamovaný Heureka XML: source = cesta, file-like nebo bytes.
    Po každém SHOPITEM uvolní element (iterparse) – vhodné pro velké feedy."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    out: list[FeedItem] = []
    for _event, el in ET.iterparse(source, events=("end",)):
        if el.tag.split("}")[-1] != "SHOPITEM":
            continue
        row = {c.tag.split("}")[-1]: (c.text or "") for c in el}
        it = _from_row(row)
        if it:
            it.condition = _heureka_condition(el) or it.condition
        el.clear()
        if it and (keep is None or keep(it)):
            out.append(it)
    return out


def parse_csv(text: str) -> list[FeedItem]:
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    return [i for i in (_from_row(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)) if i]


def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _strip_bom(raw: bytes) -> bytes:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:]
    return raw


def _money(value) -> float | None:
    """„899 CZK“, „1 299,50 CZK“ i holé „1290.0“."""
    if value is None:
        return None
    text = str(value).replace("\xa0", " ").strip()
    if not text:
        return None
    m = _MONEY_RE.search(text)
    return _f(m.group(0)) if m else None


def _in_stock_from_text(value: str | None, *, missing: bool | None = None) -> bool | None:
    if value is None or str(value).strip() == "":
        return missing
    av = re.sub(r"\s+", " ", str(value).strip().lower().replace("_", " ").replace("-", " "))
    if av in ("in stock", "instock", "available", "1", "true", "yes", "skladem"):
        return True
    if av in ("out of stock", "outofstock", "unavailable", "0", "false", "no",
              "nedostupne", "nedostupné", "vyprodano", "preorder", "backorder"):
        return False
    return missing


def normalize_category(text: str | None) -> str | None:
    """Sjednotí „A > B“ / „A -> B“ / HTML entity na cestu se „|“ (whitelist kategorií)."""
    if not text:
        return None
    s = html.unescape(html.unescape(str(text))).strip()
    if not s:
        return None
    s = s.replace("->", ">")
    s = s.replace(">", "|")
    s = re.sub(r"\s*\|\s*", " | ", s)
    s = re.sub(r"\s+", " ", s).strip(" |")
    return s or None


def unwrap_tradedoubler_url(url: str) -> str:
    """pdt.tradedoubler.com/click?…url(https%3A%2F%2Fshop%2Fprodukt) → cílová URL."""
    m = _TD_INNER_URL.search(url or "")
    if not m:
        return url
    return unquote(m.group(1))


def unwrap_allegro_affiliate_url(url: str) -> str:
    """allegro.cz/affiliate?redirect_url=<nabídka> → čistá URL nabídky.

    TradeDoubler u Allegra balí klik nejdřív do affiliate přesměrování. Dognet pak
    obaluje až tuhle nabídku, ne affiliate ani pdt.tradedoubler.com.
    """
    current = url or ""
    for _ in range(3):
        p = urlparse(current)
        host = (p.hostname or "").lower()
        if host != "allegro.cz" and not host.endswith(".allegro.cz"):
            return current
        if p.path.rstrip("/") != "/affiliate":
            return current
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        nxt = (q.get("redirect_url") or q.get("redirectUrl") or "").strip()
        if not nxt:
            return current
        current = unquote(nxt)
    return current


def clean_product_url(url: str) -> str:
    """TD click → Allegro affiliate → nabidka, bez utm a dalšího trackingu."""
    return normalize_url(strip_tracking(unwrap_allegro_affiliate_url(unwrap_tradedoubler_url(url))))


def _clean_product_url(url: str) -> str:
    return clean_product_url(url)


def _detect_format(head: bytes) -> str:
    h = head.lower()
    if b"urn:com:tradedoubler" in h or b"<productheader" in h:
        return "tradedoubler"
    if b"<productfeed" in h:
        return "feedoptimise"
    if b"base.google.com/ns/1.0" in h or (b"<rss" in h and b"<channel" in h):
        return "google"
    if b"<products" in h and (b"<producturl>" in h or b"<eancode>" in h):
        return "xupe"
    if b"<shopitem" in h or b"<shop>" in h:
        return "heureka"
    if h.lstrip().startswith(b"<"):
        return "xml"
    return "csv"


def _as_stream(source):
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(source)
    return source


def _html_text(value: str | None) -> str | None:
    """Google g:description bývá HTML. Na klasifikaci stačí holý text."""
    if not value or not str(value).strip():
        return None
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_google_xml(source, keep=None, *, include_description: bool = True) -> list[FeedItem]:
    """Google Shopping RSS (g:id, title, link, g:price, g:availability, …).

    Používáme ho jen jako katalog pro srovnávač. Ne pro Google Shopping / CSS / placenou reklamu.
    4Home má g:product_type. JBL.cz ho neposílá – popis a g:condition si necháme na třídění.
    keep(item) -> bool zahodí položku dřív, než se uloží (velké feedy).
    """
    out: list[FeedItem] = []
    for _event, el in ET.iterparse(_as_stream(source), events=("end",)):
        if _local(el.tag) != "item":
            continue
        direct = {}
        for c in list(el):
            key = _local(c.tag)
            direct.setdefault(key, c)
        link = (direct["link"].text or "").strip() if "link" in direct and direct["link"].text else ""
        price = _money(direct["price"].text if "price" in direct else None)
        sale = _money(direct["sale_price"].text if "sale_price" in direct else None)
        if not link or (price is None and sale is None):
            el.clear()
            continue
        # g:price je běžná cena. g:sale_price umí být jen cena se slevovým kódem
        # (TESLA ES400: 2 489 Kč, se SLEVA25 1 867 Kč). Do srovnání patří běžná cena.
        if price is None:
            price = sale
        title = (direct["title"].text or "").strip() if "title" in direct and direct["title"].text else ""
        item_id = (direct["id"].text or "").strip() if "id" in direct and direct["id"].text else None
        image = (direct["image_link"].text or "").strip() if "image_link" in direct and direct["image_link"].text else None
        gtin = (direct["gtin"].text or "").strip() if "gtin" in direct and direct["gtin"].text else None
        cat = None
        for key in ("product_type", "google_product_category"):
            node = direct.get(key)
            if node is not None and (node.text or "").strip():
                cat = node.text
                break
        avail = direct["availability"].text if "availability" in direct else None
        desc = None
        if include_description:
            desc = _html_text(direct["description"].text if "description" in direct else None)
        cond_node = direct.get("condition")
        condition = (cond_node.text or "").strip().lower() if cond_node is not None and cond_node.text else None
        if not condition:
            condition = _google_detail_condition(el)
        clean = _clean_product_url(link)
        item = FeedItem(
            item_id=item_id or None,
            name=title or clean,
            url=clean,
            price=price,
            ean=gtin or None,
            image=_feed_image(image),
            category=normalize_category(cat),
            original_price=None,
            in_stock=_in_stock_from_text(avail, missing=None),
            description=desc,
            condition=condition or None,
        )
        el.clear()
        if keep is None or keep(item):
            out.append(item)
    return out


def parse_xupe_xml(source, keep=None) -> list[FeedItem]:
    """Xupe custom XML (product / Price s DPH / ProductUrl / Category se „->“).

    ProductUrl je klik feedu, který obchod přesměruje na kartu produktu.
    DeliveryTime je ve dnech, stejně jako Heureka DELIVERY_DATE.
    """
    out: list[FeedItem] = []
    for _event, el in ET.iterparse(_as_stream(source), events=("end",)):
        if _local(el.tag) != "product":
            continue
        fields = {}
        for c in list(el):
            fields.setdefault(_local(c.tag), (c.text or "").strip())
        url = fields.get("producturl") or ""
        price = _money(fields.get("price"))
        el.clear()
        if not url or price is None:
            continue
        name = fields.get("name") or ""
        item = FeedItem(
            item_id=fields.get("identifier") or None,
            name=name or url,
            url=strip_tracking(url),
            price=price,
            ean=_clean_ean(fields.get("eancode")),
            image=_feed_image(fields.get("imageurl")),
            category=normalize_category(fields.get("category")),
            in_stock=parse_delivery_date(fields.get("deliverytime")),
        )
        if keep is None or keep(item):
            out.append(item)
    return out


def parse_feedoptimise_xml(source) -> list[FeedItem]:
    """FeedOptimise productFeed (Electrolux a podobné „Other“ feedy)."""
    out: list[FeedItem] = []
    for _event, el in ET.iterparse(_as_stream(source), events=("end",)):
        if _local(el.tag) != "product":
            continue
        name = url = image = ean = price_s = prev = avail = ""
        cat = None
        for c in list(el):
            t = _local(c.tag)
            txt = (c.text or "").strip()
            if t == "name":
                name = txt
            elif t in ("producturl", "url"):
                url = txt
            elif t in ("imageurl", "imgurl"):
                image = txt
            elif t == "availability":
                avail = txt
            elif t == "price":
                price_s = txt
            elif t == "previousprice":
                prev = txt
            elif t == "ean":
                ean = txt
            elif t == "categories":
                for cc in list(c):
                    if _local(cc.tag) == "category" and not cat:
                        cat = cc.attrib.get("name") or (cc.text or "")
        price = _money(price_s)
        if not url or price is None:
            el.clear()
            continue
        original = _money(prev)
        if original is not None and original <= price:
            original = None
        clean = _clean_product_url(url)
        out.append(FeedItem(
            item_id=(el.attrib.get("id") or None),
            name=name or clean,
            url=clean,
            price=price,
            ean=ean or None,
            image=_feed_image(image),
            category=normalize_category(cat),
            original_price=original,
            in_stock=_in_stock_from_text(avail, missing=None),
        ))
        el.clear()
    return out


def _td_product(el) -> FeedItem | None:
    name = image = ean = url = ""
    cat = None
    item_id = None
    price = None
    avail = None
    avail_seen = False
    for c in el.iter():
        t = _local(c.tag)
        txt = (c.text or "").strip()
        if t == "name" and not name and txt:
            name = txt
        elif t == "productimage" and not image and txt:
            image = txt
        elif t == "ean" and not ean and txt:
            ean = txt
        elif t == "category" and not cat:
            cat = c.attrib.get("name") or txt
        elif t == "producturl" and not url and txt:
            url = txt
        elif t == "field" and (c.attrib.get("name") or "").lower() == "availability":
            avail_seen = True
            avail = txt
        elif t == "field" and (c.attrib.get("name") or "").lower() in ("gtin", "ean") and not ean and txt:
            ean = txt
        elif t == "offer" and not item_id:
            item_id = c.attrib.get("sourceProductId") or c.attrib.get("id")
        elif t == "price" and txt:
            got = _money(txt)
            if got is not None:
                price = got
    if not url or price is None:
        return None
    clean = _clean_product_url(url)
    # Položka ve feedu bez explicitní dostupnosti bereme jako skladem (katalog ji nabízí).
    stock = _in_stock_from_text(avail if avail_seen else None, missing=True)
    return FeedItem(
        item_id=item_id,
        name=name or clean,
        url=clean,
        price=price,
        ean=_clean_ean(ean),
        image=_feed_image(image),
        category=normalize_category(cat),
        in_stock=stock,
    )


def parse_tradedoubler(data: bytes | str) -> tuple[list[FeedItem], int | None]:
    """Jedna stránka TradeDoubler products.xml. Vrací (položky, totalHits)."""
    raw = data.encode() if isinstance(data, str) else data
    raw = _strip_bom(raw)
    items: list[FeedItem] = []
    total: int | None = None
    for _event, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        t = _local(el.tag)
        if t == "totalhits":
            try:
                total = int((el.text or "").strip())
            except ValueError:
                total = None
            continue
        if t != "product":
            continue
        it = _td_product(el)
        if it:
            items.append(it)
        el.clear()
    return items, total


# Idle mezi kusy, a celý soubor. Visící spojení se přeskočí, pomalý obchod neblokuje ostatní.
FEED_READ_TIMEOUT_S = 25
FEED_TOTAL_S = 420
# ProMobily Heureka a BScom mají stovky MB kvůli popisům. Stream se neukládá celé,
# strop jen ať neskončí v nekonečnu. BScom v listopadu 2026 mělo 903 MiB.
FEED_MAX_BYTES = 1100 * 1024 * 1024
# Počty z posledního load_shop (doplněk k logu): ok / accessory / uncategorized / …
FILTER_STATS: dict[str, dict[str, int]] = {}
# Adresy položek, které pravidlo zahodilo jako příslušenství. Úklid DB je schová.
ACCESSORY_URLS: set[str] = set()


def _http_get(url: str) -> bytes:
    deadline = time.monotonic() + FEED_TOTAL_S
    try:
        with httpx.stream(
            "GET", url, timeout=httpx.Timeout(FEED_READ_TIMEOUT_S, connect=10.0), follow_redirects=True,
        ) as r:
            r.raise_for_status()
            chunks = []
            total = 0
            for chunk in r.iter_bytes():
                if time.monotonic() > deadline:
                    raise RuntimeError("feed přesáhl časový limit")
                chunks.append(chunk)
                total += len(chunk)
                if total > 80 * 1024 * 1024:  # 80 MB strop
                    raise RuntimeError(f"feed příliš velký ({total} B)")
            return b"".join(chunks)
    except httpx.HTTPError as e:
        raise RuntimeError(f"stažení feedu selhalo: {e}") from e


def _read_source(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        return _http_get(source)
    with open(source, "rb") as f:
        return f.read()


def _is_td_paged(source: str) -> bool:
    return source.startswith(("http://", "https://")) and bool(_TD_PAGED.search(source))


def _encode_td_matrix(query: str) -> str:
    """`minPrice=5000;q=pračka` → matrix parametry s kódovanou hodnotou."""
    parts = []
    for part in (query or "").split(";"):
        if not part:
            continue
        if "=" not in part:
            parts.append(part)
            continue
        key, value = part.split("=", 1)
        parts.append(f"{key}={quote(value, safe='')}")
    return ";".join(parts)


def _td_with_query(url: str, query: str) -> str:
    extra = _encode_td_matrix(query)
    if not extra:
        return url
    if "?" in url:
        path, qs = url.split("?", 1)
        return f"{path};{extra}?{qs}"
    return url + ";" + extra


def _td_get(url: str, *, gap: float) -> bytes:
    """GET s mezerou mezi požadavky a opakováním při 429."""
    global _td_last_http
    delay = 1.5
    last: Exception | None = None
    for attempt in range(4):
        wait = gap - (time.monotonic() - _td_last_http)
        if gap and _td_last_http and wait > 0:
            time.sleep(wait)
        try:
            data = _http_get(url)
            _td_last_http = time.monotonic()
            return data
        except RuntimeError as e:
            _td_last_http = time.monotonic()
            last = e
            if "429" not in str(e) or attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
    raise last or RuntimeError("stažení feedu selhalo")


def _fetch_td_pages(url: str, max_pages: int | None = None, *, gap: float = 0.0) -> tuple[list[FeedItem], int]:
    """Stáhne stránky jednoho TradeDoubler výpisu. Vrací (položky, počet HTTP požadavků).

    Když spadne až další stránka, vrátí se to, co už došlo — první stránka se nezahodí.
    """
    limit = TD_MAX_PAGES if max_pages is None else max(1, min(int(max_pages), TD_MAX_PAGES))
    m_size = re.search(r";pageSize=(\d+)", url, re.I)
    page_size = int(m_size.group(1)) if m_size else 100
    items: list[FeedItem] = []
    total: int | None = None
    pages = 0
    for page in range(1, limit + 1):
        page_url = re.sub(r";page=\d+", f";page={page}", url, count=1, flags=re.I)
        pages += 1
        try:
            batch, hits = parse_tradedoubler(_td_get(page_url, gap=gap))
        except RuntimeError as e:
            if items:
                log.warning("TradeDoubler stránka %s selhala (%s), beru %d položek", page, e, len(items))
                break
            raise
        if total is None:
            total = hits
        if not batch:
            break
        items.extend(batch)
        if total is not None and page * page_size >= total:
            break
        if len(batch) < page_size:
            break
    return items, pages


def _load_tradedoubler_paged(url: str, max_pages: int | None = None, *, gap: float = 0.0) -> list[FeedItem]:
    items, _pages = _fetch_td_pages(url, max_pages=max_pages, gap=gap)
    return items


def _load_tradedoubler_multi(url: str, queries: list[str], *, per_query: int, total_cap: int) -> list[FeedItem]:
    """Několik krátkých TradeDoubler dotazů místo celého katalogu.

    Každý dotaz má vlastní strop stránek a součet požadavků nepřesáhne total_cap
    (a ani TD_MULTI_TOTAL_CAP). Stejná nabídka z víc dotazů se bere jen jednou.
    Selhání jednoho dotazu ostatní nezahodí; když nesedne nic, vyhodí poslední chybu.
    """
    per_query = max(1, min(int(per_query), TD_MAX_PAGES))
    total_cap = max(1, min(int(total_cap), TD_MULTI_TOTAL_CAP))
    seen: set[str] = set()
    out: list[FeedItem] = []
    used = 0
    errors: list[str] = []
    for query in queries:
        if used >= total_cap:
            break
        cap = min(per_query, total_cap - used)
        page_url = _td_with_query(url, query)
        try:
            batch, pages = _fetch_td_pages(page_url, max_pages=cap, gap=_TD_MIN_GAP_S)
        except Exception as e:  # noqa: BLE001 – další dotaz může projít
            errors.append(f"{query}: {e}")
            log.warning("TradeDoubler dotaz %s selhal (%s)", query, e)
            used += 1
            continue
        used += max(pages, 1)
        for it in batch:
            if it.url in seen:
                continue
            seen.add(it.url)
            out.append(it)
    if not out and errors:
        raise RuntimeError(errors[-1])
    log.info("TradeDoubler dotazy: %d URL, %d stránek, %d položek", len(queries), used, len(out))
    return out


class _ChunkReader:
    """Spojí už přečtenou hlavičku se zbytkem iterátoru. read() pro iterparse, bez celého souboru v paměti."""

    def __init__(self, head: bytes, chunks, deadline: float, max_bytes: int):
        self.buf = head
        self.chunks = iter(chunks)
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.total = len(head)
        self.eof = False

    def _count(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.total > self.max_bytes:
            raise RuntimeError(f"feed příliš velký ({self.total} B)")

    def read(self, n: int = -1) -> bytes:
        if time.monotonic() > self.deadline:
            raise RuntimeError("feed přesáhl časový limit")
        if n is None or n < 0:
            parts = [self.buf]
            self.buf = b""
            if not self.eof:
                for chunk in self.chunks:
                    self._count(chunk)
                    parts.append(chunk)
                self.eof = True
            return b"".join(parts)
        while len(self.buf) < n and not self.eof:
            try:
                chunk = next(self.chunks)
            except StopIteration:
                self.eof = True
                break
            self._count(chunk)
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def _parse_stream(source, kind: str, keep=None) -> list[FeedItem]:
    if kind == "google":
        return parse_google_xml(source, keep=keep, include_description=keep is None)
    if kind == "xupe":
        return parse_xupe_xml(source, keep=keep)
    if kind == "feedoptimise":
        return parse_feedoptimise_xml(source)
    if kind == "tradedoubler":
        raw = source.read() if hasattr(source, "read") else source
        return parse_tradedoubler(raw)[0]
    if kind == "csv":
        raw = source.read() if hasattr(source, "read") else source
        text = raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw
        return parse_csv(text)
    return parse_xml_stream(source, keep=keep)


def _parse_http(url: str, keep=None, *, total_s: float | None = None) -> list[FeedItem]:
    """Stáhne feed po kusech a parsuje ho streamem. Celé XML se neskládá do jednoho bytes."""
    deadline = time.monotonic() + (FEED_TOTAL_S if total_s is None else total_s)
    timeout = httpx.Timeout(FEED_READ_TIMEOUT_S, connect=10.0)
    headers = {"User-Agent": "LovcenyBot/0.1 (+https://lovceny.cz/pro-obchody)"}
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True, headers=headers) as resp:
            resp.raise_for_status()
            chunks = resp.iter_bytes()
            head = b""
            for chunk in chunks:
                if time.monotonic() > deadline:
                    raise RuntimeError("feed přesáhl časový limit")
                head += chunk
                if len(head) >= 8192:
                    break
            head = _strip_bom(head)
            if not head.lstrip().startswith(b"<"):
                rest = _ChunkReader(b"", chunks, deadline, FEED_MAX_BYTES)
                raw = head + rest.read()
                return parse_csv(raw.decode("utf-8-sig"))
            kind = _detect_format(head[:8000])
            reader = _ChunkReader(head, chunks, deadline, FEED_MAX_BYTES)
            return _parse_stream(reader, kind, keep=keep)
    except httpx.HTTPError as e:
        raise RuntimeError(f"stažení feedu selhalo: {e}") from e


def parse_bytes(raw: bytes) -> list[FeedItem]:
    """Rozpozná Heureka / Google RSS / Xupe / FeedOptimise / TradeDoubler / CSV."""
    raw = _strip_bom(raw)
    if not raw.lstrip().startswith(b"<"):
        return parse_csv(raw.decode("utf-8-sig"))
    kind = _detect_format(raw[:8000])
    if kind == "csv":
        return parse_csv(raw.decode("utf-8-sig"))
    return _parse_stream(io.BytesIO(raw), kind)


def load(source: str) -> list[FeedItem]:
    """source = URL nebo cesta k souboru. Velké XML jde přes stream.

    TradeDoubler URL s `;page=` stáhne stránky, dokud nejsou prázdné nebo totalHits nesedí
    (nejvýš TD_MAX_PAGES).
    """
    if _is_td_paged(source):
        return _load_tradedoubler_paged(source)
    return parse_bytes(_read_source(source))


def _td_queries(shop) -> list[str]:
    raw = (getattr(shop, "extra", None) or {}).get("td_queries") or []
    return [str(q).strip() for q in raw if str(q).strip()]


def _catalog_keep(slug: str):
    """Nechá jen položky, které pravidlo zařadí. Počty filtrů jdou do FILTER_STATS."""
    from ..categories import classify_feed_item
    stats = {"ok": 0, "accessory": 0, "uncategorized": 0, "excluded": 0, "price": 0, "conflict": 0}
    FILTER_STATS[slug] = stats

    def keep(item: FeedItem) -> bool:
        _slug, reason = classify_feed_item(slug, item.name, item.category, item.price, item.url)
        stats[reason] = stats.get(reason, 0) + 1
        if reason == "accessory" and item.url:
            ACCESSORY_URLS.add(strip_tracking(item.url))
        return reason == "ok"

    return keep


def _load_shop_source(shop, src: str) -> list[FeedItem]:
    """Jedna URL feedu. Allegro (td_queries) neprochází celý katalog, jen krátké dotazy.

    Nové velké feedy (Xupe, Bscom, ProMobily) se parsují streamem a příslušenství
    ani nezařazené listy se do paměti neukládají.
    """
    from ..categories import CATALOG_RULE_SHOPS
    queries = _td_queries(shop)
    if queries and _is_td_paged(src):
        extra = getattr(shop, "extra", None) or {}
        per = int(extra.get("td_page_cap") or 1)
        total = int(extra.get("td_total_cap") or per)
        # Vlastní URL s ;q= je jeden výpis (env override), ne násobek výchozích dotazů.
        if re.search(r";q=", src, re.I):
            return _load_tradedoubler_paged(src, max_pages=min(per, TD_MAX_PAGES), gap=_TD_MIN_GAP_S)
        return _load_tradedoubler_multi(src, queries, per_query=per, total_cap=total)
    slug = getattr(shop, "slug", None)
    if slug in CATALOG_RULE_SHOPS and str(src).startswith(("http://", "https://")):
        return _parse_http(src, keep=_catalog_keep(slug))
    items = load(src)
    if slug in CATALOG_RULE_SHOPS:
        keep = _catalog_keep(slug)
        items = [it for it in items if keep(it)]
    return items


def load_shop(shop) -> list[FeedItem]:
    """Načte feed obchodu. Veřejná záloha (feed_url_fallback) jen když není env override."""
    sources = shop.feed_sources()
    if not sources:
        raise RuntimeError("feed URL není nastaven")
    errors: list[str] = []
    for i, src in enumerate(sources):
        try:
            items = _load_shop_source(shop, src)
        except Exception as e:  # noqa: BLE001 – další URL je záloha
            errors.append(str(e))
            if i + 1 < len(sources):
                log.warning("feed %s selhal (%s), zkouším zálohu", src, e)
            continue
        if getattr(shop, "slug", None) == "jbl":
            from ..categories import annotate_jbl_items
            return annotate_jbl_items(items)
        stats = FILTER_STATS.get(getattr(shop, "slug", "") or "")
        if stats:
            log.info("filtr %s: %s", shop.slug, stats)
        return items
    raise RuntimeError(errors[-1] if errors else "feed selhal")
