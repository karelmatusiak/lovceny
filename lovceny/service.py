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
        res = parse_product(client().get(offer.url))
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
    w = Watch(product=product, email=email.strip().lower(), target_price=target, only_in_stock=only_in_stock,
              token=secrets.token_urlsafe(24))
    db.add(w)
    db.commit()
    get_sender().send(w.email, f"Potvrď hlídacího psa: {product.name}",
                      f"Potvrzení: /pes/potvrdit/{w.token}\nCílová cena: {czk(target)}\n"
                      f"Odhlášení jedním klikem: /pes/zrusit/{w.token}")
    return w


def check_watches(db: Session) -> int:
    sent = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    for w in db.scalars(select(Watch).where(Watch.active.is_(True), Watch.confirmed.is_(True))).all():
        if w.notified_at and w.notified_at.replace(tzinfo=w.notified_at.tzinfo or timezone.utc) > cutoff:
            continue
        cands = [o for o in w.product.offers if o.price is not None and o.status == "ok"
                 and (not w.only_in_stock or o.in_stock)]
        hit = min(cands, key=lambda o: o.price, default=None)
        if hit and float(hit.price) <= float(w.target_price):
            shop = get_shop(hit.shop)
            get_sender().send(w.email, f"Cena klesla na {czk(hit.price)} v obchodě {shop.name if shop else hit.shop}",
                              f"{w.product.name}\n{hit.url}\nOdhlásit: /pes/zrusit/{w.token}")
            w.notified_at = datetime.now(timezone.utc)
            sent += 1
    db.commit()
    return sent
