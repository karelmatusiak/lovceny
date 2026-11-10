"""Počty z migrací do logu nasazení. Nové migrace sem zapisují keep/retire/merge."""
from __future__ import annotations

import logging

log = logging.getLogger("lovceny.migrate")


def log_counts(revision: str, **counts) -> None:
    text = " ".join(f"{key}={value}" for key, value in counts.items())
    log.info("migrace %s %s", revision, text)
