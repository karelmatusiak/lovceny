"""Dognet affiliate click URLs (go.dognet.com).

Simple deep link (campaign is inferred from the product domain):
  https://go.dognet.com/?chid=<CHID>&url=<URL-encoded target>

Channel code defaults to Lovceny's approved Lovceny.cz channel.
DOGNET_CHID overrides it; DOGNET_CHID= (empty) turns wrapping off.
The code is public (same role as the eHUB publisher id), not a secret.
"""
from __future__ import annotations

import os
from urllib.parse import quote

# Approved channel Lovceny.cz (id 36085). Override with DOGNET_CHID; empty disables.
_DEFAULT_CHID = "9CFqgvpQ"
DOGNET_CLICK = "https://go.dognet.com/"


def dognet_chid(shop_chid: str | None = None) -> str:
    """Channel code. Unset env → shop value or default; DOGNET_CHID= (empty) → wrapping off."""
    if "DOGNET_CHID" in os.environ:
        return os.environ["DOGNET_CHID"].strip()
    return (shop_chid or _DEFAULT_CHID).strip()


def dognet_click_url(target_url: str, chid: str | None = None) -> str:
    """Wrap a product URL. Empty channel or target returns target unchanged."""
    code = dognet_chid(chid).strip()
    if not (code and target_url):
        return target_url
    return f"{DOGNET_CLICK}?chid={quote(code, safe='')}&url={quote(target_url, safe='')}"
