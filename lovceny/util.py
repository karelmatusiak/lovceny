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


def cz_plural(n: int, one: str, few: str, many: str) -> str:
    """České skloňování: 1 den, 2–4 dny, 5+ (a 0) dní. Záporná čísla bereme absolutně."""
    n = abs(int(n))
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def cz_count(n: int, one: str, few: str, many: str) -> str:
    """Např. cz_count(1, 'den', 'dny', 'dní') → '1 den'."""
    n = int(n)
    return f"{n}\u00a0{cz_plural(n, one, few, many)}"
