"""Obsah e-mailů (česky, text + jednoduché HTML s logem). Odkazy jsou absolutní podle PUBLIC_URL."""
import os
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .util import czk

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates" / "email"),
                   autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
_env.filters["czk"] = czk


def public_url() -> str:
    from .web import public_url as _public_url
    return _public_url()


def _product_url(product) -> str:
    from .names import canonical_slug
    slug = canonical_slug(getattr(product, "name", "") or "", getattr(product, "slug", "") or "")
    return f"{public_url()}/produkt/{product.id}-{slug}"


@dataclass
class Email:
    subject: str
    text: str
    html: str


def _render(name: str, subject: str, **ctx) -> Email:
    base = public_url()
    ctx.update(base=base, logo=f"{base}/static/brand/logo-email.png", subject=subject)
    return Email(subject, _env.get_template(f"{name}.txt").render(**ctx).strip() + "\n",
                 _env.get_template(f"{name}.html").render(**ctx))


def confirm_email(watch) -> Email:
    base = public_url()
    return _render("confirm", f"Potvrď hlídání ceny: {watch.product.name}", p=watch.product, w=watch,
                   confirm_url=f"{base}/hlidani/potvrdit/{watch.token}",
                   unsub_url=f"{base}/hlidani/zrusit/{watch.token}",
                   product_url=_product_url(watch.product))


def alert_email(watch, offer, shop_name: str, shop_url: str) -> Email:
    base = public_url()
    return _render("alert", f"Cena klesla: {watch.product.name} za {czk(offer.price)}", p=watch.product, w=watch,
                   o=offer, shop_name=shop_name, shop_url=shop_url,
                   unsub_url=f"{base}/hlidani/zrusit/{watch.token}",
                   product_url=_product_url(watch.product))


def digest_confirm_email(sub) -> Email:
    base = public_url()
    return _render("digest_confirm", "Potvrď odběr Týdenního přehledu slev", sub=sub,
                   confirm_url=f"{base}/prehled/email/potvrdit/{sub.token}",
                   unsub_url=f"{base}/prehled/email/zrusit/{sub.token}")


def digest_email(d, items: list[dict], sub) -> Email:
    base = public_url()
    from .digest import slug, week_range
    a, b = week_range(d.year, d.week)
    return _render("digest", f"Týdenní přehled slev {d.week}. týden: {len(items)} skutečných slev", d=d, items=items,
                   start=a, end=b, page_url=f"{base}/prehled/{slug(d)}?utm_source=email",
                   unsub_url=f"{base}/prehled/email/zrusit/{sub.token}")


def budget_confirm_email(b) -> Email:
    from .budget import describe
    base = public_url()
    return _render("budget_confirm", f"Potvrď hlídání rozpočtu: {describe(b)}", what=describe(b),
                   confirm_url=f"{base}/hlidat-rozpocet/potvrdit/{b.token}",
                   unsub_url=f"{base}/hlidat-rozpocet/zrusit/{b.token}")


def budget_alert_email(b, items) -> Email:
    from .budget import describe
    from .shops import get_shop
    base = public_url()
    n = len(items)
    word = "produkt" if n == 1 else ("produkty" if n <= 4 else "produktů")
    return _render("budget_alert", f"V rozpočtu: {n} {word} – {describe(b)}", what=describe(b), items=items,
                   shop_name=lambda o: get_shop(o.shop).name if get_shop(o.shop) else o.shop,
                   manage_url=f"{base}/hlidat-rozpocet/{b.token}", unsub_url=f"{base}/hlidat-rozpocet/zrusit/{b.token}")
