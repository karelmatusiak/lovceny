"""v7 – Hlídání podle rozpočtu: „Dej mi vědět, až bude pračka Bosch do 12 000 Kč.“

Uživatel zvolí kategorii, volitelně klíčové slovo / značku (hledá se v názvu bez diakritiky) a maximální cenu.
Po každém stažení cen (fetch_prices.py v GitHub Actions) projdeme hlídání a pošleme upozornění, když se objeví
produkt, jehož aktuální nejnižší ověřená cena je na rozpočtu nebo pod ním.

Pravidla (proti spamu a duplicitám):
- Při založení si zapamatujeme produkty, které už v rozpočtu jsou (ukážeme je hned na stránce) – upozornění
  pak přijde jen na NOVÉ produkty, nebo když stejný produkt zlevní o další ≥ 3 % (REALERT_DROP).
- Jedno hlídání = nejvýš 1 upozornění za běh (souhrn „a další 2“) a nejvýš MAX_PER_DAY za den (pražský čas).
- Počítá se jen cena ověřená za posledních FRESH_DAYS dní se stavem „ok“ (demo body se nepoužívají – ty jsou
  jen v historii, ne v aktuální ceně nabídky).
- Na jeden prohlížeč nejvýš MAX_PER_ENDPOINT hlídání; zakládání je omezené i podle (hashované) IP v main.py.
Kanály: Web Push (bez e-mailu) a e-mail (double opt-in; posílá se jen s nastaveným EMAIL_BACKEND).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from .categories import BY_SLUG, _fold
from .models import BudgetWatch, BudgetHit, Product, Offer
from .util import czk

log = logging.getLogger("lovceny.budget")
PRAGUE = ZoneInfo("Europe/Prague")
REALERT_DROP = 0.03
MAX_PER_DAY = 3
MAX_PER_ENDPOINT = 10
MAX_PER_EMAIL = 10
FRESH_DAYS = 3
MIN_PRICE, MAX_PRICE = 100, 1_000_000
MAX_AGE_DAYS = 365          # hlídání bez jediného upozornění / obnovení po roce mažeme
UNCONFIRMED_DAYS = 7


class BudgetError(ValueError):
    pass


def clean_keyword(k: str | None) -> str | None:
    k = " ".join((k or "").split())[:60]
    return k or None


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def describe(b) -> str:
    c = BY_SLUG.get(b.category_slug)
    what = (c.name if c else b.category_slug) + (f" – „{b.keyword}“" if b.keyword else "")
    return f"{what} do {czk(b.max_price)}"


def matches(db, category_slug: str, keyword: str | None, max_price: float, only_in_stock: bool = False,
            fresh_only: bool = True, now: datetime | None = None) -> list[tuple[Product, Offer]]:
    """Produkty kategorie, jejichž nejnižší (ověřená) cena je ≤ rozpočet. Seřazeno podle ceny."""
    now = now or datetime.now(timezone.utc)
    kw = _fold(keyword or "").strip()
    words = kw.split()
    products = db.scalars(select(Product).where(
        Product.category_slug == category_slug, Product.retired_at.is_(None), Product.catalog_hidden.is_(False))
                          .options(selectinload(Product.offers))).all()
    out = []
    for p in products:
        if words:
            name = _fold(p.name)
            if not all(w in name for w in words):
                continue
        cands = [o for o in p.offers if o.price is not None and o.status == "ok"
                 and (not only_in_stock or o.in_stock)
                 and (not fresh_only or (o.last_checked and _aware(o.last_checked) >= now - timedelta(days=FRESH_DAYS)))]
        best = min(cands, key=lambda o: float(o.price), default=None)
        if best and float(best.price) <= float(max_price):
            out.append((p, best))
    return sorted(out, key=lambda x: float(x[1].price))


def _validate(category_slug: str, max_price) -> float:
    if category_slug not in BY_SLUG:
        raise BudgetError("Vyber kategorii.")
    try:
        mp = float(max_price)
    except (TypeError, ValueError):
        raise BudgetError("Zadej maximální cenu v korunách.")
    if not (MIN_PRICE <= mp <= MAX_PRICE):
        raise BudgetError(f"Rozpočet musí být mezi {czk(MIN_PRICE)} a {czk(MAX_PRICE)}.")
    return round(mp)


def _baseline(db, b: BudgetWatch, now=None):
    """Produkty, které už v rozpočtu jsou, si poznamenáme (bez upozornění) – ukážeme je hned na stránce."""
    have = {h.product_id: h for h in b.hits}
    for p, o in matches(db, b.category_slug, b.keyword, float(b.max_price), b.only_in_stock, fresh_only=False, now=now):
        if p.id not in have:
            db.add(BudgetHit(budget=b, product_id=p.id, price=float(o.price), notified=False))


def subscribe_push(db, category_slug: str, keyword: str | None, max_price, sub: dict,
                   only_in_stock: bool = False) -> BudgetWatch:
    from .push import valid_endpoint
    mp = _validate(category_slug, max_price)
    endpoint = (sub or {}).get("endpoint", "")
    keys = (sub or {}).get("keys") or {}
    p256dh, auth = str(keys.get("p256dh", "")), str(keys.get("auth", ""))
    if not valid_endpoint(endpoint):
        raise BudgetError("Neplatný odběr prohlížeče.")
    if not (20 <= len(p256dh) <= 200 and 8 <= len(auth) <= 100):
        raise BudgetError("Neplatné klíče odběru.")
    keyword = clean_keyword(keyword)
    same = db.scalars(select(BudgetWatch).where(BudgetWatch.endpoint == endpoint,
                                                BudgetWatch.category_slug == category_slug)).all()
    b = next((x for x in same if (x.keyword or "").lower() == (keyword or "").lower()), None)
    if b is None:
        n = db.scalar(select(func.count(BudgetWatch.id)).where(BudgetWatch.endpoint == endpoint))
        if n >= MAX_PER_ENDPOINT:
            raise BudgetError(f"V tomto prohlížeči máš už {MAX_PER_ENDPOINT} hlídání rozpočtu. Nějaké nejdřív zruš.")
        b = BudgetWatch(token=secrets.token_urlsafe(24), category_slug=category_slug, keyword=keyword,
                        channel="push", endpoint=endpoint, confirmed=True)
        db.add(b)
    elif float(b.max_price) != mp:
        b.hits.clear()                      # jiný rozpočet = nový výchozí stav
    b.p256dh, b.auth, b.max_price, b.only_in_stock = p256dh, auth, mp, bool(only_in_stock)
    db.flush()
    _baseline(db, b)
    db.commit()
    return b


def subscribe_email(db, category_slug: str, keyword: str | None, max_price, email: str,
                    only_in_stock: bool = False, sender=None) -> BudgetWatch:
    """E-mailová varianta (double opt-in). Volá se jen, když je e-mail zapnutý (EMAIL_BACKEND)."""
    mp = _validate(category_slug, max_price)
    keyword = clean_keyword(keyword)
    n = db.scalar(select(func.count(BudgetWatch.id)).where(BudgetWatch.email == email))
    if n >= MAX_PER_EMAIL:
        raise BudgetError(f"Na tento e-mail už je {MAX_PER_EMAIL} hlídání rozpočtu.")
    b = BudgetWatch(token=secrets.token_urlsafe(24), category_slug=category_slug, keyword=keyword, max_price=mp,
                    only_in_stock=bool(only_in_stock), channel="email", email=email, confirmed=False)
    db.add(b)
    db.flush()
    _baseline(db, b)
    db.commit()
    from .emails import budget_confirm_email
    if sender is None:
        from .notify import get_sender
        sender = get_sender()
    m = budget_confirm_email(b)
    sender.send(b.email, m.subject, m.text, m.html)
    return b


def update(db, b: BudgetWatch, max_price) -> BudgetWatch:
    mp = _validate(b.category_slug, max_price)
    if float(b.max_price) != mp:
        b.max_price = mp
        b.hits.clear()
        db.flush()
        _baseline(db, b)
    db.commit()
    return b


def by_endpoint(db, endpoint: str) -> list[BudgetWatch]:
    if not endpoint:
        return []
    return db.scalars(select(BudgetWatch).where(BudgetWatch.endpoint == endpoint).order_by(BudgetWatch.created_at)).all()


def delete(db, token: str) -> int:
    b = db.scalar(select(BudgetWatch).where(BudgetWatch.token == token)) if token else None
    if not b:
        return 0
    db.delete(b)
    db.commit()
    return 1


# ---------- po stažení cen ----------
def pending(db, b: BudgetWatch, now=None) -> list[tuple[Product, Offer]]:
    """Shody, o kterých jsme ještě nepsali (nový produkt / další pokles o ≥ REALERT_DROP)."""
    have = {h.product_id: h for h in b.hits}
    out = []
    for p, o in matches(db, b.category_slug, b.keyword, float(b.max_price), b.only_in_stock, now=now):
        h = have.get(p.id)
        if h is None or float(o.price) <= float(h.price) * (1 - REALERT_DROP):
            out.append((p, o))
    return out


def payload(b: BudgetWatch, items: list, base: str) -> dict:
    from .shops import get_shop
    p, o = items[0]
    s = get_shop(o.shop)
    more = len(items) - 1
    extra = "" if more <= 0 else (f" + {more} další" if more <= 4 else f" + {more} dalších")
    return {"title": f"V rozpočtu: {p.name[:60]}",
            "body": f"{czk(o.price)} v obchodě {s.name if s else o.shop}{extra} · hlídáš {describe(b)}.",
            "url": (f"{base}/produkt/{p.id}-{p.slug}?utm_source=push" if more <= 0
                    else f"{base}/hlidat-rozpocet/{b.token}?utm_source=push"),
            "unsubscribe": f"{base}/hlidat-rozpocet/zrusit/{b.token}", "tag": f"lovceny-rozpocet-{b.id}",
            "open_label": "Zobrazit", "stop_label": "Zrušit hlídání"}


def check_budgets(db, push_sender=None, email_sender=None, now: datetime | None = None) -> dict:
    from . import push as pushmod
    from .emails import public_url
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(PRAGUE).date().isoformat()
    push_sender = push_sender or pushmod.send
    push_on = push_sender is not pushmod.send or pushmod.enabled()
    if email_sender is None:
        from .digest import email_enabled
        if email_enabled():
            from .notify import get_sender
            email_sender = get_sender()
    stats = {"sent": 0, "email": 0, "capped": 0, "gone": 0, "error": 0, "skipped": 0}
    base = public_url()
    for b in db.scalars(select(BudgetWatch).where(BudgetWatch.confirmed.is_(True))).all():
        if (b.channel == "push" and not push_on) or (b.channel == "email" and email_sender is None):
            stats["skipped"] += 1
            continue
        items = pending(db, b, now)
        if not items:
            continue
        if b.sent_day == today and b.sent_count >= MAX_PER_DAY:
            stats["capped"] += 1
            continue
        if b.channel == "push":
            st = push_sender(b, payload(b, items, base))
        else:
            from .emails import budget_alert_email
            m = budget_alert_email(b, items)
            st = "ok" if email_sender.send(b.email, m.subject, m.text, m.html) else "error"
        if st == "gone":
            db.delete(b)
            stats["gone"] += 1
        elif st == "ok":
            have = {h.product_id: h for h in b.hits}
            for p, o in items:
                h = have.get(p.id)
                if h is None:
                    db.add(BudgetHit(budget=b, product_id=p.id, price=float(o.price), notified=True))
                else:
                    h.price, h.notified = float(o.price), True
            b.sent_count = (b.sent_count if b.sent_day == today else 0) + 1
            b.sent_day, b.last_sent_at = today, now
            stats["sent" if b.channel == "push" else "email"] += 1
        else:
            stats["error"] += 1
        db.commit()
    return stats


def cleanup(db, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    n = 0
    for b in db.scalars(select(BudgetWatch)).all():
        created = _aware(b.created_at)
        last = max(d for d in (created, _aware(b.last_sent_at)) if d)
        if (not b.confirmed and created < now - timedelta(days=UNCONFIRMED_DAYS)) or last < now - timedelta(days=MAX_AGE_DAYS):
            db.delete(b)
            n += 1
    db.commit()
    return n
