/* Lovceny – service worker jen pro Web Push upozornění „cena klesla“. Nic necachuje. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {body: e.data && e.data.text()}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Lovceny: cena klesla', {
    body: d.body || '', tag: d.tag || 'lovceny', renotify: true, lang: 'cs',
    icon: '/static/brand/apple-touch-icon.png', badge: '/static/brand/favicon-32.png',
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
