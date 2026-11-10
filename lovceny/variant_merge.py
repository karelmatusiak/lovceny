"""Jedna stránka na model. Barva, úložiště a RAM jsou varianty, ne další produkty.

Kanonická stránka je aktivní produkt s nejnižším id ve skupině. Ostatní id zůstávají
a jejich URL jde 301 na kanonickou stránku. Nabídky a historie cen se nepřepisují,
jen se přenesou na kanonický produkt a dostanou variantu.

Samotný import to nespouští. Jednou to zapíše migrace 0002 při nasazení.
Náhled: python -m lovceny.variant_merge
Zápis mimo migraci: python -m lovceny.variant_merge --apply
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .matching import _tokens_strong, model_family
from .migrate_log import log_counts
from .models import Product, Variant, now
from .specs import extract

# Šum v názvu, který nerozbíjí řadu modelu, ale nestačí na automatické sloučení.
_PROPOSAL_DROP = frozenset({
    "aktivnim", "hluku", "2024", "chassis", "e", "edition", "verze",
    "vydrz", "hodin", "az",
})
_ANC_WORDS = frozenset({"potlacenim", "potlacovanim", "potlaceni"})


def option_label(name: str) -> str:
    raw = extract(name or "")
    parts = []
    if raw.get("storage"):
        parts.append(str(raw["storage"]))
    if raw.get("ram"):
        parts.append(f"RAM {raw['ram']}")
    if raw.get("color"):
        parts.append(str(raw["color"]))
    return ", ".join(parts) or "Základní"


def option_signature(name: str) -> str:
    return option_label(name).casefold()


def proposal_family(name: str, category: str | None) -> tuple | None:
    """Volnější klíč pro frontu. Kód výrobce a skloňování návrh neslepí samo.

    MacBookové zkratky 128/4T/96, nano-texture „Nan“ a E-Chassis u PS5 spadnou.
    Pro, Max a generace zůstanou. Cizí modelové kódy (WGE03200 ≠ WUU28T71) zůstávají.
    """
    import re
    from .matching import _fold
    from .part_codes import PARTS
    key = model_family(name, category)
    if key is None:
        return None
    cat, brand, tokens, sig = key
    hour_numbers = set(re.findall(r"\b(\d+)\s*hodin", _fold(name or "")))
    out = []
    anc = False
    macbook = "macbook" in tokens
    for token in tokens:
        # Jen ověřené kódy z part_codes. Ostatní MPN v klíči zůstane, ať se nesejdou jiné modely.
        if token in PARTS:
            continue
        if token == "m5max":
            out.extend(("m5", "max"))
            continue
        if token in _ANC_WORDS:
            anc = True
            continue
        if token in _PROPOSAL_DROP or token in hour_numbers:
            continue
        # „bezdrátová“ je dovětek v uloženém názvu AirPods, ne rozdíl modelu.
        # Jinde slovo necháme: Mcdodo s ANC a bez něj se liší jen tímhle tokenem.
        if token == "bezdratova" and "airpods" in tokens:
            continue
        if macbook and (re.fullmatch(r"\d{3,}", token) or re.fullmatch(r"\d+t", token) or token in {"96", "nan"}):
            continue
        out.append(token)
    if anc:
        out.append("anc")
    if "macbook" in tokens:
        sig = tuple((k, v) for k, v in sig if k != "power")
    kept = tuple(sorted(set(out)))
    if not _tokens_strong(kept):
        return None
    return (cat, brand, kept, sig)


def plan_variant_merges(products: list[Product]) -> list[list[Product]]:
    """Skupiny aktivních produktů, které jsou jeden model. Jednotlivce nevrací."""
    buckets: dict[tuple, list[Product]] = {}
    for product in products:
        if product.retired_at is not None or product.catalog_hidden:
            continue
        key = model_family(product.name or "", product.category_slug)
        if key is None:
            continue
        buckets.setdefault(key, []).append(product)
    groups = []
    for members in buckets.values():
        alive = [p for p in members if p.retired_at is None]
        if len(alive) >= 2:
            groups.append(sorted(alive, key=lambda p: p.id))
    groups.sort(key=lambda g: g[0].id)
    return groups


def _summary(groups: list[list[Product]], *, applied: bool) -> dict:
    samples = []
    for group in groups[:40]:
        canonical = group[0]
        others = ", ".join(f"#{p.id} {option_label(p.name or '')}" for p in group[1:])
        samples.append(f"#{canonical.id} {canonical.name} ← {others}")
    return {
        "dry_run": not applied,
        "groups": len(groups),
        "products": sum(len(g) for g in groups),
        "retired": sum(len(g) - 1 for g in groups),
        "samples": samples,
    }


def merge_variant_group(db: Session, group: list[Product], labels: dict[int, str] | None = None) -> int:
    """Sloučí jednu skupinu jako 0002. Nejnižší id zůstane. Vrací počet vyřazených id."""
    from .catalog_repair import _move_dependents
    ordered = sorted([p for p in group if p.retired_at is None], key=lambda p: p.id)
    if len(ordered) < 2:
        return 0
    canonical = ordered[0]
    retired = 0
    for product in ordered:
        label = (labels or {}).get(product.id) or option_label(product.name or "")
        signature = (label or option_signature(product.name or "")).casefold()[:200]
        variant = db.scalar(select(Variant).where(Variant.source_product_id == product.id))
        if variant is None:
            variant = db.scalar(select(Variant).where(
                Variant.product_id == canonical.id, Variant.signature == signature,
            ))
        if variant is None:
            variant = Variant(
                product_id=canonical.id, source_product_id=product.id,
                signature=signature, label=label[:200], created_at=now(),
            )
            db.add(variant)
            db.flush()
        elif variant.product_id != canonical.id:
            variant.product_id = canonical.id
            variant.label = label[:200]
        for offer in list(product.offers):
            offer.product_id = canonical.id
            offer.variant_id = variant.id
        if product.id == canonical.id:
            continue
        _move_dependents(db, product, canonical)
        product.redirect_product_id = canonical.id
        product.retired_at = now()
        retired += 1
    db.flush()
    return retired


def apply_variant_merges(db: Session, *, dry_run: bool = True) -> dict:
    products = list(db.scalars(
        select(Product).options(selectinload(Product.offers))
    ).all())
    groups = plan_variant_merges(products)
    if not dry_run:
        for group in groups:
            merge_variant_group(db, group)
    out = _summary(groups, applied=not dry_run)
    log_counts("variant_merge", dry_run=int(dry_run), groups=out["groups"], retired=out["retired"])
    return out


def plan_variant_proposals(products: list[Product]) -> list[list[Product]]:
    """Skupiny, které jsou blízko jedné řady, ale kód ani GTIN to nepotvrzuje."""
    buckets: dict[tuple, list[Product]] = {}
    for product in products:
        if product.retired_at is not None or product.catalog_hidden or product.needs_review:
            continue
        key = proposal_family(product.name or "", product.category_slug)
        if key is None:
            continue
        buckets.setdefault(key, []).append(product)
    groups = []
    for members in buckets.values():
        alive = sorted([p for p in members if p.retired_at is None], key=lambda p: p.id)
        # Stejný model_family už umí 0002. Sem patří jen to, co se liší skloňováním nebo zkratkou.
        if len(alive) >= 2 and not _same_family(alive):
            groups.append(alive)
    groups.sort(key=lambda g: g[0].id)
    return groups


def _same_family(products: list[Product]) -> bool:
    keys = [model_family(p.name or "", p.category_slug) for p in products]
    return bool(keys) and keys[0] is not None and all(k == keys[0] for k in keys)


def enqueue_variant_proposals(db: Session) -> dict:
    """Návrh do /admin/trideni. Nic nesloučí, dokud to někdo neschválí."""
    from .triage import enqueue
    products = list(db.scalars(select(Product).options(selectinload(Product.offers))).all())
    groups = plan_variant_proposals(products)
    queued = 0
    ids = []
    for group in groups:
        ordered = sorted(group, key=lambda p: p.id)
        canonical = ordered[0]
        id_list = [p.id for p in ordered]
        ids.append(id_list)
        fingerprint = "variant_merge:" + "-".join(str(i) for i in id_list)
        names = "; ".join(f"#{p.id} {(p.name or '')[:80]}" for p in ordered)
        if enqueue(
            db, kind="variant_merge", fingerprint=fingerprint,
            summary=f"Varianta modelu: sloučit {len(ordered) - 1} do #{canonical.id}",
            reason=("Názvy jsou stejná řada (značka a model), ale kód výrobce ani GTIN to nepotvrzují. "
                    "Schválení udělá totéž co migrace variant: nejnižší id zůstane, ostatní URL jdou 301, "
                    "historie cen zůstane a barva, paměť a úložiště budou volby."),
            product_id=canonical.id, other_product_id=ordered[1].id,
            payload={"ids": id_list, "names": names},
        ):
            queued += 1
    stats = {"queued": queued, "groups": len(groups), "ids": ids}
    log_counts("variant_proposal", queued=queued, groups=len(groups))
    return stats


def apply_variant_proposal(db: Session, decision) -> tuple[bool, str]:
    import json
    try:
        data = json.loads(decision.payload or "{}")
    except json.JSONDecodeError:
        data = {}
    ids = [int(i) for i in (data.get("ids") or [])]
    if decision.product_id and decision.product_id not in ids:
        ids.append(decision.product_id)
    if decision.other_product_id and decision.other_product_id not in ids:
        ids.append(decision.other_product_id)
    products = [db.get(Product, i) for i in ids]
    products = [p for p in products if p is not None and p.retired_at is None]
    if len(products) < 2:
        return True, "už sloučeno"
    retired = merge_variant_group(db, products)
    canonical = min(products, key=lambda p: p.id)
    log_counts("variant_proposal_apply", canonical=canonical.id, retired=retired)
    return True, f"varianty sloučeny do #{canonical.id}, vyřazeno {retired}"


def main(argv=None) -> int:
    import argparse
    from .db import SessionLocal, init_db
    ap = argparse.ArgumentParser(description="Náhled nebo zápis sloučení variant modelu.")
    ap.add_argument("--apply", action="store_true", help="zapsat změny (bez toho jen náhled)")
    args = ap.parse_args(argv)
    init_db()
    db = SessionLocal()
    try:
        stats = apply_variant_merges(db, dry_run=not args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
