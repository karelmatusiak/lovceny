"""Fronta /admin/trideni se nerozhoduje ručně.

Pravidla jsou jen ta, která už katalog umí ověřit: kód výrobce, GTIN, EAN,
ITEM_ID, kategorie z feedu a tabulka mapování. Co z toho nevyjde jistě,
zůstane mimo web (needs_review, případně skrytá nabídka). Stránka produktu
pak nemá tlačítko Koupit a není ve výpisech, v sitemapě ani v počtech na
homepage. Id, URL a historie cen zůstávají.

Varianta modelu se sloučí jen když model_family souhlasí. To je stejná
podmínka jako migrace 0002: rozdíl smí být jen barva, úložiště nebo RAM,
které parser už z klíče bere. Jinak produkty zůstanou zvlášť.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .categories import BY_SLUG, is_accessory_text
from .gtin import normalize_gtin
from .matching import model_family
from .migrate_log import log_counts
from .models import Offer, Product, TriageDecision
from .offer_trust import offer_is_promoted

BUCKETS = {
    "kategorie": frozenset({"recategorize", "fallback_category", "unmapped_category"}),
    "shoda_nazvu": frozenset({"title_match"}),
    "do_vypisu": frozenset({"confirm_listing"}),
    "varianta": frozenset({"variant_merge"}),
    "odpojeni": frozenset({"detach", "reattach"}),
}
_OUTCOMES = ("published", "hidden", "merged", "detached", "separate")


def bucket_of(kind: str) -> str:
    for name, kinds in BUCKETS.items():
        if kind in kinds:
            return name
    return "ostatni"


def empty_counts() -> dict:
    out = {key: 0 for key in _OUTCOMES}
    out["buckets"] = {
        name: {key: 0 for key in _OUTCOMES} for name in (*BUCKETS, "ostatni")
    }
    return out


def _add(stats: dict, kind: str, outcome: str, n: int = 1) -> None:
    stats[outcome] = stats.get(outcome, 0) + n
    bucket = stats["buckets"].setdefault(bucket_of(kind), {key: 0 for key in _OUTCOMES})
    bucket[outcome] = bucket.get(outcome, 0) + n


def record_auto(db: Session, *, kind: str, fingerprint: str, summary: str, reason: str,
                outcome: str, shop: str | None = None, shop_category: str | None = None,
                product_id: int | None = None, offer_id: int | None = None,
                other_product_id: int | None = None, proposed_category: str | None = None,
                payload: dict | None = None) -> None:
    """Řádek do záznamu. Není pending, takže na něj nikdo nemusí klikat."""
    import json
    from .triage import log_decision
    fp = fingerprint[:200]
    hit = db.scalar(select(TriageDecision).where(TriageDecision.fingerprint == fp))
    text = f"{reason} Výsledek pravidla: {outcome}."
    if hit is not None:
        if hit.status == "pending":
            hit.status = "auto"
            hit.reason = text[:4000]
        return
    row = TriageDecision(
        fingerprint=fp, status="auto", kind=kind, shop=shop,
        shop_category=shop_category, product_id=product_id, offer_id=offer_id,
        other_product_id=other_product_id, proposed_category=proposed_category,
        summary=summary[:300], reason=text[:4000],
        payload=json.dumps(payload, ensure_ascii=False) if payload else None,
    )
    db.add(row)
    db.flush()
    log_decision(db, row, "pravidlo", outcome[:32], summary)


def families_match(names: list[str], category: str | None) -> bool:
    """Pravda jen když parser 0002 vidí jednu řadu. Jinak se neslučuje."""
    keys = [model_family(name or "", category) for name in names]
    return bool(keys) and keys[0] is not None and all(key == keys[0] for key in keys)


def confident_category(db: Session, product: Product) -> str | None:
    """Kategorie z mapování feedu, nebo už uložený slug z těch pravidel.

    Dvě různé jisté kategorie jsou nejednoznačné a vrací None.
    """
    from .triage import resolve_category
    found: set[str] = set()
    if product.category_slug in BY_SLUG and not is_accessory_text(product.name or ""):
        found.add(product.category_slug)
    for offer in product.offers or []:
        raw = (offer.shop_category or "").strip()
        if not raw:
            continue
        res = resolve_category(db, offer.shop or "", product.name or "", raw, offer.price, url=offer.url)
        if res.action == "high" and res.slug in BY_SLUG:
            found.add(res.slug)
    if len(found) == 1:
        return next(iter(found))
    return None


def listing_outcome(db: Session, product: Product) -> tuple[str, str | None]:
    """publish + slug, nebo hide. Příslušenství, chybějící pravidlo a neověřená nabídka se schovají.

    Mapování obchodu samo nestačí: druh v názvu musí s kategorií souhlasit.
    """
    from .category_guard import guarded_category
    if is_accessory_text(product.name or ""):
        return "hide", None
    mapped = confident_category(db, product) or product.category_slug
    action, slug = guarded_category(product.name or "", mapped)
    if action == "hide" or not slug:
        return "hide", None
    promoted = [
        offer for offer in (product.offers or [])
        if not getattr(offer, "withheld", False) and offer_is_promoted(offer, product=product)
    ]
    if not promoted:
        return "hide", None
    return "publish", slug


def apply_listing(db: Session, product: Product) -> str:
    from .allegro_narrow import allegro_only, is_known_brand
    from .exclusions import bury_excluded, is_excluded_brand
    if is_excluded_brand(product.name or ""):
        bury_excluded(product)
        return "hidden"
    if allegro_only(product) and not is_known_brand(product.name or ""):
        product.needs_review = True
        return "hidden"
    outcome, slug = listing_outcome(db, product)
    if outcome == "publish" and slug:
        product.category_slug = slug
        product.category = BY_SLUG[slug].name
        product.needs_review = False
        product.catalog_hidden = False
        return "published"
    product.needs_review = True
    return "hidden"


def _gtin_set(product: Product) -> set[str]:
    found = set()
    g = normalize_gtin(product.ean)
    if g:
        found.add(g)
    for offer in product.offers or []:
        g = normalize_gtin(offer.gtin)
        if g:
            found.add(g)
    return found


def unambiguous_home(offer: Offer, label: str, products: list[Product]) -> Product | None:
    """Jiný produkt, na který nabídku smí jen kód nebo GTIN. Název sám nestačí."""
    from .part_codes import PARTS, codes_in, identify_product
    current = offer.product_id
    g = normalize_gtin(offer.gtin)
    hits: dict[int, Product] = {}
    if g:
        for product in products:
            if product.id == current or product.retired_at is not None or product.catalog_hidden:
                continue
            if g in _gtin_set(product):
                hits[product.id] = product
    blob = f"{label or ''} {offer.url or ''}"
    codes = codes_in(blob)
    lines = {PARTS[code]["line"] for code in codes}
    if len(lines) > 1:
        return None
    if len(lines) == 1:
        line = next(iter(lines))
        for product in products:
            if product.id == current or product.retired_at is not None or product.catalog_hidden:
                continue
            found = identify_product(product)
            if found and found[0] == line:
                hits[product.id] = product
    if len(hits) == 1:
        return next(iter(hits.values()))
    return None


def settle_disputed_offer(db: Session, offer: Offer, *, label: str = "",
                          clear_ean: str | None = None, products: list[Product] | None = None) -> str:
    """Přesune nabídku, jen když ji GTIN nebo kód výrobce určí. Jinak ji schová."""
    if products is None:
        products = list(db.scalars(select(Product).options(selectinload(Product.offers))).all())
    target = unambiguous_home(offer, label, products)
    if target is not None:
        offer.product_id = target.id
        offer.withheld = False
        return "detached"
    offer.withheld = True
    origin = offer.product
    if origin is not None and clear_ean and origin.ean == clear_ean:
        origin.ean = None
    return "hidden"


def _names_for(decision, products: list[Product]) -> tuple[list[Product], list[str], str | None]:
    import json
    try:
        data = json.loads(decision.payload or "{}")
    except json.JSONDecodeError:
        data = {}
    ids = [int(i) for i in (data.get("ids") or [])]
    for extra in (decision.product_id, decision.other_product_id):
        if extra and extra not in ids:
            ids.append(extra)
    chosen = [p for p in products if p.id in ids and p.retired_at is None]
    if not chosen and decision.product_id:
        chosen = [p for p in products if p.id == decision.product_id]
    category = next((p.category_slug for p in chosen if p.category_slug), None)
    return chosen, [p.name or "" for p in chosen], category


def decide_pending(db: Session, decision, products: list[Product]) -> tuple[str, str]:
    """Vrátí (outcome, detail) bez zápisu. outcome je klíč z _OUTCOMES."""
    kind = decision.kind
    if kind == "title_match":
        return "hidden", "shoda jen podle názvu, nabídka se nepřipojuje"
    if kind == "model_mismatch":
        return "hidden", "jiný model, nabídka se nepřipojuje"
    if kind == "track_url":
        return "hidden", "z adresy stránky se produkt nezakládá"
    if kind == "confirm_listing":
        product = next((p for p in products if p.id == decision.product_id), None)
        if product is None:
            return "hidden", "produkt už není"
        outcome, _slug = listing_outcome(db, product)
        return ("published" if outcome == "publish" else "hidden"), outcome
    if kind in ("recategorize", "fallback_category", "unmapped_category"):
        product = next((p for p in products if p.id == decision.product_id), None)
        if product is None:
            return "hidden", "produkt už není"
        target = decision.proposed_category
        if target in ("accessory", "ignore") or is_accessory_text(product.name or ""):
            return "hidden", "příslušenství nebo nejistá kategorie"
        if target in BY_SLUG:
            promoted = any(
                not getattr(offer, "withheld", False) and offer_is_promoted(offer, product=product)
                for offer in (product.offers or [])
            )
            if promoted:
                return "published", target
            return "hidden", "kategorie je jistá, chybí ověřená nabídka"
        return "hidden", "kategorie není v pravidlech"
    if kind == "variant_merge":
        chosen, names, category = _names_for(decision, products)
        if len(chosen) >= 2 and families_match(names, category):
            return "merged", "stejná řada, rozdíl je jen barva, úložiště nebo RAM"
        return "separate", "rozdíl není jen barva, úložiště nebo RAM"
    if kind == "merge":
        chosen, names, category = _names_for(decision, products)
        from .gtin import gtins_conflict
        if len(chosen) >= 2 and families_match(names, category) and not any(
            gtins_conflict(chosen[0], other) for other in chosen[1:]
        ):
            return "merged", "stejná řada modelu"
        return "separate", "název nestačí, nebo se GTIN liší"
    if kind in ("detach", "reattach"):
        offer = next((o for p in products for o in p.offers if o.id == decision.offer_id), None)
        if offer is None:
            return "hidden", "nabídka už není"
        import json
        try:
            data = json.loads(decision.payload or "{}")
        except json.JSONDecodeError:
            data = {}
        label = data.get("label") or ""
        home = unambiguous_home(offer, label, products)
        if kind == "reattach":
            host = next((p for p in products if p.id == decision.product_id), None)
            if host is not None and home is not None and home.id == host.id:
                return "detached", "kód nebo GTIN sedí na cílový produkt"
            return "hidden", "přesun podle názvu se neprovede"
        if home is not None:
            return "detached", "kód nebo GTIN patří k jinému produktu"
        return "hidden", "sporná nabídka se schová"
    if kind == "retire":
        product = next((p for p in products if p.id == decision.product_id), None)
        if product is not None and is_accessory_text(product.name or ""):
            return "hidden", "příslušenství"
        return "separate", "vyřazení bez pravidla se neprovede"
    return "separate", "neznámý typ"


def apply_decision(db: Session, decision, products: list[Product]) -> tuple[str, str]:
    outcome, detail = decide_pending(db, decision, products)
    kind = decision.kind
    if kind == "confirm_listing":
        product = db.get(Product, decision.product_id) if decision.product_id else None
        if product is not None:
            applied = apply_listing(db, product)
            outcome = "published" if applied == "published" else "hidden"
    elif kind in ("recategorize", "fallback_category", "unmapped_category"):
        product = db.get(Product, decision.product_id) if decision.product_id else None
        if product is not None:
            target = decision.proposed_category
            if outcome == "published" and target in BY_SLUG and not is_accessory_text(product.name or ""):
                product.category_slug = target
                product.category = BY_SLUG[target].name
                applied = apply_listing(db, product)
                outcome = "published" if applied == "published" else "hidden"
            else:
                product.needs_review = True
                if target in ("accessory", "ignore") or is_accessory_text(product.name or ""):
                    product.category_slug = None
                    product.category = None
                outcome = "hidden"
    elif outcome == "merged" and kind in ("variant_merge", "merge"):
        from .variant_merge import merge_variant_group
        chosen, _names, _category = _names_for(decision, products)
        live = [db.get(Product, p.id) for p in chosen]
        live = [p for p in live if p is not None and p.retired_at is None]
        if len(live) >= 2:
            merge_variant_group(db, live)
        else:
            outcome = "separate"
    elif kind in ("detach", "reattach") and decision.offer_id:
        offer = db.get(Offer, decision.offer_id)
        if offer is not None and not getattr(offer, "withheld", False):
            import json
            try:
                data = json.loads(decision.payload or "{}")
            except json.JSONDecodeError:
                data = {}
            if kind == "reattach":
                host = db.get(Product, decision.product_id) if decision.product_id else None
                home = unambiguous_home(offer, data.get("label") or "", products)
                if host is not None and home is not None and home.id == host.id:
                    offer.product_id = host.id
                    offer.withheld = False
                    shell = db.get(Product, decision.other_product_id) if decision.other_product_id else None
                    if shell is not None and shell.id != host.id:
                        shell.redirect_product_id = host.id
                        from .models import now
                        shell.retired_at = now()
                    outcome = "detached"
                else:
                    offer.withheld = True
                    outcome = "hidden"
            else:
                result = settle_disputed_offer(
                    db, offer, label=data.get("label") or "", clear_ean=data.get("clear_ean"), products=products,
                )
                outcome = "detached" if result == "detached" else "hidden"
        elif offer is not None and offer.withheld:
            outcome = "hidden"
    elif outcome == "hidden":
        product = db.get(Product, decision.product_id) if decision.product_id else None
        if product is not None and kind in ("retire", "model_mismatch", "track_url"):
            if kind == "retire":
                product.needs_review = True
    if decision.status == "pending":
        decision.status = "auto"
        decision.reason = ((decision.reason or "") + f" Výsledek pravidla: {outcome}. {detail}")[:4000]
        from .triage import log_decision
        log_decision(db, decision, "pravidlo", outcome[:32], detail)
    return outcome, detail


def resolve_pending(db: Session, *, dry_run: bool = True) -> dict:
    """Uzavře čekající frontu. dry_run nic nezapíše."""
    products = list(db.scalars(select(Product).options(selectinload(Product.offers))).all())
    rows = list(db.scalars(
        select(TriageDecision).where(TriageDecision.status == "pending").order_by(TriageDecision.id)
    ).all())
    stats = empty_counts()
    stats["rows"] = len(rows)
    for decision in rows:
        if dry_run:
            outcome, _detail = decide_pending(db, decision, products)
        else:
            outcome, _detail = apply_decision(db, decision, products)
        if outcome not in _OUTCOMES:
            outcome = "separate"
        _add(stats, decision.kind, outcome)
    if not dry_run:
        db.flush()
    log_counts(
        "auto_resolve",
        dry_run=int(dry_run),
        rows=stats["rows"],
        published=stats["published"],
        hidden=stats["hidden"],
        merged=stats["merged"],
        detached=stats["detached"],
        separate=stats["separate"],
    )
    return stats


def review_listings(db: Session) -> dict:
    """Místo fronty Do výpisu. Publikuje jen jistou kategorii s ověřenou nabídkou."""
    from .listing_review import name_is_from_url
    from .models import TriageDecision
    approved = set(db.scalars(
        select(TriageDecision.product_id).where(
            TriageDecision.kind == "confirm_listing",
            TriageDecision.status == "approved",
            TriageDecision.product_id.isnot(None),
        )
    ).all())
    products = list(db.scalars(
        select(Product).where(Product.retired_at.is_(None)).options(selectinload(Product.offers))
    ).all())
    published = hidden = 0
    for product in products:
        if product.id in approved:
            continue
        if product.catalog_hidden and product.category_slug:
            continue
        if not product.needs_review and product.category_slug and not name_is_from_url(product):
            continue
        if is_accessory_text(product.name or ""):
            product.needs_review = True
            hidden += 1
            continue
        outcome = apply_listing(db, product)
        if outcome == "published":
            published += 1
        else:
            hidden += 1
    db.flush()
    return {"published": published, "hidden": hidden, "queued": 0, "flagged": hidden}
