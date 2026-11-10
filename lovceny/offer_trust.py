"""Která nabídka smí být „nejlevnější“ a mít tlačítko Koupit.

Neověřená cena se ve výpisech nepropaguje. Historie cen u nabídky zůstává.

Zastaralá je nabídka, jejíž poslední úspěšné pozorování je starší než oba
poslední běhy, ve kterých byla na řadě, nebo starší než STALE_MAX_AGE.
Běh obchodu, který nabídku vůbec nevybral (Mironet bere jen 24 nejstarších),
se jako minutí nepočítá. Poslední pokus zablokovaný captchou není koupitelný
bez ohledu na stáří.
"""
from __future__ import annotations

import contextvars
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .availability import offer_is_buyable

# Úspěch a razítko „byla na řadě“ vzniknou v tomtéž běhu pár vteřin od sebe.
STALE_GRACE = timedelta(minutes=45)
# Pojistka: i nabídka, na kterou rotace dlouho nedojde, po téhle době ztratí Koupit.
STALE_MAX_AGE = timedelta(days=4)
# Dva pokusy v jednom běhu (feed zapíše cenu a na konci označí všechny) jsou jeden běh.
_SAME_RUN = timedelta(minutes=45)

_CUTOFFS: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "lovceny_stale_cutoffs", default=None,
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def reset_cutoffs() -> None:
    _CUTOFFS.set(None)


def current_cutoffs():
    return _CUTOFFS.get()


def load_stale_cutoffs(db: Session) -> dict:
    """Stáří se počítá z razítek na nabídce, ne z běhu celého obchodu."""
    return {}


def use_cutoffs(db: Session) -> dict:
    data = load_stale_cutoffs(db)
    _CUTOFFS.set(data)
    return data


def note_offer_due(offer, when: datetime | None = None) -> None:
    """Nabídka byla v tomhle běhu na řadě. Přeskočená (mimo dávku) se nevolá."""
    stamp = _aware(when or datetime.now(timezone.utc))
    current = _aware(getattr(offer, "due_at", None))
    if current is not None and abs(stamp - current) < _SAME_RUN:
        offer.due_at = stamp
        return
    offer.due_prev = current
    offer.due_at = stamp


def last_success_at(offer) -> datetime | None:
    """Kdy jsme naposledy opravdu zapsali cenu, ne kdy naposledy spadla captcha."""
    explicit = _aware(getattr(offer, "last_ok_at", None))
    if explicit is not None:
        return explicit
    if getattr(offer, "status", None) == "ok":
        return _aware(getattr(offer, "last_checked", None))
    return None


def offer_is_blocked(offer) -> bool:
    """Poslední pokus o tuto nabídku skončil blokací (captcha, 403, …)."""
    return getattr(offer, "status", None) == "blocked"


def offer_is_stale(offer, cutoffs=None, now: datetime | None = None) -> bool:
    """Dva běhy, ve kterých byla na řadě, ji neviděly, nebo je úspěch starší než 4 dny.

    `cutoffs` se nepoužívá. Celý běh obchodu (Mironet 24 z 65) není minutí
    nabídky, která v dávce nebyla.
    """
    moment = _aware(now or datetime.now(timezone.utc))
    success = last_success_at(offer)
    if success is not None and moment is not None and success < moment - STALE_MAX_AGE:
        return True
    older = _aware(getattr(offer, "due_prev", None))
    newer = _aware(getattr(offer, "due_at", None))
    if older is None or newer is None:
        return False
    if success is None:
        return True
    return success < older - STALE_GRACE


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
