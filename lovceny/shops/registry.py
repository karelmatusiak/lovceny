"""Konfigurace obchodů.

source:
  scrape – cenu čteme přímo ze stránky produktu (JSON-LD / meta), jen pokud to robots.txt dovolí
  feed   – cenu bereme z oficiálního XML/CSV feedu (Heureka formát) nebo affiliate exportu
status (výsledek testu z 14. 10. 2026):
  ok       – funguje
  blocked  – obchod vrací ochranu proti botům (Cloudflare/F5/DataDome). NEOBCHÁZÍME.
"""
import os
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse


@dataclass
class Shop:
    slug: str
    name: str
    domains: list[str]
    source: str = "scrape"
    status: str = "ok"
    note: str = ""
    feed_url_env: str | None = None
    affiliate_param: str | None = None
    affiliate_env: str | None = None
    color: str = "#0B6E4F"
    extra: dict = field(default_factory=dict)
    # živé vyhledávání: funkce(query) -> list[SearchHit]; None = obchod živě nehledáme
    searcher: Callable | None = None
    # vlastní čtení ceny z detailu produktu (html -> ScrapeResult | None); None = JSON-LD/meta
    page_parser: Callable | None = None
    # obchod vrací 404 na URL bez koncového „/“ (normalize_url ho jinak odřízne)
    trailing_slash: bool = False

    def canonical_url(self, url: str) -> str:
        from ..util import normalize_url
        u = normalize_url(url)
        p = urlparse(u)
        if self.trailing_slash and p.path not in ("", "/") and not p.query and "." not in p.path.rsplit("/", 1)[-1]:
            u += "/"
        return u
    search_note: str = ""

    @property
    def feed_url(self) -> str:
        return os.environ.get(self.feed_url_env, "") if self.feed_url_env else ""

    @property
    def can_scrape(self) -> bool:
        return self.source == "scrape" and self.status == "ok"

    def affiliate_link(self, url: str) -> str:
        """Přidá affiliate parametr (např. Alza ?idp=XXXX). Bez nastaveného ID vrací URL beze změny.
        Na pořadí ani verdikt nemá vliv."""
        aff_id = os.environ.get(self.affiliate_env, "") if self.affiliate_env else ""
        if not (self.affiliate_param and aff_id):
            return url
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query) if k != self.affiliate_param]
        q.append((self.affiliate_param, aff_id))
        return urlunparse(p._replace(query=urlencode(q)))


SHOPS: dict[str, Shop] = {s.slug: s for s in [
    Shop("alza", "Alza", ["alza.cz"], source="feed", status="ok", color="#2B5BD7",
         note="Cloudflare blokuje přímé stahování – ceny z feedu / affiliate exportu.",
         search_note="Hledáme v importovaném feedu (DB).", feed_url_env="ALZA_FEED_URL", affiliate_param="idp", affiliate_env="ALZA_AFFILIATE_ID"),
    Shop("datart", "Datart", ["datart.cz"], source="feed", status="ok", color="#E2001A",
         note="F5 ochrana blokuje přímé stahování – ceny z feedu / affiliate exportu.",
         search_note="Hledáme v importovaném feedu (DB).", feed_url_env="DATART_FEED_URL", affiliate_param="utm_source", affiliate_env="DATART_AFFILIATE_ID"),
    Shop("planeo", "Planeo", ["planeo.cz"], source="scrape", status="ok", color="#FF7A45",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         search_note="Vyhledávání (/vyhledavani…) zakazuje robots.txt – živě nehledáme."),
    Shop("mironet", "Mironet", ["mironet.cz"], source="scrape", status="ok", color="#0067B1",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         searcher=lambda q: _search.search_mironet(q),
         search_note="Živé vyhledávání /ProductList/showSearch povoleno robots.txt."),
    Shop("softcom", "Softcom", ["softcom.cz"], source="scrape", status="ok", color="#E30613",
         note="Detail bez JSON-LD – cenu čteme z HTML (Cena s DPH), robots.txt povoluje.",
         searcher=lambda q: _search.search_softcom(q), page_parser=lambda h: _search.parse_softcom_detail(h),
         search_note="Živé vyhledávání /eshop/default.asp?cls=stoitems&fulltext= povoleno robots.txt."),
    Shop("svetpocitacu", "Svět počítačů", ["svetpocitacu.cz"], source="scrape", status="ok", color="#1B7F3B",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         searcher=lambda q: _search.search_svetpocitacu(q),
         search_note="Živé vyhledávání /hledani/?q= povoleno robots.txt."),
    Shop("abcom", "AB-COM", ["ab-com.cz"], source="scrape", status="ok", color="#075C89",
         note="Cena z meta tagů produktu, robots.txt povoluje.",
         searcher=lambda q: _search.search_abcom(q), trailing_slash=True,
         search_note="Živé vyhledávání /hledani?q= povoleno robots.txt."),
    Shop("4home", "4Home", ["4home.cz"], source="scrape", status="ok", color="#E4007C",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         searcher=lambda q: _search.search_4home(q), trailing_slash=True,
         search_note="Živé vyhledávání /hledat/?q= povoleno robots.txt (domácí spotřebiče, kuchyň)."),
    Shop("vestavne", "Vestavné spotřebiče", ["vestavne-spotrebice.cz"], source="scrape", status="ok", color="#8A5A00",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         searcher=lambda q: _search.search_vestavne(q), trailing_slash=True, extra={"match_slug": True},
         search_note="Živé vyhledávání /search/?search_value= povoleno robots.txt (vestavné spotřebiče)."),
    Shop("eta", "ETA", ["eta.cz"], source="scrape", status="ok", color="#D6001C",
         note="Detail bez JSON-LD – cenu čteme z HTML, robots.txt povoluje. Prodává jen značky ETA/Gorenje/Hyundai.",
         searcher=lambda q: _search.search_eta(q), page_parser=lambda h: _search.parse_eta_detail(h), trailing_slash=True,
         search_note="Živé vyhledávání /shop/vyhledavani/?q= povoleno robots.txt."),
    Shop("onlineshop", "Onlineshop", ["onlineshop.cz"], source="scrape", status="ok", color="#E85D04",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         searcher=lambda q: _search.search_onlineshop(q),
         search_note="Živé vyhledávání /vyhledavani/?q= povoleno robots.txt. Bereme běžnou cenu, ne cenu se slevovým kódem."),
    Shop("delonghi", "De'Longhi", ["delonghi.com"], source="scrape", status="ok", color="#1F3A5F",
         note="Oficiální e-shop značky (kávovary, kuchyň). JSON-LD schema.org Product, robots.txt povoluje.",
         searcher=lambda q: _search.search_delonghi(q), extra={"match_slug": True},
         search_note="Živé vyhledávání /cs-cz/search?q= povoleno robots.txt."),
    Shop("quikset", "Quikset", ["quikset.cz"], source="scrape", status="ok", color="#0A7CC1",
         note="Detail bez JSON-LD – cenu čteme z HTML (price__base), robots.txt povoluje.",
         searcher=lambda q: _search.search_quikset(q), page_parser=lambda h: _search.parse_quikset_detail(h),
         search_note="Živé vyhledávání /hledani?q= povoleno robots.txt (Apple a příslušenství)."),
    Shop("mobilyostrava", "Mobily Ostrava", ["mobilyostrava.cz"], source="scrape", status="ok", color="#C2185B",
         note="Cena z meta/HTML produktu, robots.txt povoluje (skupina * chybí).",
         searcher=lambda q: _search.search_mobilyostrava(q),
         search_note="Živé vyhledávání /cz/hledat/?search_nazev= povoleno robots.txt (malý obchod, hlavně příslušenství)."),
    Shop("istores", "iStores", ["istores.cz"], source="scrape", status="ok", color="#111111",
         note="Apple Premium Reseller. robots.txt zakazuje jen slovenské /vyhladavanie, české /vyhledavani povoluje.",
         searcher=lambda q: _search.search_istores(q),
         search_note="Živé vyhledávání /vyhledavani?q= (robots.txt ho nezakazuje)."),
    Shop("mobilprovas", "MobilProVás", ["mobilprovas.cz"], source="scrape", status="ok", color="#019CDE",
         note="Malý obchod a servis (Modřice). robots.txt neexistuje (404) = vše povoleno.",
         searcher=lambda q: _search.search_mobilprovas(q),
         search_note="Živé vyhledávání /search/?find= (robots.txt chybí)."),
    Shop("ellexgk", "Ellex GK (Sony Center)", ["ellexgk.cz"], source="scrape", status="ok", color="#003087",
         note="JSON-LD schema.org Product na stránce, robots.txt povoluje (zakazuje jen /search/suggest).",
         searcher=lambda q: _search.search_ellexgk(q),
         search_note="Živé vyhledávání /search?phrase= povoleno robots.txt (Sony, PlayStation)."),
    Shop("itex24", "ITex24", ["itex24.cz"], source="scrape", status="ok", color="#5B2C83",
         note="WooCommerce, JSON-LD schema.org Product na stránce, robots.txt povoluje.",
         searcher=lambda q: _search.search_itex24(q),
         search_note="Živé vyhledávání /?s=…&post_type=product povoleno robots.txt."),
    Shop("mobilfone", "Mobilfone", ["mobilfone.cz"], source="scrape", status="ok", color="#2E7D32",
         note="Detail bez JSON-LD – cenu čteme z HTML (#our_price_display), robots.txt povoluje.",
         page_parser=lambda h: _search.parse_mobilfone_detail(h),
         search_note="Vyhledávání (/search.php) zakazuje robots.txt – živě nehledáme, jen sledování podle URL produktu."),
    Shop("czc", "CZC", ["czc.cz"], source="scrape", status="blocked", color="#7A4BD1",
         note="czc.cz je jen rozcestník na obchod na Allegru, robots.txt zakazuje /hledat*; allegro.cz vrací DataDome (403)."),
    Shop("mall", "Mall", ["mall.cz"], source="scrape", status="blocked", color="#D93A3A",
         note="Přesměrováno na Allegro, 403 DataDome."),
    Shop("smarty", "Smarty", ["smarty.cz"], source="scrape", status="blocked", color="#169B5B",
         note="403 na homepage i produktech."),
    Shop("electroworld", "Electroworld", ["electroworld.cz"], source="scrape", status="blocked",
         note="F5 JS challenge (stejná skupina jako Datart)."),
    Shop("kasa", "Kasa", ["kasa.cz"], source="scrape", status="blocked", note="F5 JS challenge."),
    Shop("tsbohemia", "TS Bohemia", ["tsbohemia.cz"], source="scrape", status="blocked", note="403 (i robots.txt)."),
    Shop("conrad", "Conrad", ["conrad.cz"], source="scrape", status="blocked", note="403 ochrana proti botům (i robots.txt)."),
    Shop("eberry", "eBerry", ["eberry.cz"], source="scrape", status="blocked", note="403 ochrana proti botům (i robots.txt)."),
    Shop("bscom", "BScom", ["bscom.cz"], source="scrape", status="blocked", note="403 ochrana proti botům (i robots.txt)."),
]}


from . import search as _search  # noqa: E402


def get_shop(slug: str) -> Shop | None:
    return SHOPS.get(slug)


def shop_for_url(url: str) -> Shop | None:
    host = (urlparse(url).hostname or "").lower()
    for s in SHOPS.values():
        if any(host == d or host.endswith("." + d) for d in s.domains):
            return s
    return None
