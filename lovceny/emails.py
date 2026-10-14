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
    return os.environ.get("PUBLIC_URL", "https://lovceny.onrender.com").rstrip("/")


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
                   product_url=f"{base}/produkt/{watch.product.id}-{watch.product.slug}")


def alert_email(watch, offer, shop_name: str, shop_url: str) -> Email:
    base = public_url()
    return _render("alert", f"Cena klesla: {watch.product.name} za {czk(offer.price)}", p=watch.product, w=watch,
                   o=offer, shop_name=shop_name, shop_url=shop_url,
                   unsub_url=f"{base}/hlidani/zrusit/{watch.token}",
                   product_url=f"{base}/produkt/{watch.product.id}-{watch.product.slug}")
