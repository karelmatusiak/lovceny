"""Týdenní přehled slev: každý pátek večer (Praha) snímek nejlepších skutečných slev týdne
(/prehled/<rok>-<týden>) + Web Push odběratelům tématu „tydenni“; e-mailová verze jen s EMAIL_BACKEND.

„Skutečná sleva“ = stejná metodika jako /slevy (deals.real_deals): verdikt Kupuj (do 3 % nad 90denním
minimem), řazeno podle úspory oproti mediánu 90 dní. Navíc: cena ověřená za poslední 3 dny a úspora ≥ 1 %.
Demo data se nezapočítávají (pokud není LOVCENY_INCLUDE_DEMO=1 – jen pro vývoj).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .models import WeeklyDigest, PushTopicSubscription, DigestEmail

log = logging.getLogger("lovceny.digest")
PRAGUE = ZoneInfo("Europe/Prague")
TOP = 10
MIN_SAVE_PCT = 1.0
FRESH_DAYS = 3
SEND_HOURS = (15, 16, 17, 18, 19, 20, 21)  # pátek večer Praha; CET 14 UTC=15, CEST 14 UTC=16; + zpoždění
TOPIC = "tydenni"


def week_of(now: datetime | None = None) -> tuple[int, int]:
    d = (now or datetime.now(timezone.utc)).astimezone(PRAGUE)
    y, w, _ = d.isocalendar()
    return y, w


def week_range(year: int, week: int):
    from datetime import date
    start = date.fromisocalendar(year, week, 1)
    return start, start + timedelta(days=6)


def email_enabled() -> bool:
    return os.environ.get("EMAIL_BACKEND", "").strip().lower() not in ("", "log")


def build_items(db, now: datetime | None = None, top: int = TOP) -> list[dict]:
    from .deals import real_deals
    from .shops import get_shop
    now = now or datetime.now(timezone.utc)
    out = []
    for r in real_deals(db, top=200)["buys"]:
        b = r.best
        lc = b.last_checked
        if lc is None or (lc if lc.tzinfo else lc.replace(tzinfo=timezone.utc)) < now - timedelta(days=FRESH_DAYS):
            continue
        if b.status != "ok" or r.savings_pct < MIN_SAVE_PCT:
            continue
        s = get_shop(b.shop)
        out.append({"product_id": r.product.id, "name": r.product.name, "slug": r.product.slug,
                    "image": r.product.image_url, "price": round(float(b.price), 2),
                    "median90": round(float(r.verdict.median90), 2), "min90": round(float(r.verdict.min90), 2),
                    "savings_czk": round(r.savings_czk, 2), "savings_pct": round(r.savings_pct, 1),
                    "shop": b.shop, "shop_name": s.name if s else b.shop, "category": r.product.category_slug,
                    "demo": r.has_demo})
        if len(out) >= top:
            break
    return out


def generate(db, now: datetime | None = None, refresh: bool = False) -> WeeklyDigest:
    """Idempotentní: jeden snímek na týden. refresh=True přepočítá položky (jen dokud se neposlalo)."""
    y, w = week_of(now)
    d = db.scalar(select(WeeklyDigest).where(WeeklyDigest.year == y, WeeklyDigest.week == w))
    if d is not None and not (refresh and d.push_sent_at is None):
        return d
    items = build_items(db, now)
    if d is None:
        d = WeeklyDigest(year=y, week=w, created_at=now or datetime.now(timezone.utc))
        db.add(d)
    d.items = json.dumps(items, ensure_ascii=False)
    db.commit()
    return d


def items_of(d: WeeklyDigest) -> list[dict]:
    try:
        return json.loads(d.items or "[]")
    except ValueError:
        return []


def slug(d: WeeklyDigest) -> str:
    return f"{d.year}-{d.week:02d}"


def push_payload(d: WeeklyDigest, sub: PushTopicSubscription, base: str) -> dict:
    from .util import czk
    items = items_of(d)
    first = items[0] if items else None
    n = len(items)
    word = "skutečná sleva" if n == 1 else ("skutečné slevy" if 2 <= n <= 4 else "skutečných slev")
    body = (f"Např. {first['name'][:60]} za {czk(first['price'])} (−{first['savings_pct']:.0f} % proti obvyklé ceně)."
            if first else "Tento týden bez výrazných slev.")
    return {"title": f"Týdenní přehled: {n} {word}", "body": body,
            "url": f"{base}/prehled/{slug(d)}?utm_source=push", "tag": "lovceny-prehled",
            "unsubscribe": f"{base}/prehled/odhlasit/{sub.token}", "open_label": "Zobrazit přehled",
            "stop_label": "Odhlásit přehled"}


def send_push(db, d: WeeklyDigest, sender=None) -> dict:
    from . import push as pushmod
    from .emails import public_url
    sender = sender or pushmod.send
    stats = {"sent": 0, "gone": 0, "error": 0}
    if d.push_sent_at is not None:
        return stats
    if not items_of(d):
        log.info("přehled %s je prázdný – push neposíláme", slug(d))
        d.push_sent_at = datetime.now(timezone.utc)
        db.commit()
        return stats
    if sender is pushmod.send and not pushmod.enabled():
        log.info("VAPID klíče chybí – push přeskočen")
        return stats
    base = public_url()
    for s in db.scalars(select(PushTopicSubscription).where(PushTopicSubscription.topic == TOPIC)).all():
        st = sender(s, push_payload(d, s, base))
        if st == "ok":
            s.last_sent_at = datetime.now(timezone.utc)
            stats["sent"] += 1
        elif st == "gone":
            db.delete(s)
            stats["gone"] += 1
        else:
            stats["error"] += 1
    d.push_sent_at, d.push_sent = datetime.now(timezone.utc), stats["sent"]
    db.commit()
    return stats


# ---------- e-mail (připraveno, vypnuto bez EMAIL_BACKEND) ----------
def email_subscribe(db, email: str) -> DigestEmail:
    e = db.scalar(select(DigestEmail).where(DigestEmail.email == email))
    if e is None:
        e = DigestEmail(email=email, token=secrets.token_urlsafe(24))
        db.add(e)
        db.commit()
    if not e.confirmed:
        from .emails import digest_confirm_email
        from .notify import get_sender
        m = digest_confirm_email(e)
        get_sender().send(e.email, m.subject, m.text, m.html)
    return e


def send_email(db, d: WeeklyDigest, sender=None) -> int:
    if d.email_sent_at is not None or not items_of(d):
        return 0
    if sender is None:
        if not email_enabled():
            return 0
        from .notify import get_sender
        sender = get_sender()
    from .emails import digest_email
    n = 0
    for e in db.scalars(select(DigestEmail).where(DigestEmail.confirmed.is_(True))).all():
        m = digest_email(d, items_of(d), e)
        if sender.send(e.email, m.subject, m.text, m.html):
            e.last_sent_at = datetime.now(timezone.utc)
            n += 1
    d.email_sent_at, d.email_sent = datetime.now(timezone.utc), n
    db.commit()
    return n


def cleanup(db, now: datetime | None = None) -> int:
    """Nepotvrzené e-maily po 7 dnech pryč."""
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(DigestEmail).where(DigestEmail.confirmed.is_(False))).all()
    n = 0
    for r in rows:
        c = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        if c < now - timedelta(days=7):
            db.delete(r)
            n += 1
    db.commit()
    return n


def run(db, now: datetime | None = None, force: bool = False, push_sender=None, email_sender=None) -> dict:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(PRAGUE)
    if not force and (local.weekday() != 4 or local.hour not in SEND_HOURS):
        return {"skipped": f"teď je {local:%a %H:%M} Praha – přehled jde jen v pátek 15–21 h"}
    d = generate(db, now)
    out = {"digest": slug(d), "items": len(items_of(d))}
    # Dedupe: po prvním pátečním odeslání (push_sent_at) další cron sloty v okně přeskočí
    if not force and d.push_sent_at is not None:
        return {**out, "skipped": "už odesláno tento týden", "push": {"sent": 0, "gone": 0, "error": 0},
                "email": 0, "cleanup": 0}
    out["push"] = send_push(db, d, push_sender)
    out["email"] = send_email(db, d, email_sender)
    out["cleanup"] = cleanup(db, now)
    return out
