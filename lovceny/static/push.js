/* Hlídání ceny přes upozornění v prohlížeči (Web Push). Bez podpory prohlížeče/klíčů zůstává jen e-mail. */
(function () {
  const box = document.getElementById('push-box');
  if (!box) return;
  const key = box.dataset.vapid, pid = box.dataset.pid;
  const btn = document.getElementById('push-on'), off = document.getElementById('push-off');
  const msg = document.getElementById('push-msg'), tp = document.getElementById('tp');
  const stock = document.querySelector('input[name=only_in_stock]');
  const store = 'lovceny-push';
  const saved = () => { try { return JSON.parse(localStorage.getItem(store) || '{}'); } catch (_) { return {}; } };
  const save = o => { try { localStorage.setItem(store, JSON.stringify(o)); } catch (_) {} };
  const say = (t, bad) => { msg.textContent = t; msg.className = 'push-msg' + (bad ? ' bad' : ' good'); };
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window && key;
  const pwa = window.lovcenyPwa || {};
  if (!supported) {
    // iPhone/iPad: Web Push jen v aplikaci přidané na plochu (iOS 16.4+) → návod místo „nepodporuje“
    box.querySelector(pwa.ios && !pwa.standalone ? '.push-ios' : '.push-unsupported').hidden = false;
    return;
  }
  box.querySelector('.push-ui').hidden = false;
  function render() {
    const s = saved()[pid];
    btn.hidden = !!s; off.hidden = !s;
    if (s) say(`Upozornění v tomto prohlížeči je zapnuté (cíl ${Number(s.target).toLocaleString('cs-CZ')} Kč).`);
  }
  if (Notification.permission === 'denied') say('Upozornění máš v prohlížeči zakázaná. Povol je v nastavení webu, nebo použij e-mail.', true);
  render();
  function b64(s) {
    const p = '='.repeat((4 - s.length % 4) % 4), raw = atob((s + p).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  }
  btn.addEventListener('click', async () => {
    const target = parseFloat(tp && tp.value);
    if (!(target > 0)) { say('Nejdřív zadej cílovou cenu.', true); tp && tp.focus(); return; }
    btn.disabled = true; say('Čekáme na povolení v prohlížeči…');
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error('Bez povolení upozornění posílat nemůžeme.');
      const reg = await navigator.serviceWorker.register('/sw.js', {scope: '/'});
      await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(key)});
      const r = await fetch('/api/push/odebirat', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({product_id: +pid, target_price: target, only_in_stock: !!(stock && stock.checked), subscription: sub.toJSON()})});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || 'Uložení se nepovedlo, zkus to prosím znovu.');
      const all = saved(); all[pid] = {token: j.token, target}; save(all);
      render(); off.focus();
    } catch (e) {
      say(e.message || 'Něco se nepovedlo.', true);
    } finally { btn.disabled = false; }
  });
  off.addEventListener('click', async () => {
    const s = saved()[pid];
    off.disabled = true;
    try {
      if (s) await fetch('/api/push/zrusit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: s.token})});
      const all = saved(); delete all[pid]; save(all);
      render(); say('Upozornění v prohlížeči pro tento produkt jsme vypnuli.'); btn.focus();
    } finally { off.disabled = false; }
  });
})();
