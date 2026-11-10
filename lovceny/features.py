"""Vypínače vedlejších funkcí (fáze 1).

Kód i řádky v databázi zůstávají. Výchozí stav je vypnuto: stránky neberou
nové odběry a úlohy nic neposílají. Odhlášení starých odběrů dál funguje.

Obnovení jedné funkce: LOVCENY_FEATURE_BUDGET=1, DIGEST, EXTENSION, AI nebo PUSH.
Obnovení všech najednou: LOVCENY_SIDE_FEATURES=1.
"""
from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}


def _flag(*names: str) -> bool:
    for name in names:
        if os.environ.get(name, "").strip().lower() in _TRUE:
            return True
    return False


def budget_on() -> bool:
    """Hlídání podle rozpočtu."""
    return _flag("LOVCENY_SIDE_FEATURES", "LOVCENY_FEATURE_BUDGET")


def digest_on() -> bool:
    """Týdenní přehled slev."""
    return _flag("LOVCENY_SIDE_FEATURES", "LOVCENY_FEATURE_DIGEST")


def extension_on() -> bool:
    """Rozšíření prohlížeče a jeho API."""
    return _flag("LOVCENY_SIDE_FEATURES", "LOVCENY_FEATURE_EXTENSION")


def ai_on() -> bool:
    """AI hledání (Gemini)."""
    return _flag("LOVCENY_SIDE_FEATURES", "LOVCENY_FEATURE_AI")


def push_on() -> bool:
    """Upozornění v prohlížeči (cena, týdenní přehled, Black Friday)."""
    return _flag("LOVCENY_SIDE_FEATURES", "LOVCENY_FEATURE_PUSH")
