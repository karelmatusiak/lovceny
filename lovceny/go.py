"""První hop na lovceny.cz pro tlačítka do obchodu.

Safari (prevence sledování) umí zastavit přímý přechod z lovceny.cz na známý
tracker, třeba ehub.cz/system/scripts/click.php, i když ten server vrací 302.
Odkaz proto vede na /go/<id nabídky>. Odpověď je 302 na affiliate URL
(Cache-Control: no-store). Klik musí udělat prohlížeč návštěvníka.

RESOLVE_AFFILIATE zůstává vypnuté. Stáhnout click.php na serveru by klik
zapsalo z IP Cloudflare, ne z prohlížeče, a konverzní cookie by se nemusela
uložit. To affiliate podmínky nechtějí a výchozí chování to nedělá.
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from .shops.feed import strip_junk_query

# Schválně konstanta, ne env. Zapnutí by znamenalo stáhnout affiliate URL
# na serveru. Kód níž to stejně nedělá – jen 302, ať se to omylem nezapne.
RESOLVE_AFFILIATE = False

GO_FUNCTION_JS = """\
/* Vygenerováno build_static.py. /go/<id> je první hop na lovceny.cz, pak 302.
   RESOLVE_AFFILIATE je vypnuté: affiliate URL (ehub click.php, Dognet, …)
   nestahujeme. Klik a cookie musí vzniknout v prohlížeči návštěvníka, ne z IP
   Cloudflare. Kdyby se hop nepodařil, statická stránka /go/<id>.html má
   přímý odkaz na produkt (desturl). */
const RESOLVE_AFFILIATE = false;

let mapPromise = null;

export async function onRequest(context) {
  const url = new URL(context.request.url);
  const match = url.pathname.match(/^\\/go\\/(\\d+)\\/?$/);
  if (!match) return context.env.ASSETS.fetch(context.request);
  const id = match[1];
  try {
    const row = await lookup(context, id);
    const target = row && safeHttp(row.u);
    if (target && RESOLVE_AFFILIATE === false) {
      return new Response(null, {
        status: 302,
        headers: {
          Location: target,
          "Cache-Control": "no-store",
          "X-Robots-Tag": "noindex, nofollow",
          "Referrer-Policy": "strict-origin-when-cross-origin",
        },
      });
    }
  } catch (err) {
    /* mapa chybí – níž záložní HTML s odkazem na produkt */
  }
  const asset = await fetchAsset(context, "/go/" + id);
  if (asset && asset.status !== 404) return asset;
  return new Response("Odkaz do obchodu už neplatí.", {
    status: 404,
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex, nofollow",
    },
  });
}

async function lookup(context, id) {
  if (!mapPromise) {
    const mapUrl = new URL("/data/go.json", context.request.url);
    mapPromise = context.env.ASSETS.fetch(new Request(mapUrl.toString(), { method: "GET" }))
      .then(function (res) { return res.ok ? res.json() : {}; })
      .catch(function () { return {}; });
  }
  const map = await mapPromise;
  return map[id] || null;
}

function safeHttp(value) {
  if (typeof value !== "string" || value.length < 8 || value.length > 8000) return "";
  if (/[\\s\\u0000]/.test(value)) return "";
  let parsed;
  try { parsed = new URL(value); } catch (err) { return ""; }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "";
  if (parsed.username || parsed.password) return "";
  return value;
}

async function fetchAsset(context, pathname) {
  const url = new URL(context.request.url);
  url.search = "";
  const candidates = [pathname + ".html", pathname, pathname + "/"];
  let last = null;
  for (const candidate of candidates) {
    url.pathname = candidate;
    last = await context.env.ASSETS.fetch(new Request(url.toString(), { method: "GET" }));
    if (last.status !== 404) return last;
  }
  return last;
}
"""

_NOINDEX = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


def go_path(offer_id: int) -> str:
    return f"/go/{int(offer_id)}"


def safe_http_url(url: str) -> str | None:
    """http(s) bez mezer, řídicích znaků a jména s heslem. Jinak None."""
    if not isinstance(url, str) or not url or len(url) > 8000:
        return None
    if any(ord(ch) < 32 or ch in " \t" for ch in url):
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return url


def targets_for(shop, url: str) -> tuple[str, str] | None:
    """(affiliate URL, čistá URL produktu). Bez platné http(s) adresy None.

    Affiliate URL je to, kam 302 vede. Čistá URL je záloha, když hop selže
    (u eHUB je to desturl, ne click.php).
    """
    plain = safe_http_url(strip_junk_query(url or ""))
    if plain is None:
        return None
    if shop is None:
        return plain, plain
    wrapped = safe_http_url(shop.affiliate_link(url or "")) or plain
    return wrapped, plain


def fallback_page(offer_id: int, affiliate: str, plain: str, *, sponsored: bool) -> str:
    """Statická stránka, když Pages Function neběží. noindex, odkaz na produkt."""
    aff = escape(affiliate, quote=True)
    dest = escape(plain, quote=True)
    rel = "nofollow sponsored noopener" if sponsored and affiliate != plain else "nofollow noopener"
    extra = ""
    if plain != affiliate:
        extra = (
            '<p>Když se stránka obchodu neotevře, otevři přímo '
            f'<a rel="nofollow noopener" href="{dest}">stránku produktu</a>.</p>\n'
        )
    return (
        "<!doctype html>\n"
        '<html lang="cs">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        f'<meta http-equiv="refresh" content="0;url={aff}">\n'
        "<title>Přesměrování do obchodu</title>\n"
        "</head>\n"
        "<body>\n"
        "<p>Otevíráme obchod…</p>\n"
        f'<p><a rel="{rel}" href="{aff}">Pokračovat do obchodu</a></p>\n'
        f"{extra}"
        "</body>\n"
        "</html>\n"
    )


def redirect_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }


def not_found_headers() -> dict[str, str]:
    return dict(_NOINDEX)


def _linked_offer(product, offer) -> bool:
    """Jen nabídky, na které veřejná stránka opravdu odkazuje."""
    from .exclusions import is_excluded_brand
    if getattr(product, "retired_at", None) is not None:
        return False
    if getattr(product, "catalog_hidden", False) or getattr(product, "needs_review", False):
        return False
    if is_excluded_brand(getattr(product, "name", "") or ""):
        return False
    if getattr(offer, "withheld", False) or not getattr(offer, "id", None) or not offer.url:
        return False
    return True


def write_go_assets(root: Path, products) -> dict:
    """data/go.json, funkce /go/[[path]] a záložní HTML pro každou veřejnou nabídku."""
    from .shops import get_shop

    records: dict[str, dict[str, str]] = {}
    pages = 0
    for product in products:
        for offer in getattr(product, "offers", ()) or ():
            if not _linked_offer(product, offer):
                continue
            shop = get_shop(getattr(offer, "shop", "") or "")
            pair = targets_for(shop, offer.url)
            if pair is None:
                continue
            affiliate, plain = pair
            key = str(int(offer.id))
            records[key] = {"u": affiliate, "f": plain}
            dest = root / "go" / f"{key}.html"
            dest.parent.mkdir(parents=True, exist_ok=True)
            sponsored = bool(shop is not None and shop.affiliate_active and affiliate != plain)
            dest.write_text(
                fallback_page(int(offer.id), affiliate, plain, sponsored=sponsored),
                encoding="utf-8",
            )
            pages += 1
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    payload = {key: records[key] for key in sorted(records, key=int)}
    (data / "go.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    fn = root / "functions" / "go" / "[[path]].js"
    fn.parent.mkdir(parents=True, exist_ok=True)
    fn.write_text(GO_FUNCTION_JS, encoding="utf-8")
    return {"offers": len(records), "pages": pages}


def merge_routes(root: Path, include: list[str]) -> None:
    path = root / "_routes.json"
    current: list[str] = []
    if path.exists():
        current = list(json.loads(path.read_text(encoding="utf-8")).get("include") or [])
    merged = list(current)
    for item in include:
        if item not in merged:
            merged.append(item)
    path.write_text(
        json.dumps({"version": 1, "include": merged, "exclude": []}, indent=2) + "\n",
        encoding="utf-8",
    )
