"""Která nabídka smí být „nejlevnější“ a mít tlačítko Koupit.

Neověřená cena (obchod ji v posledních dvou úspěšných bězích neviděl, nebo
poslední stažení skončilo blokací) se ve výpisech nepropaguje. Historie cen
u nabídky zůstává.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .availability import offer_is_buyable

# Běh se do DB zapíše až na konci. Nabídka ověřená během něj má last_checked
# o pár minut dřív než run_at, a proto se za „neviděnou“ nepočítá.
STALE_GRACE = timedelta(minutes=45)

_CUTOFFS: contextvars.ContextVar[dict[str, datetime] | None] = contextvars.ContextVar(
    "lovceny_stale_cutoffs", default=None,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reset_cutoffs() -> None:
    _CUTOFFS.set(None)


def current_cutoffs() -> dict[str, datetime] | None:
    return _CUTOFFS.get()


def load_stale_cutoffs(db: Session) -> dict[str, datetime]:
    """Obchod → čas staršího ze dvou posledních úspěšných běhů.

    Úspěšný běh je ten, který aspoň jednu cenu opravdu zapsal (`ok` > 0).
    Méně než dva takové běhy znamenají, že nabídky obchodu ještě nejsou zastaralé.
    """
    from .models import ShopRunStat
    rows = db.execute(
        select(ShopRunStat.shop, ShopRunStat.run_at)
        .where(ShopRunStat.ok > 0)
        .order_by(ShopRunStat.run_at.desc())
    ).all()
    seen: dict[str, list[datetime]] = {}
    for shop, run_at in rows:
        bucket = seen.setdefault(shop, [])
        if len(bucket) < 2 and run_at is not None:
            bucket.append(run_at)
    out: dict[str, datetime] = {}
    for shop, times in seen.items():
        if len(times) >= 2:
            aware = _aware(times[1])
            if aware is not None:
                out[shop] = aware
    return out


def use_cutoffs(db: Session) -> dict[str, datetime]:
    data = load_stale_cutoffs(db)
    _CUTOFFS.set(data)
    return data


def offer_is_blocked(offer) -> bool:
    """Poslední pokus o tuto nabídku skončil blokací (captcha, 403, …)."""
    return getattr(offer, "status", None) == "blocked"


def offer_is_stale(offer, cutoffs: dict[str, datetime] | None = None) -> bool:
    """Poslední dva úspěšné běhy obchodu nabídku neviděly."""
    if cutoffs is None:
        cutoffs = current_cutoffs()
    if not cutoffs:
        return False
    cutoff = cutoffs.get(getattr(offer, "shop", None) or "")
    if cutoff is None:
        return False
    checked = _aware(getattr(offer, "last_checked", None))
    if checked is None:
        return True
    return checked < cutoff - STALE_GRACE


def offer_mismatches_product(offer, product=None) -> bool:
    """URL nabídky zřetelně jmenuje jiný model nebo značku než produkt."""
    product = product if product is not None else getattr(offer, "product", None)
    name = getattr(product, "name", None) or ""
    url = getattr(offer, "url", None) or ""
    if not name.strip() or not url.strip():
        return False
    from .matching import clear_model_conflict, label_from_url
    return clear_model_conflict(name, label_from_url(url))


def unverified_note(offer, cutoffs: dict[str, datetime] | None = None, product=None) -> str | None:
    if offer_mismatches_product(offer, product):
        return "Nabídka je k jinému modelu"
    if offer_is_blocked(offer):
        return "Cenu se nepodařilo ověřit"
    if offer_is_stale(offer, cutoffs):
        return "Cena neověřena"
    return None


def offer_is_promoted(offer, cutoffs: dict[str, datetime] | None = None, product=None) -> bool:
    """Smí být nejlevnější, ve slevách a mít tlačítko Koupit."""
    if not offer_is_buyable(offer):
        return False
    if unverified_note(offer, cutoffs, product):
        return False
    return True
