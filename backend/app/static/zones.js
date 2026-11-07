/*
 * Page « Lignes et zones » : tracé sur l'image d'une caméra, compteurs en direct et affluence.
 * Coordonnées enregistrées en fractions (0-1) de l'image. Tout le texte est inséré avec textContent.
 */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var COLORS = { line: "#2e5bff", zone: "#12b76a", restricted: "#d92d20", disabled: "#98a2b3", draft: "#f79009" };
  var state = { cameras: [], meta: {}, camera: null, zones: [], live: { zones: {}, cameras: {} }, selected: null,
                tool: "select", draft: [], drag: null, series: [], aspect: 0.5625 };

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function s(tag, attrs, text) {
    var n = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function api(method, path, body) {
    var options = { method: method, headers: {} };
    if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
    });
  }
  function errorText(res) {
    var d = res.data && res.data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return "Saisie invalide : " + d.map(function (x) { return (x.loc || []).slice(-1) + " " + x.msg; }).join(" ; ");
    return "Erreur " + res.status;
  }
  function cameraLabel(id) { return (state.meta[id] && state.meta[id].label) || id; }
  function selectedZone() {
    return state.zones.filter(function (z) { return z.id === state.selected; })[0] || null;
  }
  function colorOf(z) { return !z.enabled ? COLORS.disabled : (z.kind === "line" ? COLORS.line : (z.restricted ? COLORS.restricted : COLORS.zone)); }

  // ---- dessin sur l'image --------------------------------------------------------------------------
  function pt(p) { return [Math.round(p[0] * 1000), Math.round(p[1] * 1000 * state.aspect)]; }

  function drawZone(svg, z, selected) {
    var color = colorOf(z), g = s("g", { "data-zone": String(z.id) });
    var pts = z.points.map(pt);
    if (z.kind === "line") {
      g.appendChild(s("line", { x1: pts[0][0], y1: pts[0][1], x2: pts[1][0], y2: pts[1][1], stroke: color,
                                "stroke-width": selected ? 7 : 4, "vector-effect": "non-scaling-stroke" }));
      var a = ZonesLogic.sideArrow(z.points[0], z.points[1], 0.06);
      if (a) {
        var f = pt(a.from), t = pt(a.to);
        g.appendChild(s("line", { x1: f[0], y1: f[1], x2: t[0], y2: t[1], stroke: color, "stroke-width": 3,
                                  "marker-end": "url(#arrowHead)", "vector-effect": "non-scaling-stroke" }));
        g.appendChild(s("text", { x: t[0], y: t[1], fill: color, "font-size": 28, "font-weight": 700, "text-anchor": "middle", dy: -8 },
                        z.label_in || "Sens 1"));
      }
    } else {
      g.appendChild(s("polygon", { points: pts.map(function (p) { return p.join(","); }).join(" "), fill: color,
                                   "fill-opacity": selected ? 0.35 : 0.2, stroke: color, "stroke-width": selected ? 5 : 3,
                                   "vector-effect": "non-scaling-stroke" }));
    }
    var c = ZonesLogic.centroid(z.points), c2 = pt(c);
    var live = state.live.zones[z.id];
    var text = z.name + (live ? (z.kind === "zone" ? " · " + live.occupancy + (z.capacity ? "/" + z.capacity : "") : " · " + live.count_in + "/" + live.count_out) : "");
    g.appendChild(s("text", { x: c2[0], y: c2[1], fill: "#fff", stroke: "#000", "stroke-width": 5, "paint-order": "stroke",
                              "font-size": 30, "font-weight": 700, "text-anchor": "middle" }, text));
    if (selected && state.tool === "select") {
      pts.forEach(function (p, i) {
        g.appendChild(s("circle", { cx: p[0], cy: p[1], r: 14, fill: "#fff", stroke: color, "stroke-width": 4,
                                    "data-handle": String(i), "class": "zones-handle" }));
      });
    }
    svg.appendChild(g);
  }

  function draw() {
    var svg = $("overlay");
    svg.setAttribute("viewBox", "0 0 1000 " + Math.round(1000 * state.aspect));
    svg.replaceChildren();
    var defs = s("defs");
    var marker = s("marker", { id: "arrowHead", viewBox: "0 0 10 10", refX: 6, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse" });
    marker.appendChild(s("path", { d: "M0 0L10 5L0 10z", fill: "context-stroke" }));
    defs.appendChild(marker); svg.appendChild(defs);
    state.zones.forEach(function (z) { drawZone(svg, z, z.id === state.selected); });
    if (state.draft.length) {
      var pts = state.draft.map(pt);
      if (state.tool === "zone" && pts.length > 1) {
        svg.appendChild(s("polyline", { points: pts.map(function (p) { return p.join(","); }).join(" "), fill: "none",
                                        stroke: COLORS.draft, "stroke-width": 4, "stroke-dasharray": "10 6", "vector-effect": "non-scaling-stroke" }));
      } else if (state.tool === "line" && pts.length === 2) {
        svg.appendChild(s("line", { x1: pts[0][0], y1: pts[0][1], x2: pts[1][0], y2: pts[1][1], stroke: COLORS.draft, "stroke-width": 4 }));
      }
      pts.forEach(function (p) { svg.appendChild(s("circle", { cx: p[0], cy: p[1], r: 10, fill: COLORS.draft })); });
    }
    var drawing = state.tool !== "select";
    $("btnFinish").hidden = !(state.tool === "zone" && state.draft.length);
    $("btnFinish").disabled = !ZonesLogic.canFinish("zone", state.draft);
    $("btnCancel").hidden = !state.draft.length;
    $("overlay").classList.toggle("is-drawing", drawing);
    $("hint").textContent = state.tool === "line" ? "Cliquez deux points pour tracer la ligne. La flèche indique le « sens 1 »."
      : state.tool === "zone" ? "Cliquez les sommets de la zone (3 à 12), puis « Terminer la zone » ou Entrée. Échap annule."
      : (state.zones.length ? "Cliquez un tracé pour le sélectionner ; glissez ses points pour le modifier." : "Choisissez « Ligne » ou « Zone » pour tracer sur l'image.");
  }

  function setTool(tool) {
    state.tool = tool; state.draft = [];
    document.querySelectorAll("#toolSeg button").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.tool === tool)); });
    draw();
  }

  // ---- interactions --------------------------------------------------------------------------------------
  function fraction(ev) {
    return MapLogic.toFraction($("overlay").getBoundingClientRect(), ev.clientX, ev.clientY);
  }

  function finishDraft() {
    var kind = state.tool;
    if (!ZonesLogic.canFinish(kind, state.draft)) return;
    var pts = state.draft.slice();
    var base = kind === "line" ? "Ligne" : "Zone";
    var n = state.zones.filter(function (z) { return z.kind === kind; }).length + 1;
    var body = { camera_id: state.camera, kind: kind, name: base + " " + n, points: pts };
    if (kind === "line") { body.label_in = "Entrée"; body.label_out = "Sortie"; }
    api("POST", "/api/zones", body).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.draft = [];
      Shell.toast((kind === "line" ? "Ligne" : "Zone") + " ajoutée.");
      loadZones().then(function () { state.selected = res.data.id; setTool("select"); renderList(); renderEditor(); });
    });
  }

  function onPointer(kind, ev) {
    var p = fraction(ev);
    if (!p) return;
    if (state.tool === "line" || state.tool === "zone") {
      if (kind !== "down") return;
      state.draft = ZonesLogic.addPoint(state.tool, state.draft, [p.x, p.y]);
      draw();
      if (state.tool === "line" && state.draft.length === 2) finishDraft();
      return;
    }
    if (kind === "down") {
      var handle = ev.target && ev.target.closest ? ev.target.closest("[data-handle]") : null;
      var zone = selectedZone();
      if (handle && zone) { state.drag = { id: zone.id, index: Number(handle.getAttribute("data-handle")), moved: false }; return; }
      var g = ev.target && ev.target.closest ? ev.target.closest("[data-zone]") : null;
      state.selected = g ? Number(g.getAttribute("data-zone")) : null;
      renderList(); renderEditor(); draw();
    } else if (kind === "move" && state.drag) {
      var z = state.zones.filter(function (x) { return x.id === state.drag.id; })[0];
      if (!z) return;
      z.points[state.drag.index] = [Math.round(p.x * 10000) / 10000, Math.round(p.y * 10000) / 10000];
      state.drag.moved = true; draw();
    } else if (kind === "up" && state.drag) {
      var d = state.drag; state.drag = null;
      var zz = state.zones.filter(function (x) { return x.id === d.id; })[0];
      if (d.moved && zz) {
        api("PUT", "/api/zones/" + zz.id, { points: zz.points }).then(function (res) {
          if (!res.ok) Shell.toast(errorText(res), "warning");
          loadZones().then(function () { renderEditor(); draw(); });
        });
      }
    }
  }

  // ---- données ---------------------------------------------------------------------------------------------
  function loadZones() {
    if (!state.camera) { state.zones = []; return Promise.resolve(); }
    return api("GET", "/api/zones?camera_id=" + encodeURIComponent(state.camera)).then(function (res) {
      state.zones = res.ok ? res.data.zones : [];
      if (!selectedZone()) state.selected = null;
      renderList(); draw();
    });
  }

  function pollLive() {
    api("GET", "/api/analytics/live").then(function (res) {
      if (!res.ok) return;
      state.live = res.data;
      var h = state.live.cameras[state.camera];
      var chip = $("camHealth");
      chip.textContent = h && h.obstructed ? "Image masquée ou uniforme" : (h && h.frozen ? "Flux figé" : "");
      chip.hidden = !chip.textContent;
      renderList(); draw();
    });
  }

  function renderList() {
    var box = $("list");
    box.replaceChildren();
    if (!state.zones.length) { box.appendChild(el("p", "Aucun tracé sur cette caméra.", "muted")); return; }
    state.zones.forEach(function (z) {
      var live = state.live.zones[z.id];
      var b = el("button", null, "btn zones-item");
      b.type = "button";
      b.setAttribute("aria-pressed", String(z.id === state.selected));
      var name = el("b", z.name + (z.enabled ? "" : " (inactif)"));
      var detail = z.kind === "line"
        ? (live ? (z.label_in || "Sens 1") + " : " + live.count_in + " · " + (z.label_out || "Sens 2") + " : " + live.count_out : "Ligne")
        : (live ? live.occupancy + " personne(s)" + (z.capacity ? " sur " + z.capacity : "") + (live.over ? " — trop de monde" : "") +
                  (live.avg_dwell_sec != null ? " · séjour moyen " + live.avg_dwell_sec + " s" : "") : "Zone") + (z.restricted ? " · interdite" : "");
      b.append(name, el("span", detail, "muted"));
      b.addEventListener("click", function () { state.selected = z.id; renderList(); renderEditor(); draw(); loadSeries(); });
      box.appendChild(b);
    });
  }

  function renderEditor() {
    var z = selectedZone();
    $("editor").hidden = !z;
    if (!z) return;
    $("edTitle").textContent = (z.kind === "line" ? "Ligne : " : "Zone : ") + z.name;
    $("edName").value = z.name; $("edIn").value = z.label_in; $("edOut").value = z.label_out;
    $("edCap").value = z.capacity == null ? "" : String(z.capacity);
    $("edDwell").value = z.max_dwell_sec == null ? "" : String(z.max_dwell_sec);
    $("edRes").checked = !!z.restricted; $("edOn").checked = !!z.enabled;
    ["fIn", "fOut"].forEach(function (id) { $(id).hidden = z.kind !== "line"; });
    ["fCap", "fRes", "fDwell"].forEach(function (id) { $(id).hidden = z.kind !== "zone"; });
  }

  function saveEditor(ev) {
    ev.preventDefault();
    var z = selectedZone();
    if (!z) return;
    var body = { name: $("edName").value.trim(), enabled: $("edOn").checked };
    if (z.kind === "line") { body.label_in = $("edIn").value.trim() || null; body.label_out = $("edOut").value.trim() || null; }
    else {
      var cap = $("edCap").value.trim();
      body.capacity = cap === "" ? null : Number(cap);
      body.restricted = $("edRes").checked;
      var dw = $("edDwell").value.trim();
      body.max_dwell_sec = dw === "" ? null : Number(dw);
    }
    api("PUT", "/api/zones/" + z.id, body).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      Shell.toast("Enregistré."); loadZones().then(renderEditor);
    });
  }

  function deleteSelected() {
    var z = selectedZone();
    if (!z || !confirm("Supprimer « " + z.name + " » et ses statistiques ?")) return;
    api("DELETE", "/api/zones/" + z.id).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.selected = null; Shell.toast("Tracé supprimé.");
      loadZones().then(function () { renderEditor(); loadOverview(); loadSeries(); });
    });
  }

  // ---- affluence ------------------------------------------------------------------------------------------------
  function loadOverview() {
    return api("GET", "/api/analytics/overview?minutes=" + encodeURIComponent($("period").value)).then(function (res) {
      var body = $("overview").tBodies[0];
      body.replaceChildren();
      (res.ok ? res.data.items : []).forEach(function (it) {
        var tr = body.insertRow();
        tr.insertCell().textContent = cameraLabel(it.camera_id) + " — " + it.name;
        tr.insertCell().textContent = String(it.entries);
        tr.insertCell().textContent = String(it.exits);
        tr.insertCell().textContent = it.kind === "zone" ? String(it.occupancy_max) : "—";
      });
    });
  }

  function loadSeries() {
    var z = selectedZone();
    var svg = $("chart");
    svg.replaceChildren();
    $("chartInfo").textContent = "";
    if (!z) { $("chartInfo").textContent = "Sélectionnez un tracé pour voir son affluence."; return Promise.resolve(); }
    return api("GET", "/api/analytics/zones/" + z.id + "/series?minutes=" + encodeURIComponent($("period").value)).then(function (res) {
      var series = res.ok ? res.data.series : [];
      var key = z.kind === "zone" ? "occupancy_max" : "entries";
      var b = ZonesLogic.bars(series, key, 300, 80);
      if (!b.bars.length) { $("chartInfo").textContent = "Aucune donnée sur la période."; return; }
      b.bars.forEach(function (bar) {
        var r = s("rect", { x: bar.x, y: bar.y, width: bar.w, height: bar.h, fill: COLORS.line });
        r.appendChild(s("title", {}, ZonesLogic.bucketLabel(bar.bucket) + " : " + bar.value));
        svg.appendChild(r);
      });
      svg.appendChild(s("text", { x: 0, y: 96, "font-size": 8, fill: "currentColor" }, ZonesLogic.bucketLabel(series[0].bucket)));
      svg.appendChild(s("text", { x: 300, y: 96, "font-size": 8, fill: "currentColor", "text-anchor": "end" }, ZonesLogic.bucketLabel(series[series.length - 1].bucket)));
      $("chartInfo").textContent = (z.kind === "zone" ? "Pic d'occupation par tranche de 5 min — maximum " : "Passages sens 1 par tranche de 5 min — maximum ") + b.max + ".";
    });
  }

  // ---- démarrage --------------------------------------------------------------------------------------------------
  function selectCamera(id) {
    state.camera = id; state.selected = null; state.draft = [];
    Shell.prefs.set("zones.camera", id);
    $("feed").src = id ? "/video/" + encodeURIComponent(id) : "";
    loadZones().then(function () { renderEditor(); draw(); loadSeries(); });
  }

  function init() {
    Shell.mount({ active: "zones" });
    document.querySelectorAll("#toolSeg button").forEach(function (b) { b.addEventListener("click", function () { setTool(b.dataset.tool); }); });
    var svg = $("overlay");
    ["down", "move", "up"].forEach(function (k) { svg.addEventListener("pointer" + k, function (ev) { onPointer(k, ev); }); });
    svg.addEventListener("pointercancel", function (ev) { onPointer("up", ev); });
    $("btnFinish").addEventListener("click", finishDraft);
    $("btnCancel").addEventListener("click", function () { state.draft = []; draw(); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { state.draft = []; draw(); }
      else if (ev.key === "Enter" && state.tool === "zone" && state.draft.length && document.activeElement && document.activeElement.tagName !== "INPUT") finishDraft();
    });
    $("feed").addEventListener("load", function () {
      var img = $("feed");
      if (img.naturalWidth > 0 && img.naturalHeight > 0) { state.aspect = img.naturalHeight / img.naturalWidth; draw(); }
    });
    $("edForm").addEventListener("submit", saveEditor);
    $("edDelete").addEventListener("click", deleteSelected);
    $("camSelect").addEventListener("change", function () { selectCamera($("camSelect").value); });
    $("period").addEventListener("change", function () { loadOverview(); loadSeries(); });

    api("GET", "/api/cameras").then(function (res) {
      state.cameras = res.ok ? res.data.cameras : [];
      state.meta = res.ok ? res.data.camera_meta : {};
      var sel = $("camSelect");
      sel.replaceChildren();
      state.cameras.forEach(function (id) { var o = el("option", cameraLabel(id)); o.value = id; sel.appendChild(o); });
      var saved = Shell.prefs.get("zones.camera", null);
      var first = state.cameras.indexOf(saved) >= 0 ? saved : (state.cameras[0] || null);
      if (first) sel.value = first;
      selectCamera(first);
      loadOverview(); pollLive(); setInterval(pollLive, 1500); setInterval(loadOverview, 15000);
    });
  }

  window.ZonesPage = { state: state, init: init };
  init();
})();
