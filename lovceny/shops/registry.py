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

from .feed import ALLEGRO_TD_PAGE_CAP, ALLEGRO_TD_QUERIES, ALLEGRO_TD_TOTAL_CAP


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
    # Dognet channel code (chid). DOGNET_CHID v env přepíše; prázdné vypne obalení.
    dognet_chid: str | None = None
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
        if self.slug == "allegro":
            from .feed import clean_product_url
            u = clean_product_url(url)
        else:
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
        # Empty string (e.g. unset GitHub Actions secret) must not block feed_url_default.
        if env is not None and env.strip():
            return env.strip()
        return (self.extra.get("feed_url_default") or "").strip()

    def feed_sources(self) -> list[str]:
        """Primární URL a případná veřejná záloha. Env override zálohu nepoužije."""
        primary = self.feed_url
        if not primary:
            return []
        explicit = False
        if self.feed_url_env:
            env = os.environ.get(self.feed_url_env)
            explicit = bool(env and env.strip())
        out = [primary]
        if not explicit:
            fb = (self.extra.get("feed_url_fallback") or "").strip()
            if fb and fb not in out:
                out.append(fb)
        return out

    @property
    def can_scrape(self) -> bool:
        return (not self.retired) and self.source == "scrape" and self.status == "ok"

    @property
    def affiliate_active(self) -> bool:
        """True, když affiliate_link opravdu obalí URL (param ID, eHUB nebo Dognet)."""
        if self.affiliate_param and self.affiliate_env and os.environ.get(self.affiliate_env):
            return True
        if self.ehub_bid:
            from .ehub import ehub_aid
            return bool(ehub_aid())
        if self.dognet_chid:
            from .dognet import dognet_chid
            return bool(dognet_chid(self.dognet_chid))
        return False

    def status_label(self) -> tuple[str, str]:
        """(css_mod, lidský stav) pro /obchody a /jak-radime. css_mod: ok | wait."""
        networked = bool(self.ehub_bid or self.dognet_chid)
        if self.source == "feed" and self.feed_url:
            return "ok", "oficiální feed"
        if networked and self.source == "feed":
            return "wait", "affiliate napojeno, feed URL chybí"
        if networked and self.status == "blocked":
            return "ok", "affiliate odkazy (feed připravujeme)"
        if self.status == "blocked" or (self.source == "feed" and not self.feed_url):
            return "wait", "připravujeme oficiální feed"
        return "ok", "funguje"

    def affiliate_link(self, url: str) -> str:
        """Affiliate obalení: Alza/Datart parametr, eHUB click.php, nebo Dognet go.dognet.com.
        Bez nastaveného ID / a_aid / chid vrací URL beze změny. Na pořadí ani verdikt nemá vliv."""
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
        if self.dognet_chid:
            from .dognet import dognet_chid, dognet_click_url
            if dognet_chid(self.dognet_chid):
                return dognet_click_url(url, self.dognet_chid)
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
         note="Cena z meta tagů produktu, fotka z galerie detailu (data-original), robots.txt povoluje.",
         searcher=lambda q: _search.search_abcom(q), page_parser=lambda h: _search.parse_abcom_detail(h),
         trailing_slash=True,
         search_note="Živé vyhledávání /hledani?q= povoleno robots.txt."),
    Shop("4home", "4Home", ["4home.cz"], source="feed", status="ok", color="#E4007C",
         note="Ceny z XML feedu 4Home (Dognet); odkazy přes Dognet. Katalog srovnávače, ne reklama.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="FOURHOME_FEED_URL", dognet_chid="9CFqgvpQ", trailing_slash=True,
         extra={"feed_url_default": "https://www.4home.cz/export/google-products.xml",
                "dognet_campaign": "4857"}),
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
    # Dognet má i content kampaň De'Longhi. Její podmínky zakazují shopping CSS a srovnávač,
    # proto ji nenapojujeme. Zůstává jen šetrné čtení stránky produktu.
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
         note="Cena z microdata, fotka z og:image (CDN Upgates). robots.txt povoluje (zakazuje jen /search/suggest).",
         searcher=lambda q: _search.search_ellexgk(q), page_parser=lambda h: _search.parse_ellexgk_detail(h),
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
    # mall.cz / czc.cz zůstávají blocked scrape (DataDome). Marketplace feed je obchod allegro.
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
    Shop("xm", "XM.cz", ["xm.cz"], source="feed", status="ok", color="#1565C0",
         note="Ceny z oficiálního Heureka XML feedu; odkazy přes eHUB (affiliate).",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="XM_FEED_URL", ehub_bid="0ff74973",
         extra={"feed_url_default": "https://xm.cz/feed/25/fbae531dd043143e1e6f08e3025781c1f334cb9f"}),
    Shop("evolveo", "EVOLVEO", ["eshop.evolveo.cz", "evolveo.cz"], source="feed", status="ok", color="#5FB232",
         note="Ceny z oficiálního Heureka XML feedu; odkazy přes eHUB (affiliate).",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="EVOLVEO_FEED_URL", ehub_bid="f89ce7c7",
         extra={"feed_url_default": "https://feeds.mergado.com/evolveo-cz-heureka-cz-cz-ehub-affiliate-31bf20ac5bdad01b32f5bc782bcfb82e.xml"}),
    Shop("electronicstar", "Electronic-star", ["electronic-star.cz", "elestar.eu"], source="feed", status="ok",
         color="#E4572E",
         note="Ceny z Heureka XML feedu (Dognet); odkazy přes Dognet.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="ELECTRONICSTAR_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "http://feedy.elestar.eu/data/dognet_elstar_cz.xml",
                "feed_url_fallback": "https://feeds.mergado.com/electronic-star-cz-cz-e-star-dognet-f9f155c8cf2c386daff9ae418093234c.xml",
                "dognet_campaign": "3995"}),
    Shop("electrolux", "Electrolux", ["electrolux.cz"], source="feed", status="ok", color="#011E41",
         note="Ceny z XML feedu Electrolux (Dognet); odkazy přes Dognet.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="ELECTROLUX_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://www.feedoptimise.com/sy/feed/5125eac3547689c19286364534d04224.xml?campaign=CZ-Shopping-Channel&channel=Tradedoubler.com",
                "dognet_campaign": "529"}),
    Shop("dyson", "Dyson", ["dyson.cz"], source="feed", status="ok", color="#6E2C91",
         note="Ceny z XML feedu Dyson (Dognet); odkazy přes Dognet.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="DYSON_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://api.tradedoubler.com/1.0/products.xml;page=1;pageSize=100;fid=50990?token=0BF566CB031EDE04D2B0CE7B72A50F5B081B0944",
                "dognet_campaign": "429"}),
    Shop("allegro", "Allegro", ["allegro.cz"], source="feed", status="ok", color="#FF5A00",
         note="Ceny z feedu Dognet/TradeDoubler; odkazy jdou přes Dognet. Katalog srovnávače, ne reklama.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="ALLEGRO_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://api.tradedoubler.com/1.0/products.xml;page=1;pageSize=100;fid=257027?token=0BF566CB031EDE04D2B0CE7B72A50F5B081B0944",
                "dognet_campaign": "493",
                "td_queries": ALLEGRO_TD_QUERIES,
                "td_page_cap": ALLEGRO_TD_PAGE_CAP,
                "td_total_cap": ALLEGRO_TD_TOTAL_CAP}),
    Shop("pocitace24", "Počítače24", ["pocitace24.cz"], source="feed", status="ok", color="#D1214C",
         note="Ceny z XML feedu obchodu; odkazy jdou přes eHUB (affiliate).",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="POCITACE24_FEED_URL", ehub_bid="8f4cb4de",
         extra={"feed_url_default": "https://www.pocitace24.cz/universal.xml"}),
    Shop("jbl", "JBL", ["jbl.cz"], source="feed", status="ok", color="#FF4D00",
         note="Ceny z partnerského XML feedu JBL.cz; odkazy jdou přes eHUB (affiliate).",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="JBL_FEED_URL", ehub_bid="c4f80d1f",
         extra={"feed_url_default": "https://www.jbl.cz/gmerchant-JBL-cz.xml"}),
    Shop("tesla", "TESLA", ["tesla-electronics.eu"], source="feed", status="ok", color="#C8102E",
         note="Ceny z Google XML feedu TESLA (Dognet); odkazy přes Dognet. Katalog srovnávače, ne reklama.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="TESLA_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://feeds.mergado.com/tesla-electronics-eu-google-nakupy-cz-1-c9562002e37d4da3aff516b049a22d22.xml",
                "dognet_campaign": "1215"}),
    Shop("xupe", "Xupe", ["xupe.cz"], source="feed", status="ok", color="#146CDA",
         note="Ceny z XML feedu Xupe (Dognet); odkazy přes Dognet. Bereme jen listy, které sedí na naše kategorie.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="XUPE_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://www.xupe.cz/api/v3/feeds/1fb49b8d-eb53-45ac-9a42-4554131b03fb",
                "dognet_campaign": "5649"}),
    Shop("tera", "Tera", ["tera.cz"], source="feed", status="ok", color="#F58220",
         note="Ceny z Google XML feedu Tera (Dognet); odkazy přes Dognet. Katalog srovnávače, ne reklama.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="TERA_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://www.tera.cz/shopItemFeed.asp?File=GoCZ3003GCo.xml",
                "dognet_campaign": "2207"}),
    Shop("amazestore", "Amazestore", ["amazestore.cz"], source="feed", status="ok", color="#111111",
         note="Ceny z Google XML feedu Amazestore (Dognet); odkazy přes Dognet. Bereme jen sluchátka.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="AMAZESTORE_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://storage.feedyio.com/xaaetz-pv.myshopify.com/6a38f9b00be5bd1e82ea1c5f.xml",
                "dognet_campaign": "6425"}),
    Shop("misura", "Misura", ["misura.shop"], source="feed", status="ok", color="#6B4C9A",
         note="Ceny z Google XML feedu Misura (Dognet); odkazy přes Dognet. Feed nemá cesty kategorií, které umíme, takže se na web nedostane.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="MISURA_FEED_URL", dognet_chid="9CFqgvpQ",
         extra={"feed_url_default": "https://www.misura.shop/feed-google-merchant-rdrpg2bypf.xml",
                "dognet_campaign": "6271"}),
    Shop("dronpro", "DronPro", ["dronpro.cz"], source="feed", status="ok", color="#1B3A4B",
         note="Ceny z Heureka XML feedu DronPro; odkazy přes eHUB. Drony a příslušenství do katalogu nepatří.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="DRONPRO_FEED_URL", ehub_bid="5889edbd",
         extra={"feed_url_default": "https://feeds.mergado.com/dronpro-www5-superkoderi-cz-heureka-cz-f3a6350d4000723c182059807175d61f.xml",
                "ehub_campaign": "a309dd83"}),
    Shop("iphonemarket", "iPhoneMarket", ["iphonemarket.cz"], source="feed", status="ok", color="#222222",
         note="Ceny z Heureka XML feedu iPhoneMarket; odkazy přes eHUB. Jen telefony, ne kryty a skla.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="IPHONEMARKET_FEED_URL", ehub_bid="2bcd6f9d",
         extra={"feed_url_default": "https://www.iphonemarket.cz/heureka/export/products.xml?hash=jrc1IMxLqglvYjmK5UW6vy60",
                "ehub_campaign": "ff360e3f"}),
    Shop("promobily", "ProMobily", ["promobily.cz"], source="feed", status="ok", color="#E85D04",
         note="Ceny z Heureka XML feedu ProMobily (záloha Google); odkazy přes eHUB. Jen listy, které sedí na naše kategorie.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="PROMOBILY_FEED_URL", ehub_bid="9530f758",
         extra={"feed_url_default": "https://www.promobily.cz/deploy/img/fck/Image/Feeds/heureka_cz.xml",
                "feed_url_fallback": "https://feeds.mergado.com/promobily-cz-google-nakupy-cz-8f92c76d1f9409d7fa9354de57f61243.xml",
                "ehub_campaign": "711d01eb"}),
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
    Shop("bscom", "BScom", ["bscom.cz"], source="feed", status="ok", color="#1A5276",
         note="Ceny z XML feedu BScom; odkazy přes eHUB. Bereme jen listy notebooků, počítačů, sluchátek, vysavačů a hodinek.",
         search_note="Hledáme v importovaném feedu (DB).",
         feed_url_env="BSCOM_FEED_URL", ehub_bid="f4a2088a",
         extra={"feed_url_default": "https://www.bscom.cz/exchange/7813c634-8c47-47b7-8d5e-e01fbce7451b/xml/ehub.xml",
                "ehub_campaign": "b454f836"}),
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
