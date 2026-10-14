/* Lovceny – content script. Běží jen na stránkách podporovaných obchodů (host permissions v manifestu).
   Pozná stránku produktu (schema.org Product v JSON-LD / microdata), pošle URL + název + cenu
   na API Lovceny a ukáže malý odznak. Nic jiného ze stránky nečte ani neposílá. */
(function () {
  if (window.__lovcenyBadge) return;
  window.__lovcenyBadge = true;
  const ext = globalThis.browser || globalThis.chrome;

  function num(v) {
    if (v == null) return 0;
    if (typeof v === 'number') return v;
    const s = String(v).replace(/[^\d,.]/g, '').replace(/\s/g, '');
    const n = parseFloat(s.includes(',') && !s.includes('.') ? s.replace(',', '.') : s.replace(/,(?=\d{3}\b)/g, ''));
    return isFinite(n) ? n : 0;
  }
  function* walk(d) {
    if (Array.isArray(d)) { for (const x of d) yield* walk(x); return; }
    if (d && typeof d === 'object') { yield d; if (d['@graph']) yield* walk(d['@graph']); }
  }
  function fromJsonLd() {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data; try { data = JSON.parse(s.textContent); } catch (_) { continue; }
      for (const d of walk(data)) {
        const t = d['@type'];
        if (!(t === 'Product' || (Array.isArray(t) && t.includes('Product')))) continue;
        let o = d.offers || {}; if (Array.isArray(o)) o = o[0] || {};
        let price = num(o['@type'] === 'AggregateOffer' ? o.lowPrice : o.price), original = 0;
        let specs = o.priceSpecification || []; if (!Array.isArray(specs)) specs = [specs];
        for (const sp of specs) {
          const pt = String(sp.priceType || '');
          if (/Strikethrough|ListPrice/.test(pt)) original = num(sp.price); else if (!price) price = num(sp.price);
        }
        if (price > 0) return { name: d.name || '', price, original: original > price ? original : 0 };
      }
    }
    return null;
  }
  function fromMicrodata() {
    const root = document.querySelector('[itemtype*="schema.org/Product"]');
    if (!root) return null;
    const val = el => el ? (el.getAttribute('content') || el.textContent || '').trim() : '';
    const price = num(val(root.querySelector('[itemprop="price"]')) || val(root.querySelector('[itemprop="lowPrice"]')));
    return price > 0 ? { name: val(root.querySelector('[itemprop="name"]')), price, original: 0 } : null;
  }
  function fromMeta() {
    const m = document.querySelector('meta[property="product:price:amount"]');
    if (!m) return null;
    const t = document.querySelector('meta[property="og:title"]');
    const price = num(m.content);
    return price > 0 ? { name: t ? t.content : document.title, price, original: 0 } : null;
  }
  function productUrl() {
    const c = document.querySelector('link[rel="canonical"]');
    try { if (c && new URL(c.href).host === location.host) return c.href; } catch (_) {}
    return location.origin + location.pathname;
  }
  const kc = v => Math.round(v).toLocaleString('cs-CZ').replace(/\s/g, '\u00a0') + '\u00a0Kč';

  const ICON = '__LOVCENY_ICON_SVG__';
  const CSS = `:host{all:initial}
  .b{position:fixed;right:16px;bottom:16px;z-index:2147483646;max-width:320px;width:calc(100vw - 32px);box-sizing:border-box;
     font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#14211B;background:#FBF8F3;
     border:1px solid #CFE6DA;border-radius:16px;box-shadow:0 10px 30px -10px rgba(20,33,27,.35);padding:12px 14px}
  .h{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .h svg{width:22px;height:22px;flex:none;border-radius:6px}
  .h b{font-size:13px;letter-spacing:.02em;color:#0B6E4F}
  .x{margin-left:auto;border:0;background:transparent;color:#5B6B63;font-size:20px;line-height:1;cursor:pointer;width:32px;height:32px;border-radius:8px}
  .x:hover,.x:focus-visible{background:#E6F4EE;outline:2px solid #0B6E4F}
  .v{font-weight:800;font-size:15px;display:flex;gap:6px;align-items:flex-start}
  .v.fake{color:#8A3A0F}.v.buy{color:#0B6E4F}.v.wait{color:#6B4300}
  .r{color:#4A5A52;font-size:13px;margin-top:2px}
  .e{margin-top:8px;padding:8px 10px;background:#E6F4EE;border-radius:10px;font-size:13.5px}
  .e b{white-space:nowrap}
  a{color:#095C42;font-weight:700}
  .l{display:inline-block;margin-top:8px;font-size:13.5px}
  @media (prefers-reduced-motion:no-preference){.b{animation:in .2s ease-out}@keyframes in{from{opacity:0;transform:translateY(8px)}}}`;

  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  function render(d) {
    const host = document.createElement('div');
    host.id = 'lovceny-badge-host';
    const root = host.attachShadow({ mode: 'open' });
    const st = el('style'); st.textContent = CSS; root.append(st);
    const box = el('aside', 'b'); box.setAttribute('aria-label', 'Lovceny – ověření ceny');
    const h = el('div', 'h'); h.innerHTML = ICON; h.append(el('b', null, 'Lovceny'));
    const x = el('button', 'x', '×'); x.type = 'button'; x.title = 'Zavřít'; x.setAttribute('aria-label', 'Zavřít odznak Lovceny');
    x.addEventListener('click', () => host.remove()); h.append(x); box.append(h);
    const t = d.tracked && d.tracked.verdict;
    const c = d.check30;
    let line, cls = '', reason = '';
    if (c && c.flag) { line = '⚠️ Za 30 dní bylo levněji'; cls = 'fake'; reason = `Nejnižší cena za 30 dní: ${kc(c.min30)} (${new Date(c.date).toLocaleDateString('cs-CZ')})`; }
    else if (t && t.code === 'buy') { line = '✅ ' + t.label; cls = 'buy'; reason = t.reason || ''; }
    else if (t && t.code === 'fake') { line = '⚠️ ' + t.label; cls = 'fake'; reason = t.reason || ''; }
    else if (t && t.code === 'wait') { line = '⏳ ' + t.label; cls = 'wait'; reason = t.reason || ''; }
    else if (t) { line = '📊 ' + t.label; reason = t.reason || ''; }
    else { line = 'Produkt zatím nesledujeme'; reason = 'Historii ceny nemáme – ukazujeme aspoň nabídky jinde.'; }
    box.append(el('div', 'v ' + cls, line));
    if (reason) box.append(el('div', 'r', reason));
    const lp = d.lowest_elsewhere;
    if (lp && lp.price) {
      const e = el('div', 'e');
      const cheaper = d.price && lp.price < d.price;
      e.append(document.createTextNode(cheaper ? 'Jinde levněji: ' : 'Nejnižší cena jinde: '), el('b', null, kc(lp.price)),
        document.createTextNode(' (' + (lp.shop_name || lp.shop) + ')'));
      box.append(e);
    }
    const a = el('a', 'l', 'Podrobnosti na Lovceny →');
    a.href = d.lovceny_url; a.target = '_blank'; a.rel = 'noopener';
    box.append(a);
    root.append(box);
    document.documentElement.append(host);
  }

  function run() {
    const p = fromJsonLd() || fromMicrodata() || fromMeta();
    if (!p) return; // není stránka produktu
    ext.runtime.sendMessage({ type: 'lovceny-overit', url: productUrl(), name: p.name, price: p.price, original: p.original },
      d => { if (d && d.ok && d.lovceny_url) render(d); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true }); else run();
})();
