# Rozšíření Lovceny – ochrana soukromí

Platnost od 14. 10. 2026. Stejné informace jsou na https://lovceny.onrender.com/ochrana-osobnich-udaju#rozsireni.

**Co rozšíření dělá:** na stránce produktu v e-shopu, který Lovceny podporují (seznam je v oprávněních rozšíření),
přečte z dané stránky jen **adresu produktu, jeho název a cenu** (ze strukturovaných dat schema.org) a pošle je
na veřejné API Lovceny (`/api/v1/overit`). Zpět dostane verdikt, nejnižší cenu za 30 dní (pokud produkt sledujeme)
a nejnižší cenu v dalších obchodech a ukáže malý odznak.

**Co rozšíření nedělá:**
- neběží na žádných jiných webech (žádné oprávnění „všechny weby“, žádné `tabs`, `history`, `cookies`, `storage`),
- nesbírá historii prohlížení, nepoužívá analytiku ani identifikátory, neposílá nic třetím stranám,
- nečte formuláře, přihlášení, košík ani osobní údaje,
- neposílá cookies (požadavky jdou s `credentials: "omit"`).

**Na serveru Lovceny:** dotazy z rozšíření neukládáme do databáze a nezapočítávají se do statistiky návštěvnosti.
Výsledek pro danou adresu produktu držíme nejvýš 15 minut v paměti serveru (cache, aby obchody nebyly zatěžovány).
IP adresu používáme jen jako jednosměrný otisk v paměti pro omezení počtu dotazů (max. 60 za hodinu), nikam ji neukládáme.
Hosting: Render (Frankfurt, EU).

**Odinstalace:** rozšíření odeberete ve správě rozšíření prohlížeče; na našem serveru po něm nic nezůstane.

Kontakt: kontaktní formulář https://lovceny.onrender.com/o-nas#kontakt
