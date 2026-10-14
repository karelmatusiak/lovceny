/* Sdílení bez skriptů třetích stran: nativní Web Share API (mobil), jinak rozbalovací nabídka
   s kopírováním odkazu a odkazy na WhatsApp / Messenger / X / e-mail (fungují i bez JS přes <details>). */
(function () {
  document.querySelectorAll('details.share').forEach(d => {
    const sum = d.querySelector('summary'), input = d.querySelector('input'), msg = d.querySelector('.share-msg');
    const data = {title: d.dataset.shareTitle, text: d.dataset.shareText, url: d.dataset.shareUrl};
    const coarse = matchMedia('(pointer: coarse)').matches;
    if (navigator.share && coarse && (!navigator.canShare || navigator.canShare(data))) {
      sum.addEventListener('click', async e => {
        e.preventDefault();
        try { await navigator.share(data); } catch (err) { if (err && err.name !== 'AbortError') d.open = true; }
      });
    }
    d.querySelector('.share-copy-btn').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(data.url); }
      catch (_) { input.select(); try { document.execCommand('copy'); } catch (__) {} }
      msg.textContent = 'Odkaz zkopírován.';
    });
    d.querySelector('.share-close').addEventListener('click', () => { d.open = false; sum.focus(); });
    input.addEventListener('focus', () => input.select());
    d.addEventListener('keydown', e => { if (e.key === 'Escape' && d.open) { d.open = false; sum.focus(); } });
    document.addEventListener('click', e => { if (d.open && !d.contains(e.target)) d.open = false; });
  });
})();
