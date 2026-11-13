"""Veřejný katalog je jen o telefonech. Ostatní kategorie v databázi zůstávají.

Žádné vymyšlené parametry: název, značka, úložiště a barva bereme z názvu,
cenu a počet obchodů z nabídek, verdikt z naměřené historie.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .availability import offer_is_buyable
from .categories import is_accessory_text, is_non_new
from .exclusions import is_excluded_brand
from .matching import model_family
from .models import Offer, Product
from .names import guess_brand
from .shops.feed import _colors, canonicalize_name
from .util import slugify

PHONE = "telefony"
_STORAGE_RE = re.compile(r"\b(\d+)\s*(GB|TB)\b", re.I)

BANDS: tuple[tuple[str, str, int | None, int | None], ...] = (
    ("do-5", "do 5 000 Kč", None, 5000),
    ("5-10", "5–10 000 Kč", 5000, 10000),
    ("10-20", "10–20 000 Kč", 10000, 20000),
    ("20-plus", "20 000 Kč a víc", 20000, None),
)


@dataclass(frozen=True)
class Guide:
    slug: str
    title: str
    lead: str
    max_price: int | None = None
    min_price: int | None = None
    brands: tuple[str, ...] = ()
    versus: bool = False


GUIDES: tuple[Guide, ...] = (
    Guide("nejlepsi-do-5-tisic", "Nejlepší telefon do 5 000 Kč",
          "Řazeno jen podle nejnižší ceny, kterou teď máme. Žádné testy a žádné vymyšlené parametry.",
          max_price=5000),
    Guide("nejlepsi-do-8-tisic", "Nejlepší telefon do 8 000 Kč",
          "Řazeno jen podle nejnižší ceny, kterou teď máme. Žádné testy a žádné vymyšlené parametry.",
          max_price=8000),
    Guide("nejlepsi-do-10-tisic", "Nejlepší telefon do 10 000 Kč",
          "Řazeno jen podle nejnižší ceny, kterou teď máme. Žádné testy a žádné vymyšlené parametry.",
          max_price=10000),
    Guide("nejlepsi-do-15-tisic", "Nejlepší telefon do 15 000 Kč",
          "Řazeno jen podle nejnižší ceny, kterou teď máme. Žádné testy a žádné vymyšlené parametry.",
          max_price=15000),
    Guide("iphone-nebo-samsung-do-15-tisic", "iPhone nebo Samsung do 15 000 Kč",
          "Dvě značky, stejné cenové pásmo. U každého modelu jen nejnižší cena, kterou teď vidíme.",
          max_price=15000, brands=("Apple", "Samsung"), versus=True),
    Guide("iphone-nebo-samsung-nad-15-tisic", "iPhone nebo Samsung nad 15 000 Kč",
          "Dvě značky nad 15 000 Kč. Pořadí je nejnižší aktuální cena, nic jiného.",
          min_price=15000, brands=("Apple", "Samsung"), versus=True),
    Guide("nejlevnejsi-iphone", "Nejlevnější iPhone",
          "iPhony, které teď máme v katalogu, od nejnižší ceny.",
          brands=("Apple",)),
    Guide("nejlevnejsi-samsung", "Nejlevnější Samsung",
          "Samsungy, které teď máme v katalogu, od nejnižší ceny.",
          brands=("Samsung",)),
    Guide("nejlevnejsi-xiaomi", "Nejlevnější Xiaomi",
          "Xiaomi, které teď máme v katalogu, od nejnižší ceny.",
          brands=("Xiaomi",)),
)
GUIDE_BY_SLUG = {guide.slug: guide for guide in GUIDES}


@dataclass
class PhoneModel:
    slug: str
    name: str
    brand: str | None
    products: list[Product] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    lowest: float | None = None
    in_stock: bool = False
    shops: int = 0
    image: str | None = None

    @property
    def storages(self) -> list[str]:
        found = []
        for product in self.products:
            label = storage_label(product.name or "")
            if label and label not in found:
                found.append(label)
        return found

    @property
    def colors(self) -> list[str]:
        found = []
        for product in self.products:
            for color in sorted(color_labels(product.name or "")):
                if color not in found:
                    found.append(color)
        return found


def _storage_sort_key(label: str) -> int:
    match = re.match(r"(\d+)", label or "")
    if not match:
        return 10**9
    size = int(match.group(1))
    return size * 1024 if "TB" in (label or "") else size


def storage_deals(model: PhoneModel) -> list[dict]:
    """Nejnižší cena, kterou teď máme pro každé uvedené úložiště."""
    best: dict[str, dict] = {}
    for product in model.products:
        label = storage_label(product.name or "")
        if not label:
            continue
        offers = [offer for offer in model.offers if offer.product_id == product.id]
        buyable = [offer for offer in offers if offer_is_buyable(offer)]
        pool = buyable or offers
        if not pool:
            continue
        winner = min(pool, key=lambda offer: float(offer.price))
        price = float(winner.price)
        current = best.get(label)
        if current is None or price < current["price"]:
            best[label] = {
                "label": label,
                "price": price,
                "in_stock": bool(buyable),
                "product_id": product.id,
            }
    rows = sorted(best.values(), key=lambda row: _storage_sort_key(row["label"]))
    large = [row for row in rows if _storage_sort_key(row["label"]) >= 32]
    return large or rows


def anchor_product(model: PhoneModel) -> Product | None:
    """Varianta, kterou bereme do srovnání: nejlevnější skladem, jinak nejlevnější vůbec."""
    buyable = [offer for offer in model.offers if offer_is_buyable(offer)]
    pool = buyable or list(model.offers)
    if not pool:
        return model.products[0] if model.products else None
    winner = min(pool, key=lambda offer: float(offer.price))
    for product in model.products:
        if product.id == winner.product_id:
            return product
    return model.products[0]


def buying_signal(model: PhoneModel):
    """Verdikt z historie nejlevnější skladové varianty. Bez dostatku dní zůstane „Sbíráme data“."""
    from .verdict import compute
    product = anchor_product(model)
    if product is None or model.lowest is None:
        return None
    points = [
        point for offer in model.offers
        if offer.product_id == product.id
        for point in offer.points
    ]
    return compute(points, model.lowest)


def is_phone_category(slug: str | None) -> bool:
    return slug == PHONE


def is_public_phone(product: Product) -> bool:
    """Telefon, který smí na web. Příslušenství, použitý kus a cizí kategorie ne."""
    if product.category_slug != PHONE:
        return False
    if product.retired_at is not None or product.catalog_hidden or product.needs_review:
        return False
    name = product.name or ""
    if is_excluded_brand(name) or is_accessory_text(name):
        return False
    if is_non_new(name, shop_category=product.category):
        return False
    return True


def storage_label(name: str) -> str | None:
    matches = list(_STORAGE_RE.finditer(name or ""))
    if not matches:
        return None

    def _size(match: re.Match) -> int:
        amount = int(match.group(1))
        return amount * 1024 if match.group(2).upper() == "TB" else amount

    match = max(matches, key=_size)
    # 4/8/12 GB v názvu je skoro vždy operační paměť, když vedle ní je i úložiště.
    if _size(match) < 32 and any(_size(other) >= 32 for other in matches):
        match = max((item for item in matches if _size(item) >= 32), key=_size)
    return f"{int(match.group(1))} {match.group(2).upper()}"


_COLOR_CS = {
    "black": "černá", "white": "bílá", "blue": "modrá", "pink": "růžová",
    "green": "zelená", "yellow": "žlutá", "purple": "fialová", "silver": "stříbrná",
    "gold": "zlatá", "gray": "šedá", "orange": "oranžová", "red": "červená",
}


def color_labels(name: str) -> set[str]:
    canon = canonicalize_name(name or "")
    return {_COLOR_CS.get(token, token) for token in _colors(canon)}


def _color_prefixes() -> set[str]:
    from .shops.feed import _COLOR_CANON
    prefixes = set()
    for source in _COLOR_CANON:
        parts = source.split()
        if len(parts) > 1:
            prefixes.update(parts[:-1])
    return prefixes


def _model_name(names: list[str]) -> str:
    """Název modelu bez úložiště, úhlopříčky a barvy. Pořadí slov necháváme z názvu."""
    from .shops.feed import _COLOR_CANON, canonicalize_name
    colors = set(_COLOR_CANON) | set(_COLOR_CANON.values())
    prefixes = _color_prefixes()
    best = ""
    for name in names:
        words = re.findall(r"\S+", name or "")
        kept = []
        index = 0
        noise = {"mah", "ip68", "esim", "lte", "nfc", "distribuce"}
        while index < len(words):
            word = words[index].strip(".,;:()[]\"“”")
            canon = canonicalize_name(word)
            nxt_word = words[index + 1].strip(".,;:()[]\"“”") if index + 1 < len(words) else ""
            nxt = canonicalize_name(nxt_word)
            if canon in {"gb", "tb"}:
                if kept and re.fullmatch(r"\d+", canonicalize_name(kept[-1])):
                    kept.pop()
                index += 1
                continue
            if re.fullmatch(r"\d+(?:gb|tb)", canon or "") or _STORAGE_RE.fullmatch(word):
                index += 1
                continue
            if re.fullmatch(r"\d+\+\d+", word) or re.fullmatch(r"\d+gb/\d+gb", word, flags=re.I):
                index += 1
                continue
            if re.fullmatch(r"\d+/\d+(?:gb|tb)?", word, flags=re.I) or re.fullmatch(r"\d+/\d+(?:gb|tb)?", canon or ""):
                index += 1
                continue
            if canon in {"barva", "pamet", "barvy", "pl"}:
                index += 1
                continue
            if word.endswith(("\"", "″", "”")) or re.fullmatch(r"\d+(?:[.,]\d+)?\"", word):
                index += 1
                continue
            if nxt in colors and canon not in {"pro", "max", "plus", "mini", "ultra", "air", "note", "iphone", "galaxy"}:
                index += 2
                continue
            if canon in colors or canon in prefixes or canon in noise:
                index += 1
                continue
            if word:
                kept.append(word)
            index += 1
        text = re.sub(r"\s+", " ", " ".join(kept)).strip(" -/,")
        if text and (not best or (len(text), text.casefold()) < (len(best), best.casefold())):
            best = text
    return best or "Telefon"


def _family_key(name: str, product_id: int):
    key = model_family(name or "", PHONE)
    return key if key is not None else ("solo", product_id)


def _visible_offers(product: Product) -> list[Offer]:
    return [offer for offer in product.offers if not getattr(offer, "withheld", False) and offer.price is not None]


def group_phones(products: list[Product]) -> list[PhoneModel]:
    """Jedna karta na model. Úložiště a barva jsou varianty, ne samostatné modely."""
    buckets: dict[tuple, list[Product]] = {}
    order: list[tuple] = []
    for product in products:
        if not is_public_phone(product):
            continue
        key = _family_key(product.name or "", product.id)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(product)
    titled: dict[str, tuple[str, list[Product]]] = {}
    title_order: list[str] = []
    for key in order:
        group = buckets[key]
        title = _model_name([product.name or "" for product in group])
        fold = title.casefold()
        if fold not in titled:
            titled[fold] = (title, [])
            title_order.append(fold)
        titled[fold][1].extend(group)
    used_slugs: set[str] = set()
    models = []
    for fold in title_order:
        title, group = titled[fold]
        slug = slugify(title) or f"model-{group[0].id}"
        if slug in used_slugs:
            slug = f"{slug}-{group[0].id}"
        used_slugs.add(slug)
        offers: list[Offer] = []
        for product in group:
            offers.extend(_visible_offers(product))
        buyable = [offer for offer in offers if offer_is_buyable(offer)]
        priced = buyable or offers
        lowest = min((float(offer.price) for offer in priced), default=None)
        image = next((product.image_url for product in group if product.image_url), None)
        brand = next((guess_brand(product.name or "") for product in group if guess_brand(product.name or "")), None)
        if brand is None and any("iphone" in (product.name or "").casefold() for product in group):
            brand = "Apple"
        if brand and not title.casefold().startswith(brand.casefold()):
            title = f"{brand} {title}"
            slug = slugify(title) or slug
        models.append(PhoneModel(
            slug=slug,
            name=title,
            brand=brand,
            products=group,
            offers=sorted(offers, key=lambda offer: (not offer_is_buyable(offer), float(offer.price))),
            lowest=lowest,
            in_stock=bool(buyable),
            shops=len({offer.shop for offer in offers}),
            image=image,
        ))
    models.sort(key=lambda model: (-model.shops, model.lowest if model.lowest is not None else 10**12, model.name.casefold()))
    return models


def in_band(price: float | None, low: int | None, high: int | None) -> bool:
    if price is None:
        return False
    if low is not None and price < low:
        return False
    if high is not None and price >= high:
        return False
    return True


def filter_models(models: list[PhoneModel], *, brand: str = "", band: str = "") -> list[PhoneModel]:
    chosen = models
    if brand:
        wanted = brand.casefold()
        chosen = [model for model in chosen if (model.brand or "").casefold() == wanted]
    spec = next((item for item in BANDS if item[0] == band), None)
    if spec is not None:
        _slug, _label, low, high = spec
        chosen = [model for model in chosen if in_band(model.lowest, low, high)]
    return chosen


def guide_models(guide: Guide, models: list[PhoneModel]) -> list[PhoneModel]:
    chosen = models
    if guide.brands:
        allowed = {brand.casefold() for brand in guide.brands}
        chosen = [model for model in chosen if (model.brand or "").casefold() in allowed]
    chosen = [
        model for model in chosen
        if in_band(model.lowest, guide.min_price, guide.max_price)
    ]
    return sorted(chosen, key=lambda model: (model.lowest is None, model.lowest or 0, model.name.casefold()))


def load_phone_products(db: Session) -> list[Product]:
    rows = list(db.scalars(
        select(Product).where(
            Product.category_slug == PHONE,
            Product.retired_at.is_(None),
            Product.catalog_hidden.is_(False),
            Product.needs_review.is_(False),
        ).options(selectinload(Product.offers).selectinload(Offer.points))
    ).all())
    return [row for row in rows if is_public_phone(row)]


def phone_models(db: Session) -> list[PhoneModel]:
    return group_phones(load_phone_products(db))


def find_model(db: Session, slug: str) -> PhoneModel | None:
    for model in phone_models(db):
        if model.slug == slug:
            return model
    return None


def brands_in(models: list[PhoneModel]) -> list[str]:
    found = []
    for model in models:
        if model.brand and model.brand not in found:
            found.append(model.brand)
    return sorted(found, key=str.casefold)


def select_variant(model: PhoneModel, storage: str = "", color: str = "") -> list[Product]:
    chosen = model.products
    if storage:
        wanted = storage.casefold()
        chosen = [product for product in chosen if (storage_label(product.name or "") or "").casefold() == wanted]
    if color:
        wanted = color.casefold()
        chosen = [product for product in chosen if wanted in {item.casefold() for item in color_labels(product.name or "")}]
    return chosen or model.products
