/* Lovceny – background (Chrome: service worker, Firefox: background script).
   Jediná síťová komunikace rozšíření: dotaz na veřejné API Lovceny pro právě otevřený produkt.
   Žádné sledování, žádná analytika, nic se neukládá mimo krátkou cache v paměti. */
if (typeof LOVCENY_API === 'undefined' && typeof importScripts === 'function') importScripts('config.js');
const ext = globalThis.browser || globalThis.chrome;
const CACHE_MS = 15 * 60 * 1000;
const cache = new Map();

async function overit(msg) {
  const q = new URLSearchParams({ url: String(msg.url || '').slice(0, 2000) });
  if (msg.name) q.set('name', String(msg.name).slice(0, 200));
  if (msg.price > 0) q.set('price', String(msg.price));
  if (msg.original > 0) q.set('original', String(msg.original));
  const key = q.toString();
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < CACHE_MS) return hit.data;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const r = await fetch(`${LOVCENY_API}/api/v1/overit?${key}`, {
      signal: ctrl.signal, credentials: 'omit', cache: 'default', headers: { Accept: 'application/json' } });
    const data = await r.json().catch(() => ({ ok: false }));
    if (!r.ok) return { ok: false, error: data.error || `HTTP ${r.status}` };
    cache.set(key, { t: Date.now(), data });
    if (cache.size > 200) cache.delete(cache.keys().next().value);
    return data;
  } catch (e) {
    return { ok: false, error: 'Lovceny teď neodpovídají.' };
  } finally { clearTimeout(timer); }
}

ext.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== 'lovceny-overit') return false;
  overit(msg).then(sendResponse);
  return true; // odpověď asynchronně
});

// Klik na ikonu rozšíření: otevře Ověř slevu pro aktuální stránku (activeTab = URL jen po kliknutí).
const action = ext.action || ext.browserAction;
if (action && action.onClicked) {
  action.onClicked.addListener(tab => {
    const u = tab && tab.url && /^https?:/.test(tab.url) ? `${LOVCENY_API}/over-slevu?url=${encodeURIComponent(tab.url)}` : `${LOVCENY_API}/rozsireni`;
    ext.tabs.create({ url: u });
  });
}
