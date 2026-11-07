/* Page « Rapports » : paramètres, aperçu, PDF, modèles. Tout le texte est inséré avec textContent. */
(function () {
  "use strict";
  var SL = window.StatisticsLogic, SVG = "http://www.w3.org/2000/svg";
  var state = { sections: [], templates: {}, seq: 0, timer: null, tz: SL.tzOffsetFromDate(new Date()) };

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) { var n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; }
  function api(method, path, body) {
    var o = { method: method, headers: {} };
    if (body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(body); }
    return fetch(path, o).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, status: r.status, data: d }; }); });
  }
  function fail(res) { var d = res.data && res.data.detail; Shell.toast(typeof d === "string" ? d : "Erreur " + res.status, "warning"); }

  function selectedSections() {
    return Array.prototype.filter.call($("sections").querySelectorAll("input"), function (i) { return i.checked; }).map(function (i) { return i.value; });
  }
  function query() {
    var q = "minutes=" + encodeURIComponent($("period").value) + "&tz_offset_min=" + state.tz + "&sections=" + encodeURIComponent(selectedSections().join(","));
    if ($("title").value.trim()) q += "&title=" + encodeURIComponent($("title").value.trim());
    if ($("camera").value) q += "&camera_id=" + encodeURIComponent($("camera").value);
    return q;
  }

  function barChart(bars) {
    var g = SL.bars(bars.values, 456, 90), svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 480 120"); svg.setAttribute("class", "stat-chart");
    g.bars.forEach(function (b) {
      var r = document.createElementNS(SVG, "rect"); r.setAttribute("class", "bar");
      r.setAttribute("x", 12 + b.x); r.setAttribute("y", 8 + b.y); r.setAttribute("width", b.w); r.setAttribute("height", b.h);
      var t = document.createElementNS(SVG, "title"); t.textContent = bars.labels[b.i] + " h : " + b.v; r.appendChild(t); svg.appendChild(r);
    });
    [0, 6, 12, 18, 23].forEach(function (h) {
      var t = document.createElementNS(SVG, "text"); t.setAttribute("x", 12 + h * 456 / 24); t.setAttribute("y", 112); t.textContent = h + " h"; svg.appendChild(t);
    });
    return svg;
  }

  function table(t) {
    var tb = el("table", null, "table"), head = el("tr");
    t.header.forEach(function (h) { head.appendChild(el("th", h)); });
    tb.appendChild(el("thead")).appendChild(head);
    var body = el("tbody");
    t.rows.forEach(function (row) { var tr = el("tr"); row.forEach(function (c) { tr.appendChild(el("td", String(c))); }); body.appendChild(tr); });
    if (!t.rows.length) { var tr = el("tr"), td = el("td", "Aucune donnée."); td.colSpan = t.header.length; tr.appendChild(td); body.appendChild(tr); }
    tb.appendChild(body);
    return tb;
  }

  function paint(r) {
    $("pvTitle").textContent = r.title; $("pvSub").textContent = r.subtitle;
    var box = $("preview"); box.replaceChildren();
    r.sections.forEach(function (s) {
      var sec = el("section"); sec.appendChild(el("h3", s.heading, "section-title"));
      (s.paragraphs || []).forEach(function (p) { sec.appendChild(el("p", p)); });
      if (s.bars) sec.appendChild(barChart(s.bars));
      if (s.table) sec.appendChild(table(s.table));
      box.appendChild(sec);
    });
    if (!r.sections.length) box.appendChild(el("p", "Aucune section choisie.", "muted"));
  }

  function refresh() {
    var seq = ++state.seq;
    if (!selectedSections().length) { $("pdf").removeAttribute("href"); $("pdf").setAttribute("aria-disabled", "true"); $("preview").replaceChildren(el("p", "Choisissez au moins une section.", "muted")); return; }
    $("pdf").removeAttribute("aria-disabled");
    $("pdf").href = "/api/reports/report.pdf?" + query();
    api("GET", "/api/reports/preview?" + query()).then(function (r) { if (seq !== state.seq) return; if (r.ok) paint(r.data); else fail(r); });
  }
  function later() { clearTimeout(state.timer); state.timer = setTimeout(refresh, 300); }

  function paintTemplates() {
    var sel = $("tplSel"), keep = sel.value; sel.replaceChildren(el("option", "—"));
    sel.firstChild.value = "";
    Object.keys(state.templates).sort().forEach(function (n) { var o = el("option", n); o.value = n; sel.appendChild(o); });
    sel.value = state.templates[keep] ? keep : "";
  }
  function loadTemplate() {
    var t = state.templates[$("tplSel").value]; if (!t) return;
    $("title").value = t.title === "Rapport d'activité" ? "" : t.title; $("camera").value = t.camera_id || "";
    $("period").value = String(t.minutes);
    if ($("period").value !== String(t.minutes)) { var o = el("option", Math.round(t.minutes / 60) + " h"); o.value = String(t.minutes); $("period").appendChild(o); $("period").value = String(t.minutes); }
    Array.prototype.forEach.call($("sections").querySelectorAll("input"), function (i) { i.checked = t.sections.indexOf(i.value) >= 0; });
    $("tplName").value = $("tplSel").value; refresh();
  }

  function init() {
    Shell.mount({ active: "reports" });
    ["title", "period", "camera"].forEach(function (id) { $(id).addEventListener(id === "title" ? "input" : "change", later); });
    $("tplLoad").addEventListener("click", loadTemplate);
    $("tplSave").addEventListener("click", function () {
      var name = $("tplName").value.trim();
      if (!name) { Shell.toast("Donnez un nom au modèle.", "warning"); return; }
      api("PUT", "/api/reports/templates/" + encodeURIComponent(name), { title: $("title").value.trim(), camera_id: $("camera").value, sections: selectedSections(), minutes: Number($("period").value) })
        .then(function (r) { if (!r.ok) return fail(r); state.templates = r.data.templates; paintTemplates(); $("tplSel").value = name; Shell.toast("Modèle enregistré."); });
    });
    $("tplDelete").addEventListener("click", function () {
      var name = $("tplSel").value; if (!name || !confirm("Supprimer le modèle « " + name + " » ?")) return;
      api("DELETE", "/api/reports/templates/" + encodeURIComponent(name)).then(function (r) { if (!r.ok) return fail(r); state.templates = r.data.templates; paintTemplates(); Shell.toast("Modèle supprimé."); });
    });
    Promise.all([api("GET", "/api/reports/options"), api("GET", "/api/cameras")]).then(function (rs) {
      var opt = rs[0], cams = rs[1];
      if (cams.ok) {
        var meta = cams.data.camera_meta || {};
        cams.data.cameras.forEach(function (c) { var o = el("option", (meta[c] && meta[c].label) || c); o.value = c; $("camera").appendChild(o); });
      }
      if (opt.ok) {
        state.templates = opt.data.templates;
        opt.data.sections.forEach(function (s) {
          var label = el("label", null, "check"), box = el("input"); box.type = "checkbox"; box.value = s.id; box.checked = true;
          box.addEventListener("change", later); label.append(box, document.createTextNode(" " + s.label)); $("sections").appendChild(label);
        });
        paintTemplates();
      }
      refresh();
    });
  }
  window.ReportsPage = { state: state };
  init();
})();
