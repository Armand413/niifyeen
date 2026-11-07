/*
 * Page « Enregistrements » : réglages, enregistrement manuel, liste des segments, lecteur avec chronologie.
 * Le lecteur affiche les images une par une (aucun codec vidéo requis). Tout le texte est inséré avec textContent.
 */
(function () {
  "use strict";

  var L = window.RecordingsLogic;
  var state = { settings: null, hard: false, eventTypes: [], cameras: [], meta: {}, status: null, list: [], cur: null, idx: 0,
                playing: false, timer: null, schedules: [], days: [] };

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
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
  function cameraLabel(id) { return id === "*" ? "Toutes" : ((state.meta[id] && state.meta[id].label) || id); }
  function when(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "" : d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function duration(sec) {
    if (sec === null || sec === undefined) return "…";
    if (sec < 60) return Math.round(sec) + " s";
    return Math.floor(sec / 60) + " min " + Math.round(sec % 60) + " s";
  }

  // ---- état général -------------------------------------------------------------------------------------
  function paintStatus() {
    var st = state.status; if (!st) return;
    $("state").textContent = st.hard_disabled ? "Interdit par l'administrateur" : (st.enabled ? (st.recording.length ? "Enregistre : " + st.recording.map(cameraLabel).join(", ") : "Actif") : "Désactivé");
    $("usage").textContent = L.formatBytes(st.used_bytes) + " utilisés" + (st.free_bytes != null ? " · " + L.formatBytes(st.free_bytes) + " libres" : "");
    var n = $("offNotice");
    n.hidden = st.enabled;
    n.textContent = st.hard_disabled ? "L'enregistrement est interdit sur ce serveur (variable NIIFYEEN_RECORDING_DISABLED). Il ne peut pas être activé depuis l'interface."
      : "L'enregistrement vidéo est désactivé. Les enregistrements existants restent consultables ; activez-le dans les réglages pour en créer.";
    var info = [];
    Object.keys(st.sessions || {}).forEach(function (c) {
      var s = st.sessions[c]; if (s) info.push(cameraLabel(c) + " : " + L.triggerLabel(s.trigger) + (s.until ? " jusqu'à " + when(s.until) : ""));
    });
    $("mInfo").textContent = info.join(" · ") + (st.last_error ? (info.length ? " · " : "") + "Dernière erreur : " + st.last_error : "");
    $("mStart").disabled = !st.enabled; $("sEnabled").disabled = st.hard_disabled;
  }

  function loadStatus() { return api("GET", "/api/recordings/status").then(function (r) { if (r.ok) { state.status = r.data; paintStatus(); } }); }

  // ---- liste -------------------------------------------------------------------------------------------------
  function loadList() {
    var q = "?minutes=" + encodeURIComponent($("lPeriod").value) + ($("lCamera").value ? "&camera_id=" + encodeURIComponent($("lCamera").value) : "");
    return api("GET", "/api/recordings" + q).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.list = res.data.recordings; state.status = res.data.status; paintStatus(); renderList();
    });
  }

  function renderList() {
    var body = $("rows"); body.replaceChildren();
    state.list.forEach(function (r) {
      var tr = el("tr"); tr.setAttribute("data-rec", String(r.id));
      if (state.cur && state.cur.id === r.id) tr.setAttribute("aria-selected", "true");
      tr.append(el("td", cameraLabel(r.camera_id)), el("td", when(r.started_at), "num"), el("td", duration(r.duration_sec), "num"),
                el("td", L.triggerLabel(r.trigger)), el("td", L.formatBytes(r.bytes), "num"));
      var st = el("td"); st.appendChild(el("span", L.statusLabel(r.status), "tag " + (r.status === "recording" ? "warn" : (r.status === "error" ? "off" : "ok")))); tr.appendChild(st);
      tr.addEventListener("click", function () { openRecording(r.id, 0); });
      body.appendChild(tr);
    });
    $("empty").hidden = state.list.length > 0; $("table").hidden = state.list.length === 0;
  }

  // ---- lecteur -------------------------------------------------------------------------------------------------
  function stop() { state.playing = false; clearTimeout(state.timer); $("pPlay").textContent = "Lecture"; }

  function openRecording(id, startSec) {
    stop();
    return api("GET", "/api/recordings/" + id).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.cur = res.data;
      $("pTitle").textContent = "Lecture — " + cameraLabel(res.data.camera_id);
      $("pImg").hidden = !res.data.frames; $("pEmpty").hidden = res.data.frames > 0;
      $("pEmpty").textContent = "Cet enregistrement ne contient aucune image.";
      $("pSlider").max = String(Math.max(0, res.data.frames - 1)); $("pSlider").disabled = res.data.frames < 2;
      $("pPlay").disabled = res.data.frames < 2;
      $("pDelete").hidden = res.data.status === "recording";
      $("pInfo").textContent = "Image brute (sans cadres ni noms) à la résolution du flux · " + res.data.frames + " images · déclenché : " + L.triggerLabel(res.data.trigger) +
        (res.data.reason ? " (" + res.data.reason + ")" : "");
      show(Math.max(0, L.frameAt(res.data.t, startSec || 0)));
      renderList(); loadEvents();
    });
  }

  function show(i) {
    var c = state.cur; if (!c || !c.frames) return;
    state.idx = Math.max(0, Math.min(c.frames - 1, i));
    $("pImg").src = "/api/recordings/" + c.id + "/frame/" + state.idx;
    $("pSlider").value = String(state.idx);
    $("pClock").textContent = L.formatClock(c.t[state.idx]) + " / " + L.formatClock(c.t[c.t.length - 1]);
    var allow = state.settings && state.settings.allow_export && c.t0;
    $("pExport").hidden = !allow;
    if (allow) {
      var start = new Date(Date.parse(c.t0) + c.t[state.idx] * 1000), end = new Date(Math.min(Date.parse(c.t0) + c.t[c.t.length - 1] * 1000 + 1000, start.getTime() + 15 * 60000));
      $("pExport").href = "/api/recordings/export.avi?camera_id=" + encodeURIComponent(c.camera_id) + "&start=" + encodeURIComponent(start.toISOString()) + "&end=" + encodeURIComponent(end.toISOString());
    }
  }

  function tick() {
    var c = state.cur; if (!state.playing || !c) return;
    if (state.idx >= c.frames - 1) { stop(); return; }
    var delay = L.nextDelay(c.t, state.idx, Number($("pSpeed").value));
    state.timer = setTimeout(function () { show(state.idx + 1); tick(); }, delay);
  }

  function togglePlay() {
    if (state.playing) { stop(); return; }
    if (!state.cur) return;
    if (state.idx >= state.cur.frames - 1) show(0);
    state.playing = true; $("pPlay").textContent = "Pause"; tick();
  }

  function loadEvents() {
    var c = state.cur, ul = $("pEvents"); ul.replaceChildren();
    if (!c || !c.t0) return;
    var end = c.ended_at || new Date().toISOString();
    var q = "?start=" + encodeURIComponent(c.t0) + "&end=" + encodeURIComponent(end) + "&camera_id=" + encodeURIComponent(c.camera_id) + "&types=targets,people,cameras,analytics";
    api("GET", "/api/investigation/timeline" + q).then(function (res) {
      if (!res.ok || state.cur !== c) return;
      var items = res.data.items.slice().reverse();
      items.forEach(function (it) {
        var off = (Date.parse(it.ts) - Date.parse(c.t0)) / 1000;
        var li = el("li"); li.appendChild(el("span", it.title)); li.appendChild(el("span", L.formatClock(off), "muted"));
        li.style.cursor = "pointer";
        li.addEventListener("click", function () { stop(); show(Math.max(0, L.frameAt(c.t, off))); });
        ul.appendChild(li);
      });
      if (!items.length) ul.appendChild(el("li", "Aucun événement enregistré pendant ce segment."));
    });
  }

  function deleteCurrent() {
    var c = state.cur;
    if (!c || !confirm("Supprimer définitivement cet enregistrement (" + when(c.started_at) + ") ?")) return;
    api("DELETE", "/api/recordings/" + c.id).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      stop(); state.cur = null; $("pImg").hidden = true; $("pEmpty").hidden = false; $("pEmpty").textContent = "Choisissez un enregistrement dans la liste.";
      $("pPlay").disabled = true; $("pSlider").disabled = true; $("pExport").hidden = true; $("pDelete").hidden = true; $("pEvents").replaceChildren();
      Shell.toast("Enregistrement supprimé."); loadList();
    });
  }

  // ---- enregistrement manuel ----------------------------------------------------------------------------------------
  function startManual() {
    api("POST", "/api/recordings/start", { camera_id: $("mCamera").value, minutes: Number($("mMinutes").value) }).then(function (res) {
      Shell.toast(res.ok ? "Enregistrement démarré." : errorText(res), res.ok ? "info" : "warning"); loadStatus();
    });
  }
  function stopManual() {
    api("POST", "/api/recordings/stop", { camera_id: $("mCamera").value }).then(function (res) {
      Shell.toast(res.ok && res.data.stopped ? "Enregistrement arrêté." : "Aucun enregistrement manuel sur cette caméra.", "info");
      setTimeout(function () { loadStatus(); loadList(); }, 600);
    });
  }

  // ---- réglages ---------------------------------------------------------------------------------------------------------
  function chip(box, label, on, onToggle) {
    var b = el("button", label); b.type = "button"; b.setAttribute("aria-pressed", String(on));
    b.addEventListener("click", function () { var now = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", String(now)); onToggle(now); });
    box.appendChild(b);
  }

  function paintSettings() {
    var s = state.settings; if (!s) return;
    $("sEnabled").checked = !!s.enabled; $("sFps").value = s.fps; $("sRetention").value = s.retention_days; $("sPre").value = s.pre_roll_sec;
    $("sPost").value = s.post_roll_sec; $("sMax").value = s.max_storage_mb; $("sExport").checked = !!s.allow_export;
    var box = $("sEvents"); box.replaceChildren();
    state.eventTypes.forEach(function (t) {
      chip(box, t, s.event_types.indexOf(t) >= 0, function (on) {
        s.event_types = s.event_types.filter(function (x) { return x !== t; }); if (on) s.event_types.push(t);
      });
    });
    var cams = $("sCams"); cams.replaceChildren();
    state.cameras.forEach(function (c) {
      chip(cams, cameraLabel(c), s.cameras.indexOf(c) >= 0, function (on) {
        s.cameras = s.cameras.filter(function (x) { return x !== c; }); if (on) s.cameras.push(c);
      });
    });
    state.schedules = s.schedules.slice(); renderSchedules();
  }

  function renderSchedules() {
    var ul = $("sSchedules"); ul.replaceChildren();
    state.schedules.forEach(function (sch, i) {
      var li = el("li"); li.appendChild(el("span", L.scheduleLabel(sch, cameraLabel(sch.camera_id))));
      var rm = el("button", "Retirer", "btn small"); rm.type = "button";
      rm.addEventListener("click", function () { state.schedules.splice(i, 1); renderSchedules(); });
      li.appendChild(rm); ul.appendChild(li);
    });
    if (!state.schedules.length) ul.appendChild(el("li", "Aucune plage horaire."));
  }

  function addSchedule() {
    if (!state.days.length) { Shell.toast("Choisissez au moins un jour.", "warning"); return; }
    state.schedules.push({ camera_id: $("nCam").value, days: state.days.slice().sort(), start: $("nStart").value, end: $("nEnd").value });
    renderSchedules();
  }

  function saveSettings() {
    var s = state.settings;
    var body = { enabled: $("sEnabled").checked, fps: Number($("sFps").value), retention_days: Number($("sRetention").value),
                 pre_roll_sec: Number($("sPre").value), post_roll_sec: Number($("sPost").value), max_storage_mb: Number($("sMax").value),
                 allow_export: $("sExport").checked, event_types: s.event_types, cameras: s.cameras, schedules: state.schedules };
    api("PUT", "/api/recordings/settings", body).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.settings = res.data.settings; paintSettings(); Shell.toast("Réglages enregistrés."); loadStatus();
    });
  }

  // ---- démarrage ------------------------------------------------------------------------------------------------------------
  function init() {
    Shell.mount({ active: "recordings" });
    $("pPlay").addEventListener("click", togglePlay);
    $("pSlider").addEventListener("input", function () { stop(); show(Number($("pSlider").value)); });
    $("pDelete").addEventListener("click", deleteCurrent);
    $("mStart").addEventListener("click", startManual);
    $("mStop").addEventListener("click", stopManual);
    $("sSave").addEventListener("click", saveSettings);
    $("nAdd").addEventListener("click", addSchedule);
    $("lCamera").addEventListener("change", loadList); $("lPeriod").addEventListener("change", loadList);
    var days = $("nDays");
    L.DAYS.forEach(function (name, i) { chip(days, name, false, function (on) { state.days = state.days.filter(function (d) { return d !== i; }); if (on) state.days.push(i); }); });

    Promise.all([
      api("GET", "/api/cameras").then(function (res) {
        state.cameras = res.ok ? res.data.cameras : []; state.meta = res.ok ? (res.data.camera_meta || {}) : {};
        ["lCamera", "mCamera", "nCam"].forEach(function (id) {
          if (id === "nCam") { var all = el("option", "Toutes les caméras"); all.value = "*"; $(id).appendChild(all); }
          state.cameras.forEach(function (c) { var o = el("option", cameraLabel(c)); o.value = c; $(id).appendChild(o); });
        });
      }),
      api("GET", "/api/recordings/settings").then(function (res) {
        if (res.ok) { state.settings = res.data.settings; state.hard = res.data.hard_disabled; state.eventTypes = res.data.event_types; }
      })
    ]).then(function () {
      paintSettings(); return loadList();
    }).then(function () {
      var link = L.parseDeepLink(window.location.search);
      if (link.id) openRecording(link.id, link.t);
    });
    setInterval(function () { loadStatus(); }, 5000);
    setInterval(function () { if (!state.playing) loadList(); }, 15000);
  }

  window.RecordingsPage = { state: state, init: init };
  init();
})();
