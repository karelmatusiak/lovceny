/* Lovceny – service worker: Web Push upozornění „cena klesla“ + PWA (offline stránka, cache statických souborů).
   Verze: {{ version }} (mění se se změnou statických souborů → prohlížeč SW aktualizuje).
   Zásady: HTML stránky, hledání a /api/ NIKDY necachujeme (ceny musí být čerstvé) – jen při výpadku sítě
   ukážeme offline stránku. Verzované /static/ (?v=hash) a zmenšené obrázky /img/ jsou cache-first. */
const VERSION = '{{ version }}';
const STATIC_CACHE = 'lovceny-static-' + VERSION;
const IMG_CACHE = 'lovceny-img-v1';
const IMG_MAX = 120;
const OFFLINE_URL = '/offline';
const PRECACHE = {{ precache|tojson }};

self.addEventListener('install', e => {
  e.waitUntil(caches.open(STATIC_CACHE).then(c => c.addAll([OFFLINE_URL, ...PRECACHE])).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys
    .filter(k => k.startsWith('lovceny-') && k !== STATIC_CACHE && k !== IMG_CACHE)
    .map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

async function trimImages() {
  const c = await caches.open(IMG_CACHE), keys = await c.keys();
  for (let i = 0; i < keys.length - IMG_MAX; i++) await c.delete(keys[i]);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;               // obrázky obchodů apod. – nechat prohlížeči
  if (req.mode === 'navigate') {                                  // stránky: vždy síť, offline → záložní stránka
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }
  if (url.pathname.startsWith('/static/') && url.searchParams.has('v')) {   // verzované = neměnné
    e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(STATIC_CACHE).then(c => c.put(req, copy)); }
      return res;
    })));
    return;
  }
  if (url.pathname.startsWith('/img/')) {                         // zmenšené obrázky produktů
    e.respondWith(caches.open(IMG_CACHE).then(c => c.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok && res.type === 'basic') { c.put(req, res.clone()); trimImages(); }
      return res;
    }))));
  }
  // /api/, /hledat, /hledat/zive, sitemap… → bez zásahu (síť)
});

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {body: e.data && e.data.text()}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Lovceny: cena klesla', {
    body: d.body || '', tag: d.tag || 'lovceny', renotify: true, lang: 'cs',
    icon: '/static/brand/icon-192.png', badge: '/static/brand/favicon-32.png',
    data: {url: d.url || '/', unsubscribe: d.unsubscribe || ''},
    actions: d.unsubscribe ? [{action: 'open', title: 'Zobrazit cenu'}, {action: 'stop', title: 'Přestat hlídat'}] : []
  }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const d = e.notification.data || {};
  const url = e.action === 'stop' && d.unsubscribe ? d.unsubscribe : d.url;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) if (c.url === url && 'focus' in c) return c.focus();
    return self.clients.openWindow(url);
  }));
});
