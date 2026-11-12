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


def on_order_not_stock(text: str | None) -> bool:
    """„Na objednávku“ s dodáním v týdnech není skladem.

    DronPro tak píše „Na objednávku – odešleme za 1–4 týdny“ a zároveň v JSON-LD
    nechá InStock. Krátká objednávka do pár dnů (jiné obchody) skladem zůstává.
    """
    if not text:
        return False
    import unicodedata
    raw = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    idx = raw.find("na objednavku")
    if idx < 0:
        return False
    return bool(re.search(r"tydn|mesic", raw[idx:idx + 80]))


def parse_html_availability(html: str) -> bool | None:
    """Hrubá detekce dostupnosti z viditelného textu stránky."""
    if not html:
        return None
    low = html.lower()
    # Ořez na rozumnou délku – stačí body-ish část
    if len(low) > 200_000:
        low = low[:200_000]
    if on_order_not_stock(low):
        return False
    if "dpp-stock__label--objednavka" in low:
        return False
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


def offer_is_suppressed(offer) -> bool:
    """Nabídka, kterou na webu neukazujeme. Historie cen v databázi zůstává.

    Schované: withheld, captcha Mironetu, použité/repasované, příslušenství
    přilepené k hlavnímu produktu a kód modelu s jinými číslicemi
    (ECAM 220.30 ≠ 220.80). Stejné EAN jiný kód nenechá.
    """
    if getattr(offer, "withheld", False):
        return True
    from .offer_trust import offer_is_captcha_hidden
    if offer_is_captcha_hidden(offer):
        return True
    from .categories import is_accessory_text, is_non_new
    from .matching import label_from_url, model_codes_conflict
    label = label_from_url(getattr(offer, "url", "") or "")
    shop_category = getattr(offer, "shop_category", None)
    if is_non_new(label, shop_category=shop_category):
        return True
    product = getattr(offer, "product", None)
    product_name = getattr(product, "name", "") or ""
    if is_accessory_text(label) and not is_accessory_text(product_name):
        return True
    if product is not None and product_name:
        if model_codes_conflict(product_name, getattr(offer, "url", "") or "") or model_codes_conflict(product_name, label):
            return True
    return False


def priced_offers(offers) -> list:
    return [o for o in offers
            if getattr(o, "price", None) is not None and not offer_is_suppressed(o)]


def best_buyable_offer(offers, cutoffs=None, product=None):
    """Nejlevnější nabídka, kterou smíme ukázat jako cenu a tlačítko Koupit.

    Mimo hru je nedostupná, blokovaná, zastaralá a ta, jejíž adresa patří k jinému modelu.
    `product` se předá rovnou, ať kontrola adresy nespouští dotaz na vztah offer.product.
    """
    from .offer_trust import offer_is_promoted
    promoted = [o for o in offers if offer_is_promoted(o, cutoffs, product)]
    return min(promoted, key=lambda o: float(o.price), default=None)


def last_known_offer(offers):
    priced = priced_offers(offers)
    return min(priced, key=lambda o: float(o.price), default=None)


def sort_offers_for_display(offers, cutoffs=None) -> list:
    """Jen podle ceny. Ověření a sklad mění tlačítko Koupit, ne pořadí.

    `cutoffs` zůstává v podpisu kvůli volajícím. Stránka slibuje řazení výhradně podle ceny.
    """
    priced = priced_offers(offers)
    return sorted(priced, key=lambda o: (float(o.price), int(getattr(o, "id", 0) or 0)))


def visible_offer_count(offers) -> int:
    """Počet nabídek, které stránka produktu opravdu vypíše."""
    return len(priced_offers(offers))
