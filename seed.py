#!/usr/bin/env python3
"""Naplní DB katalogem populární elektroniky (seed_catalog.json, ~90 produktů z obchodů, které čtení
dovolují) a několika produkty z Planea s VYGENEROVANOU demo historií (price_points.is_demo = true).

Idempotentní: produkt se pozná podle URL nabídky; už existující se jen zařadí do kategorie, znovu se
nestahuje a demo historie se nepřidá podruhé. Ceny se stahují živě (robots.txt, ≥ 6 s mezi dotazy na doménu).

  python seed.py                  # katalog + demo produkty
  python seed.py --no-demo        # bez vygenerované demo historie
  python seed.py --category pracky --limit 5
  python seed.py --dry-run        # jen vypíše, co by přidal (nic nestahuje, nic nezapisuje)
  python seed.py --purge-demo     # smaže všechna demo data
"""
import json
from pathlib import Path
import argparse
import math
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from lovceny.db import SessionLocal, init_db
from lovceny.models import PricePoint, Offer
from lovceny.service import add_by_url

# (url, tvar demo historie)
PRODUCTS = [
    ("https://www.planeo.cz/apple-airpods-3-s-nabijecim-pouzdrem-magsafe", "buy"),
    ("https://www.planeo.cz/apple-iphone-15-128-gb-black", "fake"),
    ("https://www.planeo.cz/jbl-flip-6-teal", "wait"),
    ("https://www.planeo.cz/xiaomi-redmi-note-12-5g-4-128-gb-onyx-gray", "buy"),
]
# Alza/Datart: zdroj = feed (zatím nenastaven) → nabídka „cena bude doplněna“
FEED_OFFERS = [
    ("https://www.alza.cz/search.htm?exps=iPhone%2015%20128GB", 1),
    ("https://www.datart.cz/vyhledavani?q=iPhone%2015%20128GB", 1),
]


def demo_history(db, offer, shape, days=120):
    cur = float(offer.price)
    rnd = random.Random(offer.id)
    today = datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    for d in range(days, 0, -1):
        t = today - timedelta(days=d)
        wave = math.sin(d / 9) * 0.015 + rnd.uniform(-0.008, 0.008)
        if shape == "buy":      # dlouho dráž, teď u minima
            f = 1.10 + 0.04 * (d / days) + wave
        elif shape == "fake":   # obvyklá cena = dnešní, 3 týdny zpět zdraženo o ~18 %
            f = (1.19 if 4 <= d <= 20 else 1.0) + wave
        else:                   # wait: před měsícem výrazně levněji
            f = (0.88 if 25 <= d <= 45 else 0.97) + wave
        db.add(PricePoint(offer=offer, price=round(cur * f, -1) - 10 if cur > 1000 else round(cur * f),
                          original_price=None, captured_at=t, is_demo=True, source="demo"))


CATALOG_FILE = Path(__file__).with_name("seed_catalog.json")


def load_catalog() -> list[dict]:
    """[{cat, name, url}] – kategorie musí existovat, URL patřit scrape obchodu nebo feedu s URL."""
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def _apply_meta(prod, it):
    """Název z katalogu, když obchod žádný nedal (název je jen odvozený z URL), a obrázek z výpisu obchodu."""
    from lovceny.service import _assign_image, _name_from_url
    from lovceny.util import slugify
    if any(prod.name == _name_from_url(o.url) for o in prod.offers):
        prod.name, prod.slug = it["name"], slugify(it["name"])
    _assign_image(prod, it.get("image"))
    if not prod.category_slug:
        prod.category_slug = it["cat"]


def seed_catalog(db, items, dry_run=False, fetch=True) -> dict:
    from lovceny.categories import BY_SLUG
    from lovceny.shops import shop_for_url
    stats = {"new": 0, "existing": 0, "priced": 0, "failed": 0}
    for it in items:
        assert it["cat"] in BY_SLUG, it
        shop = shop_for_url(it["url"])
        url = shop.canonical_url(it["url"]) if shop else it["url"]
        o = db.scalar(select(Offer).where(Offer.url == url))
        if o:
            stats["existing"] += 1
            _apply_meta(o.product, it)
            db.commit()
            continue
        if dry_run:
            print(f"+  [{it['cat']}] {it['name']}  {url}")
            stats["new"] += 1
            continue
        prod, msg = add_by_url(db, url, fetch_now=fetch, name=it["name"], category_slug=it["cat"])
        if prod is None:
            print(f"!! {it['name']}: {msg}")
            stats["failed"] += 1
            continue
        prod.category_slug = it["cat"]
        _apply_meta(prod, it)
        db.commit()
        stats["new"] += 1
        price = prod.offers[0].price
        stats["priced" if price else "failed"] += 1
        print(f"{'OK' if price else '!!'} [{it['cat']}] {prod.name} – {price or msg}")
    return stats


def soft_retire_product(prod) -> bool:
    """Označ produkt jako retired (soft). Vrací True pokud byl právě označen.
    Nabídky už musí být pryč – řádek zůstává kvůli 301 na kategorii.
    """
    from lovceny.models import now as utcnow
    if prod.retired_at is not None:
        return False
    prod.retired_at = utcnow()
    return True


def retire_vestavne(db, dry_run=False) -> dict:
    """Odstraní nabídky vestavne-spotrebice.cz (ConnectTimeout z GitHub Actions) a osamělé produkty."""
    from sqlalchemy import or_
    rows = db.scalars(select(Offer).where(or_(Offer.shop == "vestavne",
                                              Offer.url.contains("vestavne-spotrebice.cz")))).all()
    stats = {"offers": 0, "products": 0}
    if dry_run:
        stats["offers"] = len(rows)
        return stats
    products = {o.product_id: o.product for o in rows}
    for o in rows:
        db.delete(o)
        stats["offers"] += 1
    db.flush()
    from lovceny.models import Product
    for pid, prod in products.items():
        db.refresh(prod)
        if not prod.offers and soft_retire_product(prod):
            stats["products"] += 1
    db.commit()
    return stats


# Max nových feed produktů na (značka, kategorie) – ať Vysavače nejsou plné 20× Sencor barev.
FEED_MAX_PER_BRAND_CAT = 6
# Allegro má v jedné kategorii desítky reálných konfigurací jedné značky (RAM/disk u notebooků).
# Strop 20 pořád brání tomu, aby Apple nebo HP zaplnily celý seed.
ALLEGRO_BRAND_CAP = 20
ALLEGRO_SEED_LIMIT = 500


# Obecná slova na začátku názvu („Notebook Hp…“) nejsou značka.
_GENERIC_PRODUCT_WORDS = frozenset({
    "notebook", "smartphone", "telefon", "mobil", "mobilni", "televizor", "televize",
    "pracka", "lednice", "lednicka", "chladnicka", "mraznicka", "mrazak", "vysavac",
    "tycovy", "roboticky", "kavovar", "automaticky", "pakovy", "sluchatka", "tablet",
    "pocitac", "stolni", "chytry", "chytre", "hodinky", "akumulatorovy", "bezdratovy",
    "bezdratova", "bezdratove", "dratova", "dratove", "novy", "nova", "nove", "herni",
    "prenosna", "prenosny", "mala", "male", "maly", "panske", "damske", "bluetooth",
    "pro", "max", "plus", "mini", "ultra", "lite", "air", "vykonny", "vykonna",
    "silny", "silna", "sportovni", "originalni", "usi",
})
_KNOWN_BRANDS = (
    "samsung", "xiaomi", "redmi", "lenovo", "asus", "acer", "dell", "huawei", "motorola",
    "nokia", "sony", "philips", "bosch", "siemens", "whirlpool", "beko", "aeg", "gorenje",
    "candy", "hisense", "tcl", "roborock", "rowenta", "delonghi", "krups", "nivona",
    "jbl", "bose", "garmin", "amazfit", "nintendo", "logitech", "honor", "realme", "poco",
    "oneplus", "nothing", "microsoft", "sharp", "panasonic", "miele", "haier", "indesit",
    "amica", "concept", "eta", "kruger", "hp", "lg",
)


def _ascii_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _feed_brand(name: str) -> str:
    n = _ascii_name(name)
    for brand in ("sencor", "dyson", "electrolux", "klarstein"):
        if brand in n:
            return brand
    if any(k in n for k in (
        "iphone", "ipad", "airpods", "apple watch", "imac", "mac mini", "mac studio",
        "macbook", "apple ",
    )):
        return "apple"
    padded = f" {n} "
    for brand in _KNOWN_BRANDS:
        if f" {brand} " in padded:
            return brand
    toks = [t for t in n.split() if t not in _GENERIC_PRODUCT_WORDS and len(t) > 2]
    return toks[0] if toks else "other"


def _round_robin(items, key_fn, order=()):
    """Střídá skupiny (nejdražší ve skupině první), ať strop nesežere jen jednu řadu."""
    buckets: dict[str, list] = {}
    for it in items:
        buckets.setdefault(key_fn(it), []).append(it)
    for group in buckets.values():
        group.sort(key=lambda x: float(x.price or 0), reverse=True)
    keys = [k for k in order if k in buckets]
    keys += [k for k in buckets if k not in keys]
    out = []
    while any(buckets.values()):
        for k in keys:
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out


def pick_feed_create_items(items, allowed_cats: tuple[str, ...], *, brand_filter=None,
                           max_per_brand_cat: int = FEED_MAX_PER_BRAND_CAT,
                           prefer_ean: bool = False, spread_categories: bool = False,
                           family_key=None, spread_by=None):
    """1 produkt na barevnou rodinu (nejlevnější skladem) + max N na značku/kategorii (vyšší cena).

    prefer_ean: při shodě rodiny nechá položku s GTIN/EAN (párování nabídek).
    spread_categories: střídá kategorie, ať max_create nesežere jen nejdražší značku.
    spread_by: střídá vlastní skupiny (u JBL řady sluchátek). Brand cap se pak neaplikuje,
    strop drží max_create volajícího.
    """
    from lovceny.categories import accept_feed_item
    from lovceny.shops.feed import variant_family_key

    key_fn = family_key or variant_family_key
    families: dict[tuple, object] = {}
    for it in items:
        cat = accept_feed_item(it.name, it.category, it.price, url=it.url)
        if cat not in allowed_cats:
            continue
        if brand_filter is not None and not brand_filter(it.name):
            continue
        brand = _feed_brand(it.name)
        fam = key_fn(it.name) or (it.name or "").lower()
        key = (cat, brand, fam)
        prev = families.get(key)
        if prev is None:
            families[key] = it
            continue

        def better(a, b):
            a_ok = a.in_stock is not False
            b_ok = b.in_stock is not False
            if a_ok != b_ok:
                return a if a_ok else b
            if prefer_ean and bool(a.ean) != bool(b.ean):
                return a if a.ean else b
            return a if float(a.price or 0) <= float(b.price or 0) else b

        families[key] = better(prev, it)

    by_cb: dict[tuple, list] = {}
    for (cat, brand, _fam), it in families.items():
        by_cb.setdefault((cat, brand), []).append(it)
    picked = []
    for group in by_cb.values():
        group.sort(key=lambda x: float(x.price or 0), reverse=True)
        picked.extend(group if spread_by else group[:max_per_brand_cat])
    if spread_by:
        return _round_robin(picked, spread_by, getattr(spread_by, "order", ()))
    if not spread_categories:
        picked.sort(key=lambda x: float(x.price or 0), reverse=True)
        return picked
    buckets: dict[str, list] = {c: [] for c in allowed_cats}
    rest = []
    for it in picked:
        cat = accept_feed_item(it.name, it.category, it.price, url=it.url)
        if cat in buckets:
            buckets[cat].append(it)
        else:
            rest.append(it)
    for group in buckets.values():
        group.sort(key=lambda x: (0 if x.ean else 1, -float(x.price or 0)))
    out = []
    while any(buckets.values()):
        for cat in allowed_cats:
            if buckets[cat]:
                out.append(buckets[cat].pop(0))
    out.extend(rest)
    return out


# Obchody, jejichž katalog držíme z feedu (retire špatných nabídek i seed).
FEED_CATALOG_SHOPS = (
    "istyle", "sencor", "xm", "evolveo", "pocitace24", "jbl",
    "electronicstar", "4home", "electrolux", "dyson", "allegro",
)

# Barvy z JBL feedu, které obecný klíč rodiny nezná (latte, squad…). Jinak by cap
# sežral víc barev jednoho modelu.
_JBL_EXTRA_COLORS = (
    "beige", "lavender", "champagne", "latte", "sand", "teal", "peach", "coral",
    "cyan", "copper", "coper", "ice", "lime", "aqua", "ghost", "squad",
)


def _jbl_family_key(name: str) -> str:
    from lovceny.shops.feed import variant_family_key
    n = name or ""
    for w in _JBL_EXTRA_COLORS:
        n = re.sub(rf"\b{re.escape(w)}\b", " ", n, flags=re.I)
    n = re.sub(r"\band\b", " ", n, flags=re.I)
    return variant_family_key(n)


# Pořadí, ve kterém seed střídá řady. Jinak by cap 25 sebral jen Quantum a Tour.
_JBL_LINE_ORDER = (
    "tune", "live", "quantum", "tour", "wave", "yurbuds",
    "endurance", "soundgear", "sense", "jr", "vibe", "other",
)


def _jbl_line(item) -> str:
    n = _ascii_name(getattr(item, "name", "") or "")
    if "yurbuds" in n.split():
        return "yurbuds"
    for line in ("soundgear", "tune", "live", "quantum", "tour", "wave", "endurance", "vibe"):
        if re.search(rf"\b{line}\b", n):
            return line
    if re.search(r"\bjr\d|\bjunior\b", n):
        return "jr"
    if re.search(r"\bsense\b", n):
        return "sense"
    return "other"


_jbl_line.order = _JBL_LINE_ORDER


def allegro_seed_limit(feed_limit: int) -> int:
    """Výchozí --feed-limit 25 nechá Allegro na 500. Jiné číslo platí i pro něj."""
    if feed_limit == 25:
        return ALLEGRO_SEED_LIMIT
    return feed_limit


def brand_cap_for_shops(shops: set[str]) -> int:
    """Allegro smí víc kusů na značku. Ostatní feedové obchody zůstávají na malém stropu."""
    if shops == {"allegro"}:
        return ALLEGRO_BRAND_CAP
    return FEED_MAX_PER_BRAND_CAT


def seed_from_feeds(db, *, istyle_limit=25, sencor_limit=25, xm_limit=25, evolveo_limit=25,
                    pocitace24_limit=25, jbl_limit=25,
                    electronicstar_limit=25, fourhome_limit=25, electrolux_limit=25, dyson_limit=25,
                    allegro_limit=ALLEGRO_SEED_LIMIT,
                    dry_run=False) -> dict:
    """Konzervativně přidá produkty z Heureka, eHUB a Dognet feedů.

    accept_feed_item + sloučení barev + FEED_MAX_PER_BRAND_CAT na značku/kategorii.
    Příslušenství (filtry, sáčky, kabely, kryty) do seedu nepatří.
    """
    from lovceny.shops import SHOPS, feed as feedmod
    from lovceny.service import apply_feed

    stats = {}
    targets = [
        ("istyle", istyle_limit, ("telefony", "notebooky", "sluchatka", "chytre-hodinky", "fotoaparaty", "chytra-domacnost")),
        ("sencor", sencor_limit, ("vysavace", "kavovary", "televize", "chytra-domacnost")),
        ("xm", xm_limit, ("telefony", "sluchatka", "chytre-hodinky", "vysavace", "kavovary")),  # bez notebooky: Heureka „Počítače a notebooky|*“ je plná monitorů/GPU
        # bez notebooky/televize: ve feedu nejsou (podložky, zdroje, antény, TV příslušenství). Telefony jen nad min. cenou.
        ("evolveo", evolveo_limit, ("sluchatka", "chytra-domacnost", "telefony")),
        # universal.xml: notebooky + stolní/repasované počítače. Kabely, brašny, monitory a software ne.
        ("pocitace24", pocitace24_limit, ("notebooky", "pocitace")),
        # JBL.cz: jen sluchátka. Reproduktory/soundbary nemají kategorii, bazar a příslušenství ne.
        ("jbl", jbl_limit, ("sluchatka",)),
        # Dognet: jen kategorie, které Lovceny umí (pračky, lednice, kávovary, vysavače).
        ("electronicstar", electronicstar_limit, ("kavovary", "lednice", "vysavace")),
        ("4home", fourhome_limit, ("vysavace", "kavovary", "pracky", "lednice")),
        ("electrolux", electrolux_limit, ("pracky", "lednice", "kavovary", "vysavace")),
        ("dyson", dyson_limit, ("vysavace",)),
        # Allegro: stejné krátké TD dotazy, po filtru stovky přístrojů (strop na značku drží rozmanitost).
        ("allegro", allegro_limit, (
            "telefony", "notebooky", "pocitace", "televize", "pracky", "lednice",
            "kavovary", "vysavace", "sluchatka", "chytre-hodinky", "konzole",
        )),
    ]

    def _apple_only(name: str) -> bool:
        n = (name or "").lower()
        apple = ("iphone", "ipad", "airpods", "apple watch", "imac", "mac mini", "mac studio",
                 "macbook air", "macbook pro", "apple macbook")
        return any(k in n for k in apple)

    for slug, limit, cats in targets:
        shop = SHOPS[slug]
        src = shop.feed_url
        if not src:
            stats[slug] = {"error": "no feed url"}
            continue
        if dry_run:
            print(f"[dry-run] {slug}: feed {src} (limit {limit}, cats {cats})")
            stats[slug] = {"dry_run": True, "limit": limit}
            continue
        try:
            items = feedmod.load_shop(shop)
        except Exception as e:  # noqa: BLE001
            print(f"!! feed {slug}: {e}")
            stats[slug] = {"error": str(e)}
            continue
        st_attach = apply_feed(db, slug, items, create_missing=False)
        if slug == "istyle":
            bf = _apple_only
        elif slug == "allegro":
            from lovceny.allegro_narrow import is_known_brand
            bf = is_known_brand
        else:
            bf = None
        picked = pick_feed_create_items(
            items, cats, brand_filter=bf,
            max_per_brand_cat=ALLEGRO_BRAND_CAP if slug == "allegro" else FEED_MAX_PER_BRAND_CAT,
            prefer_ean=slug == "allegro",
            spread_categories=slug == "allegro",
            family_key=_jbl_family_key if slug == "jbl" else None,
            # Jedna značka: strop 6 by nechal jen nejdražší Quantum. Řady se střídají, max_create drží ~25.
            spread_by=_jbl_line if slug == "jbl" else None,
        )
        st_create = apply_feed(db, slug, picked, create_missing=True, max_create=limit)
        st = {
            "loaded": len(items),
            "picked": len(picked),
            "attached": st_attach.get("attached", 0) + st_create.get("attached", 0),
            "matched": st_attach.get("matched", 0) + st_create.get("matched", 0),
            "created": st_create.get("created", 0),
            "skipped": st_attach.get("skipped", 0),
        }
        print(f"Feed seed {slug}: loaded={len(items)} picked={len(picked)} → {st}")
        stats[slug] = st
    return stats


def retire_bad_feed_items(db, dry_run=False) -> dict:
    """Odstraní příslušenství/špatné kategorie, čističe oken, barevné duplicity a převis capu.

    Soft-retire: nabídky smažeme, produktový řádek zůstane s retired_at (301 /produkt → kategorie).
    """
    from lovceny.categories import (
        MIN_PRICE_CZK, accept_feed_item, category_url_conflict, is_accessory_text,
        is_computer_display,
    )
    from lovceny.models import Product
    from lovceny.shops import SHOPS, feed as feedmod
    from lovceny.shops.feed import offer_conflicts_product, strip_tracking, variant_family_key

    stats = {"offers": 0, "products": 0, "reasons": []}
    bad_urls: set[str] = set()
    url_meta: dict[str, str] = {}
    ean_to_reject_names: dict[str, list[str]] = {}
    for slug in FEED_CATALOG_SHOPS:
        shop = SHOPS.get(slug)
        if not shop or not shop.feed_url:
            continue
        try:
            items = feedmod.load_shop(shop)
        except Exception as e:  # noqa: BLE001
            stats["reasons"].append(f"feed {slug}: {e}")
            continue
        for it in items:
            clean = strip_tracking(it.url)
            # Monitor v listu „Sluchátka“ není příslušenství. Nabídku nemažeme –
            # kategorii srovná repair_display_headphones, historie ceny zůstane.
            if is_computer_display(it.name) and not is_accessory_text(it.name, it.category):
                continue
            if is_accessory_text(it.name, it.category) or accept_feed_item(
                it.name, it.category, it.price, url=it.url
            ) is None:
                bad_urls.add(clean)
                url_meta[clean] = it.name
                if it.ean:
                    url_meta[clean + "#ean"] = it.ean
                    ean_to_reject_names.setdefault(it.ean, []).append(it.name)

    url_rx = (
        "filtr", "sacek", "sacky", "kryt", "pouzdro", "obal", "sklo", "folie", "case",
        "cover", "nahradni", "prislusenstvi", "comfort", "powerbank", "kabel", "adapter",
        "cistic-oken", "cistici-oken", "window-clean", "roboticky-cistic-oken",
    )
    offers = db.scalars(select(Offer).where(Offer.shop.in_(FEED_CATALOG_SHOPS))).all()
    to_delete = []
    for o in offers:
        clean = strip_tracking(o.url)
        low = (o.url or "").lower()
        reason = None
        prod = o.product
        if clean in bad_urls:
            reason = f"feed-reject:{url_meta.get(clean, '')[:40]}"
        elif any(k in low for k in url_rx):
            reason = "url-keyword"
        elif is_accessory_text(prod.name if prod else "", None):
            reason = "product-name-accessory"
        elif (
            prod and category_url_conflict(prod.category_slug, prod.name or "", o.url)
            and not is_computer_display(prod.name or "")
        ):
            reason = "url-category-conflict"
        else:
            slug = prod.category_slug if prod else None
            min_p = MIN_PRICE_CZK.get(slug or "", 0)
            if o.price is not None and min_p and float(o.price) < min_p:
                reason = f"price<{min_p}"
        if reason:
            to_delete.append((o, reason))

    feed_shops = FEED_CATALOG_SHOPS

    def _is_feed_only(p) -> bool:
        shops = {x.shop for x in p.offers}
        return bool(shops) and shops <= set(feed_shops)

    def _best_price(p):
        prices = [float(x.price) for x in p.offers if x.price is not None and x.in_stock is not False]
        if not prices:
            prices = [float(x.price) for x in p.offers if x.price is not None]
        return min(prices) if prices else 0.0

    def _max_price(p):
        prices = [float(x.price) for x in p.offers if x.price is not None]
        return max(prices) if prices else 0.0

    # Naplánuj variant+cap deletions na aktuálním stavu (po odečtení to_delete nabídek)
    doomed_offer_ids = {o.id for o, _ in to_delete}
    # simuluj produkty po smazání nabídek
    all_prods = list(db.scalars(select(Product)).all())
    surviving_offers = {
        p.id: [o for o in p.offers if o.id not in doomed_offer_ids]
        for p in all_prods
    }

    def feed_only_after(p) -> bool:
        shops = {o.shop for o in surviving_offers.get(p.id, [])}
        return bool(shops) and shops <= set(feed_shops)

    fam_groups: dict[tuple, list] = {}
    for p in all_prods:
        if not surviving_offers.get(p.id):
            continue
        if not feed_only_after(p):
            continue
        cat = p.category_slug or "?"
        brand = _feed_brand(p.name)
        fam = variant_family_key(p.name) or (p.name or "").lower()
        fam_groups.setdefault((cat, brand, fam), []).append(p)

    extra_products: list[tuple] = []  # (product, reason)
    keep_prefer: set[int] = set()
    for _key, group in fam_groups.items():
        if len(group) <= 1:
            keep_prefer.add(group[0].id)
            continue
        # u variant ber nejlevnější (reprezentant)
        ranked = sorted(group, key=lambda p: (
            0 if any(o.in_stock is not False for o in surviving_offers[p.id]) else 1,
            _best_price(p),
        ))
        keep_prefer.add(ranked[0].id)
        for p in ranked[1:]:
            extra_products.append((p, f"variant-of:{ranked[0].name[:40]}"))

    drop_ids = {p.id for p, _ in extra_products}
    cap_groups: dict[tuple, list] = {}
    for p in all_prods:
        if p.id in drop_ids:
            continue
        if not surviving_offers.get(p.id) or not feed_only_after(p):
            continue
        # pokud by po to_delete nezůstaly nabídky, produkt stejně spadne
        cat = p.category_slug or "?"
        brand = _feed_brand(p.name)
        cap_groups.setdefault((cat, brand), []).append(p)

    for (cat, brand), group in cap_groups.items():
        shops = set()
        for p in group:
            shops |= {o.shop for o in surviving_offers.get(p.id, [])}
        cap = brand_cap_for_shops(shops)
        if len(group) <= cap:
            continue
        ranked = sorted(group, key=_max_price, reverse=True)
        for p in ranked[cap:]:
            extra_products.append((p, f"cap:{cat}/{brand}>{cap}"))

    if dry_run:
        stats["offers"] = len(to_delete)
        stats["products"] = len({p.id for p, _ in extra_products})
        stats["reasons"] = (
            [f"{o.shop} {o.product.name[:40]!r} {r}" for o, r in to_delete[:60]]
            + [f"would-retire product {p.id} {p.name[:50]!r} ({r})" for p, r in extra_products[:60]]
        )
        return stats

    products = {}
    for o, reason in to_delete:
        products[o.product_id] = o.product
        # vyčisti EAN z feed-reject (LG TV ← ASUS EAN)
        clean = strip_tracking(o.url)
        rej_ean = url_meta.get(clean + "#ean")
        if rej_ean and o.product.ean and o.product.ean == rej_ean:
            o.product.ean = None
            stats["reasons"].append(f"clear ean {rej_ean} on product {o.product_id}")
        stats["reasons"].append(f"del offer {o.id} {o.shop} {o.product.name[:50]!r} ({reason})")
        db.delete(o)
        stats["offers"] += 1
    db.flush()
    for pid, prod in products.items():
        db.refresh(prod)
        if not prod.offers and soft_retire_product(prod):
            stats["reasons"].append(f"retire product {pid} {prod.name[:50]!r}")
            stats["products"] += 1
    db.flush()

    for p, reason in extra_products:
        # mohl už zmizet
        fresh = db.get(Product, p.id)
        if fresh is None:
            continue
        for o in list(fresh.offers):
            if o.shop in feed_shops:
                stats["reasons"].append(f"del offer {o.id} {o.shop} {fresh.name[:50]!r} ({reason})")
                db.delete(o)
                stats["offers"] += 1
        db.flush()
        db.refresh(fresh)
        if not fresh.offers and soft_retire_product(fresh):
            stats["reasons"].append(f"retire product {fresh.id} {fresh.name[:50]!r} ({reason})")
            stats["products"] += 1
    # Otrávené EAN po už smazané nabídce (LG TV ← ASUS) – jen při hard conflict názvu
    for prod in list(db.scalars(select(Product)).all()):
        if not prod.ean:
            continue
        for rname in ean_to_reject_names.get(prod.ean, []):
            if offer_conflicts_product(prod.name or "", "", rname):
                stats["reasons"].append(
                    f"clear conflicting ean {prod.ean} on product {prod.id} vs {rname[:40]!r}"
                )
                prod.ean = None
                break
    db.commit()
    return stats


def repair_display_headphones(db, dry_run=True) -> dict:
    """Sundá počítačové displeje z kategorie Sluchátka.

    Nemazá produkty, nabídky ani price_points a nenastavuje retired_at
    (301 /produkt → kategorie se tím nemění). Výchozí je suchý běh.
    Aktivní řádky jen: category_slug sluchatka + název je displej.
    """
    from lovceny.categories import is_computer_display
    from lovceny.models import Product

    rows = db.scalars(select(Product).where(Product.category_slug == "sluchatka")).all()
    displays = [p for p in rows if p.retired_at is None and is_computer_display(p.name or "")]
    headphones = [p for p in rows if p.retired_at is None and p not in displays]
    offer_n = 0
    point_n = 0
    for p in displays:
        offer_n += len(p.offers)
        for o in p.offers:
            point_n += len(o.points)
    samples = [p.name for p in displays[:40]]
    stats = {
        "dry_run": bool(dry_run),
        "sluchatka_active": len(headphones) + len(displays),
        "headphones_kept": len(headphones),
        "candidates": len(displays),
        "updated": 0 if dry_run else len(displays),
        "offers_kept": offer_n,
        "price_points_kept": point_n,
        "samples": samples,
    }
    if dry_run:
        return stats
    for p in displays:
        p.category_slug = None
        p.category = None
    db.commit()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-demo", action="store_true")
    ap.add_argument("--purge-demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--category")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--from-feeds", action="store_true",
                    help="doplnit katalog z Heureka/eHUB/Dognet feedů (včetně Počítače24, JBL, Electronic-star, 4Home, Electrolux, Dyson, Allegro)")
    ap.add_argument("--feed-limit", type=int, default=25,
                    help="max. nových produktů na obchod při --from-feeds")
    ap.add_argument("--retire-bad-feeds", action="store_true",
                    help="smazat příslušenství/špatné nabídky z feed seedu (eHUB + Dognet obchody)")
    ap.add_argument("--repair-catalog", action="store_true",
                    help="náhled úklidu katalogu (počty a seznam). Zapíše se jen s --apply.")
    ap.add_argument("--apply", action="store_true",
                    help="zapsat změny u --repair-catalog (jinak jen náhled)")
    a = ap.parse_args(argv)
    init_db()
    db = SessionLocal()
    if a.purge_demo:
        n = db.execute(delete(PricePoint).where(PricePoint.is_demo.is_(True))).rowcount
        db.commit()
        print(f"Smazáno {n} demo bodů.")
        return
    if a.repair_catalog:
        write = bool(a.apply) and not a.dry_run
        from lovceny.catalog_repair import repair_catalog_locked
        st = repair_catalog_locked(db, dry_run=not write)
        if st.get("skipped"):
            print(f"Úklid katalogu přeskočen ({st['skipped']})")
            return
        mode = "APPLY" if write else "DRY-RUN"
        print(
            f"Úklid katalogu ({mode}): prislusenstvi={st['accessories_cleared']} "
            f"kategorie={st['recategorized']} nabidky_pryc={st['offers_detached']} "
            f"rozdeleno={st['offers_split']} prepojeno={st['offers_reattached']} "
            f"vraceno={st.get('restored', 0)} redirect={st.get('redirect_fixed', 0)} "
            f"slouceno={st['products_merged']} prazdne={st['empty_retired']}"
        )
        for line in st["samples"]:
            print(f"  {line}")
        return
    if a.retire_bad_feeds:
        st = retire_bad_feed_items(db, dry_run=a.dry_run)
        print(f"Retire bad feeds: offers={st['offers']} products={st['products']}")
        for line in st.get("reasons", [])[:100]:
            print(" ", line)
        if a.from_feeds:
            fs = seed_from_feeds(
                db, istyle_limit=a.feed_limit, sencor_limit=a.feed_limit, xm_limit=a.feed_limit,
                evolveo_limit=a.feed_limit, pocitace24_limit=a.feed_limit, jbl_limit=a.feed_limit,
                electronicstar_limit=a.feed_limit, fourhome_limit=a.feed_limit,
                electrolux_limit=a.feed_limit, dyson_limit=a.feed_limit,
                allegro_limit=allegro_seed_limit(a.feed_limit), dry_run=a.dry_run)
            print(f"Feed seed: {fs}")
        return
    items = [i for i in load_catalog() if not a.category or i["cat"] == a.category][:a.limit]
    print(f"Katalog: {len(items)} položek")
    st = seed_catalog(db, items, dry_run=a.dry_run)
    print(f"Katalog hotov: {st}")
    retired = retire_vestavne(db, dry_run=a.dry_run)
    print(f"Vestavné nabídky pryč: {retired}")
    if a.from_feeds:
        fs = seed_from_feeds(
            db, istyle_limit=a.feed_limit, sencor_limit=a.feed_limit, xm_limit=a.feed_limit,
            evolveo_limit=a.feed_limit, pocitace24_limit=a.feed_limit, jbl_limit=a.feed_limit,
            electronicstar_limit=a.feed_limit, fourhome_limit=a.feed_limit,
            electrolux_limit=a.feed_limit, dyson_limit=a.feed_limit,
            allegro_limit=allegro_seed_limit(a.feed_limit), dry_run=a.dry_run)
        print(f"Feed seed: {fs}")
    if a.dry_run or a.category or a.limit:
        return
    print("Seed z holých adres je vypnutý. Produkty vznikají z katalogu nebo z feedu.")


if __name__ == "__main__":
    main()
