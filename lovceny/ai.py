"""AI asistence vyhledávání přes Google Gemini – POUZE free tier.

Zásady:
- Bez klíče (GEMINI_API_KEY), po chybě, 429/kvótě nebo timeoutu (~4 s) vracíme None → běžné hledání bez AI.
- Cache odpovědí v paměti (1 h), denní strop volání (GEMINI_DAILY_CAP, default 900) a minutový strop
  (GEMINI_RPM_CAP, default 10), po 429 pauza GEMINI_COOLDOWN_S (default 15 min).
- Neposíláme osobní údaje: jen text dotazu (e-maily/telefony vymažeme) a názvy + ceny produktů.
- Klíč nikdy nelogujeme.
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger("lovceny.ai")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CACHE_TTL_S = 3600


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Intent:
    queries: list[str]
    max_price: float | None = None
    min_price: float | None = None
    must_words: list[str] = field(default_factory=list)
    exclude_words: list[str] = field(default_factory=list)
    source: str = "ai"  # ai | rules


@dataclass
class Ranking:
    keep: list[int]
    summary: str = ""


class GeminiClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._day = ""
        self.calls_today = 0
        self._minute: list[float] = []
        self._cooldown_until = 0.0
        self.last_error = ""

    # --- konfigurace ---
    @property
    def key(self) -> str:
        return os.environ.get("GEMINI_API_KEY", "").strip()

    @property
    def model(self) -> str:
        return os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    @property
    def timeout(self) -> float:
        try:
            return float(os.environ.get("GEMINI_TIMEOUT_S", "4"))
        except ValueError:
            return 4.0

    def enabled(self) -> bool:
        return bool(self.key) and os.environ.get("AI_SEARCH", "1") != "0"

    # --- stropy ---
    def _allow_call(self) -> bool:
        now = time.time()
        with self._lock:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if day != self._day:
                self._day, self.calls_today = day, 0
            if now < self._cooldown_until:
                self.last_error = "cooldown"
                return False
            if self.calls_today >= _env_int("GEMINI_DAILY_CAP", 900):
                self.last_error = "daily cap"
                return False
            self._minute = [t for t in self._minute if now - t < 60]
            if len(self._minute) >= _env_int("GEMINI_RPM_CAP", 10):
                self.last_error = "rpm cap"
                return False
            self.calls_today += 1
            self._minute.append(now)
            return True

    # --- volání ---
    def generate_json(self, kind: str, system: str, user: str, schema: dict):
        if not self.enabled():
            return None
        ck = (kind, self.model, user)
        hit = self._cache.get(ck)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        if not self._allow_call():
            return None
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                     "temperature": 0, "maxOutputTokens": 600}}
        try:
            r = httpx.post(API.format(model=self.model), headers={"x-goog-api-key": self.key}, json=body,
                           timeout=self.timeout)
        except httpx.HTTPError as e:
            self.last_error = type(e).__name__
            log.warning("Gemini %s: %s", kind, type(e).__name__)
            return None
        if r.status_code == 429:
            self._cooldown_until = time.time() + _env_int("GEMINI_COOLDOWN_S", 900)
            self.last_error = "429"
            log.warning("Gemini 429 (kvóta free tieru) – AI vypnuta na %s s", _env_int("GEMINI_COOLDOWN_S", 900))
            return None
        if r.status_code != 200:
            self.last_error = f"HTTP {r.status_code}"
            log.warning("Gemini %s: HTTP %s", kind, r.status_code)
            return None
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
        except (KeyError, IndexError, ValueError) as e:
            self.last_error = "bad response"
            log.warning("Gemini %s: nečitelná odpověď (%s)", kind, type(e).__name__)
            return None
        self._cache[ck] = (time.time(), data)
        self.last_error = ""
        return data

    def clear(self):
        self._cache.clear()
        self._minute.clear()
        self._cooldown_until = 0.0


client = GeminiClient()


# ---------- pravidla bez AI ----------
def scrub(q: str) -> str:
    """Odstraní z dotazu případné osobní údaje (e-mail, telefon) – AI posíláme jen to podstatné."""
    q = re.sub(r"\S+@\S+", " ", q)
    q = re.sub(r"(?:\+?\d[\s-]?){9,}", " ", q)
    return " ".join(q.split())[:200]


_NL_HINT = re.compile(r"\b(do|pod|nad|od|max|maximálně|levn\w*|tich\w*|na|pro|s|se|bez|nejlep\w*|dobr\w*|kvalitn\w*|"
                      r"malou|malý|velk\w*|úsporn\w*|tisíc\w*|kč|korun\w*)\b", re.I)


def should_use_ai(q: str) -> bool:
    """Šetříme kvótu: krátký prostý dotaz (značka, model) AI nepotřebuje."""
    toks = q.split()
    if len(toks) <= 1:
        return False
    if len(toks) <= 3 and not _NL_HINT.search(q) and not re.search(r"\d{3,}\s*(kč|,-)", q, re.I):
        # „samsung galaxy s24“, „sony wh1000xm5“ – název produktu, AI by jen pálila kvótu
        return False
    return True


def _num(s: str, mult: str | None) -> float:
    v = float(s.replace(" ", "").replace(",", "."))
    if mult and re.match(r"(tis|k\b)", mult, re.I):
        v *= 1000
    return v


def rule_intent(q: str) -> Intent:
    """Záloha bez AI: vytáhne „do 10 tisíc“, „pod 5000 Kč“, „od 2 000“ a zbytek použije jako dotaz."""
    max_p = min_p = None
    num = r"(\d[\d\s]*(?:[.,]\d+)?)\s*(tis\w*|k\b)?\s*(?:kč|korun\w*|,-)?"
    m = re.search(r"\b(?:do|pod|max(?:imálně)?)\s+" + num, q, re.I)
    if m:
        max_p = _num(m.group(1), m.group(2))
    m2 = re.search(r"\b(?:od|nad|min(?:imálně)?)\s+" + num, q, re.I)
    if m2:
        min_p = _num(m2.group(1), m2.group(2))
    rest = q
    for mm in (m, m2):
        if mm:
            rest = rest.replace(mm.group(0), " ")
    rest = " ".join(rest.split()) or q
    return Intent([rest], max_p, min_p, source="rules")


# ---------- AI úlohy ----------
INTENT_SYSTEM = (
    "Jsi asistent českého srovnávače cen elektroniky a domácích spotřebičů. Z dotazu uživatele vytvoř JSON pro "
    "vyhledávání v e-shopech. queries: 1–3 krátké hledací výrazy (1–3 slova, česky, jak by je napsal zákazník "
    "do vyhledávání e-shopu, bez cen a bez slov jako 'levný'); PRVNÍ výraz musí být obecný typ produktu (např. 'robotický vysavač', 'pračka'), další mohou být užší; max_price/min_price v Kč nebo null "
    "('do 10 tisíc' = 10000); must_words: slova, která musí být v názvu produktu (typ produktu v základním tvaru, "
    "jen pokud je jasný, jinak prázdné); exclude_words: slova, která v názvu být nesmí (např. příslušenství, "
    "'kryt', 'pouzdro'), jen když z dotazu plynou. Nevymýšlej konkrétní modely."
)
INTENT_SCHEMA = {"type": "OBJECT", "properties": {
    "queries": {"type": "ARRAY", "items": {"type": "STRING"}},
    "max_price": {"type": "NUMBER", "nullable": True},
    "min_price": {"type": "NUMBER", "nullable": True},
    "must_words": {"type": "ARRAY", "items": {"type": "STRING"}},
    "exclude_words": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["queries"]}

RANK_SYSTEM = (
    "Jsi asistent českého srovnávače cen. Dostaneš dotaz uživatele a očíslovaný seznam nabídek z e-shopů "
    "(název, cena v Kč, obchod). Vrať keep = čísla nabídek, které jsou skutečně hledaným produktem "
    "(ne příslušenství, náhradní díly, spotřební materiál, hračky, nábytek ani jiný typ zboží; respektuj cenové "
    "a další požadavky dotazu). Pořadí neřeš. summary = jedna krátká česká věta pro zákazníka: kolik relevantních "
    "nabídek jsme našli, nejlevnější cena a obchod. Bez doporučování konkrétních obchodů nad rámec ceny, "
    "bez vymýšlení údajů, které v seznamu nejsou."
)
RANK_SCHEMA = {"type": "OBJECT", "properties": {
    "keep": {"type": "ARRAY", "items": {"type": "INTEGER"}},
    "summary": {"type": "STRING"}}, "required": ["keep"]}


def understand(q: str) -> Intent | None:
    q = scrub(q)
    if not q or not should_use_ai(q):
        return None
    d = client.generate_json("intent", INTENT_SYSTEM, f"Dotaz: {q}", INTENT_SCHEMA)
    if not isinstance(d, dict):
        return None
    qs = [" ".join(str(x).split())[:60] for x in (d.get("queries") or []) if str(x).strip()][:3]
    if not qs:
        return None

    def price(v):
        try:
            v = float(v)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None

    words = lambda k: [str(w).strip().lower()[:30] for w in (d.get(k) or []) if str(w).strip()][:5]  # noqa: E731
    return Intent(qs, price(d.get("max_price")), price(d.get("min_price")), words("must_words"), words("exclude_words"))


def rank(q: str, hits: list, shop_name=lambda s: s, limit: int = 40) -> Ranking | None:
    """hits: SearchHit seřazené podle ceny. Vrací indexy relevantních (do `limit`) + shrnutí."""
    q = scrub(q)
    if not q or len(hits) < 2:
        return None
    sample = hits[:limit]
    lines = "\n".join(f"{i}. {h.name[:120]} | {round(h.price)} Kč | {shop_name(h.shop)}" for i, h in enumerate(sample))
    d = client.generate_json("rank", RANK_SYSTEM, f"Dotaz: {q}\nNabídky:\n{lines}", RANK_SCHEMA)
    if not isinstance(d, dict) or not isinstance(d.get("keep"), list):
        return None
    keep = sorted({int(i) for i in d["keep"] if isinstance(i, (int, float)) and 0 <= int(i) < len(sample)})
    summary = " ".join(str(d.get("summary") or "").split())[:300]
    return Ranking(keep, summary)
