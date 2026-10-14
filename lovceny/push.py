"""Web Push (VAPID) – upozornění „cena klesla“ do prohlížeče, alternativa k e-mailu.

Klíče z env: VAPID_PUBLIC_KEY (base64url, nekomprimovaný bod P-256), VAPID_PRIVATE_KEY (base64url, 32 B),
VAPID_SUBJECT (mailto:… nebo https://…). Bez klíčů je funkce vypnutá a UI ji neukazuje.
Neukládáme e-mail ani nic dalšího: jen endpoint push služby prohlížeče + jeho šifrovací klíče a cílovou cenu.
"""
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .models import Product, PushSubscription
from .util import czk

log = logging.getLogger("lovceny.push")

# Endpointy smíme volat jen u známých push služeb prohlížečů (žádné SSRF z fetch jobu).
ALLOWED_PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com",
                      "push.apple.com", "web.push.apple.com")
MAX_PER_ENDPOINT = 50
REALERT_DROP = 0.03
MAX_AGE_DAYS = 730


def public_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "").strip()


def _private_key() -> str:
    return os.environ.get("VAPID_PRIVATE_KEY", "").strip()


def subject() -> str:
    return os.environ.get("VAPID_SUBJECT", "").strip() or "mailto:ahoj@lovceny.cz"


def enabled() -> bool:
    return bool(public_key() and _private_key())


def valid_endpoint(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and len(url) < 1000 and any(host == h or host.endswith("." + h) for h in ALLOWED_PUSH_HOSTS)


class PushError(ValueError):
    pass


def subscribe(db: Session, product: Product, sub: dict, target: float, only_in_stock: bool = False) -> PushSubscription:
    """Uloží odběr; stejný prohlížeč + produkt = aktualizace (žádné duplicity)."""
    endpoint = (sub or {}).get("endpoint", "")
    keys = (sub or {}).get("keys") or {}
    p256dh, auth = str(keys.get("p256dh", "")), str(keys.get("auth", ""))
    if not valid_endpoint(endpoint):
        raise PushError("Neplatný odběr prohlížeče.")
    if not (20 <= len(p256dh) <= 200 and 8 <= len(auth) <= 100):
        raise PushError("Neplatné klíče odběru.")
    if not target or target <= 0:
        raise PushError("Zadej cílovou cenu.")
    s = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint,
                                                 PushSubscription.product_id == product.id))
    if s is None:
        n = db.scalar(select(func.count(PushSubscription.id)).where(PushSubscription.endpoint == endpoint))
        if n >= MAX_PER_ENDPOINT:
            raise PushError(f"V tomto prohlížeči hlídáš už {MAX_PER_ENDPOINT} produktů.")
        s = PushSubscription(product=product, endpoint=endpoint, token=secrets.token_urlsafe(24))
        db.add(s)
    s.p256dh, s.auth, s.target_price, s.only_in_stock = p256dh, auth, target, only_in_stock
    s.notified_price = None
    db.commit()
    return s


def unsubscribe(db: Session, token: str = "", endpoint: str = "", product_id: int | None = None) -> int:
    q = select(PushSubscription)
    if token:
        q = q.where(PushSubscription.token == token)
    elif endpoint:
        q = q.where(PushSubscription.endpoint == endpoint)
        if product_id:
            q = q.where(PushSubscription.product_id == product_id)
    else:
        return 0
    rows = db.scalars(q).all()
    for r in rows:
        db.delete(r)
    db.commit()
    return len(rows)


def send(sub: PushSubscription, payload: dict) -> str:
    """Vrací ok | gone (odběr zanikl → smazat) | error."""
    if not enabled():
        return "error"
    from pywebpush import webpush, WebPushException
    try:
        webpush({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                json.dumps(payload, ensure_ascii=False), vapid_private_key=_private_key(),
                vapid_claims={"sub": subject()}, ttl=24 * 3600, timeout=10)
        return "ok"
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 410):
            return "gone"
        log.warning("push selhal (%s): %s", code, str(e)[:200])
        return "error"
    except Exception as e:  # noqa: BLE001 – síť apod.
        log.warning("push selhal: %s: %s", type(e).__name__, str(e)[:200])
        return "error"


def alert_payload(sub: PushSubscription, offer, shop_name: str, public_url: str) -> dict:
    p = sub.product
    return {"title": f"Cena klesla: {p.name[:80]}",
            "body": f"Teď {czk(offer.price)} v obchodě {shop_name} (tvůj cíl {czk(sub.target_price)}).",
            "url": f"{public_url}/produkt/{p.id}-{p.slug}?utm_source=push",
            "unsubscribe": f"{public_url}/hlidani/push/zrusit/{sub.token}",
            "tag": f"lovceny-{p.id}"}


def check_push(db: Session, sender=send) -> dict:
    """Volá fetch_prices.py po stažení cen. Stejná pravidla jako u e-mailu: jeden pokles = jedna notifikace."""
    from .shops import get_shop
    from .emails import public_url
    stats = {"sent": 0, "gone": 0, "error": 0}
    if not enabled() and sender is send:
        return stats
    for s in db.scalars(select(PushSubscription)).all():
        cands = [o for o in s.product.offers if o.price is not None and o.status == "ok"
                 and (not s.only_in_stock or o.in_stock)]
        hit = min(cands, key=lambda o: float(o.price), default=None)
        if not hit:
            continue
        price, target = float(hit.price), float(s.target_price)
        if price > target:
            s.notified_price = None
            continue
        if s.notified_price is not None and price > float(s.notified_price) * (1 - REALERT_DROP):
            continue
        shop = get_shop(hit.shop)
        st = sender(s, alert_payload(s, hit, shop.name if shop else hit.shop, public_url()))
        if st == "ok":
            s.notified_price, s.notified_at = price, datetime.now(timezone.utc)
            stats["sent"] += 1
        elif st == "gone":
            db.delete(s)
            stats["gone"] += 1
        else:
            stats["error"] += 1
        db.commit()
    db.commit()
    return stats


def cleanup_push(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    n = 0
    for s in db.scalars(select(PushSubscription)).all():
        last = max(d if d.tzinfo else d.replace(tzinfo=timezone.utc) for d in (s.created_at, s.notified_at) if d)
        if last < now - timedelta(days=MAX_AGE_DAYS):
            db.delete(s)
            n += 1
    db.commit()
    return n


# ---------- odběr témat (Týdenní přehled slev) ----------
TOPICS = {"tydenni": "Týdenní přehled slev"}


def subscribe_topic(db: Session, topic: str, sub: dict):
    from .models import PushTopicSubscription as T
    if topic not in TOPICS:
        raise PushError("Neznámé téma.")
    endpoint = (sub or {}).get("endpoint", "")
    keys = (sub or {}).get("keys") or {}
    p256dh, auth = str(keys.get("p256dh", "")), str(keys.get("auth", ""))
    if not valid_endpoint(endpoint):
        raise PushError("Neplatný odběr prohlížeče.")
    if not (20 <= len(p256dh) <= 200 and 8 <= len(auth) <= 100):
        raise PushError("Neplatné klíče odběru.")
    s = db.scalar(select(T).where(T.topic == topic, T.endpoint == endpoint))
    if s is None:
        s = T(topic=topic, endpoint=endpoint, token=secrets.token_urlsafe(24))
        db.add(s)
    s.p256dh, s.auth = p256dh, auth
    db.commit()
    return s


def unsubscribe_topic(db: Session, token: str = "", endpoint: str = "", topic: str = "tydenni") -> int:
    from .models import PushTopicSubscription as T
    if token:
        rows = db.scalars(select(T).where(T.token == token)).all()
    elif endpoint:
        rows = db.scalars(select(T).where(T.endpoint == endpoint, T.topic == topic)).all()
    else:
        return 0
    for r in rows:
        db.delete(r)
    db.commit()
    return len(rows)
