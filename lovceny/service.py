import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Product, Offer, PricePoint, Watch
from .shops import shop_for_url, get_shop
from .shops.scrape import PoliteClient, parse_product, Blocked, unavailable_reason
from .shops.feed import FeedItem
from .notify import get_sender
from .util import slugify, normalize_url, czk

log = logging.getLogger("lovceny")
_client: PoliteClient | None = None


def client() -> PoliteClient:
    global _client
    if _client is None:
        _client = PoliteClient()
    return _client


def _assign_image(product: Product, incoming: str | None) -> None:
    """Uloží fotku produktu. Chybějící doplní; Fast/Planeo nahradí spolehlivější URL.

    Nabídky v DB vlastní fotku neukládají, takže lepší URL přijde až s dalším
    feedem nebo scrapem. Allegro /original/ a velké /sNNNN/ → /s512/. Horší
    příchozí fotka už uloženou spolehlivou nepřepíše.
    """
    from .images import prefer_display_image
    product.image_url = prefer_display_image(product.image_url, incoming)


def _name_from_url(url: str) -> str:
    from urllib.parse import urlparse
    part = [p for p in urlparse(url).path.split("/") if p]
    s = (part[-1] if part else url).rsplit(".", 1)[0]
    s = s.replace("-", " ").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else url


def record_price(db: Session, offer: Offer, price: float, original: float | None, in_stock: bool | None,
                 source: str, when: datetime | None = None):
    """Uloží aktuální cenu. Nový bod jen když se změní cena nebo sklad.

    price<=0 / None nepatří do historie (graf by jinak šel na 0 Kč). Stejná cena
    a stejný sklad jen posunou last_checked, aby nabídka nezestárla.
    """
    try:
        prev = float(offer.price) if offer.price is not None else None
    except (TypeError, ValueError):
        prev = None
    prev_stock = offer.in_stock
    offer.last_checked = when or datetime.now(timezone.utc)
    offer.last_ok_at = offer.last_checked
    offer.status, offer.last_error = "ok", None
    offer.in_stock = in_stock
    if original is not None:
        offer.original_price = original
    if price is None:
        return
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return
    if price_f <= 0:
        # Sklad/URL můžou zůstat, ale nulovou cenu do historie nepisujeme
        return
    # Glitch / špatný match (999 Kč u 36k MacBooku): nepisuj pád pod 25 % stávající ceny
    if prev and prev > 0 and price_f < prev * 0.25:
        return
    offer.price = price_f
    if original is not None:
        try:
            orig_f = float(original)
            offer.original_price = orig_f if orig_f > 0 else None
        except (TypeError, ValueError):
            pass
    if prev is not None and round(prev, 2) == round(price_f, 2) and prev_stock == in_stock:
        return
    db.add(PricePoint(offer=offer, price=price_f, original_price=offer.original_price, in_stock=in_stock,
                      captured_at=offer.last_checked, is_demo=False, source=source))



def scrub_outlier_price_points(db: Session, ratio: float = 0.35) -> int:
    """Smaž PricePointy daleko pod mediánem produktu (feed/scrape glitche). Nízko v paměti."""
    from sqlalchemy import text
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect.startswith("sqlite"):
        sql = """
            DELETE FROM price_points WHERE id IN (
              SELECT pp.id FROM price_points pp
              JOIN offers o ON o.id = pp.offer_id
              JOIN (
                SELECT o2.product_id AS pid, AVG(pp2.price) AS av
                FROM price_points pp2 JOIN offers o2 ON o2.id = pp2.offer_id
                WHERE pp2.price > 0 AND COALESCE(pp2.is_demo, 0) = 0
                GROUP BY o2.product_id HAVING COUNT(*) >= 2
              ) m ON m.pid = o.product_id
              WHERE pp.price > 0 AND COALESCE(pp.is_demo, 0) = 0
                AND pp.price < m.av * :ratio
            )
        """
    else:
        sql = """
            DELETE FROM price_points WHERE id IN (
              SELECT pp.id FROM price_points pp
              JOIN offers o ON o.id = pp.offer_id
              JOIN (
                SELECT o2.product_id AS pid,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY pp2.price) AS med
                FROM price_points pp2 JOIN offers o2 ON o2.id = pp2.offer_id
                WHERE pp2.price > 0 AND COALESCE(pp2.is_demo, false) = false
                GROUP BY o2.product_id HAVING COUNT(*) >= 2
              ) m ON m.pid = o.product_id
              WHERE pp.price > 0 AND COALESCE(pp.is_demo, false) = false
                AND pp.price < m.med * :ratio
            )
        """
    res = db.execute(text(sql), {"ratio": ratio})
    db.commit()
    return res.rowcount or 0


def update_offer(db: Session, offer: Offer, stats=None) -> str:
    """Stáhne aktuální cenu jedné nabídky. Vrací stav. stats = monitor.FetchStats (volitelně)."""
    st, kind, code, err = _update_offer(db, offer)
    if stats is not None and st != "feed":
        stats.add(offer.shop, kind, code, err)
    return st


def _update_offer(db: Session, offer: Offer):
    """→ (stav, druh výsledku pro monitoring, HTTP status, chyba)."""
    from .offer_trust import note_offer_due
    shop = get_shop(offer.shop)
    if shop is None or not shop.can_scrape:
        if shop and shop.source == "feed":
            return "feed", None, None, None  # řeší import feedu
        offer.status = "blocked"
        offer.last_error = shop.note if shop else "neznámý obchod"
        note_offer_due(offer)
        return "blocked", "blocked", None, offer.last_error
    # Tahle nabídka byla v dávce a opravdu se o ni sáhlo. Přeskočené (čerstvé,
    # mimo limit Mironetu, backoff) se sem nedostanou a za minutí se nepočítají.
    note_offer_due(offer)
    try:
        html = client().get(offer.url)
        res = (shop.page_parser or parse_product)(html)
    except Blocked as e:
        offer.status, offer.last_error = "blocked", str(e)
        offer.last_checked = datetime.now(timezone.utc)
        kind = {"captcha": "captcha", "robots": "robots"}.get(getattr(e, "kind", ""), "blocked")
        return "blocked", kind, getattr(e, "status", None), str(e)
    except Exception as e:  # síť, 404…
        offer.status, offer.last_error = "error", f"{type(e).__name__}: {e}"[:500]
        offer.last_checked = datetime.now(timezone.utc)
        code = getattr(getattr(e, "response", None), "status_code", None)
        return "error", "error", code, offer.last_error
    if res is None:
        why = unavailable_reason(html)
        if why:
            # Vyřazený produkt: nepočítat jako selhání obchodu (skipped), přestat opakovat ve fetchi.
            offer.status, offer.last_error = "unavailable", why
            offer.in_stock = False
            offer.last_checked = datetime.now(timezone.utc)
            return "unavailable", "skipped", 200, why
        offer.status, offer.last_error = "error", "cena na stránce nenalezena (JSON-LD/meta)"
        offer.last_checked = datetime.now(timezone.utc)
        return "error", "error", 200, offer.last_error
    p = offer.product
    if res.name and (p.name == _name_from_url(offer.url)):
        p.name, p.slug = res.name, slugify(res.name)
    _assign_image(p, res.image)
    p.ean = p.ean or res.ean
    p.category = p.category or res.category
    record_price(db, offer, res.price, res.original_price, res.in_stock, "scrape")
    return "ok", "ok", 200, None


def add_by_url(db: Session, url: str, fetch_now: bool = True, *,
               name: str | None = None, category_slug: str | None = None) -> tuple[Product | None, str]:
    """Známá URL vrátí její produkt. Nový produkt vznikne jen se zadaným názvem (katalog).

    Samotná adresa stránky produkt nezaloží. Bez názvu se odkaz zapíše do fronty
    /admin/trideni a nic se nestahuje.
    """
    url = normalize_url(url)
    shop = shop_for_url(url)
    if shop is None:
        return None, "Tento obchod zatím nesledujeme."
    url = shop.canonical_url(url)
    existing = db.scalar(select(Offer).where(Offer.url == url))
    if existing:
        return existing.product, ""
    given = (name or "").strip()
    if not given:
        from .triage import enqueue
        enqueue(
            db, kind="track_url", fingerprint=f"track:{url}",
            summary=f"Odkaz bez produktu ({shop.name})",
            reason="Produkt se nezakládá z adresy stránky. Vznikne, až ho feed spáruje podle EAN nebo kódu výrobce.",
            shop=shop.slug, payload={"url": url},
        )
        db.commit()
        return None, "Ten odkaz v katalogu není. Produkt nezakládáme z adresy stránky."
    prod = Product(name=given, slug=slugify(given), category_slug=category_slug)
    offer = Offer(product=prod, shop=shop.slug, url=url, status="pending")
    db.add(prod)
    db.flush()
    msg = ""
    if shop.source == "feed":
        msg = f"{shop.name}: cena bude doplněna z oficiálního feedu."
    elif fetch_now:
        st = update_offer(db, offer)
        if st == "blocked":
            msg = f"{shop.name} blokuje automatické stahování ({offer.last_error}). Cenu zatím nemáme."
        elif st == "unavailable":
            msg = f"Produkt už není v nabídce obchodu ({offer.last_error})."
        elif st == "error":
            msg = f"Cenu se nepodařilo načíst: {offer.last_error}"
    db.commit()
    return prod, msg


def _skip_excluded_brand(offer, item) -> bool:
    """Gigabyte a Karl Lagerfeld se nepřipojují a už připojený řádek se schová."""
    from .exclusions import bury_excluded, is_excluded_brand
    names = [getattr(item, "name", None) or ""]
    if offer is not None and getattr(offer, "product", None) is not None:
        names.append(offer.product.name or "")
    if not any(is_excluded_brand(name) for name in names):
        return False
    if offer is not None:
        bury_excluded(offer.product)
    return True


def feed_display_name(raw: str) -> str:
    """Název nového produktu. Úklid z names.py, bez zkrácení karty, které by smazalo model."""
    from .names import normalize_display_title, polish_feed_display
    cleaned = polish_feed_display(raw or "") or (raw or "")
    titled = (normalize_display_title(cleaned) or cleaned).strip()
    return (titled or (raw or "").strip())[:300]


def single_part_code(name: str):
    """Jedna značka a právě jeden silný kód výrobce. Víc kódů je nejednoznačné."""
    from .matching import brand_and_mpn
    key = brand_and_mpn(name or "")
    if key is None or len(key[1]) != 1:
        return None
    return key


def catalog_products(db: Session) -> list[Product]:
    """Produkty jednou na session. Obchody pak párují podle URL, GTIN a kódu výrobce.

    Dřív každý obchod stáhl celou tabulku znovu. Po rollbacku je mezipaměť
    neplatná a načte se znovu. Ceny nabídek se nenačítají.
    """
    from sqlalchemy import func
    from sqlalchemy import inspect as sa_inspect

    cached = db.info.get("catalog_products")
    # Počet je jeden integer. Nový produkt mimo téhle mezipaměti (nebo expire_all)
    # ji zahodí. Stejný počet a živé objekty znamenají, že tabulku znovu nestahujeme.
    current = db.scalar(select(func.count()).select_from(Product)) or 0
    if cached is not None and len(cached) == current and any(
        sa_inspect(row).expired or sa_inspect(row).detached or sa_inspect(row).deleted
        for row in cached
    ):
        cached = None
    if cached is None or len(cached) != current:
        cached = list(db.scalars(select(Product)).all())
        db.info["catalog_products"] = cached
    return cached


def apply_feed(db: Session, shop_slug: str, items: list[FeedItem], create_missing: bool = False,
               max_create: int | None = None, create_identified: bool = False,
               commit: bool = True) -> dict:
    """Spáruje položky feedu s nabídkami a zapíše ceny.

    Párování: URL → ITEM_ID → GTIN → značka a kód výrobce. Shoda jen podle názvu
    jde do fronty a produkt nezaloží. Kategorie jen z mapování obchodu.

    create_identified založí nový produkt jen při jisté kategorii, platném GTIN
    nebo jednom kódu výrobce, a jen do max_create. Bez commit se změny neuloží.
    """
    from sqlalchemy import select as sa_select

    from .categories import BY_SLUG
    from .gtin import normalize_gtin
    from .models import ShopCategoryMapping
    from .shops.feed import canonicalize_name, names_match, offer_conflicts_product, strip_tracking
    from .triage import ensure_mappings, resolve_category

    shop = get_shop(shop_slug)
    trail = bool(shop and shop.trailing_slash)

    def clean_url(raw: str) -> str:
        """strip tracking + sjednocení lomítka (4Home a další trailing_slash obchody)."""
        u = normalize_url(strip_tracking(raw or ""))
        if trail and shop is not None:
            return shop.canonical_url(u)
        return u

    stats = {"matched": 0, "created": 0, "attached": 0, "skipped": 0, "skipped_accessory": 0,
             "detached": 0, "reattached": 0, "skipped_mismatch": 0, "queued": 0}
    ensure_mappings(db)
    map_rows = list(db.scalars(sa_select(ShopCategoryMapping).where(ShopCategoryMapping.shop == shop_slug)).all())
    offers = db.scalars(select(Offer).where(Offer.shop == shop_slug)).all()
    by_url = {clean_url(o.url): o for o in offers}
    by_id = {o.external_id: o for o in offers if o.external_id}
    # Jednou na běh, ne na obchod. Párování jde přes URL tohoto obchodu, GTIN a kód.
    products = catalog_products(db)
    # Jednou, ne na položku. Allegro-only neznámá značka se nesmí vrátit do výpisu.
    other_shop_ids: set[int] = set()
    if shop_slug == "allegro":
        other_shop_ids = set(db.scalars(
            sa_select(Offer.product_id).where(Offer.shop != "allegro")
        ).all())
    by_gtin: dict[str, Product] = {}
    by_gtin_offer: dict[str, Offer] = {}

    def remember_gtin(g: str | None, prod: Product, offer: Offer | None = None) -> None:
        if not g:
            return
        prev = by_gtin.get(g)
        if prev is None or (prev.retired_at is not None and prod.retired_at is None):
            by_gtin[g] = prod
        if offer is not None and g not in by_gtin_offer:
            by_gtin_offer[g] = offer

    for p in products:
        remember_gtin(normalize_gtin(p.ean), p)
    for o in offers:
        remember_gtin(o.gtin or normalize_gtin(o.product.ean), o.product, o)
    # kanonický název → produkt; kolize = None (nepárovat)
    by_canon: dict[str, Product | None] = {}
    for p in products:
        if p.retired_at is not None:
            continue
        key = canonicalize_name(p.name)
        if not key:
            continue
        by_canon[key] = None if key in by_canon else p

    def follow_redirect(p: Product | None) -> Product | None:
        """Sloučená duplicita ukazuje na přeživšího. Ostatní retired řádky necháme být."""
        seen: set[int] = set()
        while p is not None and p.retired_at is not None and p.redirect_product_id and p.id not in seen:
            seen.add(p.id)
            p = db.get(Product, p.redirect_product_id)
        return p

    def find_by_name(feed_name: str) -> Product | None:
        key = canonicalize_name(feed_name)
        hit = follow_redirect(by_canon.get(key)) if key else None
        if hit is not None and hit.retired_at is None:
            return hit
        found = []
        seen_ids: set[int] = set()
        for p in products:
            if not names_match(p.name, feed_name):
                continue
            c = follow_redirect(p)
            if c is None or c.id in seen_ids:
                continue
            seen_ids.add(c.id)
            found.append(c)
        if not found:
            return None
        if len(found) == 1:
            return found[0]
        # duplicity v katalogu (stejný kanonický název): preferuj produkt bez nabídky tohoto obchodu
        without = [p for p in found if not any(x.shop == shop_slug for x in p.offers)]
        if len(without) == 1:
            return without[0]
        return None  # nejednoznačné → konzervativně přeskočit

    from .matching import brand_and_mpn
    mpn_index: dict[tuple, list[Product]] = {}

    def index_mpn(product: Product) -> None:
        if product.retired_at is not None:
            return
        key = brand_and_mpn(product.name or "")
        if key is None:
            return
        mpn_index.setdefault(key, []).append(product)

    for product in products:
        index_mpn(product)

    def find_by_brand_mpn(feed_name: str, feed_gtin: str | None) -> Product | None:
        """Jedna shoda značky a kódu výrobce. Jiný GTIN nebo víc kandidátů se nepřipojí.

        Index se staví jednou. Dřív se kód počítal pro každý produkt u každé položky feedu.
        """
        key = brand_and_mpn(feed_name)
        if key is None:
            return None
        hits: dict[int, Product] = {}
        for cand in mpn_index.get(key, ()):
            if cand.retired_at is not None:
                continue
            known = normalize_gtin(cand.ean)
            if feed_gtin and known and feed_gtin != known:
                continue
            kept = follow_redirect(cand)
            if kept is not None and kept.retired_at is None:
                hits[kept.id] = kept
        if len(hits) == 1:
            return next(iter(hits.values()))
        return None

    def find_by_identity(feed_name: str, cat_slug: str | None) -> Product | None:
        """Stejná značka a model (Herní sluchátka … vs Sluchátka …) – nepřidávat druhý produkt."""
        from .matching import same_model
        if not cat_slug:
            return None
        hits = [
            p for p in products
            if p.retired_at is None and same_model(p.name or "", p.category_slug, feed_name, cat_slug)
        ]
        if not hits:
            return None
        return min(hits, key=lambda p: p.id)

    def stamp(offer: Offer, it: FeedItem) -> str | None:
        if it.category:
            offer.shop_category = it.category
        g = normalize_gtin(it.ean)
        if g:
            offer.gtin = g
        return g

    def assign_ean(product: Product, raw: str | None) -> None:
        if product.ean or not raw:
            return
        product.ean = normalize_gtin(raw) or raw

    def forget_gtin(raw: str | None) -> None:
        g = normalize_gtin(raw)
        if g:
            by_gtin.pop(g, None)
            by_gtin_offer.pop(g, None)

    def conflicts_with(prod: Product | None, it: FeedItem, clean: str) -> bool:
        if prod is None:
            return False
        from .matching import clear_model_conflict, label_from_url, model_codes_conflict
        # Stejné EAN jiný model nespáruje. Liší-li se číslice kódu, nespáruje se ani bez EAN.
        if model_codes_conflict(prod.name or "", it.name or "") or model_codes_conflict(prod.name or "", clean):
            return True
        if offer_conflicts_product(
            prod.name or "", clean, it.name or None, category_slug=prod.category_slug,
        ):
            return True
        return clear_model_conflict(prod.name or "", it.name or label_from_url(clean))

    def refuse_attach(prod: Product, it: FeedItem, clean: str) -> None:
        from .auto_resolve import record_auto
        record_auto(
            db, kind="model_mismatch", fingerprint=f"mismatch:{shop_slug}:{clean}"[:200],
            summary=f"Nepřipojovat „{(it.name or '')[:80]}“ k produktu #{prod.id}",
            reason="Adresa nebo název patří k jinému modelu. Pravidlo nabídku nepřipojí.",
            outcome="hidden", shop=shop_slug, shop_category=it.category, product_id=prod.id,
            payload={"url": clean, "name": it.name, "ean": it.ean},
        )
        stats["skipped_mismatch"] += 1
        stats["hidden"] = stats.get("hidden", 0) + 1

    def propose_detach(offer: Offer, it: FeedItem, clean: str) -> None:
        from .auto_resolve import record_auto, settle_disputed_offer
        by_url.pop(clean_url(offer.url), None)
        by_url.pop(clean, None)
        if offer.external_id:
            by_id.pop(offer.external_id, None)
        clear = it.ean if offer.product.ean and it.ean and offer.product.ean == it.ean else None
        if clear:
            forget_gtin(clear)
        stamp(offer, it)
        result = settle_disputed_offer(db, offer, label=it.name or "", clear_ean=clear, products=products)
        record_auto(
            db, kind="detach", fingerprint=f"detach:{offer.id}",
            summary=f"Odpojit nabídku #{offer.id} od produktu #{offer.product_id}",
            reason="Název nabídky nesedí. Přesun jen podle GTIN nebo kódu výrobce, jinak se nabídka schová.",
            outcome="detached" if result == "detached" else "hidden",
            shop=shop_slug, shop_category=it.category, product_id=offer.product_id, offer_id=offer.id,
            payload={"label": (it.name or "")[:300], "clear_ean": clear},
        )
        stats["detached"] += 1
        if result == "hidden":
            stats["hidden"] = stats.get("hidden", 0) + 1

    def bind_offer(prod: Product, clean: str, it: FeedItem) -> Offer:
        existing = next((x for x in prod.offers if x.shop == shop_slug), None)
        if existing is not None:
            by_url[clean] = existing
            stats["matched"] += 1
            return existing
        if prod.offers:
            stats["second_offer"] = stats.get("second_offer", 0) + 1
        offer = Offer(product=prod, shop=shop_slug, url=clean)
        db.add(offer)
        db.flush()
        prod.offers.append(offer)
        by_url[clean] = offer
        if it.item_id:
            by_id[it.item_id] = offer
        g = stamp(offer, it)
        remember_gtin(g or normalize_gtin(prod.ean), prod, offer)
        stats["attached"] += 1
        return offer

    held = 0
    created = 0
    for it in items:
        from .categories import is_non_new
        if is_non_new(it.name, condition=it.condition, shop_category=it.category):
            clean_used = clean_url(it.url)
            existing_used = by_url.get(clean_used) or by_id.get(it.item_id)
            if existing_used is not None and not existing_used.withheld:
                existing_used.withheld = True
                stats["withheld_used"] = stats.get("withheld_used", 0) + 1
            stats["skipped_used"] = stats.get("skipped_used", 0) + 1
            continue
        res = resolve_category(db, shop_slug, it.name, it.category, it.price, url=it.url, rows=map_rows)
        # Příslušenství / explicitní mapování accessory se jako hlavní produkt nezaloží.
        if res.action == "accessory":
            stats["skipped_accessory"] += 1
            clean = clean_url(it.url)
            leaked = by_url.get(clean) or (by_id.get(it.item_id) if it.item_id else None)
            if leaked is not None and not leaked.withheld:
                leaked.withheld = True
                stats["withheld_accessory"] = stats.get("withheld_accessory", 0) + 1
            continue
        clean = clean_url(it.url)
        gtin = normalize_gtin(it.ean)
        o = by_url.get(clean) or by_id.get(it.item_id)
        fresh = False
        if _skip_excluded_brand(o, it):
            stats["skipped"] += 1
            continue
        if o is not None and conflicts_with(o.product, it, clean):
            propose_detach(o, it, clean)
            continue
        blocked = False
        if o is None and gtin and not blocked:
            shop_offer = by_gtin_offer.get(gtin)
            if shop_offer is not None and conflicts_with(shop_offer.product, it, clean):
                refuse_attach(shop_offer.product, it, clean)
                blocked = True
            elif shop_offer is not None:
                o = shop_offer
        if o is None and not blocked:
            prod = follow_redirect(by_gtin.get(gtin)) if gtin else None
            if prod is not None and conflicts_with(prod, it, clean):
                refuse_attach(prod, it, clean)
                prod = None
                blocked = True
            if prod is None and not blocked and res.action != "skip":
                mpn_hit = find_by_brand_mpn(it.name, gtin)
                if mpn_hit is not None and conflicts_with(mpn_hit, it, clean):
                    refuse_attach(mpn_hit, it, clean)
                    blocked = True
                elif mpn_hit is not None:
                    prod = mpn_hit
            if prod is None and not gtin and not blocked:
                # Stažení cen nic nezakládá. Přesná shoda kanonického názvu stačí na záznam.
                # Procházení celého katalogu (names_match + same_model) by u tisíců položek
                # bez GTIN sežralo desítky minut a nabídku stejně nepřipojí.
                if create_missing:
                    named = find_by_name(it.name)
                    if named is None and res.slug:
                        named = find_by_identity(it.name, res.slug)
                else:
                    canon = canonicalize_name(it.name)
                    named = by_canon.get(canon) if canon else None
                if named is not None and named.retired_at is None:
                    from .auto_resolve import record_auto
                    record_auto(
                        db, kind="title_match", fingerprint=f"title:{shop_slug}:{clean}",
                        summary=f"Nepřipojovat „{(it.name or '')[:80]}“ k produktu #{named.id}",
                        reason="Shoda jen podle názvu. Nabídka se nepřipojuje a zůstane mimo katalog.",
                        outcome="hidden", shop=shop_slug, shop_category=it.category, product_id=named.id,
                        payload={"url": clean, "name": it.name},
                    )
                    stats["skipped"] += 1
                    stats["hidden"] = stats.get("hidden", 0) + 1
                    continue
            if prod is not None:
                o = bind_offer(prod, clean, it)
            elif blocked or res.action == "skip":
                stats["skipped"] += 1
                continue
            elif create_missing and res.action in ("high", "unmapped") and (
                max_create is None or created + held < max_create
            ):
                if shop_slug == "allegro":
                    from .allegro_narrow import is_known_brand
                    if not is_known_brand(it.name or ""):
                        stats["skipped"] += 1
                        continue
                hidden = res.action != "high"
                cat_slug = res.slug if res.action == "high" else None
                if cat_slug:
                    from .category_guard import guarded_category
                    action, guarded = guarded_category(it.name or "", cat_slug)
                    if action == "hide":
                        hidden = True
                        cat_slug = None
                    elif action == "move" and guarded:
                        cat_slug = guarded
                cat_label = BY_SLUG[cat_slug].name if cat_slug and cat_slug in BY_SLUG else None
                prod = Product(
                    name=it.name, slug=slugify(it.name), category=cat_label, category_slug=cat_slug,
                    catalog_hidden=hidden,
                )
                assign_ean(prod, it.ean)
                _assign_image(prod, it.image)
                o = Offer(product=prod, shop=shop_slug, url=clean)
                db.add(o)
                db.flush()
                products.append(prod)
                index_mpn(prod)
                by_url[clean] = o
                if it.item_id:
                    by_id[it.item_id] = o
                g = stamp(o, it)
                remember_gtin(g or normalize_gtin(prod.ean), prod, o)
                key = canonicalize_name(it.name)
                if key:
                    by_canon[key] = None if key in by_canon else prod
                fresh = True
                if hidden:
                    from .auto_resolve import record_auto
                    prod.needs_review = True
                    record_auto(
                        db, kind="unmapped_category", fingerprint=f"unmapped_category:{prod.id}:none",
                        summary=f"Skrýt „{(it.name or '')[:80]}“",
                        reason="Kategorie obchodu není v mapování. Produkt zůstane mimo web.",
                        outcome="hidden", shop=shop_slug, shop_category=it.category, product_id=prod.id,
                    )
                    stats["hidden"] = stats.get("hidden", 0) + 1
                    held += 1
                else:
                    created += 1
                    stats["created"] += 1
            elif create_identified and res.action == "high" and res.slug == "telefony":
                # Nový produkt jen u telefonů, s jistou kategorií a GTIN nebo jedním kódem.
                # Kryt, sklo, kabel ani nabíječka se jako telefon nezaloží.
                from .categories import is_accessory_text
                if is_accessory_text(it.name or ""):
                    stats["skipped_accessory"] += 1
                    continue
                part = None if gtin else single_part_code(it.name or "")
                if not gtin and part is None:
                    stats["skipped"] += 1
                    continue
                if part is not None and any(
                    p.retired_at is None for p in mpn_index.get(part, ())
                ):
                    stats["skipped"] += 1
                    continue
                from .category_guard import guarded_category
                if guarded_category(it.name or "", res.slug)[0] != "keep":
                    stats["skipped"] += 1
                    continue
                if shop_slug == "allegro":
                    from .allegro_narrow import is_known_brand
                    if not is_known_brand(it.name or ""):
                        stats["skipped"] += 1
                        continue
                stats["eligible"] = stats.get("eligible", 0) + 1
                by_cat = stats.setdefault("eligible_by_category", {})
                by_cat[res.slug] = by_cat.get(res.slug, 0) + 1
                if max_create is not None and created >= max_create:
                    stats["capped"] = stats.get("capped", 0) + 1
                    stats["skipped"] += 1
                    continue
                shown = feed_display_name(it.name)
                prod = Product(
                    name=shown, slug=slugify(shown), category=BY_SLUG[res.slug].name,
                    category_slug=res.slug, catalog_hidden=False, needs_review=False,
                )
                assign_ean(prod, it.ean)
                _assign_image(prod, it.image)
                o = Offer(product=prod, shop=shop_slug, url=clean)
                db.add(o)
                db.flush()
                products.append(prod)
                index_mpn(prod)
                by_url[clean] = o
                if it.item_id:
                    by_id[it.item_id] = o
                g = stamp(o, it)
                remember_gtin(g or normalize_gtin(prod.ean), prod, o)
                key = canonicalize_name(shown)
                if key:
                    by_canon[key] = None if key in by_canon else prod
                fresh = True
                created += 1
                stats["created"] += 1
                samples = stats.setdefault("samples", [])
                if len(samples) < 20:
                    samples.append({"name": shown, "category": BY_SLUG[res.slug].name})
            else:
                stats["skipped"] += 1
                continue
        elif o is not None and not fresh:
            stats["matched"] += 1
        if o is None:
            continue
        if create_identified and (o.product.category_slug or "") != "telefony":
            stats["skipped_not_phone"] = stats.get("skipped_not_phone", 0) + 1
            continue
        if conflicts_with(o.product, it, clean):
            propose_detach(o, it, clean)
            stats["skipped_mismatch"] += 1
            continue
        o.url = clean
        o.external_id = it.item_id or o.external_id
        if it.name and (o.product.name == _name_from_url(o.url) or not o.product.name):
            o.product.name, o.product.slug = it.name, slugify(it.name)
        assign_ean(o.product, it.ean)
        stamp(o, it)
        _assign_image(o.product, it.image)
        if o.product.category_slug and o.product.category and "|" in o.product.category:
            o.product.category = BY_SLUG[o.product.category_slug].name if o.product.category_slug in BY_SLUG else None
        remember_gtin(o.gtin, o.product, o)
        record_price(db, o, it.price, it.original_price, it.in_stock, "feed")
        # Vyřazený produkt zůstane vyřazený. Stažení cen ho nevrací do výpisu.
        if not fresh and o.product.retired_at is None:
            from .allegro_narrow import is_known_brand
            from .auto_resolve import apply_listing
            from .categories import is_accessory_text, is_non_new
            from .category_guard import guarded_category
            if is_non_new(o.product.name or "", shop_category=o.product.category):
                o.product.catalog_hidden = True
                o.product.needs_review = False
                if not o.withheld:
                    o.withheld = True
                    stats["withheld_used"] = stats.get("withheld_used", 0) + 1
                continue
            unknown_allegro = (
                shop_slug == "allegro"
                and o.product.id not in other_shop_ids
                and not is_known_brand(o.product.name or "")
            )
            if unknown_allegro:
                o.product.retired_at = datetime.now(timezone.utc)
                o.product.redirect_product_id = None
                stats["retired"] = stats.get("retired", 0) + 1
            elif o.product.catalog_hidden or o.product.needs_review:
                apply_listing(db, o.product)
            elif is_accessory_text(o.product.name or ""):
                o.product.needs_review = True
            elif res.action == "high" and (o.product.category_slug or res.slug) in BY_SLUG:
                action, target = guarded_category(o.product.name or "", o.product.category_slug or res.slug)
                if action == "move" and target in BY_SLUG:
                    o.product.category_slug = target
                    o.product.category = BY_SLUG[target].name
                elif action == "hide":
                    o.product.needs_review = True
            elif res.action == "unmapped" and (it.category or "").strip() and not o.product.category_slug:
                o.product.needs_review = True
                o.product.catalog_hidden = True
    # Celý feed je jeden pohled na obchod: každá jeho nabídka byla na řadě,
    # i ta, která v souboru chyběla. Rotující scrape (Mironet) sem nepatří.
    from .offer_trust import note_offer_due
    looked_at = datetime.now(timezone.utc)
    for existing in db.scalars(select(Offer).where(Offer.shop == shop_slug)).all():
        note_offer_due(existing, looked_at)
    if commit:
        db.commit()
    else:
        db.flush()
    return stats


def withhold_leaked_accessory_offers(db: Session) -> dict[str, int]:
    """Schová nabídky, jejichž adresa nebo název je příslušenství na cizím výrobku.

    Řádek a historie cen zůstanou. Na webu se neukážou a nemají Koupit.
    Vrací počty nově schovaných nabídek podle obchodu.
    """
    from .categories import BY_SLUG, is_accessory_text, offer_title_from_url
    from .shops.feed import ACCESSORY_URLS, strip_tracking

    counts: dict[str, int] = {}
    banned = set(ACCESSORY_URLS)
    offers = db.scalars(select(Offer)).all()
    for offer in offers:
        if getattr(offer, "withheld", False):
            continue
        label = offer_title_from_url(offer.url or "")
        category = offer.shop_category
        if not category and offer.product is not None and offer.product.category_slug in BY_SLUG:
            category = BY_SLUG[offer.product.category_slug].name
        url_is_case = strip_tracking(offer.url or "") in banned or is_accessory_text(label, category)
        name_is_case = (
            offer.product is not None and is_accessory_text(offer.product.name or "", category)
        )
        if name_is_case and offer.product is not None and not offer.product.catalog_hidden:
            offer.product.catalog_hidden = True
        if not url_is_case and not name_is_case:
            continue
        offer.withheld = True
        counts[offer.shop] = counts.get(offer.shop, 0) + 1
    if counts:
        db.commit()
    ACCESSORY_URLS.clear()
    return counts


def create_watch(db: Session, product: Product, email: str, target: float, only_in_stock: bool) -> Watch:
    from .emails import confirm_email
    w = Watch(product=product, email=email.strip().lower(), target_price=target, only_in_stock=only_in_stock,
              token=secrets.token_urlsafe(24))
    db.add(w)
    db.commit()
    m = confirm_email(w)
    get_sender().send(w.email, m.subject, m.text, m.html)
    return w


REALERT_DROP = 0.03  # znovu upozorníme, jen když cena klesne o další ≥ 3 % pod naposledy oznámenou


def check_watches(db: Session) -> int:
    """Volá se po každém stažení cen (fetch_prices.py). Pošle upozornění, když nejnižší cena ≤ cíl.
    Bez duplicit: stejný pokles se oznámí jednou (Watch.notified_price). Znovu jen při dalším poklesu
    o ≥ 3 %, nebo když cena mezitím vystoupala nad cíl a pak znovu spadla. Neúspěšné odeslání se nezapíše."""
    from .emails import alert_email
    sent = 0
    sender = get_sender()
    for w in db.scalars(select(Watch).where(Watch.active.is_(True), Watch.confirmed.is_(True))).all():
        cands = [o for o in w.product.offers if o.price is not None and o.status == "ok"
                 and (not w.only_in_stock or o.in_stock)]
        hit = min(cands, key=lambda o: float(o.price), default=None)
        if not hit:
            continue
        price, target = float(hit.price), float(w.target_price)
        if price > target:
            if w.notified_price is not None:
                w.notified_price = None  # cena se vrátila nad cíl → příští pokles zase oznámíme
            continue
        if w.notified_price is not None and price > float(w.notified_price) * (1 - REALERT_DROP):
            continue  # tenhle pokles už jsme oznámili
        shop = get_shop(hit.shop)
        m = alert_email(w, hit, shop.name if shop else hit.shop, shop.affiliate_link(hit.url) if shop else hit.url)
        if sender.send(w.email, m.subject, m.text, m.html):
            w.notified_at = datetime.now(timezone.utc)
            w.notified_price = price
            sent += 1
            db.commit()  # průběžně – při pádu uprostřed nepošleme nic dvakrát
    db.commit()
    return sent


WATCH_MAX_AGE_DAYS = 730      # 24 měsíců (slib v zásadách ochrany osobních údajů)
UNCONFIRMED_MAX_AGE_DAYS = 7


def cleanup_watches(db: Session, now: datetime | None = None) -> dict:
    """Smaže nepotvrzená hlídání starší 7 dnů, zrušená (neaktivní) hlídání a hlídání, u nichž od vytvoření
    i od posledního upozornění uplynulo víc než 24 měsíců."""
    now = now or datetime.now(timezone.utc)
    aware = lambda d: d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)  # noqa: E731
    stats = {"unconfirmed": 0, "inactive": 0, "expired": 0}
    for w in db.scalars(select(Watch)).all():
        created, notified = aware(w.created_at), aware(w.notified_at)
        last = max(d for d in (created, notified) if d is not None) if (created or notified) else now
        if not w.active:
            key = "inactive"
        elif not w.confirmed and created and created < now - timedelta(days=UNCONFIRMED_MAX_AGE_DAYS):
            key = "unconfirmed"
        elif last < now - timedelta(days=WATCH_MAX_AGE_DAYS):
            key = "expired"
        else:
            continue
        db.delete(w)
        stats[key] += 1
    db.commit()
    return stats


# ---------- vyhledávání podle názvu ----------
def search_db(db: Session, q: str, limit: int = 30) -> list[Product]:
    from sqlalchemy import or_
    from sqlalchemy.orm import selectinload
    stmt = (select(Product).where(
        Product.retired_at.is_(None), Product.catalog_hidden.is_(False), Product.needs_review.is_(False),
    ).options(selectinload(Product.offers)))
    for t in q.split():
        stmt = stmt.where(or_(Product.name.ilike(f"%{t}%"), Product.ean == t))
    return list(db.scalars(stmt.limit(limit)).all())


def search_feed_offers(db: Session, q: str, limit: int = 30):
    """Feed obchody (Alza, Datart): hledáme v importovaných položkách feedu v DB."""
    from .shops.registry import SHOPS
    from .shops.search import SearchHit
    feed_slugs = [s.slug for s in SHOPS.values() if s.source == "feed"]
    stmt = select(Offer).join(Product).where(
        Offer.shop.in_(feed_slugs), Offer.price.is_not(None), Product.retired_at.is_(None),
        Product.catalog_hidden.is_(False), Product.needs_review.is_(False),
        Offer.withheld.is_(False))
    for t in q.split():
        stmt = stmt.where(Product.name.ilike(f"%{t}%"))
    return [SearchHit(o.shop, o.product.name, float(o.price), get_shop(o.shop).affiliate_link(o.url),
                      o.product.image_url, float(o.original_price) if o.original_price else None)
            for o in db.scalars(stmt.limit(limit)).all()]


def offers_for_query(db: Session, q: str, live: bool = True):
    """Sloučí nabídky z feedů v DB a živého hledání; seřazeno jen podle ceny.
    Vrací (list[(hit, tracked_product|None)], shop_statuses)."""
    r = smart_offers(db, q, live=live, use_ai=False)
    return r["offers"], r["statuses"]


def _intent_filter(hits, intent):
    from .shops.search import _fold, _matches
    out = []
    for h in hits:
        if intent.max_price and h.price > intent.max_price:
            continue
        if intent.min_price and h.price < intent.min_price:
            continue
        if intent.must_words and not any(_matches(h.name, w) for w in intent.must_words):
            continue
        if intent.exclude_words and any(_fold(w) in _fold(h.name) for w in intent.exclude_words):
            continue
        out.append(h)
    return out


_term_pool = None


def smart_offers(db: Session, q: str, live: bool = True, use_ai: bool = True) -> dict:
    """Hledání s volitelnou AI (Gemini free tier). Bez AI / při chybě = klasické hledání + pravidla na ceny.
    Vrací {offers, statuses, intent, summary, ai_used}."""
    from concurrent.futures import ThreadPoolExecutor
    from . import ai
    from .features import ai_on
    from .shops.search import live_search
    global _term_pool
    if use_ai and not ai_on():
        use_ai = False
    intent = ai.understand(q) if use_ai else None
    ai_used = intent is not None
    if intent is None:
        intent = ai.rule_intent(q)
    terms = list(dict.fromkeys(intent.queries)) or [q]
    hits = []
    for t in terms:
        hits += search_feed_offers(db, t)
    statuses = []
    if live:
        if _term_pool is None:
            _term_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="terms")
        results = list(_term_pool.map(live_search, terms))
        by_shop = {}
        for res in results:
            hits += res.hits
            for st in res.shops:  # status obchodu: „ok“ vyhrává
                if st.shop not in by_shop or (st.status == "ok" and by_shop[st.shop].status != "ok"):
                    by_shop[st.shop] = st
        statuses = list(by_shop.values())
    if not hits and intent.source == "ai" and intent.must_words:  # AI výrazy příliš úzké → zkusíme typ produktu
        extra = [w for w in intent.must_words if w not in terms][:2]
        for t in extra:
            hits += search_feed_offers(db, t)
        if live and extra:
            for res in _term_pool.map(live_search, extra):
                hits += res.hits
    hits = _intent_filter(hits, intent)
    urls = [(get_shop(h.shop).canonical_url(h.url) if get_shop(h.shop) else normalize_url(h.url)) for h in hits]
    tracked = {o.url: o.product for o in db.scalars(select(Offer).where(Offer.url.in_(urls))).all()} if urls else {}
    out, seen = [], set()
    for h, u in sorted(zip(hits, urls), key=lambda x: x[0].price):
        if u in seen:
            continue
        seen.add(u)
        out.append((h, tracked.get(u)))
    summary = ""
    if use_ai and ai.should_use_ai(q) and len(out) >= 2:
        names = lambda code: (get_shop(code).name if get_shop(code) else code)  # noqa: E731
        rk = ai.rank(q, [h for h, _ in out], shop_name=names)
        if rk is not None:
            ai_used = True
            out = [out[i] for i in rk.keep]  # seřazení podle ceny zůstává
            summary = rk.summary
    return {"offers": out, "statuses": statuses, "intent": intent, "summary": summary, "ai_used": ai_used}
