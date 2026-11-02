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
    s = re.sub(r"\s+", " ", s).strip(" \t,;/-")
    return s


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
