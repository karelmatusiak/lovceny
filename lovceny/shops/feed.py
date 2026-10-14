"""Import oficiálního produktového feedu (Heureka XML formát, případně CSV).

Heureka XML:
<SHOP><SHOPITEM><ITEM_ID/><PRODUCTNAME/><URL/><PRICE_VAT/><EAN/>…</SHOPITEM></SHOP>
CSV: hlavička se stejnými názvy sloupců (ITEM_ID,PRODUCTNAME,URL,PRICE_VAT,EAN).
"""
import csv
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx


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


def _f(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", "")) if v not in (None, "") else None
    except ValueError:
        return None


def _from_row(r: dict) -> FeedItem | None:
    r = {k.upper(): (v or "").strip() for k, v in r.items() if k}
    price = _f(r.get("PRICE_VAT"))
    if not r.get("URL") or price is None:
        return None
    dd = r.get("DELIVERY_DATE", "")
    return FeedItem(item_id=r.get("ITEM_ID") or None, name=r.get("PRODUCTNAME") or r.get("PRODUCT") or r["URL"],
                    url=r["URL"], price=price, ean=r.get("EAN") or None, image=r.get("IMGURL") or None,
                    category=r.get("CATEGORYTEXT") or None, original_price=_f(r.get("PRICE_VAT_ORIGINAL")),
                    in_stock=(dd == "0") if dd != "" else None)


def parse_xml(data: bytes | str) -> list[FeedItem]:
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


def parse_csv(text: str) -> list[FeedItem]:
    dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
    return [i for i in (_from_row(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)) if i]


def load(source: str) -> list[FeedItem]:
    """source = URL nebo cesta k souboru."""
    if source.startswith(("http://", "https://")):
        r = httpx.get(source, timeout=60, follow_redirects=True)
        r.raise_for_status()
        raw = r.content
    else:
        raw = open(source, "rb").read()
    head = raw[:200].lstrip()
    return parse_xml(raw) if head.startswith(b"<") else parse_csv(raw.decode("utf-8-sig"))
