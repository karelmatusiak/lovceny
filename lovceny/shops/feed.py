"""Import oficiálního produktového feedu (Heureka XML formát, případně CSV).

Heureka XML:
<SHOP><SHOPITEM><ITEM_ID/><PRODUCTNAME/><URL/><PRICE_VAT/><EAN/>…</SHOPITEM></SHOP>
CSV: hlavička se stejnými názvy sloupců (ITEM_ID,PRODUCTNAME,URL,PRICE_VAT,EAN).

XML se parsuje streamovaně (iterparse) – feedy iStyle/Sencor mají i desítky MB.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ..util import normalize_url
from ..availability import parse_delivery_date

# Tracking parametry z feedů (Heureka UTM apod.) – pro párování a desturl je odřízneme.
_TRACKING_KEYS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
})


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


def strip_tracking(url: str) -> str:
    """normalize_url + odstranění běžných tracking parametrů (utm_*, gclid…)."""
    u = normalize_url(url)
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in _TRACKING_KEYS]
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
    """Modelové kódy (Sencor SVC 7811WH → svc7811wh po normalize; i bez mezery)."""
    # po canonicalize zbývají tokeny; spojíme i „svc“+„7811wh“ styl
    toks = canon.split()
    out = set(_MPN_RE.findall(canon.replace(" ", "")))  # celý řetězec bez mezer
    out |= set(_MPN_RE.findall(canon))
    # dvojice: písmenný prefix + číselný kód vedle sebe
    for i, t in enumerate(toks[:-1]):
        if t.isalpha() and len(t) <= 5 and any(c.isdigit() for c in toks[i + 1]):
            out.add(t + toks[i + 1])
    return {x for x in out if len(x) >= 4}


def _model_tokens(norm: str) -> set[str]:
    """Výrazné modelové tokeny (čísla/kódy typu svc7811wh, 128gb, iphone15)."""
    return {t for t in norm.split() if len(t) >= 3 and any(c.isdigit() for c in t)}


def names_match(a: str, b: str) -> bool:
    """Konzervativní shoda: kanonický název (barvy CZ↔EN), kapacita, varianta, MPN.
    Nikdy nepáruje jen podle značky. 15 ≠ 15 Plus, 128GB ≠ 256GB, černá ≠ bílá."""
    ca, cb = canonicalize_name(a), canonicalize_name(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True

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
        item_id=r.get("ITEM_ID") or None,
        name=r.get("PRODUCTNAME") or r.get("PRODUCT") or url,
        url=strip_tracking(url),
        price=price,
        ean=(r.get("EAN") or None) or None,
        image=r.get("IMGURL") or None,
        category=r.get("CATEGORYTEXT") or None,
        original_price=_f(r.get("PRICE_VAT_ORIGINAL")),
        in_stock=in_stock,
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
            out.append(it)
    return out


def parse_xml_stream(source) -> list[FeedItem]:
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
            out.append(it)
        el.clear()
    return out


def parse_csv(text: str) -> list[FeedItem]:
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    return [i for i in (_from_row(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)) if i]


def load(source: str) -> list[FeedItem]:
    """source = URL nebo cesta k souboru. Velké XML jde přes stream."""
    if source.startswith(("http://", "https://")):
        try:
            with httpx.stream("GET", source, timeout=120, follow_redirects=True) as r:
                r.raise_for_status()
                # stahujeme do temp bufferu po částech; pro typ feedu potřebujeme začátek
                chunks = []
                total = 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 80 * 1024 * 1024:  # 80 MB strop
                        raise RuntimeError(f"feed příliš velký ({total} B)")
                raw = b"".join(chunks)
        except httpx.HTTPError as e:
            raise RuntimeError(f"stažení feedu selhalo: {e}") from e
    else:
        raw = open(source, "rb").read()
    head = raw.lstrip()[:200]
    if head.startswith(b"<"):
        return parse_xml_stream(io.BytesIO(raw))
    return parse_csv(raw.decode("utf-8-sig"))
