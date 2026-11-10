"""Dostupnost nabídek: skladem / na objednávku za pár dní vs. není skladem.

Heureka DELIVERY_DATE: 0 = skladem, >0 = dny do dodání (do MAX_ORDER_DAYS
počítáme jako koupitelné), chybí/záporné = nedostupné.
JSON-LD / microdata schema.org availability + české textové hlášky.
"""
from __future__ import annotations

import re

# Heureka „na objednávku“ – ještě dává smysl nabízet (ne měsíce čekání).
MAX_ORDER_DAYS = 14

_SCHEMA_IN = ("InStock", "InStoreOnly", "LimitedAvailability", "OnlineOnly", "PreOrder", "BackOrder")
_SCHEMA_OUT = ("OutOfStock", "Discontinued", "SoldOut")

# Texty na stránkách e-shopů (CZ). „Na objednávku“ = stále koupitelné.
_OOS_TEXT = (
    "není skladem",
    "neni skladem",
    "vyprodáno",
    "vyprodano",
    "momentálně nedostupné",
    "momentalne nedostupne",
    "dočasně nedostupné",
    "docasne nedostupne",
    "zboží není dostupné",
    "zbozi neni dostupne",
)
_IN_TEXT = (
    "skladem",
    "na skladě",
    "na sklade",
    "ihned k odeslání",
    "ihned k odeslani",
    "na objednávku",
    "na objednavku",
)


def parse_delivery_date(dd) -> bool | None:
    """Heureka DELIVERY_DATE → in_stock.

    0 = skladem (True), 1..MAX_ORDER_DAYS = na objednávku (True),
    chybí / nečitelné / záporné / moc dní = nedostupné (False).
    """
    if dd is None:
        return False
    s = str(dd).strip()
    if s == "":
        return False
    try:
        n = int(float(s.replace(",", ".").replace(" ", "")))
    except ValueError:
        return False
    if n < 0:
        return False
    if n == 0:
        return True
    return n <= MAX_ORDER_DAYS


def parse_schema_availability(avail: str | None) -> bool | None:
    """schema.org availability URL/token → True/False/None (neznámé)."""
    if not avail:
        return None
    s = str(avail)
    # OutOfStock obsahuje „Stock“ – nejdřív negativní tokeny.
    if any(k in s for k in _SCHEMA_OUT):
        return False
    if any(k in s for k in _SCHEMA_IN):
        return True
    return None


def parse_html_availability(html: str) -> bool | None:
    """Hrubá detekce dostupnosti z viditelného textu stránky."""
    if not html:
        return None
    low = html.lower()
    # Ořez na rozumnou délku – stačí body-ish část
    if len(low) > 200_000:
        low = low[:200_000]
    for t in _OOS_TEXT:
        if t in low:
            return False
    # „skladem“ je podřetězec „není skladem“ – OOS už vyřízeno
    for t in _IN_TEXT:
        if t in low:
            return True
    return None


def offer_is_buyable(o) -> bool:
    """Nabídka s cenou, která není výslovně nedostupná (in_stock is not False).

    True = skladem / na objednávku; None = nevíme (stále nabízíme, dokud scrape
    neřekne False); False = není skladem → ne do CTA / žebříčků.
    """
    return getattr(o, "price", None) is not None and getattr(o, "in_stock", None) is not False


def buyable_offers(offers) -> list:
    return [o for o in offers if offer_is_buyable(o)]


def priced_offers(offers) -> list:
    return [o for o in offers if getattr(o, "price", None) is not None]


def best_buyable_offer(offers, cutoffs=None):
    """Nejlevnější nabídka, kterou smíme ukázat jako cenu a tlačítko Koupit.

    Mimo hru je nedostupná, blokovaná, zastaralá a ta, jejíž adresa patří k jinému modelu.
    """
    from .offer_trust import offer_is_promoted
    promoted = [o for o in offers if offer_is_promoted(o, cutoffs)]
    return min(promoted, key=lambda o: float(o.price), default=None)


def last_known_offer(offers):
    priced = priced_offers(offers)
    return min(priced, key=lambda o: float(o.price), default=None)


def sort_offers_for_display(offers, cutoffs=None) -> list:
    """Ověřené koupitelné napřed, neověřené za nimi, není-skladem na konci."""
    from .offer_trust import offer_is_promoted, unverified_note
    priced = priced_offers(offers)

    def _key(o):
        if offer_is_promoted(o, cutoffs):
            group = 0
        elif unverified_note(o, cutoffs):
            group = 1
        else:
            group = 2
        return (group, float(o.price))

    return sorted(priced, key=_key)
