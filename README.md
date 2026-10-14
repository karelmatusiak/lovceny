# Lovceny (lovceny.cz) – MVP

Poctivý hlídač cen elektroniky: vložíš odkaz z e-shopu, dostaneš historii ceny a verdikt
**Kupuj / Počkej / Falešná sleva**, a můžeš si nastavit **hlídacího psa**.

Stack: Python 3.12, FastAPI + Jinja2, SQLAlchemy 2 + psycopg 3 (Postgres; lokálně lze i SQLite), Chart.js.

## Funkce MVP
- **Hledání** (`/hledat?q=`): odkaz z podporovaného obchodu → založí sledovaný produkt; text → hledá ve sledovaných produktech (název/EAN).
- **Stránka produktu**: ceny podle obchodů (řazeno jen podle ceny), graf 30/90/365 dní s minimem a mediánem, verdikt.
- **Verdikt** (`lovceny/verdict.py`): Kupuj = do 3 % nad 90denním minimem; Falešná sleva = „původní“ cena > 105 % minima za 30 dní před akcí, nebo skok o ≥ 12 % během posledních 3 týdnů, který „sleva“ jen vrátila; jinak Počkej. Pod 7 dny dat → „Málo dat“.
- **Hlídací pes**: e-mail + cílová cena v tabulce `watches`, double opt-in (`/pes/potvrdit/<token>`), odhlášení jedním klikem. E-maily jsou **jen v logu** (`lovceny/notify.py`, `EMAIL_BACKEND=log`).
- **Stahovač** `fetch_prices.py`: čte JSON-LD `schema.org/Product` (fallback meta tagy), respektuje robots.txt, min. 6 s mezi požadavky na doménu, identifikuje se jako `LovcenyBot`. Ochranu proti botům **neobchází**: při 403/429/JS challenge označí nabídku `blocked`.
- **Demo data**: `seed.py` přidá skutečné produkty z Planea a vygenerovanou historii s `price_points.is_demo = true` (v UI označeno). Smazání: `python seed.py --purge-demo`.

## Obchody (test 14. 10. 2026 z vývojového serveru)
| Obchod | Zdroj | Stav |
|---|---|---|
| Planeo | stránka (JSON-LD) | ✅ funguje |
| Alza | **feed** | Cloudflare 403 → čeká na oficiální feed / affiliate |
| Datart | **feed** | F5 JS challenge → čeká na oficiální feed / affiliate |
| CZC, Mall | – | přesměrováno na Allegro, DataDome 403 |
| Smarty, TS Bohemia | – | 403 |
| Electroworld, Kasa | – | F5 JS challenge |

Přehled je i na stránce `/obchody`; konfigurace v `lovceny/shops/registry.py`.

### Alza a Datart přes oficiální zdroj (feed + affiliate)
Alza a Datart jsou nastavené jako `source="feed"`. Vložený odkaz z Alzy/Datartu vytvoří produkt s odkazem do obchodu a stavem **„cena bude doplněna“**. Ceny dodá import feedu:
- **Feed** v Heureka XML formátu (`SHOPITEM`, `ITEM_ID`, `PRODUCTNAME`, `URL`, `PRICE_VAT`, `EAN`, volitelně `IMGURL`, `CATEGORYTEXT`, `DELIVERY_DATE`) nebo CSV se stejnými sloupci. URL feedu nastavte v `ALZA_FEED_URL` / `DATART_FEED_URL` (typicky z affiliate programu). Párování: URL → ITEM_ID → EAN.
- Ruční import: `python fetch_prices.py --feed alza=sample_feeds/heureka_sample.xml` (ukázkový soubor má **smyšlené** ceny, slouží k testům).
- **Affiliate odkazy**: `ALZA_AFFILIATE_ID` přidá k odkazům `?idp=<ID>`; `DATART_AFFILIATE_ID` analogicky (název parametru upravte v registry podle podmínek programu). Na pořadí ani verdikt to nemá vliv.
- Test: `tests/test_feed.py`.

### Živé hledání v obchodech (`lovceny/shops/search.py`)
Když text v DB najde méně než 4 produkty, stránka `/hledat` hned zobrazí načítání a přes `/hledat/zive?q=` (bez JS `?zive=1`)
prohledá obchody s `searcher` v registry – souběžně, timeout 8 s/obchod, cache 10 min v paměti, ≥ 2 s mezi dotazy na doménu.
Příslušenství (obaly, fólie, řemínky…) skrýváme, pokud ho dotaz nehledá. Výsledky (+ feedové nabídky Alzy/Datartu z DB) jsou řazené jen podle ceny; tlačítko **Hlídat** (`POST /sledovat`) volá `add_by_url`.
robots.txt čteme s podporou `*`/`$` (`shops/robots.py`; stdlib parser wildcardy ignoruje).
| Obchod | Živé hledání (test 14. 10. 2026) |
|---|---|
| Mironet | ✅ `/ProductList/showSearch?EXPF=` – robots povoluje, HTML výpis (název, cena, URL, obrázek) |
| Softcom | ✅ `/eshop/default.asp?cls=stoitems&fulltext=` – detail čteme z HTML (bez JSON-LD) |
| Svět počítačů | ✅ `/hledani/?q=` – detail JSON-LD |
| AB-COM | ✅ `/hledani?q=` – detail přes meta tagy; URL musí končit „/“ (`trailing_slash`) |
| iStyle, Mobil Pohotovost, ProMobily, FotoŠkoda, iSpace, Megapixel, Expert, Elektrospot, Počítárna, GigaComputer, LevnaPC | ❌ vyhledávání zakazuje robots.txt |
| Comfor | ❌ výsledky jen přes JS (Luigi's Box) |
| Kuma | ❌ HTTP 429 |
| iWant, BScom, K24, Electronic-star, Allegro (CZC, Mall) | ❌ ochrana proti botům |
| Planeo | ❌ `/vyhledavani*` zakázáno v robots.txt (sledování produktů funguje dál) |
| Xzone | ❌ `?term=` zakázáno v robots.txt |
| Suntech | ❌ `Disallow: /` |
| Euronics | ❌ F5 JS challenge |

Obchody k oslovení kvůli feedu/affiliate: `../obchody-k-osloveni.md`.
| Alza, Datart | hledáme v importovaném feedu v DB |

## Lokální spuštění
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export DATABASE_URL=postgresql://lovceny:lovceny@localhost/lovceny   # bez proměnné se použije SQLite ./lovceny.db
.venv/bin/python seed.py               # produkty + demo historie
.venv/bin/python fetch_prices.py       # aktualizace cen
.venv/bin/uvicorn lovceny.main:app --reload   # http://localhost:8000
.venv/bin/python -m pytest -q
```
Tabulky se vytvoří automaticky (`create_all`). Pro další vývoj doporučujeme přidat Alembic migrace.

## Nasazení zdarma
1. **GitHub**: vytvořte soukromý repozitář a pushněte (`git remote add origin … && git push -u origin main`).
2. **Neon** (neon.tech): nový projekt, region Frankfurt, databáze `lovceny`. Zkopírujte *connection string* (`postgresql://…?sslmode=require`).
   Lokálně jednou spusťte `DATABASE_URL=<neon> python seed.py` (nebo `--no-demo`).
3. **Render** (render.com): *New → Blueprint* → vyberte repozitář (načte `render.yaml`). Vyplňte `DATABASE_URL` (Neon). Free instance po 15 min nečinnosti usíná, první načtení trvá ~30 s.
4. **GitHub Actions secrets** (Settings → Secrets and variables → Actions): `DATABASE_URL` (Neon), volitelně `ALZA_FEED_URL`, `DATART_FEED_URL`. Workflow `.github/workflows/fetch.yml` běží 3× denně (5:17, 11:17, 17:17 UTC), ručně přes *Run workflow*.
5. Doména: v Renderu *Custom domain* `lovceny.cz` + DNS CNAME u registrátora.

Pozn.: GitHub Actions běží z IP datacenter; obchody, které fungují odsud, mohou z Actions blokovat. Výsledek uvidíte v logu („SOUHRN“).

## Struktura
```
lovceny/main.py        FastAPI routy        lovceny/verdict.py   pravidla verdiktu
lovceny/models.py      tabulky              lovceny/service.py   přidání URL, zápis cen, feed, hlídací psi
lovceny/shops/         registry (obchody), scrape (JSON-LD, robots, rate limit), feed (Heureka XML/CSV)
lovceny/notify.py      e-mail rozhraní (log stub)
lovceny/templates/, static/   UI podle mockupu
fetch_prices.py, seed.py, sample_feeds/, tests/, render.yaml, .github/workflows/fetch.yml
```
