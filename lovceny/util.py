import re
import unicodedata


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:200] or "produkt"


def czk(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    s = f"{v:,.0f}".replace(",", "\u00a0")
    return f"{s}\u00a0Kč"


def normalize_url(u: str) -> str:
    u = u.strip().split("#")[0]
    u = re.sub(r"^http://", "https://", u)
    return u.rstrip("/")
