# Lovceny (lovceny.cz) – MVP

Poctivý hlídač cen elektroniky a domácích spotřebičů (mobily, TV a audio, foto, gaming, chytrá domácnost, velké i malé spotřebiče – pračky, lednice, myčky, vysavače, kávovary): vložíš odkaz z e-shopu, dostaneš historii ceny a verdikt
**Kupuj / Počkej / Falešná sleva**, a můžeš si nastavit **hlídání ceny** 🔔.

Stack: Python 3.12, FastAPI + Jinja2, SQLAlchemy 2 + psycopg 3 (Postgres; lokálně lze i SQLite), Chart.js.

## Funkce MVP
- **Hledání** (`/hledat?q=`): odkaz z podporovaného obchodu → založí sledovaný produkt; text → hledá ve sledovaných produktech (název/EAN).
- **Stránka produktu**: ceny podle obchodů (řazeno jen podle ceny), graf 30/90/365 dní s minimem a mediánem, verdikt.
- **Verdikt** (`lovceny/verdict.py`): Kupuj = do 3 % nad 90denním minimem; Falešná sleva = „původní“ cena > 105 % minima za 30 dní před akcí, nebo skok o ≥ 12 % během posledních 3 týdnů, který „sleva“ jen vrátila; jinak Počkej. Pod 7 dny dat → „Málo dat“.
- **Hlídání ceny**: e-mail + cílová cena v tabulce `watches`, double opt-in (`/hlidani/potvrdit/<token>`; staré `/pes/…` přesměrovávají), odhlášení jedním klikem (záznam se rovnou smaže). E-maily posílá **Brevo** (`EMAIL_BACKEND=brevo`, viz níže), jinak jen log.
- **Upozornění „cena klesla“**: `check_watches()` běží po každém stažení cen (`fetch_prices.py` v GitHub Actions). Stejný pokles se oznámí jen jednou (`watches.notified_price`); znovu jen při dalším poklesu o ≥ 3 % nebo po návratu ceny nad cíl. Neodeslaný e-mail se zkusí při dalším běhu.
- **Úklid** (`cleanup_watches()`, po každém stažení): nepotvrzená hlídání po 7 dnech, hlídání bez aktivity 24 měsíců (od vytvoření i posledního upozornění).
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
Shoda názvu je bez diakritiky, s ořezem koncovek a synonymy (lednice = chladnička, televize = televizor). Příslušenství (obaly, fólie, řemínky, stojany na TV, sůl do myčky, „miska pod lednici“…) skrýváme, pokud ho dotaz nehledá. Výsledky (+ feedové nabídky Alzy/Datartu z DB) jsou řazené jen podle ceny; tlačítko **Hlídat** (`POST /sledovat`) volá `add_by_url`.
robots.txt čteme s podporou `*`/`$` (`shops/robots.py`; stdlib parser wildcardy ignoruje).
| Obchod | Živé hledání (test 14. 10. 2026) |
|---|---|
| Mironet | ✅ `/ProductList/showSearch?EXPF=` – robots povoluje, HTML výpis (název, cena, URL, obrázek) |
| Softcom | ✅ `/eshop/default.asp?cls=stoitems&fulltext=` – detail čteme z HTML (bez JSON-LD) |
| Svět počítačů | ✅ `/hledani/?q=` – detail JSON-LD |
| AB-COM | ✅ `/hledani?q=` – detail přes meta tagy; URL musí končit „/“ (`trailing_slash`) |
| 4Home | ✅ `/hledat/?q=` – kuchyň a domácí spotřebiče, detail JSON-LD |
| Vestavné spotřebiče | ✅ `/search/?search_value=` – vestavné spotřebiče; kategorie je jen v URL (`match_slug`) |
| ETA | ✅ `/shop/vyhledavani/?q=` – značkový e-shop ETA/Gorenje/Hyundai, detail z HTML |
| Sencor, Whirlpool | ❌ vyhledávání zakazuje robots.txt |
| Dyson.cz, Philips.cz | ❌ ochrana proti botům |
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
3. **Render** (render.com): *New → Blueprint* → vyberte repozitář (načte `render.yaml`). Vyplňte `DATABASE_URL` (Neon), `BREVO_API_KEY`, `EMAIL_FROM`, volitelně `GEMINI_API_KEY`. Free instance po 15 min nečinnosti usíná, první načtení trvá ~1 min (viz „Aby Render neusínal“).
4. **GitHub Actions secrets** (Settings → Secrets and variables → Actions): `DATABASE_URL` (Neon), `EMAIL_BACKEND` = `brevo`, `BREVO_API_KEY`, `EMAIL_FROM`; volitelně `ALZA_FEED_URL`, `DATART_FEED_URL`. Volitelná *variable* `PUBLIC_URL` (výchozí `https://lovceny.onrender.com`). Workflow `.github/workflows/fetch.yml` běží 3× denně (5:17, 11:17, 17:17 UTC), ručně přes *Run workflow*; po stažení posílá upozornění a uklízí stará hlídání.
5. Doména: v Renderu *Custom domain* `lovceny.cz` + DNS CNAME u registrátora.

### E-maily přes Brevo (free tier)
- Free plán: **300 e-mailů denně** (sdíleno marketing + transakční), bez platební karty. Po vyčerpání drží Brevo až 1 000 transakčních e-mailů ve frontě na další den.
- Odesílatel `EMAIL_FROM` musí být v Brevu ověřený (6místný kód na danou adresu) nebo musí mít ověřenou doménu (DKIM + DMARC). Doporučeno: ověřit doménu `lovceny.cz`. Odesílat z @gmail.com nelze dlouhodobě (Brevo adresu nahradí, e-maily končí ve spamu).
- API: `POST https://api.brevo.com/v3/smtp/email`, hlavička `api-key`. Klíč: Brevo → *SMTP & API* → *API Keys* → *Generate a new API key*.
- Logo v e-mailu je PNG (`static/brand/logo-email.png`, Gmail SVG nezobrazí), odkazy jsou absolutní podle `PUBLIC_URL`.

### Aby Render neusínal
Render free web service usne po 15 min bez požadavku (probuzení ~1 min). Řešení: externí monitor volá `GET https://lovceny.onrender.com/healthz` každých 5–10 min.
- `/healthz` je lehký: **nesahá do DB**, takže nebudí Neon (free: 100 CU-h/měsíc, uspání po 5 min – ping s dotazem do DB by DB držel vzhůru 24/7 a limit vyčerpal). Diagnostika DB: `/healthz?db=1` (SELECT 1, latence, počty produktů/hlídání cachované 10 min).
- Hodiny: Render dává **750 free instance-hours/měsíc na workspace**; jedna služba vzhůru 24/7 spotřebuje ~744 h, tj. skoro vše (druhá free služba by už neběžela). Bezpečnější je pingovat jen přes den (např. 6–23 h), ~530 h/měsíc.
- Render pingy výslovně nezakazuje, ale jeho Acceptable Use Policy zakazuje „obcházet omezení použití“ – výklad je na Renderu. Spolehlivě a čistě řeší spánek jen placený plán (Starter).
- **Doporučeno: cron-job.org** (zdarma, interval až 1 min, lze omezit na hodiny, e-mail při selhání). Alternativa UptimeRobot (free 50 monitorů, interval 5 min, 24/7, upozornění při výpadku).
- `.github/workflows/keepalive.yml` je jen záložní (ruční spuštění); cron je vypnutý, protože v soukromém repu by sežral free minuty Actions (každý běh ≥ 1 min, limit 2 000 min/měsíc) a GitHub plánované běhy zpožďuje/vynechává.

Pozn.: GitHub Actions běží z IP datacenter; obchody, které fungují odsud, mohou z Actions blokovat. Výsledek uvidíte v logu („SOUHRN“).

## Struktura
```
lovceny/main.py        FastAPI routy        lovceny/verdict.py   pravidla verdiktu
lovceny/models.py      tabulky              lovceny/service.py   přidání URL, zápis cen, feed, hlídání cen
lovceny/shops/         registry (obchody), scrape (JSON-LD, robots, rate limit), feed (Heureka XML/CSV)
lovceny/notify.py      e-maily: log / Brevo   lovceny/emails.py + templates/email/  obsah e-mailů
lovceny/templates/, static/   UI podle mockupu
fetch_prices.py, seed.py, sample_feeds/, tests/, render.yaml, .github/workflows/fetch.yml
```
