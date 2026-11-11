"""Značky, které do veřejného katalogu nepatří a nesmí se vrátit importem.

Gigabyte a Karl Lagerfeld se schovají v datech (retired_at + catalog_hidden),
ve statickém webu, v sitemapě i při párování feedu.
"""
from __future__ import annotations

import re
import unicodedata

_RX = re.compile(r"\bgigabyte\b|\bkarl lagerfeld\b")


def _fold(name: str) -> str:
    raw = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", raw) + " "


def is_excluded_brand(name: str) -> bool:
    """True = Gigabyte nebo Karl Lagerfeld. Rozhoduje jen název, ne kategorie."""
    return bool(_RX.search(_fold(name)))


def bury_excluded(product) -> bool:
    """Schová produkt, když je to vyloučená značka. Vrací True, když se řádek změnil."""
    if product is None or not is_excluded_brand(getattr(product, "name", None) or ""):
        return False
    from .models import now
    changed = False
    if not getattr(product, "catalog_hidden", False):
        product.catalog_hidden = True
        changed = True
    if getattr(product, "retired_at", None) is None:
        product.retired_at = now()
        changed = True
    return changed
