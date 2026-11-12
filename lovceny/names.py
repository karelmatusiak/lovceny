"""Krátké zobrazovací názvy produktů (UI / SEO). Párování feedů dál běží na původním name."""
from __future__ import annotations

import html as htmlmod
import re
import unicodedata

_CATEGORY_FOLD = frozenset({
    "tv", "televize", "television", "notebook", "notebooky", "laptop",
    "sluchatka", "sluchatka s mikrofonem",
    "bezdratova sluchatka", "bezdratova sluchatka s mikrofonem",
    "pracka", "lednice", "mrazak", "kavovar", "automaticky kavovar",
    "vysavac", "roboticky vysavac",
    "herni konzole", "chytre hodinky", "hodinky",
    "fotoaparat", "digitalni fotoaparat", "sportovni kamera",
    "venkovni kamera", "kamera", "mobilni telefon", "telefon", "smart tv",
    "oled", "qled", "telo bez objektivu",
})

_SPEC_PART_RE = re.compile(
    r"(?i)^("
    r"(\d+\s*x\s*)?\d{3,5}\s*[×x]\s*\d{3,5}"
    r"|4k(\s*uhd|\s*ultra\s*hd)?"
    r"|uhd|full\s*hd|fhd|wuxga|wqhd|qhd|2\.?8k|ips"
    r"|hdmi|usb(\s*-?\s*c)?"
    r"|wi-?fi(\s*&\s*bt)?|bluetooth(\s*[\d.]+)?|bt\b|rj45|lan"
    r"|tizen|webos|android(\s*tv)?|wear\s*os|macos|windows|w11h?|ios[\d.]*"
    r"|dolby(\s*vision|\s*atmos)?"
    r"|quantum\s*hdr\+?"
    r"|apple\s*airplay"
    r"|ip\d{2}|ipx\d"
    r"|microsd|nfc|anc|gps(\+cellular)?"
    r"|ir\b|repro|mikrofon(\s*&\s*repro)?"
    r"|dotykov[yý]\s*lcd.*"
    r"|x-trans.*"
    r"|hi-?res(\s*audio)?"
    r"|stabilizace.*"
    r"|vysavani\s*a\s*mopovani|vysávání\s*a\s*mopování"
    r"|a[zž]\s+\d+\s*hodin"
    r"|vydrz.*"
    r"|softcase"
    r"|intel\s+(uhd|graphics|arc|iris).*"
    r"|amd\s+radeon.*"
    r"|apple\s+\d+-jádrov.*"
    r"|radeon\s+\w+"
    r"|bez\s*os"
    r")$"
)

_UNIT_LIST_RE = re.compile(
    r"(?i)^("
    r"\d+([.,]\d+)?\s*(hz|w|pa|db|bar|ot\.?/?(min)?|rpm|mah|l|litru|kg|cm|mm|fps|mpix|mp)"
    r"|\d+\s*mm\s*menice"
    r"|usb-?c\s*&\s*wi-?fi.*"
    r"|wi-?fi\s*&\s*bt"
    r"|\d{2,4}\s*[–-]\s*\d{2,4}\s*mm"  # délka řemínku
    r")$"
)

_AFTER_COMMA_SPEC = re.compile(
    r"(?i),\s*(?=(?:qled|oled|neo\s*qled|miniled|mini\s*led|4k|uhd|full\s*hd|"
    r"quantum|tizen|webos|android|hdmi|usb|wi-?fi|bluetooth|apple\s*airplay|"
    r"smart\s*tv|hdr)\b)"
)

# Barva / materiál – tokeny k rozlišení
_COLOR_RE = re.compile(
    r"(?i)\b("
    r"cern[aáeyý]|bil[aáeyý]|stribrn[aáeyý]|stříbrn[aáeyý]|sed[aáeyý]|šed[aáeyý]|"
    r"modr[aáeyý]|zelen[aáeyý]|cerven[aáeyý]|červen[aáeyý]|ruzov[aáeyý]|růžov[aáeyý]|"
    r"zlat[aáeyý]|oranzov[aáeyý]|oranžov[aáeyý]|fialov[aáeyý]|hned[aáeyý]|hněd[aáeyý]|"
    r"titanov\w*|black|white|silver|blue|green|pink|gold|midnight|starlight|"
    r"graphite|obsidian|natural|přírodn\w*|prirodn\w*|bridlic\w*|břidlic\w*|"
    r"vesmirn\w*|vesmírn\w*|korálov\w*|koralov\w*|cosmic\s*blue|space\s*black|"
    r"space\s*gray|space\s*grey"
    r")\b"
)

_RAM_SSD_RE = re.compile(
    r"(?i)^\s*(\d{1,2}\s*(?:gb|tb)\s*/\s*\d{2,4}\s*(?:gb|tb)(?:\s*ssd|\s*ufs)?"
    r"|\d{1,4}\s*(?:gb|tb)(?:\s*ssd|\s*ufs)?)\s*$"
)

_CPU_SPEC_RE = re.compile(
    r"(?i)\b("
    r"intel\s+(core\s+)?(ultra\s+)?\w+.*"
    r"|amd\s+ryzen.*"
    r"|athlon\s+\w+.*"
    r"|apple\s+m[1-5](\s+\d+[.,]\d+\s*ghz)?"
    r"|snapdragon.*"
    r"|\d+[.,]\d+\s*ghz"
    r"|\d+\s*jaderrov\w*"
    r")"
)

_SCREEN_RE = re.compile(r'(?i)^\s*\d{1,2}([.,]\d)?\s*(?:"|”|″|\'\')?\s*(fhd|wuxga|wqhd|qhd|2\.?8k|ips|oled|retina)?(\s+ips)?\s*$')


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _clean_ws(s: str) -> str:
    s = htmlmod.unescape(s or "")
    s = s.replace("\xa0", " ").replace("／", "/")
    s = re.sub(r"\s+", " ", s).strip()
    # „model , barva“ vznikne, když se mezi slovem a čárkou smaže kategorie.
    # Čárka před číslem („26,5“) se neroztrhne.
    s = re.sub(r"\s+([,;])", r"\1", s)
    s = re.sub(r"([,;])(?=[^\s\d])", r"\1 ", s)
    return s.strip(" \t,;/-")


def _is_category_part(part: str) -> bool:
    f = _fold(part).strip()
    if f in _CATEGORY_FOLD:
        return True
    if re.fullmatch(r"\d{2}\s*\"?\s*tv", f):
        return True
    if f.startswith("roboticky ") or f.startswith("robotický "):
        return True
    return False



_INLINE_CAT_RE = re.compile(
    r"(?i)\b("
    r"televize|television|notebooky?|laptop|sluchátka|sluchatka|"
    r"lednice|mrazák|mrazak|kávovar|kavovar|"
    r"vysavač|vysavac"
    r")\b"
)


def _strip_inline_category(s: str) -> str:
    """„LG televize 43QNED…“ → „LG 43QNED…“ (OLED/QLED necháme – panel)."""
    return _clean_ws(_INLINE_CAT_RE.sub(" ", s))


def _is_junk_part(part: str) -> bool:
    p = part.strip()
    if not p:
        return True
    if _is_category_part(p):
        return True
    if _SPEC_PART_RE.match(p) or _UNIT_LIST_RE.match(p):
        return True
    if _CPU_SPEC_RE.fullmatch(p) or (_CPU_SPEC_RE.search(p) and "gb" not in p.lower()):
        # „Intel Core Ultra 5 325“ / „Apple M3 4.0GHz“
        if not _RAM_SSD_RE.match(p):
            return True
    if re.search(r"(?i)\b\d+-jádrov", p):
        return True
    if p.count("/") >= 2 and len(p) > 40:
        return True
    return False


def _variant_bits_from_parts(parts: list[str]) -> list[str]:
    """Z dalších segmentů vytáhni RAM/SSD a případně čistou barvu."""
    bits: list[str] = []
    for part in parts:
        p = part.strip()
        if _RAM_SSD_RE.match(p):
            bits.append(re.sub(r"\s+", "", p.upper().replace("SSD", " SSD").replace("UFS", " UFS")).replace(" / ", "/")
                        if False else _norm_cap(p))
            continue
        if _is_junk_part(p) or _SCREEN_RE.match(p):
            continue
        # holá barva
        if _COLOR_RE.fullmatch(p.strip()) or (len(p.split()) <= 2 and _COLOR_RE.search(p) and len(p) < 24):
            if not any(_fold(p) in _fold(b) for b in bits):
                bits.append(_clean_ws(p))
    return bits


def _norm_cap(p: str) -> str:
    p = _clean_ws(p)
    p = re.sub(r"\s*/\s*", "/", p)
    p = re.sub(r"(?i)\s*(gb|tb)\b", lambda m: m.group(1).upper(), p)
    p = re.sub(r"(?i)\s*ssd\b", " SSD", p)
    p = re.sub(r"(?i)\s*ufs\b", " UFS", p)
    p = re.sub(r"\s+", " ", p).strip()
    return p


def _trim_watch(s: str) -> str:
    """Apple Watch: velikost + barva těla; bez popisu řemínku."""
    # titanové pouzdro s … tahem → titanové
    def _pouzdro_repl(m):
        return " přírodně titanové" if m.group(1) else " titanové"
    s2 = re.sub(
        r"(?i)\s+(přírodně\s+|prirodne\s+)?titanové\s+pouzdro\s+(s|se)\s+.+$",
        _pouzdro_repl,
        s,
    )
    def _telo_repl(m):
        # ponech „Přírodní/přírodně“ před „titanové tělo“
        return " titanové"
    s2 = re.sub(r"(?i)\s+titanové\s+tělo\s*[-–].+$", _telo_repl, s2)
    # „Přírodní titanové“ už v názvu – OK; „tělo“ bez řemínku
    s2 = re.sub(r"(?i)\s+titanové\s+tělo\s*$", " titanové", s2)
    s2 = re.sub(r"(?i)\s+hliníkové\s+pouzdro\s+(s|se)\s+.+$", " hliníkové", s2)
    # „přírodně titanové“ udrž
    s2 = re.sub(r"(?i)\s+(přírodně)\s*$", r" \1 titanové", s2)
    # barva + titanové už v názvu
    s2 = re.sub(r"(?i)\b(břidlicově šedé|zlaté|stříbrné|černé|bílé|růžové|modré)\s+titanové\b",
                lambda m: m.group(0), s2)
    # usekni „- S“ / „- M/L“ velikost řemínku na konci
    s2 = re.sub(r"\s*[-–]\s*(S|M|L|S/M|M/L)\s*$", "", s2, flags=re.I)
    s2 = re.sub(r"\s*\(\s*S\s*/\s*M\s*\)\s*$", "", s2, flags=re.I)
    s2 = re.sub(r"\s*\(\s*M\s*/\s*L\s*\)\s*$", "", s2, flags=re.I)
    return _clean_ws(s2)


def _trim_macbook_pro_comma(s: str) -> str | None:
    """MacBook Pro 16,2\", Apple M5 Pro …, 48GB, 1TB SSD – barva"""
    if not re.match(r"(?i)^macbook\s+pro\b", s):
        return None
    color = None
    mcol = re.search(r"[-–]\s*(.+)$", s)
    if mcol:
        color = _clean_ws(mcol.group(1))
        s = _clean_ws(s[: mcol.start()])
    # vytáhni size, chip, ram, ssd
    size = re.search(r'(?i)(\d{1,2}([.,]\d)?\s*(?:"|”|″|\'\')?)', s)
    chip = re.search(r"(?i)(Apple\s+M[1-5](?:\s+Pro|\s+Max|\s+Ultra)?)", s)
    ram = re.search(r"(?i)(\d{1,3}\s*GB)", s)
    ssd = re.search(r"(?i)(\d{1,3}\s*(?:GB|TB)\s*SSD|\d{1,3}\s*TB\b)", s)
    bits = ["MacBook Pro"]
    if size:
        bits.append(size.group(1).replace(" ", ""))
    if chip:
        bits.append(chip.group(1).replace("Apple ", ""))
    if ram:
        bits.append(ram.group(1).replace(" ", ""))
    if ssd:
        bits.append(re.sub(r"\s+", "", ssd.group(1).upper()).replace("SSD", " SSD").strip())
    if color:
        bits.append(color)
    return _clean_ws(" ".join(bits))


def _trim_first_segment(seg: str) -> str:
    s = _clean_ws(seg)
    mb = _trim_macbook_pro_comma(s)
    if mb:
        return mb

    if re.search(r"(?i)\b(watch|airpods)\b", s):
        return _trim_watch(s)

    # Lenovo IdeaPad Slim 3/15IRH10/i5-…/… slash-spec style
    if s.count("/") >= 4 and " / " not in s:
        # keep brand + line + first SKU-ish token + color at end
        bits = [b for b in s.split("/") if b.strip()]
        color = None
        if bits and _COLOR_RE.search(bits[-1]) and len(bits[-1]) < 40:
            color = re.search(r"(?i)(cosmic\s*blue|space\s*black|[^/]+)$", bits[-1])
            color = _clean_ws(bits[-1]) if not re.search(r"(?i)\b(os|intel|amd|gb)\b", bits[-1]) else None
            # last segment often „Intel int/bez OS/Cosmic Blue“ already split
        head = bits[0]
        sku = bits[1] if len(bits) > 1 and re.search(r"[A-Z0-9]{4,}", bits[1], re.I) else None
        caps = [b for b in bits if _RAM_SSD_RE.match(b) or re.fullmatch(r"(?i)\d{1,4}GB", b) or re.fullmatch(r"(?i)\d{1,4}GB/\d{2,4}GB", b)]
        screen = next((b for b in bits if re.match(r'(?i)\d{1,2}([.,]\d)?\s*"', b)), None)
        out_bits = [head]
        if sku:
            out_bits.append(sku)
        if screen:
            out_bits.append(screen.replace(" ", ""))
        out_bits.extend(_norm_cap(c) for c in caps[:2])
        # color from last path segment if present
        last = bits[-1]
        cm = re.search(r"(?i)(cosmic\s*blue|space\s*black|space\s*gr[ae]y|silver|black|blue|grey|gray|černá|stříbrná|šedá|modrá)$", last)
        if cm:
            out_bits.append(cm.group(1))
        return _clean_ws(" ".join(out_bits))

    m = _AFTER_COMMA_SPEC.search(s)
    if m and m.start() > 10:
        head, tail = s[: m.start()].rstrip(), s[m.start() + 1 :].strip()
        panel = re.search(r"(?i)\b(neo\s*qled|qled|oled|mini\s*led|miniled)\b", tail)
        if panel and not re.search(r"(?i)\b(qled|oled|mini\s*led)\b", head):
            label = panel.group(1)
            label = "Mini LED" if "mini" in label.lower() else label.upper() if label.lower() in {"qled", "oled"} else label
            head = f"{head} {label}"
        s = head

    # „43" TV QLED, …“ / „50" TV LED, 3840 x …“ → „43" QLED“ / „50" LED“
    s = re.sub(
        r'(?i)(\d{2,3}\s*(?:"|”|″|\'\'))\s*TV\s+(Neo\s*QLED|QLED|OLED|LED|Crystal\s*UHD)\b(?:\s*,.*)?$',
        r"\1 \2",
        s,
    )
    s = re.sub(r'(?i)(\d{2,3}\s*(?:"|”|″|\'\'))\s*TV\b', r"\1", s)
    s = re.sub(r"(?i)\bTV,\s*", "", s)
    # usekni zbylé „, 3840 x 2160, 50 Hz, …“
    s = re.sub(r"(?i),\s*\d{3,5}\s*[x×]\s*\d{3,5}\b.*$", "", s)
    s = re.sub(r"(?i),\s*\d+\s*Hz\b.*$", "", s)
    s = re.sub(r"(?i)\s*\+\s*softcase\b", "", s)
    return _clean_ws(s)



_KNOWN_BRANDS = frozenset({
    "Apple", "Samsung", "Xiaomi", "Huawei", "Honor", "Sony", "LG", "Bosch",
    "Siemens", "Electrolux", "Whirlpool", "Miele", "Gorenje", "Candy", "Beko",
    "AEG", "Philips", "Panasonic", "Canon", "Nikon", "Fujifilm", "Hisense",
    "TCL", "Sharp", "Lenovo", "ASUS", "Acer", "HP", "Dell", "MSI", "Razer",
    "Nintendo", "Microsoft", "Google", "OnePlus", "Oppo", "Realme", "Nothing",
    "Dyson", "Rowenta", "DeLonghi", "Nespresso", "Krups", "Tefal", "Braun",
    "KitchenAid", "Garmin", "Polar", "Fitbit", "Amazfit", "Epson", "Brother",
    "Logitech", "HyperX", "SteelSeries", "Corsair", "JBL", "Bose", "Sennheiser",
    "GoPro", "DJI", "Meta", "Sencor", "ETA", "Concept", "Banquet", "Remington",
    "Oral-B", "iRobot", "Roborock", "TrueLife", "Niceboy", "Lamax",
})
_BRAND_BY_FOLD = {b.casefold(): b for b in _KNOWN_BRANDS}


def guess_brand(name: str) -> str | None:
    """Guess brand from product name (first token / known list). For filters only."""
    if not name:
        return None
    tokens = name.strip().replace("\u00a0", " ").split()
    if not tokens:
        return None
    # Multi-word brands first (Oral-B, DeLonghi as one token already)
    t0 = tokens[0].strip(".,;:()[]")
    hit = _BRAND_BY_FOLD.get(t0.casefold())
    if hit:
        return hit
    # HP/LG etc. already covered; try stripping trademark marks
    t0 = t0.rstrip("™®")
    return _BRAND_BY_FOLD.get(t0.casefold())


def short_name(name: str, *, max_len: int = 78) -> str:
    """Brand + model + klíčová varianta; bez výčtu specifikací."""
    raw = _clean_ws(name)
    if not raw:
        return ""

    # už krátký a bez „ / “ spec seznamu
    if " / " not in raw and len(raw) <= max_len and not _AFTER_COMMA_SPEC.search(raw):
        # pořád zkrátit Watch / MacBook Pro comma
        if re.search(r"(?i)\bwatch\b", raw) or re.match(r"(?i)^macbook\s+pro\b", raw):
            return _strip_inline_category(_trim_first_segment(raw))[:max_len]
        return _strip_inline_category(raw)[:max_len]

    parts = [p.strip() for p in re.split(r"\s+/+\s*", raw) if p.strip()]
    head = _trim_first_segment(parts[0] if parts else raw)
    extras = _variant_bits_from_parts(parts[1:]) if len(parts) > 1 else []

    # Notebooky: doplň úhlopříčku z parts pokud chybí v head
    if not re.search(r'(?i)\d{1,2}([.,]\d)?\s*(?:"|”|″)', head):
        for p in parts[1:]:
            sm = re.match(r'(?i)^\s*(\d{1,2}([.,]\d)?\s*(?:"|”|″|\'\'|palc\w*))', p)
            if sm and not _is_category_part(p):
                inch = sm.group(1).replace(" ", "")
                if inch not in head:
                    head = f"{head} {inch}"
                break

    # Kapacity: pokud head nemá GB a extras ano
    cap_count = len(re.findall(r"(?i)\b\d+\s*(gb|tb)\b", head))
    out_bits = [head]
    for e in extras:
        ef = _fold(e)
        if ef in _fold(" ".join(out_bits)):
            continue
        if _RAM_SSD_RE.match(e):
            if cap_count >= 2:
                continue
            out_bits.append(e)
            cap_count += 1
        else:
            out_bits.append(e)
        if len(out_bits) >= 4:
            break

    out = _clean_ws(" ".join(out_bits))
    out = _strip_inline_category(re.sub(r"\s{2,}", " ", out).strip(" ,/;"))
    if len(out) > max_len:
        cut = out[: max_len - 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        out = cut.rstrip(" ,/;") + "…"
    return out or raw[:max_len]


def _distinguishing_token(full: str, base: str) -> str | None:
    """Najdi smysluplný token z originálu, který v short chybí (barva, kapacita, tah…)."""
    base_fold = _fold(base)
    # typ tahu / band (Watch) – často jediný rozdíl mezi SKU
    m = re.search(
        r"(?i)\b(?:světle\s+|tmavě\s+)?"
        r"(modr\w*|cern\w*|čern\w*|bil\w*|bíl\w*)?\s*"
        r"(milánsk\w*|milansk\w*|trailov\w*|alpsk\w*|ocean\w*|sportovní\s*loop|sport\s*loop)\b",
        full,
    )
    if m:
        style = m.group(2)
        # zkrácené tvary: milánský / alpský / trailový
        style = re.sub(r"(?i)(milánsk|milansk)\w*", "milánský", style)
        style = re.sub(r"(?i)(alpsk)\w*", "alpský", style)
        style = re.sub(r"(?i)(trailov)\w*", "trailový", style)
        color = (m.group(1) or "").strip()
        if color:
            color = re.sub(r"(?i)modr\w*", "modrý", color)
            color = re.sub(r"(?i)čern\w*|cern\w*", "černý", color)
            tok = f"{color} {style}"
        else:
            tok = style
        if _fold(tok) not in base_fold and _fold(style) not in base_fold:
            return _clean_ws(tok)
    # velikost řemínku
    m = re.search(r"(?i)(?:[-–]\s*|\()\s*(S/M|M/L|S|M|L)\s*(?:\)|$)", full)
    if m and _fold(m.group(1)) not in base_fold:
        return m.group(1)
    # barva (včetně příslovce)
    for m in _COLOR_RE.finditer(full):
        tok = m.group(0)
        if _fold(tok) not in base_fold:
            start = m.start()
            prev = full[max(0, start - 18) : start]
            pm = re.search(
                r"(?i)(břidlicově|bridlicove|přírodně|prirodne|světle|tmavě|vesmírně|vesmirne)\s*$",
                prev,
            )
            if pm:
                tok = pm.group(1) + " " + tok
            return _clean_ws(tok)
    # kapacita
    for m in re.finditer(r"(?i)\b\d{1,4}\s*(?:gb|tb)(?:\s*ssd)?\b", full):
        if _fold(m.group(0)) not in base_fold:
            return _norm_cap(m.group(0))
    # modelový / SKU token (ne čisté číslo, ne mm rozsah)
    for tok in re.findall(r"[A-Za-zÁ-ž][A-Za-zÁ-ž0-9-]{2,}", full):
        if _fold(tok) not in base_fold:
            return tok
    return None


def _fit(base: str, extra: str, max_len: int) -> str:
    cand = _clean_ws(f"{base} {extra}")
    if len(cand) <= max_len:
        return cand
    room = max_len - len(extra) - 1
    if room < 12:
        return (base[: max_len - 1].rsplit(" ", 1)[0] + "…")[:max_len]
    trimmed = base[:room].rsplit(" ", 1)[0].rstrip(" ,;·")
    return _clean_ws(f"{trimmed} {extra}")


def uniquify_short_names(full_names: list[str], *, max_len: int = 78) -> list[str]:
    """Stejný short_name → doplň rozlišující token z originálu."""
    shorts = [short_name(n, max_len=max_len) for n in full_names]
    # až 2 kola – po doplnění tokenů můžou zůstat shody (S/M vs M/L)
    for _ in range(2):
        by: dict[str, list[int]] = {}
        for i, s in enumerate(shorts):
            by.setdefault(s.casefold(), []).append(i)
        changed = False
        for idxs in by.values():
            if len(idxs) < 2:
                continue
            for i in idxs:
                extra = _distinguishing_token(full_names[i], shorts[i])
                if not extra:
                    continue
                new = _fit(shorts[i], extra, max_len)
                if new != shorts[i]:
                    shorts[i] = new
                    changed = True
            # dořeš zbývající shody indexem velikosti řemínku / pořadím
            seen: dict[str, int] = {}
            for i in idxs:
                k = shorts[i].casefold()
                if k not in seen:
                    seen[k] = i
                    continue
                extra = _distinguishing_token(full_names[i], shorts[i]) or f"#{i + 1}"
                shorts[i] = _fit(shorts[i], extra, max_len)
                changed = True
        if not changed:
            break
    return shorts


def display_name(name: str) -> str:
    return short_name(name)


# Slova, která sama o sobě nejsou značka ani model. Neodstraňují se, jen se
# přes ně přeskakuje, když se hledá, čím název začít.
_CARD_STOP = frozenset({
    "a", "i", "k", "s", "u", "v", "ve", "na", "do", "od", "po", "pri", "za", "ze", "se", "si",
    "pres", "pri", "the", "and", "with", "for",
    "sluchatka", "sluchatko", "headphone", "headphones", "earbud", "earbuds", "headset",
    "televize", "televizor", "television", "notebook", "notebooky", "laptop",
    "pracka", "kavovar", "vysavac", "smartphone", "telefon", "mobil",
    "bezdratova", "bezdratove", "bezdratovy", "bezdratovych", "dratova", "dratove", "dratovy",
    "bluetooth", "bt", "anc", "enc", "tws", "ows", "usb", "usb-c", "usbc", "wifi", "wi-fi", "nfc", "gps", "hifi", "hi-fi",
    "stereo", "mikrofon", "mikrofonem", "dokanalova", "dokanalovy", "dokanalove", "dokanalovych",
    "otevrena", "otevrene", "otevrenym", "sportovni", "herni", "profesionalni", "studiova", "studiove",
    "nahlavni", "panske", "damske", "chytre", "hodinky", "naramek", "bezdratovym",
    "usi", "ousko", "ouskem", "hlavovy", "most", "nastavitelnym", "kostni", "vedeni",
    "behani", "beh", "hovory", "volanim", "volani", "prekladac", "preklad", "jazykovy",
    "novy", "nova", "nove", "model", "novinka", "akce", "sleva", "hit", "top", "bestseller",
    "skladem", "darek", "zdarma", "super",     "ultra", "plus",
    "printed", "logo", "logos", "metal", "barva", "smart", "new", "lehka", "lehke",
    "vlajkova", "lod", "kvalitni", "vynikajici", "orig", "org",
    "pancerovy", "pancerove", "vojensky", "maly", "sikove", "pomucky", "tajna",
    "dokovaci", "stanice", "pouzdro", "pouzdrem", "aktivnim", "potlacenim", "hluku",
    "bezdratovych",
})

# Značky, které se jen poznají ve zdrojovém textu. Do názvu se nikdy nedoplňují.
_EXTRA_BRANDS = (
    "Baseus", "Guess", "Savio", "JLab", "Belkin", "Blitzwolf", "Awei", "Mcdodo", "Modecom",
    "Cubot", "QCY", "Sencor", "Klarstein", "Kärcher", "Dunu", "Sivga", "Maono", "Feegar",
    "WiWU", "DKNY", "BMW", "Poly", "Creative", "Poco", "Lelit", "Pykson", "Rampage",
    "Occamo", "Haylou", "TechniSat", "Marshall", "Elonix", "Edifier", "Plantronics", "Fifine",
)
_CARD_BRAND_FOLDS = frozenset(_fold(b) for b in _KNOWN_BRANDS) | frozenset(_fold(b) for b in _EXTRA_BRANDS)
# Kanonický zápis pro schema.org. Známý seznam přepíše extras (Sencor, JBL, HP…).
_BRAND_CANON = {_fold(b): b for b in _EXTRA_BRANDS}
_BRAND_CANON.update({_fold(b): b for b in _KNOWN_BRANDS})


_CARD_CATEGORY = frozenset({
    "sluchatka", "sluchatko", "headphone", "headphones", "earbud", "earbuds", "headset",
    "televize", "televizor", "television", "notebook", "notebooky", "laptop",
    "pracka", "kavovar", "vysavac", "smartphone", "telefon", "mobil",
})

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_CAPACITY_RE = re.compile(r"(?i)^\d+([.,]\d+)?\s*(gb|tb|mm|cm|mah|hz|w)$")
_VERSION_RE = re.compile(r"(?i)^\d+[.,]\d+$")
_MODEL_CODE_RE = re.compile(
    r"(?i)^(?!\d+(gb|tb|mm|cm|mah|hz|w)$)(?!\d+[.,]\d+$)(?!ipx?\d+$)"
    r"(?=[a-z0-9-]*\d)(?=[a-z0-9-]*[a-z])[a-z0-9][a-z0-9-]{1,}$"
)


def _token_fold(token: str) -> str:
    return _fold(token).strip(".,;:()[]{}+\"'`")


def _is_card_stop(token: str) -> bool:
    return _token_fold(token) in _CARD_STOP


def _is_card_brand(token: str) -> bool:
    return _token_fold(token) in _CARD_BRAND_FOLDS


def _is_model_code(token: str) -> bool:
    raw = token.strip(".,;:()[]").replace(" ", "")
    if not raw or "/" in raw or _YEAR_RE.match(raw) or _CAPACITY_RE.match(raw) or _VERSION_RE.match(raw):
        return False
    if re.fullmatch(r"(?i)ipx?\d+[a-z]?", raw) or re.fullmatch(r"(?i)\d+[hv]", raw) or re.fullmatch(r"(?i)\d+g", raw):
        return False
    return _MODEL_CODE_RE.match(raw) is not None


def _buried_sku(token: str) -> bool:
    """Kód, který smí předběhnout výplň, jen když v názvu není značka."""
    raw = token.strip(".,;:()[]")
    if not _is_model_code(raw):
        return False
    if "-" in raw:
        return True
    return re.fullmatch(r"(?i)[a-z]{1,4}\d{2,}[a-z]{0,3}", raw) is not None


def _is_color_token(token: str) -> bool:
    raw = token.strip(".,;:()[]")
    if not raw:
        return False
    match = _COLOR_RE.search(raw)
    return match is not None and match.group(0) == raw


def _is_hour_or_spec(token: str) -> bool:
    raw = token.strip(".,;:()[]").replace(" ", "")
    if not raw:
        return False
    return bool(
        _YEAR_RE.match(raw)
        or _VERSION_RE.match(raw)
        or _CAPACITY_RE.match(raw)
        or re.fullmatch(r"(?i)\d+h", raw)
        or re.fullmatch(r"(?i)\d+g", raw)
        or re.fullmatch(r"(?i)ipx?\d+", raw)
    )


_COMPAT_PREV = frozenset({"pro", "k", "ke", "for", "compatible", "kompatibilni"})
_CARD_CONDITION = frozenset({
    "originalni", "original", "kompatibilni", "kompatibilne", "kompatibilita",
    "repasovany", "repasovane", "repasovana", "bazar", "bazarovy", "bazarova",
    "pouzity", "pouzita", "pouzite", "zanovni", "rozbaleny", "rozbalene", "rozbalena",
    "refurbished", "used",
})


def _split_card_tokens(text: str) -> list[str]:
    out = []
    for part in text.split():
        tok = part.strip()
        if not tok:
            continue
        if tok in {",", ";", ".", ":"}:
            if out and not out[-1].endswith(tok):
                out[-1] += tok
            continue
        if tok.strip(".,;:–-—/|()[]") == "":
            continue
        out.append(tok)
    return out


# Jen celý token, který opravdu je tahle zkratka nebo značka. Ne „Ancillary“, ne „JblTune“.
_POLISH_TOKEN = {
    "anc": "ANC",
    "enc": "ENC",
    "jbl": "JBL",
    "bluetooth": "Bluetooth",
    "usb-c": "USB-C",
    "usbc": "USB-C",
}


def _polish_token(token: str) -> str:
    """Sjednotí zápis známé zkratky. Ostatní slova, včetně češtiny, nechá jak přišla."""
    match = re.match(r"^([(\[\"']*)(.*?)([)\]\"',.;:]*)$", token)
    if not match or not match.group(2):
        return token
    pre, core, post = match.groups()
    key = _fold(core).replace("–", "-").replace("—", "-").replace(" ", "")
    shown = _POLISH_TOKEN.get(key)
    if shown is None:
        return token
    return pre + shown + post


def _slash_dup(token: str) -> str:
    """„černá/černá“ je totéž slovo dvakrát, ne dvě varianty."""
    if "/" not in token:
        return token
    bits = [b for b in token.split("/") if b]
    if len(bits) >= 2 and all(_token_fold(b) == _token_fold(bits[0]) for b in bits):
        return bits[0]
    return token


def _blocks_brand(token: str) -> bool:
    """Delší slovo před značkou obvykle znamená, že značka v názvu není výrobce."""
    fold = _token_fold(token)
    if not fold or _is_card_stop(token) or _is_color_token(token) or _is_hour_or_spec(token):
        return False
    if fold in _CARD_CONDITION or fold in _CARD_CATEGORY:
        return False
    return len(fold) >= 4


def _brand_index(tokens: list[str]) -> int | None:
    """První značka výrobce. „pro/k iPhone“ ani značka až za jiným názvem se nebere."""
    for i, tok in enumerate(tokens):
        if not _is_card_brand(tok):
            continue
        prev = _token_fold(tokens[i - 1]) if i else ""
        if prev in _COMPAT_PREV:
            continue
        if any(_blocks_brand(p) for p in tokens[:i]):
            continue
        return i
    return None


def title_brand(name: str) -> str | None:
    """Značka, kterou karty opravdu dají na začátek názvu. Neznámou nehádáme.

    Stejná pravidla jako ``card_label``: známá značka, ne „pro/k Sony“, ne značka
    až za cizím slovem. MacBook bez Apple, první obecné slovo ani obchod se nedomýšlí.
    """
    raw = _clean_ws(name)
    if not raw:
        return None
    prose = short_name(raw)
    seen: set[str] = set()
    for source in (prose, raw):
        if source in seen:
            continue
        seen.add(source)
        tokens = [_slash_dup(t) for t in _split_card_tokens(source)]
        idx = _brand_index(tokens)
        if idx is None:
            continue
        return _BRAND_CANON.get(_token_fold(tokens[idx]))
    return None


def _buried_model_index(tokens: list[str]) -> int | None:
    """Kód modelu schovaný za obecnými slovy, jen když v názvu není značka."""
    if _brand_index(tokens) is not None:
        return None
    for i, tok in enumerate(tokens):
        if i == 0 or not _buried_sku(tok):
            continue
        prev = tokens[i - 1]
        if not _is_card_stop(prev) and not _is_color_token(prev):
            continue
        return i
    return None


def _take_run(tokens: list[str], start: int) -> list[int]:
    """Značka a hned následující slova modelu, bez barvy, verze a výplně."""
    idxs = [start]
    k = start + 1
    while k < len(tokens) and len(idxs) < 4:
        tok = tokens[k]
        if _is_card_stop(tok) or _is_color_token(tok) or _is_hour_or_spec(tok):
            break
        idxs.append(k)
        k += 1
    return idxs


def _drop_category(tokens: list[str]) -> list[str]:
    out = []
    i = 0
    while i < len(tokens):
        fold = _token_fold(tokens[i])
        nxt = _token_fold(tokens[i + 1]) if i + 1 < len(tokens) else ""
        if fold == "novy" and nxt == "model":
            i += 2
            continue
        if fold == "chytre" and nxt == "hodinky":
            i += 2
            continue
        if fold in _CARD_CATEGORY:
            i += 1
            continue
        out.append(_slash_dup(tokens[i]))
        i += 1
    return out


def _join_card(tokens: list[str]) -> str:
    text = " ".join(_polish_token(t) for t in tokens if t)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return _clean_ws(text)


# Obchodní výplň z feedu. Samostatná slova, ne části značek (Ancillary, DualSense).
_FEED_ALWAYS = (
    r"nov[éeý]\s+by\s+harman",
    r"by\s+harman",
    r"smartphone",
    r"televizor",
    r"televize",
)
# Až když v názvu je model. Forma sluchátek jen u sluchátek, ať „drátová myš“ zůstane.
_FEED_WITH_MODEL = (
    r"dual\s+sim",
    r"nfc",
    r"origin[aá]ln[íi]",
    r"original",
    r"nov[éeý]",
)
_FEED_HEADPHONES = (
    r"origin[aá]ln[íi]\s+(?:bezdr[aá]tov\w+\s+|dr[aá]tov\w+\s+|bluetooth\s+|in[\s-]?ear\s+)*sluch[aá]tk\w+",
    r"(?:bezdr[aá]tov\w+|dr[aá]tov\w+|bluetooth|in[\s-]?ear)\s+sluch[aá]tk\w+",
    r"na\s+u[sš]i\s+s\s+mikrofonem",
    r"s\s+mikrofonem",
    r"na\s+u[sš]i",
    r"do\s+u[sš][ií]",
    r"in[\s-]?ear",
    r"dr[aá]tov[áéý]",
    r"bezdr[aá]tov[áéý]",
)
_SPEC_TOKEN = re.compile(
    r"(?i)^(?:\d+\s*cd(?:\s*/\s*m(?:i|2|²))?|\d+cd(?:/(?:mi|m2|m²))?|24/7|so|mi)$"
)


def _normalize_inch_quotes(text: str) -> str:
    """`55\"`, `55''` a `55″` jsou totéž co 55\"."""
    s = text.replace('\\"', '"').replace("\\'", "'")
    s = re.sub(r"(\d{1,3})\s*'{2,}", r'\1"', s)
    s = re.sub(r"(\d{1,3})\s*[″”„]", r'\1"', s)
    s = re.sub(r'(\d{1,3})\s*\\+"', r'\1"', s)
    return s


def _has_model_token(text: str) -> bool:
    for tok in _split_card_tokens(text):
        raw = tok.strip(".,;:()[]\"'")
        if not raw or raw in {"/", "-"}:
            continue
        if _is_model_code(raw):
            return True
        if re.fullmatch(r"\d{2,4}", raw):
            return True
        if re.search(r"(?i)\d+\s*/\s*\d+\s*(gb|tb)|\d+\s*(gb|tb)", raw):
            return True
    return False


def _headphone_feed(text: str) -> bool:
    folded = _fold(text)
    return any(bit in folded for bit in ("sluch", "headphone", "earbud", "in-ear", "in ear", "tune"))


def _spec_dump(text: str) -> bool:
    return bool(re.search(r"(?i)\d+\s*cd|24\s*/\s*7", text) or "''" in text or '\\"' in text)


def _drop_echo_tokens(tokens: list[str]) -> list[str]:
    """Druhá stejná značka a `T290`, když už v názvu je `290`."""
    drop: set[int] = set()
    folds = [_token_fold(t) for t in tokens]
    seen_brand = False
    for i, tok in enumerate(tokens):
        if _is_card_brand(tok):
            if seen_brand:
                drop.add(i)
            else:
                seen_brand = True
        echo = re.fullmatch(r"t(\d{2,4})", folds[i])
        if echo and echo.group(1) in folds:
            drop.add(i)
    if not drop:
        return tokens
    return [tok for i, tok in enumerate(tokens) if i not in drop]


def polish_feed_display(text: str) -> str:
    """Sundá z už zkráceného názvu surový feed: předponu obchodu, „by Harman“, Dual SIM, uvozovky u úhlopříčky.

    Nemění uložený název. Bez modelu (samotná „sluchátka na uši“) výplň nechá, ať se produkty pořád liší.
    """
    original = text or ""
    s = _normalize_inch_quotes(_clean_ws(original))
    if not s:
        return ""
    for pat in _FEED_ALWAYS:
        s = re.sub(rf"(?i)\b(?:{pat})\b", " ", s)
    if _has_model_token(s):
        patterns = _FEED_WITH_MODEL
        if _headphone_feed(original) or _headphone_feed(s):
            patterns = _FEED_HEADPHONES + patterns
        for pat in patterns:
            s = re.sub(rf"(?i)\b(?:{pat})\b", " ", s)
    s = re.sub(r'(?i)(\d{1,3}\s*")\s+LED\b', r"\1", s)
    if _spec_dump(original):
        s = re.sub(r",+", " ", s)
        kept = []
        for tok in _split_card_tokens(s):
            core = tok.strip(".,;:")
            if _SPEC_TOKEN.match(core.replace(" ", "")) or _SPEC_TOKEN.match(core):
                continue
            if re.fullmatch(r"(?i)led", core) and re.search(r'\d{1,3}\s*"', s):
                continue
            kept.append(tok.strip(","))
        s = " ".join(kept)
    tokens = _drop_echo_tokens(_split_card_tokens(_clean_ws(s)))
    return _clean_ws(" ".join(tokens))


def _card_label_core(name: str) -> str:
    """Viditelný název na kartě. Původní `name` se nemění.

    Značka a model, pokud ve zdroji opravdu jsou, jdou na začátek. Pryč jdou jen
    samostatná kategorie („sluchátka“) a opakovaný stejný token („černá/černá“)
    a spojení „nový model“. Originální/kompatibilní, barva, kapacita a generace
    zůstávají. Celé tokeny ANC, ENC, USB-C, Bluetooth a JBL se sjednotí, ostatní
    slova se nepřepisují a značka se nedomýšlí. Bez spolehlivé identity se vrátí
    dosavadní zkrácení.
    """
    raw = _clean_ws(name)
    if not raw:
        return ""
    # Nejdřív existující zkrácení specifikací (lomítka, televize, MacBook).
    # Přeuspořádání nesmí vrátit výčet, který short_name už sundal.
    prose = short_name(raw)
    tokens = [_slash_dup(t) for t in _split_card_tokens(prose)]
    if not tokens:
        return prose
    brand_at = _brand_index(tokens)
    if brand_at is None:
        buried = _buried_model_index(tokens)
        if buried is None:
            return _join_card(tokens) or prose
        used = {buried}
        head = [tokens[buried]]
    else:
        used_list = _take_run(tokens, brand_at)
        # Značka už je na začátku: nesahej na pozdější kód, ať se neroztrhne řada.
        if brand_at > 0 and not any(_is_model_code(tokens[i]) for i in used_list):
            for j in range(brand_at + 1, min(len(tokens), brand_at + 8)):
                tok = tokens[j]
                if not _is_model_code(tok) or _is_hour_or_spec(tok):
                    continue
                prev = j - 1
                if prev not in used_list and prev != brand_at:
                    prev_tok = tokens[prev]
                    if not _is_card_stop(prev_tok) and not _is_color_token(prev_tok) and not _is_hour_or_spec(prev_tok):
                        used_list.append(prev)
                used_list.append(j)
                break
        used = set(used_list)
        head = [tokens[i] for i in used_list]
    if not used or used == set(range(len(head))) and tokens[: len(head)] == head:
        return _join_card(_drop_category(tokens) if brand_at not in (None, 0) else tokens) or prose
    tail = []
    head_folds = {_token_fold(t) for t in head}
    skip_next_model = False
    for i, tok in enumerate(tokens):
        if i in used:
            continue
        fold = _token_fold(tok)
        if skip_next_model:
            skip_next_model = False
            if fold == "model":
                continue
        if fold == "novy" and i + 1 < len(tokens) and _token_fold(tokens[i + 1]) == "model":
            skip_next_model = True
            continue
        if fold == "chytre" and i + 1 < len(tokens) and _token_fold(tokens[i + 1]) == "hodinky":
            continue
        if fold == "hodinky" and i > 0 and _token_fold(tokens[i - 1]) == "chytre":
            continue
        if fold in _CARD_CATEGORY:
            continue
        if fold in head_folds and (_is_card_brand(tok) or _is_model_code(tok)):
            continue
        tail.append(tok)
    return _join_card(head + tail) or prose


# Jednotky a zkratky, které nemají dostat Title Case („mah“ → „Mah“).
_DISPLAY_UNIT = frozenset({
    "mah", "rpm", "fps", "mpx", "mpix", "kwh", "wh", "nit", "nits", "hz", "pa", "db",
})
# Běžná česká slova bez diakritiky. Řada typu Omen/Surface se zvětší, „kryt“ a „bez“ ne.
_NO_TITLE = frozenset({
    "bez", "fotky", "fotka", "foto", "kryt", "kryty", "obal", "obaly", "pouzdro",
    "kabel", "kabely", "sluchatka", "telefon", "telefony", "notebook", "notebooky",
    "televize", "televizor", "vysavac", "kavovar", "pracka", "lednice", "konzole",
    "hodinky", "cerny", "cerna", "cerne", "bily", "bila", "bile", "sedy", "seda",
    "sede", "modry", "zeleny", "zlaty", "ruzovy", "stribrny", "novy", "nova", "nove",
    "originalni", "silikonovy", "kovovy", "plastovy", "edice", "barva", "barvy",
    "nebo", "jen", "vice", "mene", "jako", "jsou", "tak", "uz", "jeste",
})
_DISPLAY_ACRONYM = {
    "ai": "AI", "tv": "TV", "led": "LED", "oled": "OLED", "qled": "QLED",
    "ssd": "SSD", "hdd": "HDD", "ram": "RAM", "cpu": "CPU", "gpu": "GPU",
    "nfc": "NFC", "gps": "GPS", "hdr": "HDR", "uhd": "UHD", "fhd": "FHD",
    "ips": "IPS", "os": "OS", "usb": "USB", "bt": "BT", "anc": "ANC",
    "enc": "ENC", "gb": "GB", "tb": "TB", "wifi": "Wi-Fi", "wi-fi": "Wi-Fi",
}


def _normalize_display_token(token: str) -> str:
    """Značka ze seznamu, kód modelu velkými, ostatní malá latinská slova s velkým začátkem.

    Čeština s diakritikou a už napsaná velikost písmen zůstávají. Uložený název se nemění.
    """
    match = re.match(r"^([(\[\"']*)(.*?)([)\]\"',.;:]*)$", token)
    if not match or not match.group(2):
        return token
    pre, core, post = match.groups()
    if ("/" in core or "." in core) and re.search(r"\d", core) and re.search(r"[A-Za-z]", core):
        return pre + core.upper() + post
    brand = _BRAND_CANON.get(_token_fold(core))
    if brand and _is_card_brand(core):
        return pre + brand + post
    if _is_model_code(core):
        return pre + core.upper() + post
    cap = re.fullmatch(r"(\d+)\s*(gb|tb)", core, re.I)
    if cap:
        return pre + cap.group(1) + cap.group(2).upper() + post
    ac = _DISPLAY_ACRONYM.get(core.lower())
    if ac and core.isalpha() and core.islower():
        return pre + ac + post
    if (re.fullmatch(r"[a-z]{3,}", core) and not _is_card_stop(core)
            and core not in _DISPLAY_UNIT and core not in _NO_TITLE):
        return pre + core[:1].upper() + core[1:] + post
    return token


def normalize_display_title(text: str) -> str:
    """Viditelný zápis. ``samsung qe85qn900f`` → ``Samsung QE85QN900F``, ``Hp`` → ``HP``."""
    tokens = [_normalize_display_token(t) for t in _split_card_tokens(text or "")]
    return _join_card(tokens)


def card_label(name: str) -> str:
    """Viditelný název na kartě. Původní `name` se nemění.

    Nejdřív dosavadní zkrácení (značka a model dopředu, bez výčtu specifikací),
    pak ``polish_feed_display``: předpony typu Smartphone / Televizor / Bluetooth
    sluchátka, marketing „originální … nové by Harman“, druhé stejné značky a
    úhlopříčka zapsaná jako ``55\\" LED``.
    Nakonec jen zápis: kanonická značka ze seznamu a kódy modelu velkými písmeny.
    """
    core = _card_label_core(name)
    polished = polish_feed_display(core) or core
    return normalize_display_title(polished) or polished


def canonical_slug(name: str, stored: str | None = None) -> str:
    """Veřejný slug. Ručně nastavený slug necháme.

    Když je uložený slug jen ``slugify`` syrového feedu a úklid názvu ho změní,
    kanonická adresa je slug vyčištěného názvu. Stará adresa pak 301.
    Úklid, který slug nezmění, adresu neposouvá.
    """
    from .util import slugify

    stored_s = (stored or "").strip().strip("-")
    core = _card_label_core(name or "")
    polished = polish_feed_display(core) or core
    new_slug = slugify(polished) if polished else ""
    if not new_slug or new_slug == "produkt":
        return stored_s or "produkt"
    raw_slug = slugify(name or "")
    core_slug = slugify(core) if core else ""
    if stored_s and stored_s not in {raw_slug, core_slug, new_slug}:
        return stored_s
    if new_slug == core_slug:
        return stored_s or new_slug
    return new_slug
