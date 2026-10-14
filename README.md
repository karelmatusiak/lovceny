# Lovceny (lovceny.cz) – MVP

Poctivý hlídač cen elektroniky a domácích spotřebičů (mobily, TV a audio, foto, gaming, chytrá domácnost, velké i malé spotřebiče – pračky, lednice, myčky, vysavače, kávovary): vložíš odkaz z e-shopu, dostaneš historii ceny a verdikt
**Kupuj / Počkej / ⚠️ Za 30 dní bylo levněji**, a můžeš si nastavit **hlídání ceny** 🔔.

Stack: Python 3.12, FastAPI + Jinja2, SQLAlchemy 2 + psycopg 3 (Postgres; lokálně lze i SQLite). Graf historie na detailu je inline SVG ze serveru (bez JS knihovny); Od v7 i Srovnání produktů kreslí graf jako SVG na serveru – žádná JS knihovna z CDN.

## Funkce MVP
- **Hledání** (`/hledat?q=`): odkaz z podporovaného obchodu → založí sledovaný produkt; text → hledá ve sledovaných produktech (název/EAN).
- **Stránka produktu**: ceny podle obchodů (řazeno jen podle ceny), verdikt a **historie ceny** (`lovceny/chart.py`): inline SVG vykreslené na serveru, přepínač 30 dní / 90 dní / celá historie bez JS (radio + CSS), aktuální cena, minimum (zvýrazněné), průměr, štítek verdiktu Kupuj/Počkej v grafu a krátké vysvětlení. Užší varianta SVG pro mobil. Při 0–1 záznamu srozumitelná hláška místo grafu.
- **Verdikt** (`lovceny/verdict.py`, prahy jako konstanty `MIN_DAYS`, `BUY_OVER_MIN`, `REF_OVER_MIN30`, `JUMP`, `JUMP_BACK`): Kupuj = do 3 % nad 90denním minimem; **Za 30 dní bylo levněji** (interní kód `fake`) = „původní“ cena > 105 % nejnižší naměřené ceny za 30 dní před akcí (logika § 12a ZOS), nebo skok o ≥ 12 % během posledních 3 týdnů, který sleva vrátila – a zároveň za 30 dní opravdu bylo levněji než dnes; jinak Počkej. Pod 7 dny dat → „Málo dat“. U štítku vždy data (`Verdict.ref_min30`, `ref_date`, `tooltip`: „Nejnižší cena za posledních 30 dní byla X Kč (dne D). Sleva se počítá z vyšší ceny.“) a odkaz na metodiku. Formulace jsou čistě faktické (žádné „falešná“ apod., hlídá to `tests/test_transparency.py`).
- **Transparentnost (v5)**: `/jak-radime` (Jak řadíme a hodnotíme – pořadí jen podle ceny bez dopravy, žádné placené pozice, seznam obchodů z registry se stavem, časy stahování z `fetch.yml`, prahy verdiktů přímo z kódu, zdroje, omezení); odkaz „Jak řadíme?“ u výsledků hledání, živého hledání, kategorií a tabulky cen produktu, v patičce a v sitemap. `/pro-obchody` (User-Agent, robots.txt, rate limit, co ukládáme, oprava/vyřazení do 7 dní přes formulář s tématem „Oprava/vyřazení obchodu“, `/o-nas?tema=obchod-oprava`). `/podminky` (Podmínky užívání; provozovatel „bude doplněn po založení živnosti“). Přesměrování 301: `/metodika`, `/bot`, `/pro-e-shopy`, `/podminky-uzivani`. User-Agent bota odkazuje na `https://lovceny.cz/pro-obchody`.
- **Affiliate upozornění**: makro `affiliate_note()` („Odkaz může být provizní – na pořadí to nemá vliv.“) je skryté, dokud není `LOVCENY_AFFILIATE=1` nebo nastavené affiliate ID některého obchodu (`ALZA_AFFILIATE_ID`…).
- **Hlídání ceny**: e-mail + cílová cena v tabulce `watches`, double opt-in (`/hlidani/potvrdit/<token>`; staré `/pes/…` přesměrovávají), odhlášení jedním klikem (záznam se rovnou smaže). E-maily posílá **Brevo** (`EMAIL_BACKEND=brevo`, viz níže), jinak jen log.
- **Upozornění „cena klesla“**: `check_watches()` běží po každém stažení cen (`fetch_prices.py` v GitHub Actions). Stejný pokles se oznámí jen jednou (`watches.notified_price`); znovu jen při dalším poklesu o ≥ 3 % nebo po návratu ceny nad cíl. Neodeslaný e-mail se zkusí při dalším běhu.
- **Úklid** (`cleanup_watches()`, po každém stažení): nepotvrzená hlídání po 7 dnech, hlídání bez aktivity 24 měsíců (od vytvoření i posledního upozornění).
- **Stahovač** `fetch_prices.py`: čte JSON-LD `schema.org/Product` (fallback meta tagy), respektuje robots.txt, min. 6 s mezi požadavky na doménu, identifikuje se jako `LovcenyBot`. Ochranu proti botům **neobchází**: při 403/429/JS challenge označí nabídku `blocked`.
- **Katalog** (`seed_catalog.json`, 96 produktů ve 12 kategoriích – telefony, TV, notebooky, sluchátka, pračky, lednice, kávovary, vysavače, konzole, hodinky, foťáky, chytrá domácnost) z obchodů, které čtení dovolují (Mironet, Softcom, Svět počítačů, AB-COM, 4Home, Vestavné spotřebiče; od v4 i Onlineshop, De'Longhi, Quikset, ITex24, Ellex GK…). `seed.py` je idempotentní (klíč = URL nabídky). **Spuštění na produkci:** GitHub → Actions → *Naplnit databázi (katalog produktů)* → *Run workflow* (volby např. `--no-demo`, `--dry-run`, `--category pracky`). Používá secret `DATABASE_URL`. Ceny pak udržuje `fetch.yml`.
- **Sledovat tento produkt**: výsledek živého hledání otevře náhled `/nahled?url=` (data z cache hledání, nic se znovu nestahuje) s tlačítkem *Sledovat tento produkt* (`POST /sledovat`). Už sledovaná URL = přesměrování (dedup). Nové produkty: max. 10/h na IP (ukládá se jen hash IP na 24 h) a 300/den celkem.
- **Kategorie a SEO**: `/kategorie`, `/kategorie/<slug>` (+ `/nejlevnejsi`, `/ve-sleve`) s unikátním titulkem a popisem; JSON-LD `Product` + `AggregateOffer`, `BreadcrumbList`, `ItemList`; `sitemap.xml`, `robots.txt`; kanonické URL a OpenGraph podle `PUBLIC_URL` (výchozí `https://lovceny.onrender.com`, po napojení domény nastavit `https://lovceny.cz`). Hledání, porovnání a náhled jsou `noindex`. Zařazení do kategorie: `lovceny/categories.py` (`classify`, i pro nově přidané produkty).
- **Upozornění v prohlížeči (Web Push)**: alternativa k e-mailu u Hlídat cenu. Service worker `/sw.js`, odběr `POST /api/push/odebirat` (jen endpointy známých push služeb), odhlášení tlačítkem, z oznámení („Přestat hlídat“) nebo `/hlidani/push/zrusit/<token>`. Posílá `fetch_prices.py` po stažení cen (`lovceny/push.py`, stejná pravidla proti duplicitám jako e-mail; zaniklé odběry 404/410 maže). Bez `VAPID_PUBLIC_KEY` + `VAPID_PRIVATE_KEY` je funkce skrytá. Klíče: base64url (veřejný = nekomprimovaný bod P-256, soukromý = 32 B), `VAPID_SUBJECT` = `mailto:…`.
- **Výkon a přístupnost**: brotli/gzip (`brotli-asgi`), statické soubory s `?v=<hash>` a `Cache-Control: immutable` na rok, JS `defer`, rozměry obrázků, systémová písma (žádné webfonty k preloadu), skip link, viditelný fokus, landmarky, popisky formulářů, tap targety ≥ 44 px.
- **Obrázky produktů přes vlastní proxy** (`lovceny/images.py`, `/img/<šířka>?u=<URL>`): stáhne obrázek jen z hostitelů našich obchodů (domény z registry + CDN `fast.eu`), respektuje robots.txt, max. 8 MB / 40 Mpx, timeout 6 s, redirect jen na povolený host; zmenší do WebP (80/160/320/640 px, Pillow) a uloží do diskové LRU cache (`IMG_CACHE_DIR`, výchozí `/tmp/lovceny-img`; strop `IMG_CACHE_MAX_MB`, výchozí 150 – na Renderu je disk dočasný, po restartu se cache znovu naplní). Odpověď `Cache-Control: public, max-age=31536000, immutable`; šablony mají `srcset` + `sizes` (makro `shop_img`). Při chybě 302 na originál. Detail produktu (mobil, Lighthouse): výkon 89 → 100, LCP 3,8 s → 1,4 s (obrázek 452 kB → 1,6 kB).
- **Monitoring blokací obchodů**: `fetch_prices.py` ukládá za každý běh a obchod počty OK / blokováno (403/429/503) / captcha (JS challenge) / robots.txt / chyby a HTTP kódy (`shop_run_stats`, 90 dní). Veřejný přehled `/stav` (úspěšnost za 14 dní, čerstvost cen). Pravidla (`lovceny/monitor.py`): v běhu selhala většina požadavků (≥ 3 pokusy), nebo poslední úspěšná cena je starší než 2 dny. `--health-out health.json` → `shop_health_issues.py` ve workflow založí / aktualizuje GitHub issue (štítek `stav-obchodu`, česky, komentář jen při změně důvodu) a po zotavení ho okomentuje a zavře. Spadne-li běh dřív, vznikne issue „Stahování cen neproběhlo“. Používá jen vestavěný `GITHUB_TOKEN` (`permissions: issues: write`), notifikace chodí z GitHubu.
- **PWA**: `/manifest.webmanifest` (česky, ikony 192/512 + maskable, barvy značky), service worker `/sw.js` (šablona `templates/sw.js`, verze podle obsahu statických souborů) – Web Push + offline stránka `/offline` + cache-first pro verzované `/static/` a zmenšené `/img/`. Stránky, hledání a `/api/` se **necachují** (ceny vždy čerstvé). Výzva „Přidat na plochu“ (`static/pwa.js`): Android/desktop nativní dialog, iPhone návod Sdílet → Přidat na plochu; od 2. návštěvy, „Teď ne“ = 30 dní klid. Na iPhonu se u Hlídat cenu ukáže, že upozornění fungují jen z plochy (iOS 16.4+).
- **Vlastní analytika bez cookies** (`lovceny/analytics.py`): cesta, doména referreru, normalizovaný dotaz (bez e-mailů/čísel/odkazů), počet výsledků, produkt. Unikáty = `sha256(denní sůl + IP + UA)`; sůl se denně mění a maže, otisky starší 24 h se nulují (denní součet v `analytics_daily`). DNT / GPC a boti se nezaznamenávají. Admin `/admin` (HTTP Basic, heslo = `ADMIN_TOKEN`, jméno libovolné; bez tokenu 404): zobrazení po dnech, top stránky, produkty, hledání, hledání bez výsledků, referrery, zprávy z formuláře.
- **Sdílení a O nás**: tlačítko Sdílet na detailu (Web Share API na mobilu, jinak kopírování odkazu + WhatsApp / Messenger / X / e-mail, bez skriptů třetích stran, funguje i bez JS). `/o-nas`: mise, řazení jen podle ceny, verdikt, rozdíl oproti Heurece/Zboží.cz, jak vyděláváme (affiliate, nikdy nemění pořadí), FAQ s JSON-LD `FAQPage`, kontaktní formulář (DB, honeypot, 5/h na IP v paměti, mazání po 12 měsících).
- **Demo data**: `seed.py` přidá i skutečné produkty z Planea a vygenerovanou historii s `price_points.is_demo = true` (v UI označeno). Smazání: `python seed.py --purge-demo`.

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
| Onlineshop.cz | ✅ `/vyhledavani/?q=` – bereme běžnou cenu, ne cenu se slevovým kódem; detail JSON-LD (v4) |
| De'Longhi (oficiální e-shop) | ✅ `/cs-cz/search?q=` – karty bez stabilních tříd, ceny podle „aktuální/původní cena“; `match_slug`; detail JSON-LD (v4) |
| Quikset | ✅ `/hledani?q=` – Apple a příslušenství, detail z HTML (v4) |
| iStores | ❌ robots.txt nejasný (zakazuje jen slovenské `/vyhladavanie`), vyřazeno z ohleduplnosti |
| ITex24 | ✅ `/?s=…&post_type=product` – WooCommerce, 0 Kč (cena na dotaz) vynecháváme (v4) |
| Ellex GK (Sony Center) | ✅ `/search?phrase=` – Upgates, detail JSON-LD (v4) |
| Mobily Ostrava, MobilProVás | ✅ malé obchody (`/cz/hledat/?search_nazev=`, `/search/?find=`) (v4) |
| Mobilfone | ⚠️ `/search.php` zakázáno – jen sledování podle URL, cena z HTML (v4) |
| Sencor, Whirlpool, Rowenta, Samsung.cz, LG, Siemens/BSH, Philips, Emos, Expert, iStyle, Mobil Pohotovost, CZC (rozcestník na Allegro), h-shop | ❌ vyhledávání zakazuje robots.txt |
| Penta | ❌ `Disallow: /` pro ostatní roboty |
| Conrad, TS Bohemia, Smarty, eBerry, BScom, Euroking, Mader, Lenovo, Xiaomi (mi.com), Tefal, Allegro/Mall, Megapixel (Anubis) | ❌ ochrana proti botům (403 / Cloudflare) |
| SlevyElektro, Sporilek, Tipa, K24, Comfor, Garmin, Huawei, Apple | ❌ výsledky hledání jen přes JS / interní API – neobcházíme |
| Dyson.cz | ❌ robots.txt zakazuje `/catalogsearch/` |
| ProMobily, FotoŠkoda, iSpace, Elektrospot, Počítárna, GigaComputer, LevnaPC | ❌ vyhledávání zakazuje robots.txt |
| Kuma | ❌ HTTP 429 |
| iWant, Electronic-star | ❌ ochrana proti botům |
| Planeo | ❌ `/vyhledavani*` zakázáno v robots.txt (sledování produktů funguje dál) |
| Xzone | ❌ `?term=` zakázáno v robots.txt |
| Suntech | ❌ `Disallow: /` |
| Euronics | ❌ F5 JS challenge |

Obchody k oslovení kvůli feedu/affiliate: `../obchody-k-osloveni.md`.
| Alza, Datart | hledáme v importovaném feedu v DB |

## v6 – Kdy nakupovat, Ověř slevu, rozšíření, týdenní přehled

- **/kdy-nakupovat** (`lovceny/seasons.py`): kalendář typických slevových období (jasně označený jako *obecný vzorec*) a tip u každé kategorie i produktu. Sekce *Naše data* se ukáže jen při dostatku vlastních měření (≥ 5 produktů s historií, ≥ 6 různých měsíců, ≥ 3 produkty a ≥ 7 dní na produkt v měsíci, rozdíl ≥ 2 %); demo data se nepočítají. Jinak poctivě „zatím málo dat“.
- **/over-slevu** (`lovceny/verify.py`): vložený odkaz se stáhne jen u obchodu z registru se stavem `ok` (robots.txt, max 3 MB, 10 s, přesměrování jen v rámci domén obchodu, kontrola veřejné IP u každého skoku – ochrana proti SSRF). U feedových/zakázaných/neznámých obchodů se nic nestahuje, jen se hledá podle názvu z adresy. Kontrola 30 dní (§ 12a ZOS) z našich měření (min. 7 dní). Limit 20 ověření/h na hash IP, 1000/den celkem, cache 15 min. Výsledek má sdílitelnou adresu.
- **Rozšíření prohlížeče** (`extension/`, MV3 pro Chrome i Firefox): `python extension/build.py --out dist` vytvoří zipy (stahují se i z `/rozsireni`). Rozšíření pošle na `/api/v1/overit` jen adresu, název a cenu ze strukturovaných dat stránky (bez cookies), server obchod nestahuje. API: CORS jen pro `chrome-extension://` / `moz-extension://`, 60 dotazů/h na hash IP, cache. Texty pro obchody: `extension/STORE.md`, soukromí `extension/PRIVACY.md`. E2E test: `node extension/tests/e2e.js` (headless Chromium, uložená stránka Onlineshopu); v pytestu běží, když je nastaveno `LOVCENY_E2E_NODE_PATH` (node_modules s Playwrightem).
- **Týdenní přehled** (`lovceny/digest.py`, `weekly_digest.py`, `.github/workflows/digest.yml`): každý pátek 17:17 (Praha) uloží přehled `/prehled/<rok>-<týden>` (archiv `/prehled`) a pošle Web Push odběratelům tématu `tydenni` (přepínač na /prehled a /slevy). Cron běží v 15:17 i 16:17 UTC, skript sám pustí jen tu správnou pražskou hodinu a je idempotentní. E-mailová verze je připravená, ale vypnutá, dokud není nastaven `EMAIL_BACKEND` (jiný než `log`).

Nové proměnné prostředí:

| Proměnná | Význam |
|---|---|
| `EXTENSION_ORIGINS` | volitelné: čárkami oddělený přesný seznam originů rozšíření pro CORS (po zveřejnění v obchodech – zúží CORS na skutečná ID) |
| `LOVCENY_DNS_FAKEIP_OK` | **jen pro vývoj**: povolí „fake-IP“ rozsah 198.18.0.0/15 z lokálního DNS proxy. V produkci nenastavovat. |
| `EMAIL_BACKEND` (v GitHub Actions) | zapne e-mailový týdenní přehled; bez něj se posílá jen Web Push |

GitHub Actions `digest.yml` potřebuje secrets `DATABASE_URL`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`, `PUBLIC_URL` (a pro e-mail `BREVO_*`). Ruční spuštění s `force` vytvoří přehled kdykoli.

## v7 – Srovnání produktů, Hlídání podle rozpočtu, Black Friday
- **Srovnání produktů** (`/porovnat?p=ID-slug,ID-slug[,ID-slug]`, 2–3 produkty; token může být i jen slug nebo jen ID): vedle sebe aktuální nejnižší cena a obchod, ceny ve všech obchodech, verdikt, nejnižší cena za 30 dní (před dneškem; u štítku „Za 30 dní bylo levněji“ stejné číslo jako u štítku), minimum a medián 90 dní, počet dní sledování, EAN a **klíčové parametry vyčtené jen z názvu produktu** (`lovceny/specs.py` – paměť, úložiště, úhlopříčka, panel, Hz, 5G, kg, otáčky, litry, bar, W, cm, barva; co v názvu není, je „–“, na stránce je poznámka „orientačně, ověř u obchodu“). Graf historie = více čar v jednom SVG (`chart.build_multi`, barva + vzor čáry, přepínač 30 / 90 dní / rok bez JS). Tlačítko **⚖️ Přidat do srovnání** na kartách, v detailu, na /slevy a /black-friday; výběr v `localStorage` (`lovceny_porovnani`, `lovceny_porovnani_slugy`), žádné cookies; sticky lišta „Srovnat (n)“. Sdílení přes Web Share / kopírování odkazu. Stránka je vždy `noindex, follow`, není v sitemap a **není** v robots.txt zakázaná (aby crawler noindex viděl). Staré `/porovnani?ids=` → 301 na `/porovnat?p=`.
- **Hlídání podle rozpočtu** (`lovceny/budget.py`, `/hlidat-rozpocet`, formulář i na každé stránce kategorie): kategorie + volitelné slovo/značka (hledá se v názvu bez diakritiky, všechna slova) + maximální cena (+ „jen skladem“). Kanál Web Push (bez e-mailu); e-mail je připravený (double opt-in, šablony `budget_confirm`, `budget_alert`), ale **vypnutý, dokud není `EMAIL_BACKEND`**. Po každém stažení cen (`fetch_prices.py` → `budget.check_budgets`) se pošle upozornění, když je nejnižší **ověřená** cena (stav ok, ověřeno ≤ 3 dny) ≤ rozpočet. Proti duplicitám tabulka `budget_hits`: produkty v rozpočtu už při založení se uloží jako výchozí stav (ukážeme je hned, nehlásíme), stejný produkt znovu jen při dalším poklesu o ≥ 3 %. Limity: 1 upozornění na hlídání za běh (souhrn „+ N další“), max. 3 denně (pražský den), max. 10 hlídání na prohlížeč / e-mail, zakládání 10/h na IP (jen hash v paměti). Správa bez účtu: `/hlidat-rozpocet/<token>` (shody, změna rozpočtu, zrušení), zrušení jedním klikem `/hlidat-rozpocet/zrusit/<token>` (i z oznámení), seznam „Tvoje hlídání v tomto prohlížeči“ podle push odběru (`POST /api/rozpocet/moje`). Bez JS funguje náhled „Ukázat, co teď do rozpočtu vychází“. Úklid: nepotvrzené e-maily po 7 dnech, hlídání bez upozornění po 12 měsících.
- **Black Friday** (`lovceny/blackfriday.py`, `/black-friday`): datum se počítá (pátek po 4. čtvrtku v listopadu → 2026 = 27. 11., Cyber Monday 30. 11.), odpočet (server + JS po 30 s), souhrn, sekce **Opravdu nejnižší ceny** (dnešní cena = nejnižší za celou dobu sledování, aspoň 30 dní dat, a zároveň Kupuj), seznam všech aktuálních slev (obchod uvádí slevu ≥ 3 %, nebo Kupuj a ≥ 3 % pod mediánem 90 dní; jen ceny ověřené ≤ 3 dny) s verdiktem, nejnižší cenou za 30 dní, obvyklou cenou; filtr kategorie / obchod (GET, filtrované URL jsou `noindex`); vysvětlení § 12a + FAQ (JSON-LD `FAQPage`, `ItemList`, `BreadcrumbList`), v sitemap. Jen naše data, demo body se nepočítají. **Push přehled** (téma `black-friday`, přepínač na stránce, `static/bf.js`): `fetch_prices.py` → `blackfriday.run_digest` pošle v den BF a CM první běh po 7:00 Praha, jednou (zámek v tabulce `topic_sends`). **Banner na úvodu** se ukazuje automaticky 1. 11. – 1. 12.
- **DB**: nové tabulky `budget_watches`, `budget_hits`, `topic_sends` – vytvoří je `init_db()` (create_all) při startu; pro ruční/auditované nasazení je idempotentní SQL `migrations/007_v7_rozpocet_black_friday.sql`.
- Testy: `tests/test_v7_compare.py`, `test_v7_budget.py`, `test_v7_black_friday.py`; axe 0 chyb (desktop + mobil) na všech nových stránkách; snímky `screens/v7-*.png`.

Nové (volitelné) proměnné prostředí v7 – nic není povinné, výchozí hodnoty fungují:

| Proměnná | Význam |
|---|---|
| `BF_BANNER_FROM`, `BF_BANNER_TO` | období banneru na úvodu, `MM-DD` nebo `YYYY-MM-DD` (výchozí `11-01` až `12-01`) |
| `BF_DIGEST_DAYS` | dny Black Friday push přehledu, čárkami: `bf`, `cm` nebo datum (výchozí `bf,cm`) |
| `BF_DIGEST_HOUR` | od kolika hodin (Praha) se přehled smí poslat (výchozí `7`) |

Workflow se neměnily: hlídání rozpočtu i Black Friday přehled běží v existujícím `fetch.yml` (používá už nastavené `DATABASE_URL`, `VAPID_*`, `EMAIL_BACKEND`, `PUBLIC_URL`).

## Lokální spuštění
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export DATABASE_URL=postgresql://lovceny:lovceny@localhost/lovceny   # bez proměnné se použije SQLite ./lovceny.db
.venv/bin/python seed.py               # produkty + demo historie
.venv/bin/python fetch_prices.py       # aktualizace cen
.venv/bin/uvicorn lovceny.main:app --reload   # http://localhost:8000
.venv/bin/python -m pytest -q
```
Proměnné pro v3: `ADMIN_TOKEN` (min. 16 znaků, jinak je /admin vypnutý), volitelně `IMG_CACHE_DIR`, `IMG_CACHE_MAX_MB`.

Tabulky se vytvoří automaticky (`create_all`). Pro další vývoj doporučujeme přidat Alembic migrace.

## Nasazení zdarma
1. **GitHub**: vytvořte soukromý repozitář a pushněte (`git remote add origin … && git push -u origin main`).
2. **Neon** (neon.tech): nový projekt, region Frankfurt, databáze `lovceny`. Zkopírujte *connection string* (`postgresql://…?sslmode=require`).
   Naplnění: GitHub → Actions → *Naplnit databázi (katalog produktů)* → *Run workflow* (idempotentní, lze spustit opakovaně).
3. **Render** (render.com): *New → Blueprint* → vyberte repozitář (načte `render.yaml`). Vyplňte `DATABASE_URL` (Neon), `BREVO_API_KEY`, `EMAIL_FROM`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` (+ `VAPID_SUBJECT`), volitelně `GEMINI_API_KEY`. Free instance po 15 min nečinnosti usíná, první načtení trvá ~1 min (viz „Aby Render neusínal“).
4. **GitHub Actions secrets** (Settings → Secrets and variables → Actions): `DATABASE_URL` (Neon), `EMAIL_BACKEND` = `brevo`, `BREVO_API_KEY`, `EMAIL_FROM`; pro upozornění v prohlížeči `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT` (stejné hodnoty jako v Renderu); volitelně `ALZA_FEED_URL`, `DATART_FEED_URL`. Volitelná *variable* `PUBLIC_URL` (výchozí `https://lovceny.onrender.com`). Workflow `.github/workflows/fetch.yml` běží 3× denně (5:17, 11:17, 17:17 UTC), ručně přes *Run workflow*; po stažení posílá upozornění a uklízí stará hlídání.
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
lovceny/budget.py      hlídání podle rozpočtu  lovceny/blackfriday.py  Black Friday  lovceny/specs.py  parametry z názvu
migrations/            idempotentní SQL pro ruční nasazení
lovceny/templates/, static/   UI podle mockupu
fetch_prices.py, seed.py, sample_feeds/, tests/, render.yaml, .github/workflows/fetch.yml
```
