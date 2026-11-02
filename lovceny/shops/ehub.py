"""eHUB affiliate click URLs (pm.ehub.cz).

Link template:
  https://ehub.cz/system/scripts/click.php?a_aid=<AID>&a_bid=<BID>&desturl=<URL-encoded target>

Publisher a_aid defaults to Lovceny's approved ID; set EHUB_AID= to disable wrapping.
Campaign a_bid values live on Shop.ehub_bid in registry.
"""
from __future__ import annotations

import os
from urllib.parse import quote

# Approved publisher ID for info@lovceny.cz (override with EHUB_AID, empty disables).
_DEFAULT_AID = "667205fc"
EHUB_CLICK = "https://ehub.cz/system/scripts/click.php"


def ehub_aid() -> str:
    """Publisher ID. Unset → default; EHUB_AID= (empty) → wrapping off."""
    if "EHUB_AID" in os.environ:
        return os.environ["EHUB_AID"].strip()
    return _DEFAULT_AID


def ehub_click_url(a_bid: str, dest_url: str, a_aid: str | None = None) -> str:
    """Build tracking click URL. dest_url is the product page (no further encoding beyond quote)."""
    aid = (a_aid if a_aid is not None else ehub_aid()).strip()
    bid = (a_bid or "").strip()
    if not (aid and bid and dest_url):
        return dest_url
    return f"{EHUB_CLICK}?a_aid={quote(aid, safe='')}&a_bid={quote(bid, safe='')}&desturl={quote(dest_url, safe='')}"
