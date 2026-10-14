/* Srovnání produktů: výběr v localStorage (žádné cookies), max. 3. Sticky lišta „⚖️ Srovnat (n)“.
   Sdílitelná adresa: /porovnat?p=ID-slug,ID-slug – URL je zdroj pravdy. */
(function () {
  var K = 'lovceny_porovnani', KS = 'lovceny_porovnani_slugy', MAX = 3;
  function get() { try { return (JSON.parse(localStorage.getItem(K)) || []).map(Number).filter(Boolean).slice(0, MAX); } catch (e) { return []; } }
  function slugs() { try { return JSON.parse(localStorage.getItem(KS)) || {}; } catch (e) { return {}; } }
  function set(l) {
    l = l.slice(0, MAX);
    try {
      localStorage.setItem(K, JSON.stringify(l));
      var s = slugs(), keep = {}; l.forEach(function (id) { if (s[id]) keep[id] = s[id]; });
      localStorage.setItem(KS, JSON.stringify(keep));
    } catch (e) {}
    render();
  }
  function remember(id, slug) { if (!slug) return; try { var s = slugs(); s[id] = slug; localStorage.setItem(KS, JSON.stringify(s)); } catch (e) {} }
  function url(l) { var s = slugs(); return '/porovnat' + (l.length ? '?p=' + l.map(function (id) { return id + (s[id] ? '-' + s[id] : ''); }).join(',') : ''); }
  function say(t) { var h = document.getElementById('cmp-hint'); if (h) h.textContent = t; }
  function toggle(b) {
    var id = +b.dataset.cmpId, l = get(), i = l.indexOf(id);
    if (i >= 0) l.splice(i, 1);
    else if (l.length >= MAX) { set(l); say('Srovnat jde nejvýš ' + MAX + ' produkty – nejdřív nějaký odeber.'); return; }
    else { remember(id, b.dataset.cmpSlug); l.push(id); }
    set(l);
  }
  function render() {
    var l = get();
    document.querySelectorAll('[data-cmp-id]').forEach(function (b) {
      var on = l.indexOf(+b.dataset.cmpId) >= 0;
      b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.classList.toggle('on', on);
      var t = b.querySelector('.cmp-txt'); if (t) t.textContent = on ? 'Ve srovnání ✓' : 'Přidat do srovnání';
    });
    var bar = document.getElementById('cmp-bar'); if (!bar) return;
    var onPage = !!document.getElementById('cmp-page');
    bar.hidden = l.length === 0 || onPage;
    document.getElementById('cmp-n').textContent = l.length;
    var go = document.getElementById('cmp-go'); go.href = url(l);
    go.classList.toggle('disabled', l.length < 2); go.setAttribute('aria-disabled', l.length < 2 ? 'true' : 'false');
    say(l.length < 2 ? 'Přidej ještě aspoň 1 produkt' : (l.length >= MAX ? 'Maximum ' + MAX + ' produkty' : ''));
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-cmp-id]');
    if (b) { e.preventDefault(); toggle(b); return; }
    var r = e.target.closest('[data-cmp-remove]');
    if (r) { var l = get().filter(function (x) { return x !== +r.dataset.cmpRemove; }); set(l); location.href = url(l); return; }
    if (e.target.id === 'cmp-clear') { set([]); return; }
    var go = e.target.closest('#cmp-go');
    if (go && get().length < 2) { e.preventDefault(); }
  });
  // Stránka srovnání: ?p= je zdroj pravdy (sdílený odkaz), ?pridat=ID přidá právě sledovaný produkt k výběru.
  var page = document.getElementById('cmp-page');
  if (page) {
    var toks = (page.dataset.tokens || '').split(',').filter(Boolean), add = +page.dataset.pridat;
    toks.forEach(function (t) { var m = t.match(/^(\d+)-(.+)$/); if (m) remember(+m[1], m[2]); });
    var ids = toks.map(function (t) { return parseInt(t, 10); }).filter(Boolean);
    if (add) {
      var merged = get(); ids.forEach(function (i) { if (merged.indexOf(i) < 0) merged.push(i); });
      if (merged.indexOf(add) < 0) merged.push(add);
      merged = merged.slice(-MAX); set(merged); location.replace(url(merged));
    } else if (ids.length) set(ids);
    else if (get().length) location.replace(url(get()));
  }
  window.lovcenyCmp = { render: render, get: get, url: url };
  if (document.readyState !== 'loading') render(); else document.addEventListener('DOMContentLoaded', render);
})();
