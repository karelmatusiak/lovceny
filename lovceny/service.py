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


def _name_from_url(url: str) -> str:
    from urllib.parse import urlparse
    part = [p for p in urlparse(url).path.split("/") if p]
    s = (part[-1] if part else url).rsplit(".", 1)[0]
    s = s.replace("-", " ").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else url


def record_price(db: Session, offer: Offer, price: float, original: float | None, in_stock: bool | None,
                 source: str, when: datetime | None = None):
    """Uloží aktuální cenu. price<=0 / None nepatří do historie (graf by jinak šel na 0 Kč)."""
    offer.last_checked = when or datetime.now(timezone.utc)
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
    try:
        prev = float(offer.price) if offer.price is not None else None
    except (TypeError, ValueError):
        prev = None
    if prev and prev > 0 and price_f < prev * 0.25:
        return
    offer.price = price_f
    if original is not None:
        try:
            orig_f = float(original)
            offer.original_price = orig_f if orig_f > 0 else None
        except (TypeError, ValueError):
            pass
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
    shop = get_shop(offer.shop)
    if shop is None or not shop.can_scrape:
        if shop and shop.source == "feed":
            return "feed", None, None, None  # řeší import feedu
        offer.status = "blocked"
        offer.last_error = shop.note if shop else "neznámý obchod"
        return "blocked", "blocked", None, offer.last_error
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
    p.image_url = p.image_url or res.image
    p.ean = p.ean or res.ean
    p.category = p.category or res.category
    if not p.category_slug:
        from .categories import classify
        p.category_slug = classify(p.name, p.category)
    record_price(db, offer, res.price, res.original_price, res.in_stock, "scrape")
    return "ok", "ok", 200, None


def add_by_url(db: Session, url: str, fetch_now: bool = True) -> tuple[Product | None, str]:
    """Vložený odkaz → sledovaný produkt. Vrací (produkt, zpráva)."""
    url = normalize_url(url)
    shop = shop_for_url(url)
    if shop is None:
        return None, "Tento obchod zatím nesledujeme."
    url = shop.canonical_url(url)
    existing = db.scalar(select(Offer).where(Offer.url == url))
    if existing:
        return existing.product, ""
    name = _name_from_url(url)
    prod = Product(name=name, slug=slugify(name))
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


def apply_feed(db: Session, shop_slug: str, items: list[FeedItem], create_missing: bool = False,
               max_create: int | None = None) -> dict:
    """Spáruje položky feedu s nabídkami a zapíše ceny.

    Párování: URL → ITEM_ID → EAN nabídky obchodu → EAN produktu → konzervativní shoda názvu
    (barvy CZ↔EN, kapacita, varianta, MPN). create_missing jen když žádná shoda.
    """
    from .shops.feed import canonicalize_name, names_match, strip_tracking, offer_conflicts_product
    from .categories import BY_SLUG, accept_feed_item, classify, is_accessory_text

    stats = {"matched": 0, "created": 0, "attached": 0, "skipped": 0, "skipped_accessory": 0, "detached": 0, "reattached": 0, "skipped_mismatch": 0}
    offers = db.scalars(select(Offer).where(Offer.shop == shop_slug)).all()
    by_url = {strip_tracking(o.url): o for o in offers}
    by_id = {o.external_id: o for o in offers if o.external_id}
    by_ean_offer = {o.product.ean: o for o in offers if o.product.ean}
    products = list(db.scalars(select(Product)).all())
    by_ean_prod = {p.ean: p for p in products if p.ean}
    # kanonický název → produkt; kolize = None (nepárovat)
    by_canon: dict[str, Product | None] = {}
    for p in products:
        key = canonicalize_name(p.name)
        if not key:
            continue
        by_canon[key] = None if key in by_canon else p

    def find_by_name(feed_name: str) -> Product | None:
        key = canonicalize_name(feed_name)
        hit = by_canon.get(key)
        if hit is not None:
            return hit
        found = [p for p in products if names_match(p.name, feed_name)]
        if not found:
            return None
        if len(found) == 1:
            return found[0]
        # duplicity v katalogu (stejný kanonický název): preferuj produkt bez nabídky tohoto obchodu
        without = [p for p in found if not any(x.shop == shop_slug for x in p.offers)]
        if len(without) == 1:
            return without[0]
        return None  # nejednoznačné → konzervativně přeskočit

    def clean_category_label(name: str, feed_cat: str | None) -> tuple[str | None, str | None]:
        """(category_slug, lidský label) – nikdy surový Heureka CATEGORYTEXT."""
        slug = classify(name, feed_cat)
        label = BY_SLUG[slug].name if slug and slug in BY_SLUG else None
        return slug, label

    created = 0
    for it in items:
        # Příslušenství / náhradní díly nikdy nepřipojovat ani nevytvářet (filtr ≠ vysavač).
        if is_accessory_text(it.name, it.category):
            stats["skipped_accessory"] += 1
            continue
        clean = strip_tracking(it.url)
        o = by_url.get(clean) or by_id.get(it.item_id)
        # EAN/name soft-match jen když položka projde whitelist + min. cenou
        accepted = accept_feed_item(it.name, it.category, it.price, url=it.url)
        # Historicky špatně soft-matchnuté desturl (AirPods→iPhone): odpoj / přesuň
        if o is not None and it.name and offer_conflicts_product(o.product.name, clean, it.name):
            correct = find_by_name(it.name) if accepted else None
            # vyhoď z indexů před přesunem/smazáním
            by_url.pop(strip_tracking(o.url), None)
            if o.external_id:
                by_id.pop(o.external_id, None)
            # otrávené EAN z chybného soft-match (LG TV ← ASUS EAN)
            if o.product.ean and it.ean and o.product.ean == it.ean and (
                correct is None or correct.id != o.product_id
            ):
                by_ean_prod.pop(it.ean, None)
                by_ean_offer.pop(it.ean, None)
                o.product.ean = None
            if correct is not None and correct.id != o.product_id:
                other = next((x for x in correct.offers if x.shop == shop_slug and x.id != o.id), None)
                if other is not None:
                    db.delete(o)
                    o = other
                    stats["detached"] += 1
                else:
                    o.product = correct
                    stats["reattached"] += 1
            else:
                db.delete(o)
                o = None
                stats["detached"] += 1
            if o is not None:
                by_url[clean] = o
                if o.external_id:
                    by_id[o.external_id] = o
        if o is None and accepted:
            o = by_ean_offer.get(it.ean) if it.ean else None
            # EAN na nabídce jiného produktu, ale názvy kolidují → nevěřit EAN
            if o is not None and it.name and offer_conflicts_product(o.product.name, clean, it.name):
                stats["skipped_mismatch"] += 1
                o = None
        if o is None:
            if not accepted:
                stats["skipped"] += 1
                continue
            prod = by_ean_prod.get(it.ean) if it.ean else None
            # EAN v katalogu nesmí přebít jasný konflikt názvů (LG TV ← ASUS monitor)
            if prod is not None and it.name and offer_conflicts_product(prod.name, clean, it.name):
                stats["skipped_mismatch"] += 1
                prod = None
            if prod is None:
                prod = find_by_name(it.name)
            if prod is not None:
                # už má tento obchod nabídku na produktu? pak aktualizuj tu
                existing = next((x for x in prod.offers if x.shop == shop_slug), None)
                if existing is not None:
                    o = existing
                    by_url[clean] = o
                    stats["matched"] += 1
                else:
                    o = Offer(product=prod, shop=shop_slug, url=clean)
                    db.add(o)
                    db.flush()
                    by_url[clean] = o
                    if it.item_id:
                        by_id[it.item_id] = o
                    if prod.ean:
                        by_ean_offer[prod.ean] = o
                    stats["attached"] += 1
            elif create_missing and accepted and (max_create is None or created < max_create):
                cat_slug = accepted
                cat_label = BY_SLUG[cat_slug].name if cat_slug in BY_SLUG else None
                prod = Product(name=it.name, slug=slugify(it.name), ean=it.ean, image_url=it.image,
                               category=cat_label, category_slug=cat_slug)
                o = Offer(product=prod, shop=shop_slug, url=clean)
                db.add(o)
                db.flush()
                products.append(prod)
                by_url[clean] = o
                if it.item_id:
                    by_id[it.item_id] = o
                if it.ean:
                    by_ean_prod[it.ean] = prod
                    by_ean_offer[it.ean] = o
                by_canon[canonicalize_name(it.name)] = prod
                created += 1
                stats["created"] += 1
            else:
                stats["skipped"] += 1
                continue
        else:
            stats["matched"] += 1
        if it.name and offer_conflicts_product(o.product.name, clean, it.name):
            # Nenechávat špatný desturl – odpoj (případně vyčisti EAN z feedu)
            by_url.pop(strip_tracking(o.url), None)
            by_url.pop(clean, None)
            if o.external_id:
                by_id.pop(o.external_id, None)
            if o.product.ean and it.ean and o.product.ean == it.ean:
                o.product.ean = None
                by_ean_prod.pop(it.ean, None)
                by_ean_offer.pop(it.ean, None)
            db.delete(o)
            stats["detached"] += 1
            stats["skipped_mismatch"] += 1
            continue
        o.url = clean  # sjednotit bez UTM
        o.external_id = it.item_id or o.external_id
        if it.name and (o.product.name == _name_from_url(o.url) or not o.product.name):
            o.product.name, o.product.slug = it.name, slugify(it.name)
        o.product.ean = o.product.ean or it.ean
        o.product.image_url = o.product.image_url or it.image
        if not o.product.category_slug:
            slug, label = clean_category_label(o.product.name, it.category)
            o.product.category_slug = slug
            if not o.product.category or "|" in (o.product.category or ""):
                o.product.category = label
        elif o.product.category and "|" in o.product.category:
            # oprav surový Heureka path z dřívějších importů
            o.product.category = BY_SLUG[o.product.category_slug].name if o.product.category_slug in BY_SLUG else None
        record_price(db, o, it.price, it.original_price, it.in_stock, "feed")
    db.commit()
    return stats


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
    stmt = select(Product)
    for t in q.split():
        stmt = stmt.where(or_(Product.name.ilike(f"%{t}%"), Product.ean == t))
    return list(db.scalars(stmt.limit(limit)).all())


def search_feed_offers(db: Session, q: str, limit: int = 30):
    """Feed obchody (Alza, Datart): hledáme v importovaných položkách feedu v DB."""
    from .shops.registry import SHOPS
    from .shops.search import SearchHit
    feed_slugs = [s.slug for s in SHOPS.values() if s.source == "feed"]
    stmt = select(Offer).join(Product).where(Offer.shop.in_(feed_slugs), Offer.price.is_not(None))
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
    from .shops.search import live_search
    global _term_pool
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
