"""Normalizace GTIN/EAN. Bereme jen 8, 12, 13 nebo 14 číslic s platným kontrolním součtem."""
from __future__ import annotations

import re


def check_digit(body: str) -> int:
    """GS1: zprava střídavě váhy 3 a 1, začíná se vahou 3."""
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - (total % 10)) % 10


def checksum_ok(digits: str) -> bool:
    if len(digits) not in (8, 12, 13, 14) or not digits.isdigit():
        return False
    return check_digit(digits[:-1]) == int(digits[-1])


def normalize_gtin(raw: str | None) -> str | None:
    """Vrátí nejkratší platný tvar, nebo None.

    Úvodní nuly u GTIN-14 (Shoptet posílá EAN-13 jako 14 číslic) se odříznou,
    když kratší tvar pořád vychází a má platný součet. Neplatný kód je None.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    candidates: list[str] = []
    if len(digits) in (8, 12, 13, 14):
        candidates.append(digits)
    for length in (14, 13, 12, 8):
        if len(digits) <= length:
            continue
        head, tail = digits[:-length], digits[-length:]
        if head and set(head) <= {"0"}:
            candidates.append(tail)
    stripped = digits.lstrip("0")
    if len(stripped) in (8, 12, 13, 14):
        candidates.append(stripped)
    valids = [c for c in candidates if checksum_ok(c)]
    if not valids:
        return None
    return sorted(valids, key=lambda s: (len(s), s))[0]


def product_gtins(product) -> set[str]:
    """Platná GTIN na produktu a jeho nabídkách."""
    out: set[str] = set()
    g = normalize_gtin(getattr(product, "ean", None))
    if g:
        out.add(g)
    for offer in getattr(product, "offers", None) or []:
        if getattr(offer, "gtin", None):
            out.add(offer.gtin)
    return out


def gtins_conflict(a, b) -> bool:
    """Oba produkty mají GTIN a neshodují se. Prázdná strana konflikt není."""
    ga, gb = product_gtins(a), product_gtins(b)
    if not ga or not gb:
        return False
    return ga != gb
