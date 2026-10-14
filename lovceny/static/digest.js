/* Týdenní přehled slev – přepínač odběru (Web Push téma „tydenni“). Odděleno od hlídání cen produktů. */
(function () {
  const box = document.getElementById('prehled-optin');
  if (!box) return;
  const key = box.dataset.vapid;
  if (!key) return;
  const pwa = window.lovcenyPwa || {};
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  if (!supported) { box.querySelector(pwa.ios && !pwa.standalone ? '.dg-ios' : '.dg-unsupported').hidden = false; return; }
  const ui = box.querySelector('.dg-ui'), btn = document.getElementById('dg-toggle');
  const state = document.getElementById('dg-state'), msg = document.getElementById('dg-msg');
  const store = 'lovceny-prehled';
  const get = () => { try { return JSON.parse(localStorage.getItem(store) || 'null'); } catch (_) { return null; } };
  const set = v => { try { v ? localStorage.setItem(store, JSON.stringify(v)) : localStorage.removeItem(store); } catch (_) {} };
  const say = (t, bad) => { msg.textContent = t; msg.className = 'push-msg' + (bad ? ' bad' : ' good'); };
  ui.hidden = false;
  function render() {
    const on = !!get();
    btn.setAttribute('aria-checked', on ? 'true' : 'false');
    state.textContent = on ? 'Zapnuto' : 'Vypnuto';
  }
  function b64(s) {
    const p = '='.repeat((4 - s.length % 4) % 4), raw = atob((s + p).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  }
  render();
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const cur = get();
      if (cur) {
        await fetch('/api/push/tema/zrusit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: cur.token }) });
        set(null); render(); say('Týdenní přehled je vypnutý.');
        return;
      }
      say('Čekáme na povolení v prohlížeči…');
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error('Bez povolení upozornění přehled posílat nemůžeme.');
      const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
      const r = await fetch('/api/push/tema', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic: 'tydenni', subscription: sub.toJSON() }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || 'Uložení se nepovedlo, zkus to prosím znovu.');
      set({ token: j.token }); render(); say('Hotovo – přehled přijde v pátek.');
    } catch (e) { say(e.message || 'Něco se nepovedlo.', true); }
    finally { btn.disabled = false; }
  });
})();
