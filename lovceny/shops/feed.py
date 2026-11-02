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


def _model_tokens(norm: str) -> set[str]:
    """Výrazné modelové tokeny (čísla/kódy typu svc7811wh, iphone, 128gb)."""
    return {t for t in norm.split() if len(t) >= 3 and any(c.isdigit() for c in t)}


def names_match(a: str, b: str) -> bool:
    """Konzervativní shoda názvů: identický normalizovaný text, nebo silný překryv modelů.
    Nikdy nepáruje jen podle značky / obecného slova."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # kratší je podřetězec delšího a sdílí aspoň jeden modelový token
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if shorter in longer:
        ta, tb = _model_tokens(na), _model_tokens(nb)
        return bool(ta and tb and ta & tb)
    ta, tb = _model_tokens(na), _model_tokens(nb)
    # obě strany mají ≥2 modelové tokeny a všechny kratší strany jsou v delší
    if ta and tb and len(ta) >= 2 and len(tb) >= 2:
        return ta <= tb or tb <= ta
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
    return FeedItem(
        item_id=r.get("ITEM_ID") or None,
        name=r.get("PRODUCTNAME") or r.get("PRODUCT") or url,
        url=strip_tracking(url),
        price=price,
        ean=(r.get("EAN") or None) or None,
        image=r.get("IMGURL") or None,
        category=r.get("CATEGORYTEXT") or None,
        original_price=_f(r.get("PRICE_VAT_ORIGINAL")),
        in_stock=(dd == "0") if dd != "" else None,
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
