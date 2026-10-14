/* Black Friday: odpočet (aktualizace po minutě, bez nervózního blikání) + přepínač odběru tématu (Web Push). */
(function () {
  const c = document.getElementById('bf-count');
  if (c && c.dataset.bf) {
    const end = new Date(c.dataset.bf).getTime();
    const set = (u, v) => { const el = c.querySelector('[data-u="' + u + '"]'); if (el) el.textContent = v; };
    const tick = () => {
      const ms = end - Date.now();
      if (ms <= 0) return;
      set('d', Math.floor(ms / 864e5)); set('h', Math.floor(ms / 36e5) % 24); set('m', Math.floor(ms / 6e4) % 60);
    };
    tick(); setInterval(tick, 30000);
  }
  document.querySelectorAll('.topic-optin').forEach(box => {
    const key = box.dataset.vapid, topic = box.dataset.topic, store = box.dataset.store;
    if (!key) return;
    const pwa = window.lovcenyPwa || {};
    const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    if (!supported) { box.querySelector(pwa.ios && !pwa.standalone ? '.dg-ios' : '.dg-unsupported').hidden = false; return; }
    const ui = box.querySelector('.dg-ui'), btn = box.querySelector('.dg-toggle');
    const state = box.querySelector('.t-state'), msg = box.querySelector('.t-msg');
    const get = () => { try { return JSON.parse(localStorage.getItem(store) || 'null'); } catch (_) { return null; } };
    const put = v => { try { v ? localStorage.setItem(store, JSON.stringify(v)) : localStorage.removeItem(store); } catch (_) {} };
    const say = (t, bad) => { msg.textContent = t; msg.className = 'push-msg t-msg' + (bad ? ' bad' : ' good'); };
    const b64 = s => { const p = '='.repeat((4 - s.length % 4) % 4), raw = atob((s + p).replace(/-/g, '+').replace(/_/g, '/')); return Uint8Array.from(raw, ch => ch.charCodeAt(0)); };
    const render = () => { const on = !!get(); btn.setAttribute('aria-checked', on ? 'true' : 'false'); state.textContent = on ? 'Zapnuto' : 'Vypnuto'; };
    ui.hidden = false; render();
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const cur = get();
        if (cur) {
          await fetch('/api/push/tema/zrusit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: cur.token }) });
          put(null); render(); say('Vypnuto. Nic posílat nebudeme.'); return;
        }
        say('Čekáme na povolení v prohlížeči…');
        if (await Notification.requestPermission() !== 'granted') throw new Error('Bez povolení upozornění nemůžeme nic poslat.');
        const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
        await navigator.serviceWorker.ready;
        const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
        const r = await fetch('/api/push/tema', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic, subscription: sub.toJSON() }) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.detail || 'Uložení se nepovedlo, zkus to prosím znovu.');
        put({ token: j.token }); render(); say('Hotovo – ozveme se ráno v den akce.');
      } catch (e) { say(e.message || 'Něco se nepovedlo.', true); }
      finally { btn.disabled = false; }
    });
  });
})();
