/* Hlídání podle rozpočtu – Web Push. Žádné cookies; seznam vlastních hlídání se načte podle odběru prohlížeče. */
(function () {
  const form = document.getElementById('bw-form');
  if (!form) return;
  const key = form.dataset.vapid;
  if (!key) return;
  const msg = document.getElementById('bw-msg');
  const say = (t, bad) => { msg.textContent = t; msg.className = 'push-msg' + (bad ? ' bad' : ' good'); };
  const pwa = window.lovcenyPwa || {};
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  if (!supported) { const el = form.querySelector(pwa.ios && !pwa.standalone ? '.bw-ios' : '.bw-unsupported'); if (el) el.hidden = false; return; }
  form.querySelector('.bw-push').hidden = false;
  const btn = document.getElementById('bw-go');
  function b64(s) {
    const p = '='.repeat((4 - s.length % 4) % 4), raw = atob((s + p).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  }
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  async function mine() {
    const box = document.getElementById('bw-mine'), list = document.getElementById('bw-mine-list');
    if (!box) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration('/');
      const sub = reg && await reg.pushManager.getSubscription();
      if (!sub) return;
      const r = await fetch('/api/rozpocet/moje', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ endpoint: sub.endpoint }) });
      const j = await r.json();
      if (!j.items || !j.items.length) { box.hidden = true; return; }
      list.innerHTML = j.items.map(i => `<li><a href="${esc(i.manage_url)}">${esc(i.label)}</a> <button type="button" class="btn-link bw-x" data-token="${esc(i.token)}">Zrušit<span class="sr-only"> hlídání ${esc(i.label)}</span></button></li>`).join('');
      box.hidden = false;
    } catch (_) { /* nic */ }
  }
  document.addEventListener('click', async e => {
    const x = e.target.closest('.bw-x');
    if (!x) return;
    x.disabled = true;
    await fetch('/api/rozpocet/zrusit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: x.dataset.token }) }).catch(() => {});
    mine();
  });
  btn.addEventListener('click', async () => {
    const cat = form.elements.category.value, max = form.elements.max_price.value.trim();
    if (!cat) { say('Vyber prosím kategorii.', true); form.elements.category.focus && form.elements.category.focus(); return; }
    if (!max || +max < 100) { say('Zadej maximální cenu (aspoň 100 Kč).', true); form.elements.max_price.focus(); return; }
    btn.disabled = true;
    try {
      say('Čekáme na povolení v prohlížeči…');
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error('Bez povolení upozornění hlídat nemůžeme. Povol je v nastavení webu.');
      const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
      await navigator.serviceWorker.ready;
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(key) });
      const r = await fetch('/api/rozpocet', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: cat, keyword: form.elements.keyword.value, max_price: +max,
          only_in_stock: form.elements.only_in_stock.checked, subscription: sub.toJSON() }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || 'Uložení se nepovedlo, zkus to prosím znovu.');
      msg.className = 'push-msg good';
      msg.innerHTML = `Hotovo – hlídáme <b>${esc(j.label)}</b>. ` + (j.now_matching
        ? `Už teď vychází ${j.now_matching} ${j.now_matching === 1 ? 'produkt' : (j.now_matching < 5 ? 'produkty' : 'produktů')} – <a href="${esc(j.manage_url)}">zobrazit</a>.`
        : `<a href="${esc(j.manage_url)}">Správa hlídání</a>`);
      mine();
    } catch (e) { say(e.message || 'Něco se nepovedlo.', true); }
    finally { btn.disabled = false; }
  });
  mine();
})();
