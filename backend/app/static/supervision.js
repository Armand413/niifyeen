/*
 * Centre de supervision : grille de caméras (1 à 16), état en direct, fil d'événements,
 * détails d'une caméra, cibles. Les images arrivent par UN seul WebSocket (/ws/video) ;
 * l'état par /ws/live ; les événements par /api/events.
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 */
(function () {
  "use strict";

  var LAYOUTS = [1, 2, 4, 6, 9, 16];
  var GRID = { 1: [1, 1], 2: [2, 1], 4: [2, 2], 6: [3, 2], 9: [3, 3], 16: [4, 4] };   // [colonnes, lignes]
  var FPS_BY_LAYOUT = { 1: 15, 2: 12, 4: 10, 6: 8, 9: 6, 16: 5 };
  var STATUS_TEXT = { online: "En ligne", connecting: "Connexion…", reconnecting: "Reconnexion…", offline: "Hors ligne", stopped: "Arrêtée" };
  var STATUS_DOT = { online: "ok", connecting: "warn", reconnecting: "warn", offline: "bad", stopped: "" };
  var FAMILIES = {
    targets: ["target_seen", "target_moved", "target_lost"],
    people: ["person_recognized", "person_inactive_seen"],
    cameras: ["camera_offline", "camera_online", "camera_added", "camera_removed", "camera_enabled", "camera_disabled",
              "camera_obstructed", "camera_cleared", "camera_frozen"]
  };
  var EVENT_ICON = { target_seen: "target", target_moved: "route", target_lost: "target",
                     person_recognized: "user", person_inactive_seen: "user", camera_offline: "camera", camera_online: "camera",
                     camera_added: "camera", camera_removed: "camera", camera_enabled: "camera", camera_disabled: "camera",
                     camera_obstructed: "camera", camera_cleared: "camera", camera_frozen: "camera" };
  var FILTERS = [["", "Tout"], ["targets", "Cibles"], ["people", "Personnes"], ["cameras", "Caméras"]];
  var MAX_EVENTS = 200;

  var prefs = Shell.prefs;
  var state = {
    cameras: [], meta: {}, status: {}, targets: [], people: [],
    layout: LAYOUTS.indexOf(prefs.get("layout", 4)) >= 0 ? prefs.get("layout", 4) : 4,
    page: 0, annotations: prefs.get("annotations", true) !== false, sound: !!prefs.get("sound", false),
    selected: null, family: prefs.get("family", ""), tab: "Events",
    events: [], feedLastId: 0, unread: 0, fetching: false, identitiesVisible: true,
    camerasVersion: undefined, reloading: false
  };
  var tiles = {};          // id -> références DOM de la tuile
  var detail = null;       // références DOM du panneau « Caméra »
  var videoSocket = null, videoDelay = 500;
  var liveSocket = null, liveDelay = 500, pending = null;

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function safeColor(c) { return /^#[0-9a-f]{6}$/i.test(c || "") ? c : "#ef4444"; }
  function cameraById(id) { return state.cameras.filter(function (c) { return c.id === id; })[0]; }
  function cameraLabel(id) { return ((state.meta[id] || cameraById(id) || {}).label) || id || "?"; }
  function fmtTime(iso) { var d = new Date(iso); return isNaN(d.getTime()) ? "" : d.toLocaleTimeString(); }
  function perPage() { return state.layout; }
  function pageCount() { return Math.max(1, Math.ceil(state.cameras.length / perPage())); }
  function visibleCameras() {
    state.page = Math.min(state.page, pageCount() - 1);
    return state.cameras.slice(state.page * perPage(), state.page * perPage() + perPage());
  }

  // ---- tuiles ----------------------------------------------------------------------------------
  function makeTile(cam) {
    var root = el("div", null, "tile");
    root.setAttribute("data-cam", cam.id);
    root.tabIndex = 0;
    root.setAttribute("role", "button");
    root.setAttribute("aria-label", "Caméra " + cam.label);
    var img = el("img", null, "tile-img");
    img.alt = "Flux de la caméra " + cam.label;
    img.decoding = "async";

    var top = el("div", null, "tile-top");
    var name = el("div", null, "tile-name");
    var nameB = el("b", cam.label), loc = el("span", cam.location || "");
    name.append(nameB, loc);
    var st = el("div", null, "tile-state");
    var dot = el("span", null, "dot"), stText = el("span", ""), fps = el("span", "");
    st.append(dot, stText, fps);
    top.append(name, st);

    var bottom = el("div", null, "tile-bottom");
    var badges = el("div", null, "tile-badges");
    var persons = el("span", null, "badge");
    persons.title = "Personnes détectées";
    var personsText = el("span", "0");
    persons.append(Shell.icon("user"), personsText);
    var targetsBox = el("span", null, "tile-badges");
    badges.append(persons, targetsBox);
    var expand = el("button", null, "icon-btn");
    expand.type = "button"; expand.title = "Plein écran"; expand.setAttribute("aria-label", "Plein écran : " + cam.label);
    expand.appendChild(Shell.icon("expand"));
    bottom.append(badges, expand);

    var offline = el("div", "", "tile-offline");
    root.append(img, top, bottom, offline);

    root.addEventListener("click", function () { select(cam.id); });
    root.addEventListener("dblclick", function () { toggleFullscreen(root); });
    root.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(cam.id); }
    });
    expand.addEventListener("click", function (e) { e.stopPropagation(); toggleFullscreen(root); });

    return { root: root, img: img, dot: dot, stText: stText, fps: fps, persons: persons, personsText: personsText,
             targetsBox: targetsBox, offline: offline, url: null, prevUrl: null, sig: "" };
  }

  function buildTiles() {
    Object.keys(tiles).forEach(function (id) { if (tiles[id].url) URL.revokeObjectURL(tiles[id].url); });
    tiles = {};
    state.cameras.forEach(function (cam) { tiles[cam.id] = makeTile(cam); });
  }

  function renderGrid() {
    var grid = $("grid");
    var shown = visibleCameras();
    var base = GRID[state.layout];
    var cols = Math.max(1, Math.min(base[0], shown.length || 1));
    var rows = Math.max(1, Math.ceil(shown.length / cols));
    grid.style.setProperty("--cols", String(cols));
    grid.style.setProperty("--rows", String(rows));
    grid.replaceChildren.apply(grid, shown.map(function (c) { return tiles[c.id].root; }));
    $("empty").hidden = state.cameras.length > 0;
    grid.hidden = state.cameras.length === 0;

    var pages = pageCount();
    $("pager").hidden = pages <= 1;
    $("pageLabel").textContent = (state.page + 1) + " / " + pages;
    $("pagePrev").disabled = state.page <= 0;
    $("pageNext").disabled = state.page >= pages - 1;

    Array.prototype.forEach.call($("layoutSeg").children, function (b) {
      b.setAttribute("aria-pressed", String(Number(b.getAttribute("data-layout")) === state.layout));
    });
    Object.keys(tiles).forEach(function (id) { tiles[id].root.classList.toggle("selected", id === state.selected); });
    updateTiles();
    sendSubscription();
  }

  function updateTiles() {
    state.cameras.forEach(function (cam) {
      var t = tiles[cam.id];
      if (!t) return;
      var s = state.status[cam.id] || { status: "connecting", fps: 0, persons: 0 };
      var down = s.status !== "online";
      t.dot.className = "dot " + (STATUS_DOT[s.status] || "");
      t.stText.textContent = STATUS_TEXT[s.status] || s.status;
      t.fps.textContent = s.status === "online" ? s.fps.toFixed(0) + " i/s" : "";
      t.personsText.textContent = String(s.persons || 0);
      t.root.classList.toggle("is-down", down);
      t.offline.textContent = s.status === "offline" ? "Caméra hors ligne"
        : s.status === "reconnecting" ? "Reconnexion en cours…"
        : s.status === "connecting" ? "Connexion à la caméra…" : s.status === "stopped" ? "Caméra arrêtée" : "";

      var here = state.targets.filter(function (x) { return x.active && x.camera_id === cam.id; });
      var sig = here.map(function (x) { return x.id + x.label + x.color; }).join("|");
      if (sig !== t.sig) {
        t.sig = sig;
        t.targetsBox.replaceChildren.apply(t.targetsBox, here.map(function (x) {
          var b = el("span", null, "badge target-badge");
          b.style.setProperty("--target-color", safeColor(x.color));
          b.append(Shell.icon("target"), el("span", x.label));
          return b;
        }));
        t.root.classList.toggle("has-target", here.length > 0);
        if (here.length) t.root.style.setProperty("--target-color", safeColor(here[0].color));
        else t.root.style.removeProperty("--target-color");
      }
    });
    updateChips();
    updateDetails();
    updateTargetsPane();
  }

  // ---- indicateurs -------------------------------------------------------------------------------
  function setChip(id, iconName, label, value, extraClass) {
    var chip = $(id);
    chip.className = "chip" + (extraClass ? " " + extraClass : "");
    chip.replaceChildren(Shell.icon(iconName), el("span", label), el("b", value));
  }

  function updateChips() {
    var total = state.cameras.length;
    var online = state.cameras.filter(function (c) { return (state.status[c.id] || {}).status === "online"; }).length;
    var persons = state.cameras.reduce(function (n, c) { return n + ((state.status[c.id] || {}).persons || 0); }, 0);
    var active = state.targets.filter(function (t) { return t.active; }).length;
    setChip("chipCams", "camera", "En ligne", online + " / " + total);
    setChip("chipPeople", "user", "Personnes visibles", String(persons));
    setChip("chipTargets", "target", "Cibles détectées", active + " / " + state.targets.length, active ? "target" : "");
    var down = total - online;
    var chip = $("chipAlerts");
    chip.hidden = down === 0;
    if (down) setChip("chipAlerts", "bell", "Hors ligne", String(down), "alert");
    var count = $("countTargets");
    count.textContent = String(state.targets.length);
  }

  // ---- panneau « Caméra » ---------------------------------------------------------------------------
  function select(id) {
    state.selected = id;
    Object.keys(tiles).forEach(function (k) { tiles[k].root.classList.toggle("selected", k === id); });
    buildDetails();
    openTab("Camera");
    loadCameraEvents();
  }

  function buildDetails() {
    var box = $("cameraDetails");
    var cam = cameraById(state.selected);
    detail = null;
    if (!cam) { box.replaceChildren(el("p", "Cliquez sur une caméra pour voir ses informations.", "muted")); return; }

    var head = el("div", null, "section-title");
    head.textContent = cam.label + (cam.location ? " — " + cam.location : "");
    var dl = el("dl", null, "kv");
    var refs = {};
    [["status", "État"], ["source", "Source"], ["size", "Résolution"], ["fps", "Images / seconde"],
     ["latency", "Latence de traitement"], ["persons", "Personnes visibles"], ["reconnects", "Reconnexions"],
     ["age", "Dernière image"], ["targets", "Cibles présentes"], ["people", "Personnes reconnues"]].forEach(function (row) {
      var dd = el("dd", "—");
      refs[row[0]] = dd;
      dl.append(el("dt", row[1]), dd);
    });
    var actions = el("div");
    actions.style.cssText = "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap";
    var full = el("button", null, "btn"); full.type = "button";
    full.append(Shell.icon("expand"), el("span", "Plein écran"));
    full.addEventListener("click", function () { if (tiles[cam.id]) toggleFullscreen(tiles[cam.id].root); });
    var plan = el("a", null, "btn"); plan.href = "/cameras";
    plan.append(Shell.icon("route"), el("span", "Plan et cheminement"));
    actions.append(full, plan);

    var evTitle = el("div", "Derniers événements de cette caméra", "section-title");
    var evList = el("ul", null, "feed");
    var evEmpty = el("p", "Aucun événement.", "muted");
    box.replaceChildren(head, dl, actions, evTitle, evList, evEmpty);
    detail = { refs: refs, evList: evList, evEmpty: evEmpty, cam: cam, sig: "" };
    updateDetails();
  }

  function updateDetails() {
    if (!detail) return;
    var cam = detail.cam, s = state.status[cam.id] || {}, r = detail.refs;
    r.status.replaceChildren(el("span", null, "dot " + (STATUS_DOT[s.status] || "")), document.createTextNode(" " + (STATUS_TEXT[s.status] || "—")));
    r.source.textContent = cam.source || "—";
    r.size.textContent = s.width ? s.width + " × " + s.height : "—";
    r.fps.textContent = s.status === "online" ? s.fps.toFixed(1) : "—";
    r.latency.textContent = s.latency_ms !== null && s.latency_ms !== undefined ? s.latency_ms + " ms" : "—";
    r.persons.textContent = String(s.persons || 0);
    r.reconnects.textContent = String(s.reconnects || 0);
    r.age.textContent = s.age_sec !== null && s.age_sec !== undefined ? "il y a " + s.age_sec.toFixed(1) + " s" : "jamais";

    var here = state.targets.filter(function (t) { return t.active && t.camera_id === cam.id; });
    var people = state.people.filter(function (p) { return p.camera_id === cam.id; });
    var sig = here.map(function (t) { return t.id; }).join(",") + "/" +
              people.map(function (p) { return [p.participant_id, p.label, p.status, p.active, p.ambiguous].join(":"); }).join(",");
    if (sig !== detail.sig) {
      detail.sig = sig;
      r.targets.replaceChildren.apply(r.targets, here.length ? here.map(function (t) {
        var chip = el("span", null, "status-badge"); chip.style.marginRight = "10px";
        var sw = el("span", null, "swatch"); sw.style.background = safeColor(t.color);
        chip.append(sw, el("span", " " + t.label));
        return chip;
      }) : [document.createTextNode("Aucune")]);
      r.people.replaceChildren.apply(r.people, people.length ? people.map(personLine) : [document.createTextNode("Aucune")]);
    }
  }

  var MATCH_TEXT = { probable: "correspondance probable", a_verifier: "à vérifier" };

  /** Une personne reconnue : lien vers sa fiche, statut de correspondance (jamais « identité certaine »), score. */
  function personLine(p) {
    var line = el("div");
    line.style.marginBottom = "4px";
    if (p.participant_id) {
      var a = el("a", p.label); a.href = "/people/" + encodeURIComponent(p.participant_id); a.title = "Ouvrir la fiche";
      line.appendChild(a);
    } else line.appendChild(document.createTextNode(p.label));
    var notes = [];
    if (p.status) notes.push(MATCH_TEXT[p.status] || p.status);
    if (p.ambiguous) notes.push("plusieurs personnes proches");
    if (p.active === false) notes.push("fiche désactivée");
    if (p.score !== null && p.score !== undefined) notes.push(p.score.toFixed(2));
    if (notes.length) { var sub = el("span", " — " + notes.join(", "), "muted"); line.appendChild(sub); }
    return line;
  }

  function loadCameraEvents() {
    if (!detail) return;
    var d = detail;
    fetch("/api/events?limit=8&camera_id=" + encodeURIComponent(d.cam.id)).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || detail !== d) return;
        d.evList.replaceChildren.apply(d.evList, j.events.map(function (e) { return eventItem(e, false); }));
        d.evEmpty.hidden = j.events.length > 0;
      }).catch(function () { /* le panneau reste tel quel */ });
  }

  // ---- panneau « Cibles » ---------------------------------------------------------------------------------
  var targetsSig = "";
  function updateTargetsPane() {
    var sig = JSON.stringify(state.targets.map(function (t) { return [t.id, t.label, t.color, t.active, t.camera_id, t.score && t.score.toFixed(2), (t.path || []).length]; }));
    if (sig === targetsSig) return;
    targetsSig = sig;
    var box = $("targetsList");
    if (!state.targets.length) {
      box.replaceChildren(el("p", "Aucune cible définie. Ajoutez-en depuis la page « Cibles et personnes ».", "muted"));
      return;
    }
    box.replaceChildren.apply(box, state.targets.map(function (t) {
      var row = el("div", null, "target-row");
      var sw = el("span", null, "swatch"); sw.style.background = safeColor(t.color);
      var main = el("div"); main.style.cssText = "flex:1;min-width:0";
      var last = (t.path || []).length ? t.path[t.path.length - 1].camera_id : null;
      var text = t.active ? "Détectée : " + cameraLabel(t.camera_id) + (typeof t.score === "number" ? " (" + t.score.toFixed(2) + ")" : "")
        : last ? "Non détectée — dernière caméra : " + cameraLabel(last) : "Pas encore repérée";
      main.append(el("b", t.label), el("div", text, "muted"));
      row.append(sw, main);
      if (last) {
        row.style.cursor = "pointer";
        row.addEventListener("click", function () { goToCamera(t.active ? t.camera_id : last); });
      }
      return row;
    }));
  }

  // ---- fil d'événements ----------------------------------------------------------------------------------------
  function matchesFilter(e) {
    return !state.family || FAMILIES[state.family].indexOf(e.type) >= 0;
  }

  function eventItem(e, fresh) {
    var li = el("li", null, fresh ? "fresh" : "");
    li.append(el("div", null, "sev " + e.severity));
    var body = el("div");
    var meta = el("div", null, "meta");
    meta.append(Shell.icon(EVENT_ICON[e.type] || "bell"), el("span", fmtTime(e.timestamp)));
    if (e.camera_id) meta.append(el("span", "· " + cameraLabel(e.camera_id)));
    if (e.payload && e.payload.participant_id) {
      var card = el("a", "Ouvrir la fiche");
      card.href = "/people/" + encodeURIComponent(e.payload.participant_id);
      card.addEventListener("click", function (ev) { ev.stopPropagation(); });
      meta.append(card);
    }
    body.append(meta, el("div", e.title, "title"));
    li.appendChild(body);
    if (e.camera_id) li.addEventListener("click", function () { goToCamera(e.camera_id); });
    return li;
  }

  function renderFeed(freshIds) {
    var list = $("feed");
    var shown = state.events.filter(matchesFilter).slice(0, 80);
    list.replaceChildren.apply(list, shown.map(function (e) { return eventItem(e, freshIds && freshIds.indexOf(e.id) >= 0); }));
    $("feedEmpty").hidden = shown.length > 0;
    var badge = $("countEvents");
    badge.textContent = String(state.unread);
    badge.hidden = state.unread === 0;
  }

  function renderFilters() {
    var row = $("filterRow");
    row.replaceChildren.apply(row, FILTERS.map(function (f) {
      var b = el("button", f[1]); b.type = "button";
      b.setAttribute("aria-pressed", String(state.family === f[0]));
      b.addEventListener("click", function () { state.family = f[0]; prefs.set("family", f[0]); renderFilters(); loadEvents(); });
      return b;
    }));
  }

  function loadEvents() {
    return fetch("/api/events?limit=60" + (state.family ? "&family=" + state.family : "")).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j) return;
        state.events = j.events;
        state.feedLastId = Math.max(state.feedLastId, j.last_id || 0);
        state.unread = 0;
        renderFeed();
      }).catch(function () { /* nouvel essai au prochain événement */ });
  }

  function fetchNewEvents() {
    if (state.fetching) return;
    state.fetching = true;
    var after = state.feedLastId;
    fetch("/api/events?limit=50&after_id=" + after).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        state.fetching = false;
        if (!j) return;
        state.feedLastId = Math.max(state.feedLastId, j.last_id || 0);
        var known = {};
        state.events.forEach(function (e) { known[e.id] = true; });
        var fresh = j.events.filter(function (e) { return !known[e.id]; });
        if (!fresh.length) return;
        state.events = fresh.concat(state.events).slice(0, MAX_EVENTS);
        var visible = fresh.filter(matchesFilter);
        if (state.tab !== "Events") state.unread += visible.length;
        renderFeed(fresh.map(function (e) { return e.id; }));
        announce(fresh.slice().reverse());
        if (detail) loadCameraEvents();
      }).catch(function () { state.fetching = false; });
  }

  function announce(list) {
    list.forEach(function (e) {
      var important = e.severity !== "info" || e.type === "target_seen" || e.type === "target_moved";
      if (!important) return;
      Shell.toast(e.title, e.severity);
      if (state.sound) Shell.beep(e.severity === "warning" ? 440 : 880, 160);
    });
  }

  function goToCamera(id) {
    var idx = state.cameras.map(function (c) { return c.id; }).indexOf(id);
    if (idx < 0) return;
    state.page = Math.floor(idx / perPage());
    renderGrid();
    select(id);
  }

  // ---- onglets, plein écran, mode moniteur --------------------------------------------------------------------
  function openTab(name) {
    state.tab = name;
    ["Events", "Camera", "Targets"].forEach(function (n) {
      var tab = $("tab" + n), pane = $("pane" + n);
      tab.setAttribute("aria-selected", String(n === name));
      pane.classList.toggle("active", n === name);
    });
    if (name === "Events") { state.unread = 0; renderFeed(); }
  }

  function toggleFullscreen(node) {
    try {
      if (document.fullscreenElement) { document.exitFullscreen(); return; }
      if (node && node.requestFullscreen) node.requestFullscreen();
    } catch (e) { /* plein écran refusé par le navigateur */ }
  }

  function setMonitor(on) {
    document.body.classList.toggle("monitor", on);
    $("monitorBtn").setAttribute("aria-pressed", String(on));
    try {
      if (on && !document.fullscreenElement && document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
      if (!on && document.fullscreenElement) document.exitFullscreen();
    } catch (e) { /* ignoré */ }
  }

  function setAnnotations(on) {
    state.annotations = on;
    prefs.set("annotations", on);
    var b = $("annotBtn");
    b.setAttribute("aria-pressed", String(on));
    b.replaceChildren(Shell.icon(on ? "eye" : "eyeoff"), el("span", on ? "Annotations" : "Sans annotations"));
    sendSubscription();
  }

  function setSound(on) {
    state.sound = on;
    prefs.set("sound", on);
    var b = $("soundBtn");
    b.setAttribute("aria-pressed", String(on));
    b.replaceChildren(Shell.icon(on ? "volume" : "mute"), el("span", on ? "Son activé" : "Son coupé"));
    if (on) Shell.beep(660, 120);
  }

  function setLayout(n) {
    state.layout = n;
    prefs.set("layout", n);
    renderGrid();
  }

  // ---- flux vidéo (un seul WebSocket pour toutes les caméras) --------------------------------------------------
  function sendSubscription() {
    if (!videoSocket || videoSocket.readyState !== 1) return;
    var cams = document.hidden ? [] : visibleCameras().map(function (c) { return c.id; });
    videoSocket.send(JSON.stringify({ subscribe: cams, annotations: state.annotations, fps: FPS_BY_LAYOUT[state.layout] }));
  }

  function showFrame(id, bytes) {
    var t = tiles[id];
    if (!t) return;
    var url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
    if (t.prevUrl) URL.revokeObjectURL(t.prevUrl);
    t.prevUrl = t.url;
    t.url = url;
    t.img.onload = function () { if (t.prevUrl) { URL.revokeObjectURL(t.prevUrl); t.prevUrl = null; } };
    t.img.src = url;
  }

  function connectVideo() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    videoSocket = new WebSocket(proto + "://" + location.host + "/ws/video");
    videoSocket.binaryType = "arraybuffer";
    videoSocket.onopen = function () { videoDelay = 500; sendSubscription(); };
    videoSocket.onmessage = function (m) {
      if (typeof m.data === "string") return;
      var buf = new Uint8Array(m.data);
      var n = buf[0];
      showFrame(new TextDecoder().decode(buf.subarray(1, 1 + n)), buf.subarray(1 + n));
    };
    videoSocket.onclose = function () { setTimeout(connectVideo, videoDelay); videoDelay = Math.min(5000, videoDelay * 2); };
    videoSocket.onerror = function () { try { videoSocket.close(); } catch (e) { /* ignoré */ } };
  }

  // ---- état en direct ------------------------------------------------------------------------------------------
  function applyLive() {
    var d = pending; pending = null;
    if (!d) return;
    state.status = d.cameras_status || state.status;
    state.targets = d.targets || [];
    state.people = d.people || [];
    state.meta = d.camera_meta || state.meta;
    updateTiles();
    if ((d.feed_last_id || 0) > state.feedLastId) fetchNewEvents();
    // Une caméra a été ajoutée, retirée ou désactivée : on recharge la liste sans recharger la page.
    if (d.cameras_version !== undefined && state.camerasVersion !== undefined && d.cameras_version !== state.camerasVersion && !state.reloading) {
      state.reloading = true;
      state.camerasVersion = d.cameras_version;
      loadSnapshot().then(function () { state.reloading = false; });
    }
  }

  function connectLive() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    liveSocket = new WebSocket(proto + "://" + location.host + "/ws/live");
    liveSocket.onopen = function () { liveDelay = 500; Shell.setLink(true, "Temps réel : connecté"); };
    liveSocket.onmessage = function (m) {
      try { pending = JSON.parse(m.data); } catch (e) { return; }
      if (!window.requestAnimationFrame) { applyLive(); return; }
      if (!applyLive.scheduled) { applyLive.scheduled = true; requestAnimationFrame(function () { applyLive.scheduled = false; applyLive(); }); }
    };
    liveSocket.onclose = function () {
      Shell.setLink(false, "Temps réel : déconnecté");
      setTimeout(connectLive, liveDelay); liveDelay = Math.min(5000, liveDelay * 2);
    };
    liveSocket.onerror = function () { try { liveSocket.close(); } catch (e) { /* ignoré */ } };
  }

  // ---- initialisation --------------------------------------------------------------------------------------------
  function buildToolbar() {
    var seg = $("layoutSeg");
    seg.replaceChildren.apply(seg, LAYOUTS.map(function (n) {
      var b = el("button", String(n)); b.type = "button";
      b.setAttribute("data-layout", String(n));
      b.title = n + (n > 1 ? " caméras par page" : " caméra par page");
      b.setAttribute("aria-label", b.title);
      b.addEventListener("click", function () { setLayout(n); });
      return b;
    }));
    $("pagePrev").appendChild(Shell.icon("left"));
    $("pageNext").appendChild(Shell.icon("right"));
    $("pagePrev").addEventListener("click", function () { state.page = Math.max(0, state.page - 1); renderGrid(); });
    $("pageNext").addEventListener("click", function () { state.page = Math.min(pageCount() - 1, state.page + 1); renderGrid(); });
    $("annotBtn").addEventListener("click", function () { setAnnotations(!state.annotations); });
    $("soundBtn").addEventListener("click", function () { setSound(!state.sound); });
    $("monitorBtn").replaceChildren(Shell.icon("monitor"), el("span", "Mode moniteur"));
    $("monitorBtn").addEventListener("click", function () { setMonitor(!document.body.classList.contains("monitor")); });
    $("fullBtn").replaceChildren(Shell.icon("expand"), el("span", "Plein écran"));
    $("fullBtn").addEventListener("click", function () { toggleFullscreen(document.documentElement); });
    ["Events", "Camera", "Targets"].forEach(function (n) { $("tab" + n).addEventListener("click", function () { openTab(n); }); });
    setAnnotations(state.annotations);
    setSoundQuiet();
    renderFilters();
  }

  function setSoundQuiet() {
    var b = $("soundBtn");
    b.setAttribute("aria-pressed", String(state.sound));
    b.replaceChildren(Shell.icon(state.sound ? "volume" : "mute"), el("span", state.sound ? "Son activé" : "Son coupé"));
  }

  function bindKeys() {
    document.addEventListener("keydown", function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      var k = (e.key || "").toLowerCase();
      if (k === "f") toggleFullscreen(document.documentElement);
      else if (k === "m") setMonitor(!document.body.classList.contains("monitor"));
      else if (k === "a") setAnnotations(!state.annotations);
      else if (e.key === "ArrowRight") { state.page = Math.min(pageCount() - 1, state.page + 1); renderGrid(); }
      else if (e.key === "ArrowLeft") { state.page = Math.max(0, state.page - 1); renderGrid(); }
    });
    document.addEventListener("visibilitychange", sendSubscription);
    document.addEventListener("fullscreenchange", function () {
      if (!document.fullscreenElement && document.body.classList.contains("monitor")) setMonitor(false);
    });
  }

  function loadSnapshot() {
    return fetch("/api/supervision/cameras").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (j) {
      state.cameras = j.cameras;
      state.targets = j.targets || [];
      state.people = j.people || [];
      state.identitiesVisible = j.identities_visible !== false;
      state.feedLastId = Math.max(state.feedLastId, j.feed_last_id || 0);
      state.camerasVersion = j.cameras_version;
      state.meta = {};
      j.cameras.forEach(function (c) { state.meta[c.id] = { label: c.label, location: c.location }; state.status[c.id] = c; });
      if (state.selected && !cameraById(state.selected)) state.selected = null;
      buildTiles();
      renderGrid();
      buildDetails();
      return loadEvents();
    }).catch(function () {
      Shell.toast("Impossible de charger les caméras. Nouvel essai dans 3 s…", "warning");
      return new Promise(function (resolve) { setTimeout(function () { resolve(loadSnapshot()); }, 3000); });
    });
  }

  Shell.mount({ active: "supervision" });
  buildToolbar();
  bindKeys();
  openTab("Events");
  loadSnapshot().then(function () { connectVideo(); connectLive(); });
})();
