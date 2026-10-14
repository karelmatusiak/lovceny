"""Strukturovaná data (JSON-LD), sitemap.xml a robots.txt. Absolutní URL podle PUBLIC_URL."""
import json
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from markupsafe import Markup
from sqlalchemy import select

from .categories import CATEGORIES
from .models import Product
from .web import public_url


def jsonld(obj) -> Markup:
    """Bezpečné vložení do <script type="application/ld+json"> (žádné </script> v datech)."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return Markup(s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _abs(path: str) -> str:
    return path if path.startswith("http") else public_url() + path


def breadcrumb_ld(crumbs) -> dict:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": name, "item": _abs(url)}
                                for i, (name, url) in enumerate(crumbs)]}


def product_ld(p: Product, offers, verdict=None) -> dict:
    from .shops import get_shop
    priced = [o for o in offers if o.price is not None]
    d = {"@context": "https://schema.org", "@type": "Product", "name": p.name,
         "url": _abs(f"/produkt/{p.id}-{p.slug}")}
    if p.image_url:
        d["image"] = p.image_url
    if p.ean:
        d["gtin13" if len(p.ean) == 13 else "gtin"] = p.ean
    if p.category:
        d["category"] = p.category
    if priced:
        prices = [float(o.price) for o in priced]
        d["offers"] = {
            "@type": "AggregateOffer", "priceCurrency": "CZK", "lowPrice": f"{min(prices):.2f}",
            "highPrice": f"{max(prices):.2f}", "offerCount": len(priced),
            "availability": "https://schema.org/InStock" if any(o.in_stock for o in priced) else "https://schema.org/OutOfStock",
            "offers": [{"@type": "Offer", "price": f"{float(o.price):.2f}", "priceCurrency": "CZK", "url": o.url,
                        "seller": {"@type": "Organization", "name": get_shop(o.shop).name if get_shop(o.shop) else o.shop},
                        **({"availability": "https://schema.org/InStock" if o.in_stock else "https://schema.org/OutOfStock"}
                           if o.in_stock is not None else {})} for o in sorted(priced, key=lambda o: float(o.price))]}
    return d


def itemlist_ld(rows, path: str) -> dict:
    return {"@context": "https://schema.org", "@type": "ItemList", "url": _abs(path),
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": _abs(f"/produkt/{p.id}-{p.slug}"), "name": p.name}
                                for i, (p, _b, _v) in enumerate(rows[:50])]}


STATIC_PAGES = ["/", "/slevy", "/kategorie", "/obchody", "/o-nas", "/ochrana-osobnich-udaju", "/cookies", "/jak-radime", "/pro-obchody", "/podminky", "/kdy-nakupovat", "/over-slevu", "/rozsireni", "/prehled", "/hlidat-rozpocet", "/black-friday"]


def sitemap_xml(db) -> str:
    base = public_url()
    urls = [(base + p, None) for p in STATIC_PAGES]
    for c in CATEGORIES:
        urls += [(f"{base}/kategorie/{c.slug}", None), (f"{base}/kategorie/{c.slug}/nejlevnejsi", None),
                 (f"{base}/kategorie/{c.slug}/ve-sleve", None)]
    for pid, slug, created in db.execute(select(Product.id, Product.slug, Product.created_at).order_by(Product.id)).all():
        urls.append((f"{base}/produkt/{pid}-{slug}", created))
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, lm in urls:
        out.append(f"<url><loc>{escape(u)}</loc>" + (f"<lastmod>{lm.date().isoformat()}</lastmod>" if lm else "") + "</url>")
    out.append("</urlset>")
    return "\n".join(out)


def robots_txt() -> str:
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /hledat",
        "Disallow: /nahled",
        "Disallow: /porovnani",
        "Disallow: /hlidani/",
        "Disallow: /hlidat-rozpocet/",
        "Disallow: /black-friday/odhlasit/",
        "Disallow: /api/",
        "Disallow: /admin",
        "Disallow: /img/",
        "Disallow: /offline",
        f"Sitemap: {public_url()}/sitemap.xml", ""])
