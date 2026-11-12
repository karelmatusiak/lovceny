/* Statický katalog: hledání, filtry kategorií, Black Friday, srovnání a ověření odkazu.
   Data jsou v /data/katalog.json (a /data/url.json pro adresy nabídek). Server se nevolá. */
(function () {
  var TRACK = { utm_source: 1, utm_medium: 1, utm_campaign: 1, utm_content: 1, utm_term: 1,
    gclid: 1, fbclid: 1, msclkid: 1, mc_cid: 1, mc_eid: 1, fo_c: 1, fo_k: 1, fo_s: 1, fo_m: 1, fo_v: 1 };
  var katalogP = null;
  var urlP = null;

  function fold(s) {
    return (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  }
  function czk(n) {
    if (n == null || n === "") return "";
    var s = Math.round(Number(n)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
    return s + "\u00a0Kč";
  }
  function katalog() {
    if (!katalogP) katalogP = fetch("/data/katalog.json").then(function (r) { return r.json(); });
    return katalogP;
  }
  function urls() {
    if (!urlP) urlP = fetch("/data/url.json").then(function (r) { return r.json(); });
    return urlP;
  }
  function normUrl(raw) {
    var text = (raw || "").trim();
    if (/^www\./i.test(text)) text = "https://" + text;
    var u;
    try { u = new URL(text); } catch (e) { return ""; }
    var host = u.hostname.toLowerCase().replace(/^www\./, "");
    var path = u.pathname.replace(/\/$/, "") || "/";
    var q = [];
    u.searchParams.forEach(function (v, k) {
      var key = k.toLowerCase();
      if (TRACK[key] || key.indexOf("utm_") === 0) return;
      q.push(k + "=" + v);
    });
    return host + path + (q.length ? "?" + q.join("&") : "");
  }
  function isUrl(q) { return /^(https?:\/\/|www\.)/i.test((q || "").trim()); }
  function card(it) {
    var href = "/produkt/" + it.i + "-" + it.s;
    var buy = it.k && it.h
      ? '<a class="btn btn-shop-sm" href="' + esc(it.h) + '" rel="' + esc(it.r || "nofollow noopener") + '" target="_blank">Do obchodu <span aria-hidden="true">→</span></a>'
      : "";
    var oos = it.k ? "" : '<span class="pc-oos">Momentálně není skladem</span>';
    return '<div class="card pcard"><a class="pcard-link" href="' + href + '"><div class="p-head"><div class="p-meta"><div class="p-name sm">' +
      esc(it.n) + '</div><div class="p-cat">' + esc(it.sn || "") + '</div></div></div><div class="pc-row"><div class="verdict v-' +
      esc(it.v || "none") + '"><span class="verdict-badge"><span class="verdict-label">' + esc(it.vl || "") +
      '</span></span></div><div class="pc-prices"><b class="pc-price">' + (it.p != null ? czk(it.p) : "") + "</b>" + oos +
      "</div></div></a><div class=\"pcard-act\"><div class=\"pcard-buttons\">" +
      '<button type="button" class="btn-cmp-quiet" data-cmp-id="' + it.i + '" data-cmp-slug="' + esc(it.s) + '" aria-pressed="false"><span aria-hidden="true">⚖️</span> <span class="cmp-txt">Přidat do srovnání</span></button>' +
      buy + "</div></div></div>";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function sortItems(list, desc) {
    return list.slice().sort(function (a, b) {
      if (!a.k !== !b.k) return a.k ? -1 : 1;
      var pa = a.p == null ? (desc ? -1 : 1e18) : Number(a.p);
      var pb = b.p == null ? (desc ? -1 : 1e18) : Number(b.p);
      return desc ? pb - pa : pa - pb;
    });
  }
  function matchQuery(items, q) {
    var tokens = fold(q).split(/\s+/).filter(Boolean);
    if (!tokens.length) return [];
    return items.filter(function (it) {
      if (it.e && tokens.length === 1 && tokens[0] === String(it.e)) return true;
      var name = fold(it.n + " " + (it.f || ""));
      return tokens.every(function (t) { return name.indexOf(t) >= 0; });
    });
  }

  function renderSearch() {
    var root = document.getElementById("katalog-hledani");
    if (!root) return;
    var params = new URLSearchParams(location.search);
    var q = params.get("q") || "";
    var desc = params.get("razeni") === "cena-desc";
    var stock = params.get("skladem") === "1";
    var input = root.querySelector('input[name="q"]');
    if (input) input.value = q;
    var sel = root.querySelector('select[name="razeni"]');
    if (sel) sel.value = desc ? "cena-desc" : "cena";
    var box = root.querySelector('input[name="skladem"]');
    if (box) box.checked = stock;
    var stav = document.getElementById("katalog-stav");
    var out = document.getElementById("katalog-vysledky");
    if (!q) {
      if (out) out.innerHTML = "";
      if (stav) stav.textContent = "Napiš název, značku nebo EAN. Hledáme v katalogu, který už sledujeme.";
      return;
    }
    if (isUrl(q)) {
      if (stav) stav.textContent = "Hledám tenhle odkaz v katalogu…";
      urls().then(function (map) {
        var hit = map[normUrl(q)];
        if (hit) { location.href = hit; return; }
        if (stav) stav.textContent = "Ten odkaz v katalogu nemáme. Nové produkty teď z webu přidat nejde – obchod z webu nestahujeme.";
        if (out) out.innerHTML = "";
      }).catch(function () { if (stav) stav.textContent = "Katalog se nepodařilo načíst."; });
      return;
    }
    katalog().then(function (items) {
      var found = matchQuery(items, q);
      if (stock) found = found.filter(function (it) { return it.k; });
      found = sortItems(found, desc).slice(0, 30);
      if (stav) stav.textContent = found.length ? ("Nalezeno " + found.length + " v katalogu. Živé hledání v obchodech na statickém webu neběží.") : "V katalogu nic není. Živé hledání v obchodech po přechodu na statický web neběží.";
      if (out) out.innerHTML = found.map(card).join("");
      if (window.lovcenyCmp) window.lovcenyCmp.render();
    }).catch(function () { if (stav) stav.textContent = "Katalog se nepodařilo načíst."; });
  }

  function hookSearch() {
    var form = document.getElementById("katalog-form");
    var filters = document.getElementById("katalog-filtry");
    if (!form) return;
    function go(ev) {
      if (ev) ev.preventDefault();
      var q = new FormData(form).get("q") || "";
      var razeni = filters ? (new FormData(filters).get("razeni") || "cena") : "cena";
      var stock = filters && filters.querySelector('input[name="skladem"]').checked;
      var params = new URLSearchParams();
      if (q) params.set("q", q);
      if (razeni === "cena-desc") params.set("razeni", "cena-desc");
      if (stock) params.set("skladem", "1");
      var next = "/hledat" + (params.toString() ? "?" + params.toString() : "");
      history.pushState(null, "", next);
      renderSearch();
    }
    form.addEventListener("submit", go);
    if (filters) {
      filters.addEventListener("submit", go);
      filters.addEventListener("change", go);
    }
    window.addEventListener("popstate", renderSearch);
    renderSearch();
  }

  function applyCategory(form) {
    var slug = form.getAttribute("data-katalog");
    var view = form.getAttribute("data-view") || "";
    var brand = form.znacka ? form.znacka.value : "";
    var od = form.od && form.od.value !== "" ? Number(form.od.value) : null;
    var to = form.do && form.do.value !== "" ? Number(form.do.value) : null;
    var stock = !!(form.skladem && form.skladem.checked);
    var active = !!(brand || od != null || to != null || stock);
    var grid = document.querySelector(".cat-grid");
    var pager = document.querySelectorAll(".pager");
    if (!active) {
      if (location.search && /(?:^|[?&])(znacka|od|do|skladem)=/.test(location.search)) {
        location.href = location.pathname;
      }
      return;
    }
    katalog().then(function (items) {
      var rows = items.filter(function (it) { return it.c === slug && !it.a && it.p != null; });
      if (view === "ve-sleve") rows = rows.filter(function (it) { return it.v === "buy" && it.k; });
      if (brand) rows = rows.filter(function (it) { return it.b === brand; });
      if (od != null) rows = rows.filter(function (it) { return Number(it.p) >= od; });
      if (to != null) rows = rows.filter(function (it) { return Number(it.p) <= to; });
      if (stock) rows = rows.filter(function (it) { return it.k; });
      rows = sortItems(rows, false);
      if (view === "nejlevnejsi") rows = rows.slice(0, 20);
      if (grid) grid.innerHTML = rows.length ? rows.map(card).join("") : "<p><b>Žádný produkt neodpovídá filtrům.</b></p>";
      pager.forEach(function (p) { p.hidden = true; });
      var count = document.querySelector(".cat-count");
      if (count) count.textContent = "Filtr: " + rows.length + " produktů v katalogu · řazeno podle ceny";
      if (window.lovcenyCmp) window.lovcenyCmp.render();
    });
  }
  function hookCategory() {
    var form = document.querySelector("form.cat-filters[data-katalog]");
    if (!form) return;
    form.addEventListener("submit", function (ev) { ev.preventDefault(); applyCategory(form); });
    form.addEventListener("change", function () { applyCategory(form); });
    var q = new URLSearchParams(location.search);
    if (q.get("znacka") || q.get("od") || q.get("do") || q.get("skladem")) {
      if (form.znacka && q.get("znacka")) form.znacka.value = q.get("znacka");
      if (form.od && q.get("od")) form.od.value = q.get("od");
      if (form.do && q.get("do")) form.do.value = q.get("do");
      if (form.skladem && q.get("skladem")) form.skladem.checked = true;
      applyCategory(form);
    }
  }

  function hookBf() {
    var form = document.querySelector("form[data-bf-filter]");
    if (!form) return;
    function apply(ev) {
      if (ev) ev.preventDefault();
      var cat = form.kategorie ? form.kategorie.value : "";
      var shop = form.obchod ? form.obchod.value : "";
      document.querySelectorAll("[data-cat]").forEach(function (el) {
        var ok = (!cat || el.getAttribute("data-cat") === cat) && (!shop || el.getAttribute("data-shop") === shop);
        el.hidden = !ok;
      });
      var params = new URLSearchParams();
      if (cat) params.set("kategorie", cat);
      if (shop) params.set("obchod", shop);
      history.replaceState(null, "", "/black-friday" + (params.toString() ? "?" + params.toString() : "") + "#bf-list-h");
    }
    form.addEventListener("submit", apply);
    form.addEventListener("change", apply);
    var q = new URLSearchParams(location.search);
    if (q.get("kategorie") || q.get("obchod")) {
      if (form.kategorie) form.kategorie.value = q.get("kategorie") || "";
      if (form.obchod) form.obchod.value = q.get("obchod") || "";
      apply();
    }
  }

  function hookCompare() {
    var box = document.getElementById("cmp-static");
    if (!box) return;
    var params = new URLSearchParams(location.search);
    var raw = params.get("p") || "";
    if (!raw && params.get("ids")) raw = params.get("ids");
    var ids = raw.split(/[,\s]+/).map(function (part) {
      var m = String(part).match(/^(\d+)/);
      return m ? Number(m[1]) : 0;
    }).filter(Boolean).slice(0, 3);
    if (ids.length < 2) return;
    katalog().then(function (items) {
      var by = {};
      items.forEach(function (it) { by[it.i] = it; });
      var rows = ids.map(function (id) { return by[id]; }).filter(Boolean);
      if (rows.length < 2) return;
      var empty = document.querySelector(".cmp-empty");
      if (empty) empty.hidden = true;
      var priced = rows.filter(function (it) { return it.k && it.p != null; });
      var cheap = priced.length > 1 ? priced.slice().sort(function (a, b) { return a.p - b.p; })[0].i : null;
      box.innerHTML = '<p class="sub">Srovnání z katalogu. Graf historie je na stránce každého produktu.</p><div class="cmp-scroll"><div class="cmp-grid" style="--n:' +
        rows.length + '">' + rows.map(function (it) {
          return '<article class="card cmp-col' + (it.i === cheap ? " is-cheapest" : "") + '"><h2 class="cmp-name"><a href="/produkt/' +
            it.i + "-" + it.s + '">' + esc(it.n) + '</a></h2><p class="cmp-price"><b>' + czk(it.p) + "</b> " + esc(it.sn || "") +
            "</p><p>" + esc(it.vl || "") + (it.k ? "" : " · není skladem") + "</p>" +
            (it.k && it.h ? '<a class="btn btn-shop-sm" href="' + esc(it.h) + '" rel="' + esc(it.r || "nofollow noopener") + '" target="_blank">Do obchodu</a>' : "") +
            "</article>";
        }).join("") + "</div></div>";
    });
  }

  function hookVerify() {
    var form = document.querySelector("form.ov-form");
    if (!form || !document.querySelector("script[src*='katalog.js']")) return;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var input = form.querySelector('input[name="url"]');
      var raw = input ? input.value : "";
      urls().then(function (map) {
        var hit = map[normUrl(raw)];
        if (hit) { location.href = hit; return; }
        var note = form.parentNode.querySelector(".ov-static-miss");
        if (!note) {
          note = document.createElement("div");
          note.className = "notice warn ov-static-miss";
          form.parentNode.appendChild(note);
        }
        note.textContent = "Ten odkaz v katalogu nemáme. Stránku obchodu ze statického webu nestahujeme – ověřit umíme jen produkt, který už sledujeme.";
      });
    });
  }

  function boot() {
    hookSearch();
    hookCategory();
    hookBf();
    hookCompare();
    hookVerify();
  }
  if (document.readyState !== "loading") boot();
  else document.addEventListener("DOMContentLoaded", boot);
})();
