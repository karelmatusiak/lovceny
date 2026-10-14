"""Klíčové parametry pro Srovnání produktů – jen to, co jde spolehlivě vyčíst z názvu produktu
(obchody je v názvu běžně uvádějí: „65\" OLED“, „8 kg“, „8GB/256GB“). Nic si nedomýšlíme:
co v názvu není, zobrazíme jako „–“. Na stránce je vždy poznámka, že jde o údaje z názvu."""
from __future__ import annotations

import re
import unicodedata

COLORS = {"cern": "černá", "bil": "bílá", "stribrn": "stříbrná", "sed": "šedá", "modr": "modrá", "zelen": "zelená",
          "cerven": "červená", "ruzov": "růžová", "fialov": "fialová", "zlat": "zlatá", "titan": "titanová",
          "graphite": "grafitová", "black": "černá", "white": "bílá", "silver": "stříbrná", "blue": "modrá",
          "green": "zelená", "pink": "růžová", "gold": "zlatá", "inox": "nerez", "nerez": "nerez", "midnight": "tmavě modrá"}

# (klíč, popisek) – pořadí řádků v tabulce
LABELS = [("ram", "Operační paměť"), ("storage", "Úložiště"), ("screen", "Úhlopříčka"), ("panel", "Typ displeje"),
          ("hz", "Obnovovací frekvence"), ("net", "Síť"), ("load", "Kapacita náplně"), ("rpm", "Otáčky ždímání"),
          ("volume", "Objem"), ("pressure", "Tlak"), ("power", "Výkon / příkon"), ("width", "Rozměr"),
          ("color", "Barva")]


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _num(s: str) -> str:
    return s.replace(".", ",")


def extract(name: str) -> dict[str, str]:
    n = " " + (name or "") + " "
    f = " " + _fold(name) + " "
    out: dict[str, str] = {}
    m = re.search(r"\b(\d{1,2})\s?(?:GB)?\s?[/+]\s?(\d{2,4})\s?(GB|TB)\b", n, re.I)
    if m:
        out["ram"], out["storage"] = f"{m.group(1)} GB", f"{m.group(2)} {m.group(3).upper()}"
    else:
        m = re.search(r"\b(\d{2,4}|[1-8])\s?(GB|TB)\b", n, re.I)
        if m and (m.group(2).upper() == "TB" or int(m.group(1)) >= 32):
            out["storage"] = f"{m.group(1)} {m.group(2).upper()}"
        m2 = re.search(r"\b(\d{1,2})\s?GB\s?RAM\b", n, re.I)
        if m2:
            out["ram"] = f"{m2.group(1)} GB"
    m = re.search(r"\b(\d{1,3}(?:[.,]\d{1,2})?)\s?(?:\"|''|”|″|palc\w*|inch)", n, re.I)
    if m and 3 <= float(m.group(1).replace(",", ".")) <= 120:
        out["screen"] = f"{_num(m.group(1))}\""
    else:
        m = re.search(r"\b(?:qe|ue|tq|tu|oled|kd-?|xr-?|tx-?|lg\s?)(\d{2})[a-z]", f)
        if m and 24 <= int(m.group(1)) <= 98:
            out["screen"] = f"{m.group(1)}\""
    for k, label in (("oled", "OLED"), ("qled", "QLED"), ("mini led", "Mini LED"), ("miniled", "Mini LED"),
                     ("amoled", "AMOLED"), ("ips", "IPS"), ("nanocell", "NanoCell")):
        if re.search(r"\b" + re.escape(k) + r"\b", f):
            out["panel"] = label
            break
    m = re.search(r"\b(\d{2,3})\s?hz\b", f)
    if m:
        out["hz"] = f"{m.group(1)} Hz"
    if re.search(r"\b5g\b", f):
        out["net"] = "5G"
    elif re.search(r"\blte\b|\b4g\b", f):
        out["net"] = "4G / LTE"
    m = re.search(r"\b(\d{1,2}(?:[.,]\d)?)\s?kg\b", f)
    if m and 1 <= float(m.group(1).replace(",", ".")) <= 25:
        out["load"] = f"{_num(m.group(1))} kg"
    m = re.search(r"\b(\d{3,4})\s?(?:ot\.?|otacek|rpm)\b", f)
    if m and 400 <= int(m.group(1)) <= 2000:
        out["rpm"] = f"{m.group(1)} ot./min"
    m = re.search(r"\b(\d{1,3}(?:[.,]\d)?)\s?(?:l|litru|litr)\b", f)
    if m and 0.2 <= float(m.group(1).replace(",", ".")) <= 999:
        out["volume"] = f"{_num(m.group(1))} l"
    m = re.search(r"\b(\d{1,2})\s?bar\b", f)
    if m:
        out["pressure"] = f"{m.group(1)} bar"
    m = re.search(r"\b(\d{2,4})\s?w\b", f)
    if m and 10 <= int(m.group(1)) <= 4000:
        out["power"] = f"{m.group(1)} W"
    m = re.search(r"\b(\d{2,3})\s?cm\b", f)
    if m:
        out["width"] = f"{m.group(1)} cm"
    for k, v in COLORS.items():
        if re.search(r"\b" + k, f):
            out["color"] = v
            break
    return out


def table(names: list[str]) -> list[tuple[str, list[str]]]:
    """Řádky (popisek, hodnoty po produktech) jen pro parametry, které má aspoň jeden produkt."""
    found = [extract(n) for n in names]
    rows = []
    for key, label in LABELS:
        vals = [d.get(key, "–") for d in found]
        if any(v != "–" for v in vals):
            rows.append((label, vals))
    return rows
