"""Kategorie z mapování obchodu smí zůstat, jen když název neříká jiný druh výrobku.

Žádný odhad modelu. Druh určí jen jednoznačné slovo v názvu (hodinky, vysavač,
televize, …). Když název ukazuje přesně jednu jinou kategorii, produkt se tam
přesune. Když ukazuje víc druhů a současná kategorie mezi nimi není, schová se.
Příslušenství není hlavní produkt. Název bez druhového slova mapování nepopírá.
"""
from __future__ import annotations

import re
import unicodedata

from .categories import BY_SLUG, is_accessory_text

# Silný druh výrobku. Slabá slova (termostat na mrazáku) se počítají, jen když
# silný druh v názvu není.
_STRONG: dict[str, tuple[str, ...]] = {
    "chytre-hodinky": (
        r"\bhodink", r"\bwatche?s?\b", r"\bsmartwatch", r"\bsmart band\b",
        r"\bmi band\b", r"\bamazfit\b", r"\bforerunner\b", r"\bvenu\b", r"\bfenix",
        r"\bgalaxy watch", r"\bband\b",
    ),
    "sluchatka": (
        r"\bsluchat", r"\bairpods\b", r"\bearbuds?\b", r"\bheadphones?\b",
        r"\bearphones?\b", r"\bbuds\d?\b", r"\bheadset\b", r"\bbonemax\b",
        r"\bbonepro\b", r"\bquietcomfort\b",
    ),
    "vysavace": (r"\bvysavac", r"\bvacuum\b", r"\broborock\b", r"\broomba\b", r"\bdyson v\d"),
    "televize": (r"\bteleviz", r"\btv\b", r"\bqled\b", r"\bqned\b"),
    "notebooky": (
        r"\bnotebook", r"\blaptop\b", r"\bmacbook\b", r"\bideapad\b", r"\bthinkpad\b",
        r"\bvivobook\b", r"\baspire\b", r"\blatitude\b", r"\bprobook\b", r"\belitebook\b",
    ),
    "telefony": (
        r"\biphone\b", r"\bsmartphone\b", r"\bmobilni telefon", r"\bgalaxy [saz]\d",
        r"\bgalaxy z\b", r"\bpixel\b", r"\bredmi\b", r"\bpoco\b", r"\bmotorola\b",
        r"\boneplus\b", r"\bleitzphone\b",
    ),
    "pracky": (r"\bprack",),
    "lednice": (r"\blednic", r"\bchladnic", r"\bmraznic", r"\bmrazak"),
    "kavovary": (r"\bkavovar", r"\bespresso\b", r"\bcoffee maker\b", r"\bcoffeemachine\b"),
    "konzole": (r"\bplaystation\b", r"\bnintendo switch\b", r"\bxbox series\b", r"\bps5\b"),
    "fotoaparaty": (r"\bfotoapar", r"\bgopro\b", r"\bosmo\b", r"\beos\b", r"\bnikon z\d"),
    "pocitace": (
        r"\bstolni pocitac", r"\ball in one\b", r"\bprodesk\b", r"\boptiplex\b",
        r"\bthinkcentre\b", r"\bthinkstation\b", r"\bpc24\b",
    ),
}
_WEAK: dict[str, tuple[str, ...]] = {
    "chytra-domacnost": (
        r"\bzasuvk", r"\btermostat", r"\bchuvic", r"\bdoorbell\b", r"\bshelly\b",
        r"\bhue\b", r"\btapo\b", r"\becho\b", r"\bnest\b", r"\bsnimac\b",
        r"\bvideochuvic", r"\bbaby monitor\b",
    ),
    "konzole": (r"\bkonzole\b",),
}
_STORAGE = re.compile(r"\b(\d+)\s*(gb|tb)\b")


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", raw) + " "


def _match(folded: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, folded) for pattern in patterns)


def type_hits(name: str) -> set[str]:
    """Kategorie, které název sám jednoznačně pojmenuje. Prázdná množina = nic neříká."""
    if is_accessory_text(name or ""):
        return set()
    folded = _fold(name)
    hits = {slug for slug, patterns in _STRONG.items() if _match(folded, patterns)}
    if not hits:
        hits = {slug for slug, patterns in _WEAK.items() if _match(folded, patterns)}
    if "sluchatka" in hits:
        hits.discard("konzole")
        hits.discard("telefony")
    if "chytre-hodinky" in hits:
        hits.discard("telefony")
        hits.discard("konzole")
    if "vysavace" in hits:
        hits.discard("chytra-domacnost")
    if "lednice" in hits or "pracky" in hits:
        hits.discard("chytra-domacnost")
    return hits


def guarded_category(name: str, current: str | None) -> tuple[str, str | None]:
    """keep / move / hide a cílová kategorie.

    keep nechává současnou. move ji mění na druh z názvu. hide je needs_review.
    """
    if is_accessory_text(name or ""):
        return "hide", None
    hits = type_hits(name)
    if len(hits) > 1:
        if current in hits:
            return "keep", current
        return "hide", None
    if len(hits) == 1:
        target = next(iter(hits))
        if target == current:
            return "keep", current
        if target in BY_SLUG:
            return "move", target
        return "hide", None
    if current in BY_SLUG:
        return "keep", current
    return "hide", None


def storage_labels(text: str) -> set[str]:
    folded = _fold(text)
    return {f"{int(amount)}{unit}" for amount, unit in _STORAGE.findall(folded)}


def storage_conflict(product_name: str, other: str) -> bool:
    """True = oba texty uvádějí kapacitu a neshodnou se (2 TB proti 512 GB)."""
    left, right = storage_labels(product_name), storage_labels(other)
    if not left or not right:
        return False
    return left.isdisjoint(right)


def offer_type_conflict(product_name: str, offer_text: str) -> bool:
    """True = adresa nebo název nabídky je jiný druh výrobku než produkt."""
    product_hits = type_hits(product_name)
    offer_hits = type_hits(offer_text)
    if not product_hits or not offer_hits:
        return False
    return product_hits.isdisjoint(offer_hits)


def category_against_offer(category_slug: str | None, offer_text: str) -> bool:
    """True = nabídka pojmenovává jiný druh, než je kategorie produktu.

    Název bez druhového slova (jen kód modelu) kategorii nepopírá.
    Telefon (Galaxy S25 FE) na televizi (OM55N) ano.
    """
    if not category_slug or category_slug not in BY_SLUG:
        return False
    hits = type_hits(offer_text or "")
    if not hits:
        return False
    return category_slug not in hits
