/*
 * Page « Carte » : plans par étage, caméras avec champ de vision, cibles en direct, placement à la souris,
 * rejeu d'un trajet enregistré et analyse des déplacements.
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 */
(function () {
  "use strict";

  var L = window.MapLogic, NS = "http://www.w3.org/2000/svg";
  var DEFAULTS = { heading: 0, fov: 70, view_range: 0.18 };
  var REPLAY_COLOR = "#7a5af8";
  var state = { floors: [], floorId: null, meta: {}, targets: [], editing: false, selected: null, drag: null,
                trajectories: [], replay: null, timer: null, version: 0 };

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function api(method, path, body) {
    var options = { method: method, headers: {} };
    if (body instanceof FormData) options.body = body;
    else if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
    });
  }
  function errorText(res) {
    var d = res.data && res.data.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && !Array.isArray(d) && d.message) return d.message;
    if (Array.isArray(d)) return "Saisie invalide : " + d.map(function (x) { return (x.loc || []).slice(-1) + " " + x.msg; }).join(" ; ");
    return "Erreur " + res.status;
  }
  function duration(sec) {
    if (sec < 1) return "< 1 s";
    if (sec < 60) return Math.round(sec) + " s";
    var m = Math.floor(sec / 60);
    return sec < 3600 ? m + " min " + Math.round(sec % 60) + " s" : Math.floor(sec / 3600) + " h " + (m % 60) + " min";
  }
  function cameraLabel(id) { return (state.meta[id] && state.meta[id].label) || id; }
  function currentFloor() {
    return state.floors.filter(function (f) { return f.id === state.floorId; })[0] || null;
  }
  function hasFloors() { return state.floors.length > 0; }

  // ---- dessin ----------------------------------------------------------------------------------------
  function planTargets() {
    if (state.replay) return state.replay.targets;
    return state.targets;
  }

  function draw(force) {
    var floor = currentFloor();
    var size = floor ? L.planSize(floor.width, floor.height) : L.planSize(0, 0);
    var svg = $("mapSvg");
    svg.setAttribute("viewBox", "0 0 " + size.width + " " + size.height);
    var meta = L.camerasOn(state.meta, floor ? floor.id : null);
    var image = L.safeImage(floor ? floor.image_url : null);

    SitePlan.render(svg, $("mapStatus"), meta, planTargets(), {
      width: size.width, height: size.height, force: !!force,
      signature: [image, state.editing, state.selected, state.version].join("|"),
      before: function (root, doc, dim) {
        var bg = doc.createElementNS(NS, "image");
        bg.setAttribute("href", image);
        bg.setAttribute("x", "0"); bg.setAttribute("y", "0");
        bg.setAttribute("width", String(dim.width)); bg.setAttribute("height", String(dim.height));
        bg.setAttribute("preserveAspectRatio", "none");
        root.appendChild(bg);
        Object.keys(meta).forEach(function (id) {
          var m = meta[id];
          if (!m.placed || typeof m.fov !== "number" || typeof m.view_range !== "number") return;
          var d = L.wedgePath(m.x * dim.width, m.y * dim.height, m.view_range * dim.width, m.heading || 0, m.fov);
          if (!d) return;
          var w = doc.createElementNS(NS, "path");
          w.setAttribute("d", d); w.setAttribute("class", "map-fov" + (id === state.selected && state.editing ? " is-selected" : ""));
          root.appendChild(w);
        });
      }
    });
    if (!floor && hasFloors()) $("mapStatus").textContent = "Aucun étage sélectionné.";
    $("mapSvg").classList.toggle("is-editing", state.editing);
    var onFloor = Object.keys(meta).length;
    $("mapHint").textContent = state.editing
      ? "Glissez une caméra pour la déplacer. Sélectionnez-en une pour régler sa direction et son champ de vision."
      : (onFloor ? "" : (hasFloors() ? "Aucune caméra n'est placée sur cet étage. Cliquez sur « Modifier le plan »." : ""));
  }

  // ---- étages ----------------------------------------------------------------------------------------
  function renderTabs() {
    var box = $("floorTabs");
    box.replaceChildren();
    if (!hasFloors()) { box.hidden = true; return; }
    box.hidden = false;
    state.floors.forEach(function (f) {
      var b = el("button", f.name);
      b.type = "button"; b.setAttribute("role", "tab");
      b.setAttribute("aria-pressed", String(f.id === state.floorId));
      b.addEventListener("click", function () { selectFloor(f.id); });
      box.appendChild(b);
    });
  }

  function selectFloor(id) {
    state.floorId = id; state.selected = null;
    Shell.prefs.set("map.floor", id);
    renderTabs(); renderEdit(); draw(true); loadCoverage();
  }

  /** Couverture géométrique de l'étage (champ de vision uniquement : murs et obstacles ignorés). */
  function loadCoverage() {
    var info = $("cvInfo"), body = $("cvCams").tBodies[0];
    body.replaceChildren();
    if (state.floorId == null) { info.textContent = "Aucun étage."; return; }
    var id = state.floorId;
    api("GET", "/api/floors/" + id + "/coverage").then(function (res) {
      if (id !== state.floorId) return;
      if (!res.ok) { info.textContent = "Couverture indisponible."; return; }
      var c = res.data;
      if (!c.cameras_counted) { info.textContent = "Aucune caméra active placée sur cet étage."; return; }
      info.textContent = c.covered_pct + " % du plan est dans le champ d'au moins une caméra (" + c.overlap_pct +
        " % vu par plusieurs, " + c.blind_pct + " % en angle mort). Estimation géométrique : les murs et obstacles ne sont pas pris en compte, la couverture réelle est donc inférieure ou égale.";
      c.cameras.forEach(function (k) {
        var tr = body.insertRow();
        tr.insertCell().textContent = k.label || k.camera_id;
        tr.insertCell().textContent = k.covered_pct + " %";
        tr.insertCell().textContent = k.only_pct + " %";
      });
    });
  }

  function loadFloors() {
    return api("GET", "/api/floors").then(function (res) {
      if (!res.ok) { Shell.toast("Étages indisponibles.", "warning"); return; }
      state.floors = res.data.floors || [];
      var ids = state.floors.map(function (f) { return f.id; });
      if (ids.indexOf(state.floorId) < 0) {
        var saved = Shell.prefs.get("map.floor", null);
        state.floorId = ids.indexOf(saved) >= 0 ? saved : (ids.length ? ids[0] : null);
      }
      renderTabs();
    });
  }

  function loadCameras() {
    return api("GET", "/api/cameras").then(function (res) {
      if (res.ok) { state.meta = res.data.camera_meta || {}; state.version++; }
    });
  }

  function pollLive() {
    if (state.replay) return;
    api("GET", "/api/supervision/cameras").then(function (res) {
      if (!res.ok) return;
      state.targets = res.data.targets || [];
      var active = state.targets.filter(function (t) { return t.active; }).length;
      $("liveChip").textContent = state.targets.length ? state.targets.length + " cible(s), " + active + " vue(s)" : "Aucune cible";
      draw(false);
    });
  }

  // ---- édition -----------------------------------------------------------------------------------------
  function saveCamera(id, fields) {
    return api("PUT", "/api/admin/cameras/" + encodeURIComponent(id), fields).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); }
      return loadCameras().then(function () { renderEdit(); draw(true); loadCoverage(); return res.ok; });
    });
  }

  function slider(label, min, max, step, value, unit, onChange) {
    var wrap = el("div", null, "field");
    var id = "sl" + label.replace(/\W/g, "");
    var lab = el("label", label + " : " + value + unit); lab.setAttribute("for", id);
    var input = el("input"); input.type = "range"; input.id = id;
    input.min = String(min); input.max = String(max); input.step = String(step); input.value = String(value);
    input.addEventListener("input", function () { lab.textContent = label + " : " + input.value + unit; onChange(Number(input.value), false); });
    input.addEventListener("change", function () { onChange(Number(input.value), true); });
    wrap.append(lab, input);
    return wrap;
  }

  function renderEdit() {
    $("editPanel").hidden = !state.editing;
    if (!state.editing) return;

    var un = L.unplaced(state.meta, hasFloors());
    var box = $("unplacedBox");
    box.replaceChildren();
    if (!hasFloors()) {
      box.appendChild(el("p", "Ajoutez d'abord un étage (ci-dessous) pour pouvoir y placer les caméras. En attendant, le plan illustratif est affiché.", "muted"));
    } else if (un.length) {
      box.appendChild(el("h3", "Caméras à placer sur « " + (currentFloor() ? currentFloor().name : "") + " »"));
      un.forEach(function (id) {
        var b = el("button", cameraLabel(id), "btn small");
        b.type = "button";
        b.setAttribute("aria-pressed", String(state.selected === id));
        b.addEventListener("click", function () { state.selected = id; renderEdit(); draw(true); });
        box.appendChild(b);
      });
      box.appendChild(el("p", "Choisissez une caméra, puis cliquez sur le plan.", "muted"));
    }

    var panel = $("camPanel");
    panel.replaceChildren();
    var m = state.selected && state.meta[state.selected];
    if (m && m.placed && m.floor_id === state.floorId) {
      var id = state.selected;
      panel.appendChild(el("h3", "Caméra : " + m.label));
      var heading = typeof m.heading === "number" ? m.heading : DEFAULTS.heading;
      var fov = typeof m.fov === "number" ? m.fov : DEFAULTS.fov;
      var range = typeof m.view_range === "number" ? m.view_range : DEFAULTS.view_range;
      function live(field) {
        return function (value, commit) {
          state.meta[id][field] = value;
          if (state.meta[id].fov == null) state.meta[id].fov = fov;
          if (state.meta[id].view_range == null) state.meta[id].view_range = range;
          draw(true);
          if (commit) {
            var body = {}; body[field] = value; body.fov = state.meta[id].fov; body.view_range = state.meta[id].view_range;
            body.heading = state.meta[id].heading == null ? heading : state.meta[id].heading;
            saveCamera(id, body);
          }
        };
      }
      panel.append(
        slider("Direction", 0, 360, 5, Math.round(heading), "°", live("heading")),
        slider("Ouverture", 10, 360, 5, Math.round(fov), "°", live("fov")),
        slider("Portée", 2, 60, 1, Math.round(range * 100), " %", function (v, commit) { live("view_range")(v / 100, commit); }));
      var remove = el("button", "Retirer du plan", "btn small danger");
      remove.type = "button";
      remove.addEventListener("click", function () {
        saveCamera(id, { floor_id: null, x: null, y: null, heading: null, fov: null, view_range: null }).then(function () { state.selected = null; renderEdit(); draw(true); });
      });
      panel.appendChild(remove);
    }

    var f = currentFloor();
    var actions = $("floorActions");
    actions.replaceChildren();
    if (f) {
      var del = el("button", "Supprimer l'étage « " + f.name + " »", "btn small danger");
      del.type = "button";
      del.addEventListener("click", function () {
        if (!confirm("Supprimer l'étage « " + f.name + " » ? Ses caméras redeviendront non placées.")) return;
        api("DELETE", "/api/floors/" + f.id).then(function (res) {
          if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
          Shell.toast("Étage supprimé.");
          Promise.all([loadFloors(), loadCameras()]).then(function () { renderEdit(); draw(true); });
        });
      });
      var rename = el("button", "Renommer / remplacer le plan", "btn small");
      rename.type = "button";
      rename.addEventListener("click", function () {
        var name = $("fName").value.trim(), level = $("fLevel").value, file = $("fFile").files[0];
        var fd = new FormData();
        if (name) fd.append("name", name);
        fd.append("level", level);
        if (file) fd.append("file", file);
        api("PUT", "/api/floors/" + f.id, fd).then(function (res) {
          if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
          Shell.toast(res.data.ratio_changed ? "Plan remplacé : ses proportions ont changé, vérifiez la position des caméras." : "Étage mis à jour.",
                      res.data.ratio_changed ? "warning" : undefined);
          loadFloors().then(function () { state.version++; renderEdit(); draw(true); });
        });
      });
      actions.append(rename, del);
    }
  }

  function onPlanPointer(kind, ev) {
    var floor = currentFloor();
    var svg = $("mapSvg");
    if (!state.editing || !floor) return;
    var hit = ev.target && ev.target.closest ? ev.target.closest("[data-camera]") : null;

    if (kind === "down") {
      if (hit) {
        var id = hit.getAttribute("data-camera");
        state.selected = id; state.drag = { id: id, moved: false };
        if (svg.setPointerCapture && ev.pointerId !== undefined) { try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* ignoré */ } }
        renderEdit(); draw(true);
        return;
      }
      var m = state.selected && state.meta[state.selected];
      if (m && !(m.placed && m.floor_id === floor.id)) {            // caméra à placer : un clic sur le plan
        var p = L.toFraction(svg.getBoundingClientRect(), ev.clientX, ev.clientY);
        if (!p) return;
        saveCamera(state.selected, { floor_id: floor.id, x: p.x, y: p.y, heading: DEFAULTS.heading,
                                     fov: DEFAULTS.fov, view_range: DEFAULTS.view_range });
      }
    } else if (kind === "move" && state.drag) {
      var q = L.toFraction(svg.getBoundingClientRect(), ev.clientX, ev.clientY);
      var cam = state.meta[state.drag.id];
      if (!q || !cam) return;
      cam.x = q.x; cam.y = q.y; state.drag.moved = true;
      draw(true);
    } else if (kind === "up" && state.drag) {
      var d = state.drag; state.drag = null;
      var c = state.meta[d.id];
      if (d.moved && c) saveCamera(d.id, { x: c.x, y: c.y });
    }
  }

  // ---- rejeu et analyse ---------------------------------------------------------------------------------
  function loadTrajectories() {
    var minutes = $("rPeriod").value;
    return api("GET", "/api/history/trajectories?minutes=" + encodeURIComponent(minutes)).then(function (res) {
      state.trajectories = res.ok ? (res.data.items || []) : [];
      var pick = $("rPick");
      pick.replaceChildren();
      var none = el("option", state.trajectories.length ? "Choisir un trajet…" : "Aucun trajet enregistré");
      none.value = ""; pick.appendChild(none);
      state.trajectories.forEach(function (t, i) {
        var o = el("option", (t.label || "Trajet") + " — " + new Date(t.started).toLocaleString() + " — " + t.moves.length + " étape(s)");
        o.value = String(i); pick.appendChild(o);
      });
      stopReplay();
      renderAnalysis();
    });
  }

  function renderAnalysis() {
    var a = L.analyse(state.trajectories);
    $("aInfo").textContent = a.trajectories ? a.trajectories + " trajet(s) sur la période." : "Aucun déplacement enregistré sur la période.";
    var body = $("aCams").tBodies[0];
    body.replaceChildren();
    a.cameras.forEach(function (c) {
      var tr = body.insertRow();
      tr.insertCell().textContent = cameraLabel(c.camera_id);
      tr.insertCell().textContent = String(c.visits);
      tr.insertCell().textContent = c.dwell_sec > 0 ? duration(c.dwell_sec) : "—";
    });
    var tb = $("aTrans").tBodies[0];
    tb.replaceChildren();
    a.transitions.slice(0, 8).forEach(function (t) {
      var tr = tb.insertRow();
      tr.insertCell().textContent = cameraLabel(t.from) + " → " + cameraLabel(t.to);
      tr.insertCell().textContent = String(t.count);
    });
    $("aCams").hidden = !a.cameras.length; $("aTrans").hidden = !a.transitions.length;
  }

  function stopReplay() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    $("rPlay").textContent = "Lecture";
  }

  function setReplayTime(t) {
    var r = state.replay;
    if (!r) return;
    r.t = t;
    var done = L.movesUntil(r.moves, t);
    var last = done.length ? done[done.length - 1] : null;
    r.targets = [{ id: 0, label: r.label, color: REPLAY_COLOR, active: !!last && t < r.end, camera_id: last ? last.camera_id : null,
                   score: last ? last.score : null,
                   path: done.map(function (m) { return { camera_id: m.camera_id, ts: m.ts, score: m.score }; }) }];
    $("rClock").textContent = new Date(t * 1000).toLocaleTimeString() + (last ? " — " + cameraLabel(last.camera_id) : "");
    // L'étage affiché suit la caméra courante du trajet.
    if (last && state.meta[last.camera_id] && state.meta[last.camera_id].floor_id && state.meta[last.camera_id].floor_id !== state.floorId) {
      state.floorId = state.meta[last.camera_id].floor_id; renderTabs();
    }
    draw(true);
  }

  function pickTrajectory() {
    stopReplay();
    var idx = $("rPick").value;
    var t = idx === "" ? null : state.trajectories[Number(idx)];
    if (!t) { state.replay = null; $("rSlider").disabled = true; $("rPlay").disabled = true; $("rClock").textContent = ""; draw(true); return; }
    var moves = t.moves.filter(function (m) { return typeof m.ts === "number"; });
    var sp = L.span(moves);
    if (!sp) return;
    state.replay = { moves: moves, start: sp[0], end: sp[1], label: t.label || "Trajet", t: sp[0], targets: [] };
    $("rSlider").disabled = false; $("rPlay").disabled = false; $("rSlider").value = "0";
    setReplayTime(sp[0]);
  }

  function slideTo() {
    var r = state.replay;
    if (!r) return;
    setReplayTime(r.start + (r.end - r.start) * Number($("rSlider").value) / 1000);
  }

  function togglePlay() {
    var r = state.replay;
    if (!r) return;
    if (state.timer) { stopReplay(); return; }
    if (r.t >= r.end) { setReplayTime(r.start); $("rSlider").value = "0"; }
    $("rPlay").textContent = "Pause";
    state.timer = setInterval(function () {
      var next = r.t + 0.1 * Number($("rSpeed").value);
      if (next >= r.end) { next = r.end; stopReplay(); }
      $("rSlider").value = String(r.end > r.start ? Math.round((next - r.start) / (r.end - r.start) * 1000) : 1000);
      setReplayTime(next);
    }, 100);
  }

  // ---- démarrage -----------------------------------------------------------------------------------------
  function init() {
    Shell.mount({ active: "map" });
    $("btnEdit").addEventListener("click", function () {
      state.editing = !state.editing; state.selected = null;
      $("btnEdit").setAttribute("aria-pressed", String(state.editing));
      renderEdit(); draw(true);
    });
    var svg = $("mapSvg");
    ["down", "move", "up"].forEach(function (k) {
      svg.addEventListener("pointer" + k, function (ev) { onPlanPointer(k, ev); });
    });
    svg.addEventListener("pointercancel", function (ev) { onPlanPointer("up", ev); });

    $("floorForm").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var file = $("fFile").files[0];
      if (!file) { Shell.toast("Choisissez le fichier du plan.", "warning"); return; }
      var fd = new FormData();
      fd.append("name", $("fName").value.trim()); fd.append("level", $("fLevel").value); fd.append("file", file);
      api("POST", "/api/floors", fd).then(function (res) {
        if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
        Shell.toast("Étage ajouté.");
        $("floorForm").reset();
        loadFloors().then(function () { selectFloor(res.data.id); });
      });
    });
    $("rPeriod").addEventListener("change", loadTrajectories);
    $("rPick").addEventListener("change", pickTrajectory);
    $("rSlider").addEventListener("input", function () { stopReplay(); slideTo(); });
    $("rPlay").addEventListener("click", togglePlay);

    Promise.all([loadFloors(), loadCameras()]).then(function () {
      renderEdit(); draw(true); loadCoverage(); pollLive(); setInterval(pollLive, 3000);
    });
    loadTrajectories();
  }

  window.MapPage = { state: state, init: init };
  init();
})();
