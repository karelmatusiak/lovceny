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
    # eHUB campaign link ID (a_bid); when set, affiliate_link wraps via eHUB click.php
    ehub_bid: str | None = None
    color: str = "#0B6E4F"
    extra: dict = field(default_factory=dict)
    # živé vyhledávání: funkce(query) -> list[SearchHit]; None = obchod živě nehledáme
    searcher: Callable | None = None
    # vlastní čtení ceny z detailu produktu (html -> ScrapeResult | None); None = JSON-LD/meta
    page_parser: Callable | None = None
    # obchod vrací 404 na URL bez koncového „/“ (normalize_url ho jinak odřízne)
    trailing_slash: bool = False
    # skrytý z /obchody a /stav (např. vestavné – runner ConnectTimeout)
    retired: bool = False

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
        """URL feedu z env; pokud chybí, použije se extra[feed_url_default] (veřejné Heureka feedy)."""
        if not self.feed_url_env:
            return ""
        env = os.environ.get(self.feed_url_env)
        if env is not None:
            return env.strip()
        return (self.extra.get("feed_url_default") or "").strip()

    @property
    def can_scrape(self) -> bool:
        return (not self.retired) and self.source == "scrape" and self.status == "ok"

    @property
    def affiliate_active(self) -> bool:
        """True, když affiliate_link opravdu obalí URL (param ID nebo eHUB a_bid + a_aid)."""
        if self.affiliate_param and self.affiliate_env and os.environ.get(self.affiliate_env):
            return True
        if self.ehub_bid:
            from .ehub import ehub_aid
            return bool(ehub_aid())
        return False

    def status_label(self) -> tuple[str, str]:
        """(css_mod, lidský stav) pro /obchody a /jak-radime. css_mod: ok | wait."""
        if self.source == "feed" and self.feed_url:
            return "ok", "oficiální feed"
        if self.ehub_bid and self.source == "feed":
            return "wait", "affiliate napojeno, feed URL chybí"
        if self.ehub_bid and self.status == "blocked":
            return "ok", "affiliate odkazy (feed připravujeme)"
        if self.status == "blocked" or (self.source == "feed" and not self.feed_url):
            return "wait", "připravujeme oficiální feed"
        return "ok", "funguje"

    def affiliate_link(self, url: str) -> str:
        """Affiliate obalení: Alza/Datart parametr, nebo eHUB click.php (a_bid na Shop).
        Bez nastaveného ID / a_aid vrací URL beze změny. Na pořadí ani verdikt nemá vliv."""
        aff_id = os.environ.get(self.affiliate_env, "") if self.affiliate_env else ""
        if self.affiliate_param and aff_id:
            p = urlparse(url)
            q = [(k, v) for k, v in parse_qsl(p.query) if k != self.affiliate_param]
            q.append((self.affiliate_param, aff_id))
            return urlunparse(p._replace(query=urlencode(q)))
        if self.ehub_bid:
            from .ehub import ehub_aid, ehub_click_url
            aid = ehub_aid()
            if aid:
                return ehub_click_url(self.ehub_bid, url, aid)
        return url


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
         note="Vyřazeno: GitHub Actions runnerům ConnectTimeout – katalog držíme na jiných obchodech.",
         searcher=lambda q: _search.search_vestavne(q), trailing_slash=True, retired=True, extra={"match_slug": True},
         search_note="Vyřazeno z katalogu (ConnectTimeout z runnerů)."),
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
    Shop("istores", "iStores", ["istores.cz"], source="scrape", status="blocked", color="#555555",
         note="Z ohleduplnosti nestahujeme (nejasný robots). Připravujeme oficiální / affiliate feed."),
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
         note="Přímé stahování nejde (Allegro/DataDome). Připravujeme oficiální / affiliate feed."),
    Shop("mall", "Mall", ["mall.cz"], source="scrape", status="blocked", color="#D93A3A",
         note="Přesměrováno na Allegro (DataDome). Připravujeme oficiální / affiliate feed."),
    Shop("smarty", "Smarty", ["smarty.cz"], source="scrape", status="blocked", color="#169B5B",
         note="Přímé stahování blokuje ochrana. Affiliate odkazy přes eHUB jsou napojené; oficiální XML feed ještě nemáme (žádost u affiliate manažera).",
         ehub_bid="1651aa06"),
    Shop("istyle", "iStyle", ["istyle.cz"], source="feed", status="ok", color="#000000",
         note="Ceny z oficiálního Heureka XML feedu; odkazy přes eHUB (affiliate).",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="ISTYLE_FEED_URL", ehub_bid="92ecd9a4",
         extra={"feed_url_default": "https://storage.feedyio.com/istyle-cz.myshopify.com/68b6d626f576022fbe107ccf.xml"}),
    Shop("sencor", "Sencor", ["sencor.cz"], source="feed", status="ok", color="#E30613",
         note="Ceny z oficiálního Heureka XML feedu; odkazy přes eHUB (affiliate).",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="SENCOR_FEED_URL", ehub_bid="67b5afc3",
         extra={"feed_url_default": "https://intranet.fastplus.sk/easyfeed/easyfeed/cz/heureka_RF4xDM0vDlB9/OUT/sencor_heureka.xml"}),
    Shop("electroworld", "Electroworld", ["electroworld.cz"], source="scrape", status="blocked", color="#E2001A",
         note="F5 ochrana jako Datart. Připravujeme oficiální / affiliate feed."),
    Shop("kasa", "Kasa", ["kasa.cz"], source="scrape", status="blocked", color="#C45C00",
         note="F5 ochrana. Připravujeme oficiální / affiliate feed."),
    Shop("tsbohemia", "TS Bohemia", ["tsbohemia.cz"], source="scrape", status="blocked", color="#333333",
         note="403 / robots. Připravujeme oficiální / affiliate feed."),
    Shop("conrad", "Conrad", ["conrad.cz"], source="scrape", status="blocked", color="#000000",
         note="Ochrana proti botům. Připravujeme oficiální / affiliate feed."),
    Shop("eberry", "eBerry", ["eberry.cz"], source="scrape", status="blocked", color="#8B1E3F",
         note="Ochrana proti botům. Připravujeme oficiální / affiliate feed."),
    Shop("bscom", "BScom", ["bscom.cz"], source="scrape", status="blocked", color="#1A5276",
         note="Ochrana proti botům. Připravujeme oficiální / affiliate feed."),
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
