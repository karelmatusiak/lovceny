/* PWA: service worker + nabídka „Přidat na plochu“.
   Automaticky:
   - ne v nainstalované aplikaci (standalone)
   - ne při prvním příchodu (první relace prohlížeče)
   - od druhé relace nejvýš jednou za relaci
   - až po zájmu: 20 s na stránce, nebo první posun
   - jen když instalace dává smysl (beforeinstallprompt, nebo návod v iOS Safari)
   „Teď ne“, Zavřít a Escape = 30 dní klid v localStorage (lovceny-pwa).
   „Na plochu“ v patičce nabídku otevře i v tom klidu.
   Úspěch instalace nehlásíme; lištu schováme při appinstalled.
   Když úložiště nejde zapsat, automaticky neukazujeme. Žádné cookies. */
(function (root) {
  const KEY = 'lovceny-pwa';
  const SESSION = 'lovceny-pwa-session';
  const PROMPTED = 'lovceny-pwa-prompted';
  const COOLDOWN_MS = 30 * 864e5;
  const ENGAGE_MS = 20000;

  function shouldAutoShow(s) {
    if (!s || s.standalone || !s.storageOk || !s.installReady) return false;
    if ((s.sessions || 0) < 2) return false;
    if (s.promptedThisSession || !s.engaged) return false;
    if (s.dismissedAt && (s.now - s.dismissedAt) < COOLDOWN_MS) return false;
    return true;
  }

  root.lovcenyPwaPolicy = {shouldAutoShow, COOLDOWN_MS, ENGAGE_MS, KEY};
  if (!root.document || !root.document.getElementById) return;

  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const iosSafari = ios && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|GSA\//.test(ua);

  function storageGet(store, key) {
    try { return store.getItem(key); } catch (_) { return null; }
  }
  function storageSet(store, key, value) {
    try { store.setItem(key, value); return true; } catch (_) { return false; }
  }
  function storageOk(store) {
    try {
      const probe = KEY + '-probe';
      store.setItem(probe, '1');
      store.removeItem(probe);
      return true;
    } catch (_) { return false; }
  }
  const persist = storageOk(localStorage);
  const sessionOk = storageOk(sessionStorage);

  function load() {
    if (!persist) return {};
    try {
      const data = JSON.parse(localStorage.getItem(KEY) || '{}');
      return data && typeof data === 'object' ? data : {};
    } catch (_) { return {}; }
  }
  function save(patch) {
    if (!persist) return false;
    try {
      localStorage.setItem(KEY, JSON.stringify(Object.assign(load(), patch)));
      return true;
    } catch (_) { return false; }
  }

  if ('serviceWorker' in navigator) {
    const reg = () => navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {});
    if (document.readyState === 'complete') setTimeout(reg, 1500);
    else addEventListener('load', () => setTimeout(reg, 1500));
  }

  if (persist && sessionOk && storageGet(sessionStorage, SESSION) !== '1') {
    const st = load();
    if (save({sessions: (st.sessions || 0) + 1})) storageSet(sessionStorage, SESSION, '1');
  }

  const box = document.getElementById('pwa-install');
  const triggers = document.querySelectorAll('[data-pwa-open]');
  root.lovcenyPwa = {ios, iosSafari, standalone, shouldAutoShow, COOLDOWN_MS, ENGAGE_MS};
  if (standalone || !box) {
    triggers.forEach(t => { t.hidden = true; });
    return;
  }

  const btn = box.querySelector('.pwa-yes');
  const steps = box.querySelector('.pwa-ios');
  const closeBtn = box.querySelector('.pwa-close');
  let deferred = null;
  let engaged = false;
  let triggersOn = false;

  function promptedThisSession() {
    return storageGet(sessionStorage, PROMPTED) === '1';
  }
  function markPrompted() {
    storageSet(sessionStorage, PROMPTED, '1');
  }
  function snoozed(now) {
    const st = load();
    return !!(st.dismissed && (now || Date.now()) - st.dismissed < COOLDOWN_MS);
  }

  function show(force) {
    box.hidden = false;
    if (force && closeBtn) closeBtn.focus();
  }
  function hide(dismiss) {
    box.hidden = true;
    markPrompted();
    if (dismiss) save({dismissed: Date.now()});
  }
  function openIos() {
    if (!iosSafari) return;
    steps.hidden = false;
    btn.hidden = true;
    const no = box.querySelector('.pwa-no');
    if (no) no.textContent = 'Zavřít';
  }
  function enableTriggers() {
    triggers.forEach(t => { t.hidden = false; });
    if (triggersOn) return;
    triggersOn = true;
    triggers.forEach(t => t.addEventListener('click', e => {
      e.preventDefault();
      openIos();
      show(true);
    }));
  }
  function attemptAuto() {
    const st = load();
    if (!shouldAutoShow({
      storageOk: persist && sessionOk,
      standalone: false,
      installReady: !!deferred || iosSafari,
      sessions: st.sessions || 0,
      promptedThisSession: promptedThisSession(),
      dismissedAt: st.dismissed || 0,
      now: Date.now(),
      engaged,
    })) return;
    show(false);
    markPrompted();
  }

  function armEngagement() {
    if (!persist || !sessionOk) return;
    const st = load();
    if ((st.sessions || 0) < 2 || snoozed()) return;
    let done = false;
    const go = () => { if (done) return; done = true; engaged = true; attemptAuto(); };
    setTimeout(go, ENGAGE_MS);
    addEventListener('scroll', go, {passive: true, once: true});
  }

  function onBeforeInstallPrompt(e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    deferred = e;
    enableTriggers();
    attemptAuto();
  }
  function onAppInstalled() {
    deferred = null;
    hide(false);
    triggers.forEach(t => { t.hidden = true; });
  }

  if (iosSafari) {
    box.classList.add('is-ios');
    box.querySelector('.pwa-text').textContent = 'Otevřeš hlídané ceny jedním klepnutím a na iPhonu ti pak budou chodit i upozornění na pokles ceny.';
    btn.textContent = 'Jak přidat na plochu';
    btn.addEventListener('click', openIos);
    enableTriggers();
  } else if (ios) {
    triggers.forEach(t => {
      t.hidden = false;
      t.addEventListener('click', e => {
        e.preventDefault();
        alert('Na iPhonu otevři Lovceny v Safari, klepni na Sdílet a zvol „Přidat na plochu“.');
      });
    });
  }
  addEventListener('beforeinstallprompt', onBeforeInstallPrompt);
  btn.addEventListener('click', async () => {
    if (iosSafari && !deferred) { openIos(); return; }
    if (!deferred || typeof deferred.prompt !== 'function') return;
    const pending = deferred;
    try {
      await pending.prompt();
      const choice = pending.userChoice ? await pending.userChoice : {};
      if (deferred === pending) deferred = null;
      hide(choice.outcome !== 'accepted');
    } catch (_) { /* dialog selhal – úspěch nehlásíme a lištu neschováváme */ }
  });
  box.querySelectorAll('.pwa-no, .pwa-close').forEach(b => b.addEventListener('click', () => hide(true)));
  box.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); hide(true); } });
  addEventListener('appinstalled', onAppInstalled);
  armEngagement();

  root.lovcenyPwa.onBeforeInstallPrompt = onBeforeInstallPrompt;
  root.lovcenyPwa.onAppInstalled = onAppInstalled;
  root.lovcenyPwa.attemptAuto = function () { engaged = true; attemptAuto(); };
})(typeof window !== 'undefined' ? window : globalThis);
