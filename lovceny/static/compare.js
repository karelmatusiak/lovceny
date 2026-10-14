/* Výběr produktů k porovnání: localStorage, max. 4. Sticky lišta „⚖️ Porovnat (n)“. */
(function () {
  var K = 'lovceny_porovnani', MAX = 4;
  function get() { try { return (JSON.parse(localStorage.getItem(K)) || []).map(Number).filter(Boolean).slice(0, MAX); } catch (e) { return []; } }
  function set(l) { try { localStorage.setItem(K, JSON.stringify(l.slice(0, MAX))); } catch (e) {} render(); }
  function url(l) { return '/porovnani?ids=' + l.join(','); }
  function toggle(id) {
    var l = get(), i = l.indexOf(id);
    if (i >= 0) l.splice(i, 1);
    else if (l.length >= MAX) { alert('Porovnat lze najednou nejvýš ' + MAX + ' produkty. Nejdřív nějaký odeber.'); return; }
    else l.push(id);
    set(l);
  }
  function render() {
    var l = get();
    document.querySelectorAll('[data-cmp-id]').forEach(function (b) {
      var on = l.indexOf(+b.dataset.cmpId) >= 0;
      b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.classList.toggle('on', on);
      var t = b.querySelector('.cmp-txt'); if (t) t.textContent = on ? 'V porovnání ✓' : 'Porovnat';
    });
    var bar = document.getElementById('cmp-bar'); if (!bar) return;
    var onPage = !!document.getElementById('cmp-page');
    bar.hidden = l.length === 0 || onPage;
    document.getElementById('cmp-n').textContent = l.length;
    var go = document.getElementById('cmp-go'); go.href = url(l);
    go.classList.toggle('disabled', l.length < 2); go.setAttribute('aria-disabled', l.length < 2 ? 'true' : 'false');
    document.getElementById('cmp-hint').textContent = l.length < 2 ? 'Přidej ještě aspoň 1 produkt' : (l.length >= MAX ? 'Maximum ' + MAX + ' produkty' : '');
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-cmp-id]');
    if (b) { e.preventDefault(); toggle(+b.dataset.cmpId); return; }
    var r = e.target.closest('[data-cmp-remove]');
    if (r) { var l = get().filter(function (x) { return x !== +r.dataset.cmpRemove; }); set(l); location.href = url(l); return; }
    if (e.target.id === 'cmp-clear') { set([]); return; }
    if (e.target.id === 'cmp-go' && get().length < 2) { e.preventDefault(); }
    if (e.target.id === 'cmp-copy') {
      var done = function () { e.target.textContent = 'Zkopírováno ✓'; };
      if (navigator.clipboard) navigator.clipboard.writeText(location.href).then(done, done); else done();
    }
  });
  // Stránka porovnání: URL ?ids= je zdroj pravdy (sdílený odkaz), ?pridat=ID přidá právě sledovaný produkt k výběru.
  var page = document.getElementById('cmp-page');
  if (page) {
    var ids = (page.dataset.ids || '').split(',').map(Number).filter(Boolean), add = +page.dataset.pridat;
    if (add) {
      var merged = get(); if (merged.indexOf(add) < 0) merged.push(add);
      merged = merged.slice(-MAX); set(merged); location.replace(url(merged));
    } else if (ids.length) set(ids);
    else if (get().length) location.replace(url(get()));
  }
  window.lovcenyCmp = { render: render, get: get };
  if (document.readyState !== 'loading') render(); else document.addEventListener('DOMContentLoaded', render);
})();
