"""Jednorázové soft-tombstony za produkty hard-smazané před soft-retire (2026-11-03).

Bez těchto řádků by /produkt/<id>-… zůstalo 404. Idempotentní: existující id přeskočí.
"""
from __future__ import annotations

from sqlalchemy import text

# (id, name, category_slug) – category_slug dle cap/reject důvodu při smazání
TOMBSTONES: list[tuple[int, str, str | None]] = [
    (160, "iPhone 17e 256 GB bílý", "telefony"),
    (162, "iPhone 17e 512 GB jemně růžová", "telefony"),
    (166, "Apple iPhone 16 Plus 256GB - růžový", "telefony"),
    (167, "Apple iPhone 16 128GB - růžový", "telefony"),
    (170, "Apple iPhone 16 128GB - modrozelený", "telefony"),
    (171, "Apple iPhone 16e 128GB - černý", "telefony"),
    (230, 'MacBook Pro 14,2", Apple M5 Pro 18jádrové CPU, 20j', "notebooky"),
    (233, 'MacBook Pro 14,2", Apple M5 Pro 18jádrové CPU, 20j', "notebooky"),
    (235, "Sencor SVC 3001 Orca", "vysavace"),
    (246, "Sencor SVC 5001YL", "vysavace"),
    (248, "Sencor SVC 8936TI", "vysavace"),
    (249, "Sencor SVC 1080TI", "vysavace"),
    (250, "Sencor SVC 9300BK", "vysavace"),
    (255, "Sencor SVC 8505TI", "vysavace"),
    (267, "Apple iPhone 18 Pro 2 TB stříbrný", "telefony"),
    (268, "Apple Watch Ultra 4 • GPS + Cellular • 49mm pouzdro", "chytre-hodinky"),
    (269, "Apple Watch Ultra 4 • GPS + Cellular • 49mm pouzdro", "chytre-hodinky"),
    (270, "Apple Watch Ultra 4 • GPS + Cellular • 49mm pouzdro", "chytre-hodinky"),
    (271, "Apple Watch Ultra 4 • GPS + Cellular • 49mm pouzdro", "chytre-hodinky"),
    (272, "Apple Watch Ultra 4 • GPS + Cellular • 49mm pouzdro", "chytre-hodinky"),
    (273, "Apple Watch Ultra 4 • GPS + Cellular • 49mm pouzdro", "chytre-hodinky"),
    (287, "NETGEAR M4350-12X12F Řízený L3 10G Ethernet", None),
    (289, "55'' LED Samsung OM55N-S - FHD,4000cd,MI,SO,24/7", "televize"),
    (290, "Apple iPhone 18 Pro barva Burgundy paměť 2 TB", "telefony"),
    (291, "NETGEAR M4350-8X8F Řízený L3 10G Ethernet", None),
    (292, "Gigabyte AORUS PRIME 3/AP3A7N7-5100/Tower/R7-9700X", None),
    (293, "75'' LED Samsung QB75C - UHD,350cd,MI,16/7 LH75QBC", "televize"),
    (296, "65'' D-LED Philips 65BDL4050Q-UHD,500cd,AN,24/7", "televize"),
    (297, "49'' D-LED Philips 49BDL2105X-FHD,IPS,500cd,VW,24/", "televize"),
    (299, "65'' D-LED Philips 65BDL5050D-UHD,600cd,AN,24/7", "televize"),
    (300, "MikroTik CCR2216-1G-12XS-2XQ, Cloud Core Router", None),
    (301, "GIGABYTE Radeon AI PRO R9700 AI TOP (rev. 1.0)/32G", None),
]


def restore_tombstones(db) -> int:
    """Vloží chybějící soft-retired řádky. Vrací počet nově vložených."""
    from .models import Product, now
    from .util import slugify

    n = 0
    for pid, name, cat in TOMBSTONES:
        if db.get(Product, pid) is not None:
            continue
        db.add(Product(
            id=pid, name=name, slug=slugify(name)[:300],
            category_slug=cat, retired_at=now(),
        ))
        n += 1
    if not n:
        return 0
    db.commit()
    # Postgres: posuň sequence za MAX(id), ať nová INSERT nekoliduje
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(text(
            "SELECT setval(pg_get_serial_sequence('products', 'id'),"
            " (SELECT COALESCE(MAX(id), 1) FROM products))"
        ))
        db.commit()
    return n
