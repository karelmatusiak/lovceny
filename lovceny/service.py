import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Product, Offer, PricePoint, Watch
from .shops import shop_for_url, get_shop
from .shops.scrape import PoliteClient, parse_product, Blocked
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
    offer.price, offer.original_price, offer.in_stock = price, original, in_stock
    offer.status, offer.last_error = "ok", None
    offer.last_checked = when or datetime.now(timezone.utc)
    db.add(PricePoint(offer=offer, price=price, original_price=original, in_stock=in_stock,
                      captured_at=offer.last_checked, is_demo=False, source=source))


def update_offer(db: Session, offer: Offer) -> str:
    """Stáhne aktuální cenu jedné nabídky. Vrací stav."""
    shop = get_shop(offer.shop)
    if shop is None or not shop.can_scrape:
        if shop and shop.source == "feed":
            return "feed"  # řeší import feedu
        offer.status = "blocked"
        offer.last_error = shop.note if shop else "neznámý obchod"
        return "blocked"
    try:
        html = client().get(offer.url)
        res = (shop.page_parser or parse_product)(html)
    except Blocked as e:
        offer.status, offer.last_error = "blocked", str(e)
        offer.last_checked = datetime.now(timezone.utc)
        return "blocked"
    except Exception as e:  # síť, 404…
        offer.status, offer.last_error = "error", f"{type(e).__name__}: {e}"[:500]
        offer.last_checked = datetime.now(timezone.utc)
        return "error"
    if res is None:
        offer.status, offer.last_error = "error", "cena na stránce nenalezena (JSON-LD/meta)"
        return "error"
    p = offer.product
    if res.name and (p.name == _name_from_url(offer.url)):
        p.name, p.slug = res.name, slugify(res.name)
    p.image_url = p.image_url or res.image
    p.ean = p.ean or res.ean
    p.category = p.category or res.category
    record_price(db, offer, res.price, res.original_price, res.in_stock, "scrape")
    return "ok"


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
        elif st == "error":
            msg = f"Cenu se nepodařilo načíst: {offer.last_error}"
    db.commit()
    return prod, msg


def apply_feed(db: Session, shop_slug: str, items: list[FeedItem], create_missing: bool = False) -> dict:
    """Spáruje položky feedu s nabídkami (URL → ITEM_ID → EAN) a zapíše ceny."""
    stats = {"matched": 0, "created": 0, "skipped": 0}
    offers = db.scalars(select(Offer).where(Offer.shop == shop_slug)).all()
    by_url = {normalize_url(o.url): o for o in offers}
    by_id = {o.external_id: o for o in offers if o.external_id}
    by_ean = {o.product.ean: o for o in offers if o.product.ean}
    for it in items:
        o = by_url.get(normalize_url(it.url)) or by_id.get(it.item_id) or (by_ean.get(it.ean) if it.ean else None)
        if o is None:
            if not create_missing:
                stats["skipped"] += 1
                continue
            prod = None
            if it.ean:
                prod = db.scalar(select(Product).where(Product.ean == it.ean))
            prod = prod or Product(name=it.name, slug=slugify(it.name), ean=it.ean, image_url=it.image,
                                   category=it.category)
            o = Offer(product=prod, shop=shop_slug, url=normalize_url(it.url))
            db.add(o)
            stats["created"] += 1
        else:
            stats["matched"] += 1
        o.external_id = it.item_id or o.external_id
        if it.name and o.product.name == _name_from_url(o.url):
            o.product.name, o.product.slug = it.name, slugify(it.name)
        o.product.ean = o.product.ean or it.ean
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
