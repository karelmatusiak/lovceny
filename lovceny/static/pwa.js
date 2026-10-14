/* PWA: registrace service workeru (offline stránka, cache statiky, Web Push) + výzva „Přidat na plochu“.
   Android/desktop Chrome/Edge: nativní dialog (beforeinstallprompt). iPhone/iPad Safari: návod (Sdílet → Přidat na plochu).
   Odmítnutí si pamatujeme 30 dní v localStorage (lovceny-pwa), žádné cookies. */
(function () {
  const KEY = 'lovceny-pwa', DAY = 864e5;
  const ls = () => { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (_) { return {}; } };
  const save = o => { try { localStorage.setItem(KEY, JSON.stringify(Object.assign(ls(), o))); } catch (_) {} };
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const iosSafari = ios && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS|GSA\//.test(ua);
  window.lovcenyPwa = {ios, iosSafari, standalone};

  if ('serviceWorker' in navigator) {
    const reg = () => navigator.serviceWorker.register('/sw.js', {scope: '/'}).catch(() => {});
    if (document.readyState === 'complete') setTimeout(reg, 1500); else addEventListener('load', () => setTimeout(reg, 1500));
  }
  if (standalone) return;

  const box = document.getElementById('pwa-install');
  const triggers = document.querySelectorAll('[data-pwa-open]');
  if (!box) return;
  const btn = box.querySelector('.pwa-yes'), no = box.querySelector('.pwa-no'), steps = box.querySelector('.pwa-ios');
  let deferred = null;
  const st = ls();
  const visits = (st.visits || 0) + 1; save({visits});
  const snoozed = st.dismissed && Date.now() - st.dismissed < 30 * DAY;

  function show(force) {
    if (!force && snoozed) return;
    box.hidden = false;
    if (force) (iosSafari ? box.querySelector('.pwa-title') : btn).focus();
  }
  function hide(dismiss) { box.hidden = true; if (dismiss) save({dismissed: Date.now()}); }
  function enableTriggers() { triggers.forEach(t => { t.hidden = false; t.addEventListener('click', e => { e.preventDefault(); openIos(); show(true); }); }); }
  function openIos() { if (iosSafari) { steps.hidden = false; btn.hidden = true; no.textContent = 'Zavřít'; } }

  if (iosSafari) {
    box.classList.add('is-ios');
    box.querySelector('.pwa-text').textContent = 'Otevřeš hlídané ceny jedním klepnutím a na iPhonu ti pak budou chodit i upozornění na pokles ceny.';
    btn.textContent = 'Jak přidat na plochu';
    btn.addEventListener('click', openIos);
    enableTriggers();
    if (visits >= 2) show(false);
  } else if (ios) {
    triggers.forEach(t => { t.hidden = false; t.addEventListener('click', e => { e.preventDefault(); alert('Na iPhonu otevři Lovceny v Safari, klepni na Sdílet a zvol „Přidat na plochu“.'); }); });
  }
  addEventListener('beforeinstallprompt', e => {
    e.preventDefault(); deferred = e;
    enableTriggers();
    if (visits >= 2) show(false);
  });
  btn.addEventListener('click', async () => {
    if (!deferred) return;
    deferred.prompt();
    const r = await deferred.userChoice.catch(() => ({}));
    deferred = null;
    hide(r.outcome !== 'accepted');
  });
  no.addEventListener('click', () => hide(true));
  box.addEventListener('keydown', e => { if (e.key === 'Escape') hide(true); });
  addEventListener('appinstalled', () => { hide(false); triggers.forEach(t => t.hidden = true); });
})();
