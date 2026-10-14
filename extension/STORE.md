# Lovceny – texty pro obchody s rozšířeními

Verze 0.1.0 · balíčky: `python extension/build.py` → `extension/dist/lovceny-chrome-0.1.0.zip`, `lovceny-firefox-0.1.0.zip`
(stejné zipy stahuje i stránka https://lovceny.onrender.com/rozsireni). Ikony: `lovceny/static/brand/icon-512.png` (128 px se generuje).
Zásady ochrany soukromí (URL do formulářů): https://lovceny.onrender.com/ochrana-osobnich-udaju#rozsireni
Kontakt pro recenzenty: kontaktní formulář https://lovceny.onrender.com/o-nas#kontakt (soukromý e-mail neuvádět – po založení použít ahoj@lovceny.cz).

---

## Chrome Web Store

**Název:** Lovceny – ověř slevu

**Krátký popis (max. 132 znaků):**
Na stránce produktu v e-shopu ukáže nejnižší cenu za 30 dní, verdikt Kupuj/Počkej a nejlevnější nabídku jinde.

**Kategorie:** Nakupování · **Jazyk:** čeština

**Podrobný popis:**
Lovceny je poctivý hlídač cen elektroniky a spotřebičů z českých e-shopů. Rozšíření ti ukáže fakta přímo na stránce produktu – nemusíš nic kopírovat.

Co uvidíš v malém odznaku v rohu stránky:
• verdikt podle historie ceny: Kupuj (cena u 90denního minima), Počkej, nebo „Za 30 dní bylo levněji“, když se sleva počítá z vyšší ceny, než jakou jsme za posledních 30 dní naměřili (logika § 12a zákona o ochraně spotřebitele),
• nejnižší cenu za posledních 30 dní s datem – pokud produkt sledujeme,
• nejlevnější nabídku stejného produktu v dalších obchodech,
• odkaz na podrobné ověření na Lovceny s grafem ceny a hlídáním ceny.

Pořadí nabídek je vždy jen podle ceny, nikdo nám neplatí za umístění. Verdikt je algoritmický odhad z naměřených cen, ne záruka – rozhodující je vždy cena v e-shopu.

Soukromí na prvním místě:
• běží jen v obchodech, které Lovceny podporují – jinde nemá přístup,
• posílá jen adresu produktu, jeho název a cenu ze stránky, kterou máš otevřenou,
• žádná analytika, žádné cookies, žádné identifikátory, nic se neprodává ani nepředává třetím stranám.

Odznak jde zavřít křížkem. Kliknutím na ikonu rozšíření otevřeš ověření aktuální stránky na Lovceny.

**Jediný účel (Single purpose):**
Zobrazit na stránce produktu v podporovaném e-shopu historii a srovnání ceny z Lovceny (verdikt, nejnižší cena za 30 dní, nejlevnější nabídka jinde).

**Zdůvodnění oprávnění:**
- *Přístup k hostitelům (podporované e-shopy):* content script na stránce produktu přečte ze strukturovaných dat (schema.org Product) název a cenu a ukáže odznak. Seznam domén je omezený na obchody, které Lovceny sledují.
- *Přístup k lovceny.onrender.com / lovceny.cz:* dotaz na veřejné API Lovceny (`/api/v1/overit`).
- *activeTab:* po kliknutí na ikonu rozšíření otevře ověření adresy aktuální karty na webu Lovceny. Bez kliknutí se adresa nepoužije.
- *Vzdálený kód:* nepoužíváme. Všechen JavaScript je součástí balíčku.

**Údaje (Privacy practices):**
- Shromažďované údaje: *Aktivita na webu* – jen adresa (URL) právě otevřené stránky produktu v podporovaném obchodě a *Obsah webu* – název a cena produktu z této stránky. Slouží výhradně k zobrazení výsledku uživateli.
- Neprodáváme údaje třetím stranám · nepoužíváme je k účelům nesouvisejícím s jediným účelem · nepoužíváme je k hodnocení úvěruschopnosti.

**Snímky obrazovky (1280×800):** odznak na stránce produktu (`screens/v6-rozsireni-odznak-desktop.png`), stránka Ověř slevu (`screens/v6-over-slevu-vysledek-desktop.png`), stránka /rozsireni.

---

## Firefox Add-ons (addons.mozilla.org)

**Název:** Lovceny – ověř slevu · **ID:** `rozsireni@lovceny.cz` · **Minimální verze:** Firefox 140

**Shrnutí (max. 250 znaků):**
Poctivý hlídač cen z českých e-shopů přímo na stránce produktu: verdikt Kupuj/Počkej, nejnižší cena za posledních 30 dní a nejlevnější nabídka v dalších obchodech. Bez analytiky a cookies, jen na podporovaných obchodech.

**Popis:** stejný jako podrobný popis pro Chrome Web Store (výše).

**Kategorie:** Nakupování · **Značky:** ceny, slevy, nakupování, srovnání cen, čeština

**Sběr dat (data_collection_permissions, povinné):** `browsingActivity` (URL stránky produktu), `websiteContent` (název a cena produktu). Zobrazí se uživateli při instalaci.

**Licence:** Všechna práva vyhrazena (lze změnit na MIT, pokud bude zdrojový kód veřejný).

**Zásady ochrany soukromí:** text `extension/PRIVACY.md` (vložit do pole „Zásady ochrany osobních údajů“).

**Poznámky pro recenzenty:**
Rozšíření nemá žádný build krok kromě `build.py`, který vloží ikonu (SVG) do content.js, adresu API do config.js a seznam domén do manifestu. Kód není minifikovaný. Pro test otevřete například stránku produktu na https://www.onlineshop.cz/ nebo https://www.mironet.cz/ – vpravo dole se ukáže odznak. API je veřejné, bez přihlášení.
