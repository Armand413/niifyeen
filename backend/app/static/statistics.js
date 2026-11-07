/*
 * Page « Statistiques » : totaux anonymes, profils horaires, carte de chaleur, comparaison, transitions.
 * Tout le texte est inséré avec textContent ; les graphiques sont des éléments SVG / canvas construits un par un.
 */
(function () {
  "use strict";

  var L = window.StatisticsLogic;
  var SVG = "http://www.w3.org/2000/svg";
  var state = { minutes: 1440, camera: "", cameras: [], meta: {}, tz: L.tzOffsetFromDate(new Date()), seq: 0 };

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) { var n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; }
  function sv(tag, attrs, text) {
    var n = document.createElementNS(SVG, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function api(path) {
    return fetch(path).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
    });
  }
  function fail(res) {
    var d = res.data && res.data.detail;
    Shell.toast(typeof d === "string" ? d : "Erreur " + res.status, "warning");
  }
  function camName(id) { return (state.meta[id] && state.meta[id].label) || id; }
  function td(text, cls) { return el("td", text, cls); }

  function kpi(value, label) {
    var d = el("div", null, "stat"); d.appendChild(el("b", value)); d.appendChild(el("span", label)); return d;
  }

  // ---- vue d'ensemble ------------------------------------------------------------------------------------------
  function paintOverview(o) {
    var k = $("kpis"); k.replaceChildren();
    k.append(kpi(String(o.passages), "Passages"), kpi(String(o.observations), "Observations"),
             kpi(String(o.unique_enrolled), "Personnes enrôlées uniques"), kpi(String(o.unknown_passages), "Passages d'inconnus"),
             kpi(L.formatDuration(o.avg_duration_sec), "Durée moyenne d'un passage"));
    $("kpiNote").textContent = "Cible : " + o.by_mode.TARGET + " · Enrôlée : " + o.by_mode.DB + " · Inconnu : " + o.by_mode.UNK + " (passages).";
    var rows = $("camRows"); rows.replaceChildren();
    o.by_camera.forEach(function (c) {
      var tr = el("tr");
      tr.append(td(camName(c.camera_id)), td(String(c.observations), "num"), td(String(c.passages), "num"), td(String(c.unique_enrolled), "num"), td(L.formatDuration(c.avg_duration_sec), "num"));
      rows.appendChild(tr);
    });
    $("camTable").hidden = !o.by_camera.length; $("camEmpty").hidden = o.by_camera.length > 0;

    hourChart(o.by_hour); weekTable(o.by_weekday_hour);

    var zr = $("zoneRows"); zr.replaceChildren();
    o.zones.forEach(function (z) {
      var tr = el("tr"); tr.append(td(z.name + " (" + camName(z.camera_id) + ")"), td(String(z.entries), "num"), td(String(z.exits), "num"), td(String(z.occupancy_max), "num")); zr.appendChild(tr);
    });
    $("zoneEmpty").hidden = o.zones.length > 0;
  }

  function hourChart(values) {
    var svg = $("hourChart"); svg.replaceChildren();
    var g = L.bars(values, 456, 100);
    svg.appendChild(sv("line", { x1: 12, y1: 110, x2: 468, y2: 110, stroke: "currentColor", opacity: 0.3 }));
    g.bars.forEach(function (b) {
      var r = sv("rect", { "class": "bar", x: 12 + b.x, y: 8 + b.y, width: b.w, height: b.h });
      r.appendChild(sv("title", {}, b.i + " h : " + b.v + " passage(s)"));
      svg.appendChild(r);
    });
    [0, 6, 12, 18, 23].forEach(function (h) { svg.appendChild(sv("text", { x: 12 + h * 456 / 24, y: 124 }, h + " h")); });
    svg.appendChild(sv("text", { x: 12, y: 8 }, g.max ? "max " + g.max : "aucune donnée"));
  }

  function weekTable(matrix) {
    var t = $("week"); t.replaceChildren();
    var max = 0; matrix.forEach(function (r) { r.forEach(function (v) { if (v > max) max = v; }); });
    var head = el("tr"); head.appendChild(el("th"));
    for (var h = 0; h < 24; h++) head.appendChild(el("th", h % 3 === 0 ? String(h) : ""));
    t.appendChild(head);
    matrix.forEach(function (row, d) {
      var tr = el("tr"); tr.appendChild(el("th", L.WEEKDAYS[d]));
      row.forEach(function (v, h) {
        var c = el("td", null, "cell"); c.style.background = L.heatColor(v, max); c.title = L.WEEKDAYS[d] + " " + h + " h : " + v; tr.appendChild(c);
      });
      t.appendChild(tr);
    });
  }

  // ---- comparaison -----------------------------------------------------------------------------------------------------
  function paintCompare(c) {
    var rows = $("cmpRows"); rows.replaceChildren();
    var defs = [["passages", "Passages"], ["observations", "Observations"], ["unique_enrolled", "Personnes enrôlées uniques"],
                ["unknown_passages", "Passages d'inconnus"], ["avg_duration_sec", "Durée moyenne (s)"]];
    defs.forEach(function (d) {
      var x = c.delta[d[0]], tr = el("tr");
      tr.append(td(d[1]), td(x.a === null ? "—" : String(x.a), "num"), td(x.b === null ? "—" : String(x.b), "num"), td(L.formatDelta(x), "num"));
      rows.appendChild(tr);
    });
    $("cmpRange").textContent = "Période : " + new Date(c.a.since).toLocaleString("fr-FR") + " → " + new Date(c.a.until).toLocaleString("fr-FR") +
      " · Référence : " + new Date(c.b.since).toLocaleString("fr-FR") + " → " + new Date(c.b.until).toLocaleString("fr-FR");
    $("cmpNote").textContent = "« n/a » : la référence est à zéro, un pourcentage n'aurait pas de sens.";
  }

  // ---- carte de chaleur ------------------------------------------------------------------------------------------------
  function paintHeat(h) {
    var cv = $("heat"), ctx = cv.getContext && cv.getContext("2d");
    if (ctx) {
      ctx.clearRect(0, 0, cv.width, cv.height);
      var cw = cv.width / h.cols, ch = cv.height / h.rows;
      h.cells.forEach(function (c) { ctx.fillStyle = L.heatColor(c[2], h.max); ctx.fillRect(c[0] * cw, c[1] * ch, cw, ch); });
    }
    $("heatInfo").textContent = h.total ? h.total + " observation(s) · " + h.note : "Aucune position enregistrée pour cette caméra sur la période.";
  }

  // ---- transitions ----------------------------------------------------------------------------------------------------
  function paintGraph(t) {
    var svg = $("graph"); svg.replaceChildren();
    var g = L.graph(t.nodes, t.edges, 360, 260);
    svg.appendChild(sv("defs", {})).appendChild(sv("marker", { id: "arr", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto" })).appendChild(sv("path", { d: "M0 0L10 5L0 10z", fill: "currentColor" }));
    g.edges.forEach(function (e) {
      var p = sv("path", { "class": "edge", d: e.d, "stroke-width": e.width, "marker-end": "url(#arr)" });
      p.appendChild(sv("title", {}, camName(e.from) + " → " + camName(e.to) + " : " + e.count));
      svg.appendChild(p); svg.appendChild(sv("text", { x: e.lx, y: e.ly, "text-anchor": "middle" }, String(e.count)));
    });
    g.nodes.forEach(function (n) {
      svg.appendChild(sv("circle", { "class": "node", cx: n.x, cy: n.y, r: 13 }));
      svg.appendChild(sv("text", { "class": "node-label", x: n.x, y: n.y + 26, "text-anchor": "middle" }, camName(n.id)));
    });
    var ul = $("edges"); ul.replaceChildren();
    t.edges.forEach(function (e) {
      var li = el("li"); li.appendChild(el("span", camName(e.from) + " → " + camName(e.to)));
      li.appendChild(el("span", e.count + " (cibles " + e.target + ", enrôlées " + e.enrolled + ") · médiane " + L.formatDuration(e.median_gap_sec), "muted")); ul.appendChild(li);
    });
    if (!t.edges.length) ul.appendChild(el("li", "Aucune transition sur cette période."));
    $("graphNote").textContent = t.note;
  }

  // ---- chargement ---------------------------------------------------------------------------------------------------------
  function load() {
    var seq = ++state.seq, q = L.query(state.minutes, state.camera, state.tz);
    $("csv").href = "/api/stats/overview.csv?" + q;
    api("/api/stats/overview?" + q).then(function (r) { if (seq === state.seq) { if (r.ok) paintOverview(r.data); else fail(r); } });
    api("/api/stats/compare?" + L.query(state.minutes, state.camera)).then(function (r) { if (seq === state.seq) { if (r.ok) paintCompare(r.data); else fail(r); } });
    api("/api/stats/transitions?minutes=" + state.minutes + "&max_gap_sec=" + encodeURIComponent($("gap").value)).then(function (r) { if (seq === state.seq) { if (r.ok) paintGraph(r.data); else fail(r); } });
    loadHeat(seq);
  }

  function loadHeat(seq) {
    var cam = $("heatCam").value;
    if (!cam) { $("heatInfo").textContent = "Aucune caméra."; return; }
    api("/api/stats/heatmap?camera_id=" + encodeURIComponent(cam) + "&minutes=" + state.minutes).then(function (r) {
      if (seq !== undefined && seq !== state.seq) return;
      if (r.ok) paintHeat(r.data); else fail(r);
    });
  }

  function init() {
    Shell.mount({ active: "statistics" });
    var seg = $("period");
    L.PERIODS.forEach(function (p) {
      var b = el("button", p.label); b.type = "button"; b.setAttribute("aria-pressed", String(p.minutes === state.minutes));
      b.addEventListener("click", function () {
        state.minutes = p.minutes;
        Array.prototype.forEach.call(seg.children, function (x) { x.setAttribute("aria-pressed", String(x === b)); });
        load();
      });
      seg.appendChild(b);
    });
    $("camera").addEventListener("change", function () { state.camera = $("camera").value; load(); });
    $("heatCam").addEventListener("change", function () { loadHeat(); });
    $("gap").addEventListener("change", load);
    api("/api/cameras").then(function (r) {
      state.cameras = r.ok ? r.data.cameras : []; state.meta = r.ok ? (r.data.camera_meta || {}) : {};
      state.cameras.forEach(function (c) {
        var o = el("option", camName(c)); o.value = c; $("camera").appendChild(o);
        var h = el("option", camName(c)); h.value = c; $("heatCam").appendChild(h);
      });
      load();
    });
    setInterval(load, 60000);
  }

  window.StatisticsPage = { state: state, load: load };
  init();
})();
