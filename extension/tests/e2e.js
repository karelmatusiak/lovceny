/* E2E: rozšíření se načte v headless Chromiu a na uložené stránce produktu ukáže odznak.
   Spuštění: NODE_PATH=<node_modules s playwright> node extension/tests/e2e.js [--shot screens/…png]
   API Lovceny nahrazuje malý stub server (ověřuje, co rozšíření posílá). */
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require('playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'onlineshop_detail.html');
const PRODUCT_URL = 'https://www.onlineshop.cz/standardni-pracky-se-susickou/pracka-se-susickou-miele-wtd160-wcs-891247P.html';
const OTHER_URL = 'https://www.onlineshop.cz/kontakty';
const shotArg = process.argv.indexOf('--shot');
const SHOT = shotArg > 0 ? process.argv[shotArg + 1] : null;
const MOBILE = process.argv.includes('--mobile');
const fail = m => { console.error('FAIL', m); process.exit(1); };

(async () => {
  const seen = [];
  const srv = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://x');
    if (u.pathname !== '/api/v1/overit') { res.writeHead(404); return res.end(); }
    seen.push({ q: Object.fromEntries(u.searchParams), origin: req.headers.origin || '', cookie: req.headers.cookie || '' });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok: true, status: 'ok', url: u.searchParams.get('url'), price: +u.searchParams.get('price'),
      tracked: { id: 7, url: 'https://lovceny.onrender.com/produkt/7-x', verdict: { code: 'fake', label: 'Za 30 dní bylo levněji', reason: '' } },
      check30: { min30: 24990, date: '2026-10-02', flag: true, days: 40, label: 'Za 30 dní bylo levněji' },
      lowest_elsewhere: { shop: 'mironet', shop_name: 'Mironet', name: 'Miele WTD160 WCS', price: 24490, url: 'https://www.mironet.cz/x' },
      elsewhere_count: 3, lovceny_url: 'https://lovceny.onrender.com/over-slevu?url=' + encodeURIComponent(u.searchParams.get('url')) }));
  });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  const api = `http://127.0.0.1:${srv.address().port}`;
  const out = fs.mkdtempSync(path.join(os.tmpdir(), 'lovceny-ext-'));
  execFileSync(process.env.PYTHON || 'python3', [path.join(ROOT, 'extension', 'build.py'), '--api', api, '--out', out], { stdio: 'inherit' });
  const dir = path.join(out, 'chrome');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'lovceny-prof-'));
  const ctx = await chromium.launchPersistentContext(profile, {
    channel: 'chromium', headless: true, locale: 'cs-CZ',
    viewport: MOBILE ? { width: 390, height: 844 } : { width: 1366, height: 900 }, deviceScaleFactor: MOBILE ? 3 : 1.5,
    args: [`--disable-extensions-except=${dir}`, `--load-extension=${dir}`] });
  try {
    let [sw] = ctx.serviceWorkers();
    if (!sw) sw = await ctx.waitForEvent('serviceworker', { timeout: 15000 });
    if (!/^chrome-extension:\/\/[a-p]{32}\/background\.js$/.test(sw.url())) fail('service worker: ' + sw.url());
    const html = fs.readFileSync(FIXTURE, 'utf8');
    await ctx.route('https://**/*', route => {
      const u = route.request().url();
      if (u === PRODUCT_URL) return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
      if (u === OTHER_URL) return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<!doctype html><title>Kontakty</title><h1>Kontakty</h1>' });
      return route.abort();
    });
    const page = await ctx.newPage();
    await page.goto(PRODUCT_URL, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#lovceny-badge-host', { state: 'attached', timeout: 15000 });
    const text = await page.evaluate(() => document.getElementById('lovceny-badge-host').shadowRoot.querySelector('.b').innerText);
    for (const want of ['Lovceny', 'Za 30 dní bylo levněji', '24\u00a0990\u00a0Kč', 'Jinde levněji', '24\u00a0490\u00a0Kč', 'Mironet', 'Podrobnosti na Lovceny'])
      if (!text.includes(want)) fail(`odznak neobsahuje „${want}“: ${text}`);
    if (/falešn/i.test(text)) fail('nevhodná formulace');
    const href = await page.evaluate(() => document.getElementById('lovceny-badge-host').shadowRoot.querySelector('a').href);
    if (!href.startsWith('https://lovceny.onrender.com/over-slevu?url=')) fail('odkaz: ' + href);
    const q = seen[0] && seen[0].q;
    if (!q || q.url !== PRODUCT_URL || !q.name.includes('Miele WTD160') || !(+q.price > 0)) fail('dotaz na API: ' + JSON.stringify(seen));
    if (seen[0].cookie) fail('rozšíření poslalo cookies');
    // Origin: Chrome ho u GET na hostitele s host_permissions neposílá; CORS na serveru řeší ostatní případy
    if (SHOT) {
      await page.waitForTimeout(400);
      await page.screenshot({ path: SHOT });
    }
    // zavření
    await page.evaluate(() => document.getElementById('lovceny-badge-host').shadowRoot.querySelector('.x').click());
    if (await page.$('#lovceny-badge-host')) fail('odznak nejde zavřít');
    // stránka bez produktu → žádný odznak ani dotaz
    const n = seen.length;
    const p2 = await ctx.newPage();
    await p2.goto(OTHER_URL, { waitUntil: 'domcontentloaded' });
    await p2.waitForTimeout(1500);
    if (await p2.$('#lovceny-badge-host')) fail('odznak na stránce bez produktu');
    if (seen.length !== n) fail('dotaz na API ze stránky bez produktu');
    console.log('OK rozšíření: service worker', sw.url(), '· odznak:', text.replace(/\s+/g, ' ').trim());
  } finally {
    await ctx.close();
    srv.close();
  }
})().catch(e => fail(e.stack || e));
