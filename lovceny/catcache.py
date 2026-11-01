"""In-process TTL cache for hot category listings (Render free tier: keep it small)."""
from __future__ import annotations

import time
from threading import Lock

# 7 min: warm TTFB target <1s; invalidate after price fetch
TTL_S = 7 * 60
MAX_KEYS = 48  # ~dozen categories × a few views; stay light on 512 MB

_lock = Lock()
_store: dict[str, tuple[float, object]] = {}


def get(key: str):
    now = time.monotonic()
    with _lock:
        item = _store.get(key)
        if not item:
            return None
        exp, val = item
        if now >= exp:
            _store.pop(key, None)
            return None
        return val


def set(key: str, val) -> None:
    now = time.monotonic()
    with _lock:
        if len(_store) >= MAX_KEYS:
            # drop expired first, then oldest by expiry
            expired = [k for k, (e, _) in _store.items() if now >= e]
            for k in expired:
                _store.pop(k, None)
            while len(_store) >= MAX_KEYS:
                oldest = min(_store, key=lambda k: _store[k][0])
                _store.pop(oldest, None)
        _store[key] = (now + TTL_S, val)


def clear() -> None:
    with _lock:
        _store.clear()
