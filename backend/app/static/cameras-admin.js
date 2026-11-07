/*
 * Gestion des caméras : liste en direct, ajout / modification / suppression à chaud, test de connexion
 * avec aperçu, détection des webcams et des caméras ONVIF.
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 * Les jetons et mots de passe ne sont jamais réaffichés : une source laissée vide est conservée.
 */
(function () {
  "use strict";

  var STATUS_TEXT = { online: "En ligne", connecting: "Connexion…", reconnecting: "Reconnexion…", offline: "Hors ligne",
                      stopped: "Arrêtée", disabled: "Désactivée", error: "Erreur" };
  var STATUS_DOT = { online: "ok", connecting: "warn", reconnecting: "warn", offline: "bad", stopped: "", disabled: "", error: "bad" };
  var KIND_TEXT = { webcam: "Webcam USB", http: "Flux HTTP", rtsp: "Caméra IP (RTSP)" };
  var HOST_RE = /^[A-Za-z0-9._-]+$/;

  var state = { cameras: [], max: 32, version: null, editing: null, live: {}, loading: false, opener: null };
  var refs = {};            // id -> cellules mises à jour en direct

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function withIcon(btn, icon, text) { btn.replaceChildren(Shell.icon(icon), el("span", text)); return btn; }
  function cameraById(id) { return state.cameras.filter(function (c) { return c.id === id; })[0]; }

  function api(method, path, body) {
    var options = { method: method, headers: {} };
    if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
    });
  }
  function errorOf(res) {
    var d = res.data && res.data.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) return { message: d.message || "Erreur.", field: d.field || null };
    if (typeof d === "string") return { message: d, field: null };
    if (Array.isArray(d)) return { message: "Requête invalide : " + d.map(function (x) { return (x.loc || []).slice(-1) + " " + x.msg; }).join(" ; "), field: null };
    return { message: "Erreur " + res.status, field: null };
  }

  // ---- liste ---------------------------------------------------------------------------------------
  function load() {
    if (state.loading) return Promise.resolve();
    state.loading = true;
    return api("GET", "/api/admin/cameras").then(function (res) {
      state.loading = false;
      if (!res.ok) { Shell.toast("Liste des caméras indisponible.", "warning"); return; }
      state.cameras = res.data.cameras; state.max = res.data.max; state.version = res.data.version;
      renderTable();
    }).catch(function () { state.loading = false; });
  }

  function actionButton(icon, text, cls, handler, disabled) {
    var b = el("button", null, "btn small" + (cls ? " " + cls : ""));
    b.type = "button"; withIcon(b, icon, text); b.disabled = !!disabled;
    b.addEventListener("click", handler);
    return b;
  }

  function renderTable() {
    var body = $("rows");
    refs = {};
    body.replaceChildren.apply(body, state.cameras.map(function (c) {
      var tr = el("tr");
      var name = el("td"); name.append(el("div", c.label, "cell-title"), el("div", c.id, "cell-sub"));
      var kind = el("td"); kind.appendChild(el("span", KIND_TEXT[c.kind] || c.kind, "tag"));
      var source = el("td", c.source_display); if (c.has_secret) source.title = "Contient un jeton ou des identifiants (masqués)";
      var status = el("td"), dot = el("span", null, "dot"), statusText = el("span", ""); statusText.style.marginLeft = "7px";
      status.append(dot, statusText);
      var fps = el("td", "—", "num"), latency = el("td", "—", "num"), size = el("td", "—", "num");
      var actions = el("td"), box = el("div", null, "row-actions");
      box.append(
        actionButton("edit", "Modifier", "", function () { openForm(c, this); }),
        actionButton("power", c.enabled ? "Désactiver" : "Activer", "", function () { toggle(c); }),
        actionButton("refresh", "Relancer", "", function () { restart(c); }, !c.enabled),
        actionButton("trash", "Supprimer", "danger", function () { remove(c); }));
      actions.appendChild(box);
      tr.append(name, el("td", c.location || "—"), kind, source, status, fps, latency, size, actions);
      refs[c.id] = { dot: dot, status: statusText, fps: fps, latency: latency, size: size, cam: c };
      return tr;
    }));
    $("emptyMsg").hidden = state.cameras.length > 0;
    $("btnAdd").disabled = state.cameras.length >= state.max;
    updateLive();
  }

  function updateLive() {
    var online = 0;
    state.cameras.forEach(function (c) {
      var r = refs[c.id];
      var s = c.enabled ? (state.live[c.id] || c) : { status: "disabled" };
      var status = s.status || "connecting";
      if (status === "online") online++;
      if (!r) return;
      r.dot.className = "dot " + (STATUS_DOT[status] || "");
      r.status.textContent = STATUS_TEXT[status] || status;
      r.status.title = c.error || "";
      r.fps.textContent = status === "online" ? Number(s.fps).toFixed(1) : "—";
      r.latency.textContent = status === "online" && s.latency_ms !== null && s.latency_ms !== undefined ? s.latency_ms + " ms" : "—";
      r.size.textContent = status === "online" && s.width ? s.width + " × " + s.height : "—";
    });
    $("chipCount").replaceChildren(Shell.icon("camera"), el("span", "Caméras"), el("b", state.cameras.length + " / " + state.max));
    $("chipOnline").replaceChildren(Shell.icon("monitor"), el("span", "En ligne"), el("b", String(online)));
  }

  // ---- actions sur une caméra ---------------------------------------------------------------------------
  function toggle(c) {
    api("PUT", "/api/admin/cameras/" + encodeURIComponent(c.id), { enabled: !c.enabled }).then(function (res) {
      if (!res.ok) { Shell.toast(errorOf(res).message, "warning"); return; }
      Shell.toast(c.label + (c.enabled ? " désactivée." : " activée."));
      load();
    });
  }
  function restart(c) {
    api("POST", "/api/admin/cameras/" + encodeURIComponent(c.id) + "/restart").then(function (res) {
      Shell.toast(res.ok ? c.label + " relancée." : errorOf(res).message, res.ok ? "info" : "warning");
      load();
    });
  }
  function remove(c) {
    if (!window.confirm("Supprimer la caméra « " + c.label + " » ?\nSon historique de détections est conservé.")) return;
    api("DELETE", "/api/admin/cameras/" + encodeURIComponent(c.id)).then(function (res) {
      Shell.toast(res.ok ? c.label + " supprimée." : errorOf(res).message, res.ok ? "info" : "warning");
      load();
    });
  }

  // ---- formulaire ------------------------------------------------------------------------------------------
  function groups(type) {
    return { webcam: type === "webcam", pc: type === "pc", url: type === "http" || type === "rtsp" };
  }

  function applyType() {
    var type = $("fType").value, on = groups(type);
    Array.prototype.forEach.call(document.querySelectorAll("[data-g]"), function (f) { f.hidden = !on[f.getAttribute("data-g")]; });
    $("fUrlLabel").textContent = type === "rtsp" ? "Adresse du flux RTSP" : "Adresse du flux HTTP";
    $("fUrl").placeholder = type === "rtsp" ? "rtsp://identifiant:motdepasse@192.168.1.64:554/chemin" : "http://192.168.1.31:8081/video";
    $("fUrlHint").textContent = type === "rtsp"
      ? "Le chemin et les identifiants dépendent de la marque de la caméra (voir sa notice)."
      : "Flux MJPEG d'une caméra IP ou d'une application de téléphone.";
    var capture = type === "webcam";
    $("fCapW").disabled = !capture; $("fCapH").disabled = !capture;
  }

  function clearErrors() {
    Array.prototype.forEach.call(document.querySelectorAll(".field.invalid"), function (f) { f.classList.remove("invalid"); });
    Array.prototype.forEach.call(document.querySelectorAll(".field .error"), function (e) { e.textContent = ""; });
    $("formError").hidden = true; $("formError").textContent = "";
  }

  function showError(field, message) {
    var targets = Array.prototype.filter.call(document.querySelectorAll('[data-f="' + field + '"]'), function (f) { return !f.hidden; });
    if (targets.length) { targets[0].classList.add("invalid"); var e = targets[0].querySelector(".error"); if (e) e.textContent = message; return; }
    $("formError").textContent = message; $("formError").hidden = false;
  }

  function resetTest() { var box = $("testBox"); box.hidden = true; box.replaceChildren(); box.className = "result-box"; }

  function openForm(cam, opener) {
    state.editing = cam ? cam.id : null;
    state.opener = opener || null;
    clearErrors(); resetTest();
    $("modalTitle").textContent = cam ? "Modifier « " + cam.label + " »" : "Ajouter une caméra";
    $("form").reset();
    $("fType").value = cam ? (cam.kind === "rtsp" ? "rtsp" : cam.kind === "webcam" ? "webcam" : "http") : "webcam";
    $("fLabel").value = cam ? cam.label : "";
    $("fLocation").value = cam ? cam.location : "";
    $("fEnabled").value = !cam || cam.enabled ? "1" : "0";
    $("fCapW").value = cam && cam.capture_width ? cam.capture_width : "";
    $("fCapH").value = cam && cam.capture_height ? cam.capture_height : "";
    $("fFps").value = cam && cam.process_fps ? cam.process_fps : "";
    $("fX").value = cam && cam.x !== null && cam.x !== undefined ? cam.x : "";
    $("fY").value = cam && cam.y !== null && cam.y !== undefined ? cam.y : "";
    if (cam && cam.kind === "webcam") { var m = /(\d+)\s*$/.exec(cam.source_display); $("fIndex").value = m ? m[1] : "0"; }
    $("currentSource").hidden = !cam || cam.kind === "webcam";
    $("currentSourceText").textContent = cam ? "Source actuelle : " + cam.source_display + (cam.has_secret ? " (jeton ou identifiants masqués)" : "") +
      ". Laissez les champs de source vides pour la conserver." : "";
    applyType();
    $("btnSave").textContent = cam ? "Enregistrer les modifications" : "Ajouter la caméra";
    $("modal").hidden = false;
    $("fLabel").focus();
  }

  function closeForm() {
    $("modal").hidden = true;
    if (state.opener && state.opener.focus) state.opener.focus();
    state.opener = null;
  }

  /** Source saisie : { source } (à envoyer), { keep: true } (laisser telle quelle, en modification) ou { error }. */
  function readSource() {
    var type = $("fType").value, editing = state.editing !== null;
    if (type === "webcam") {
      var index = $("fIndex").value.trim();
      if (!/^\d+$/.test(index) || Number(index) > 31) return { error: "Indiquez un numéro de webcam entre 0 et 31.", field: "source" };
      return { source: Number(index) };
    }
    if (type === "pc") {
      var host = $("fHost").value.trim();
      if (!host) return editing ? { keep: true } : { error: "Indiquez l'adresse IP de l'ordinateur.", field: "source" };
      if (!HOST_RE.test(host)) return { error: "Adresse invalide : saisissez seulement l'adresse (par exemple 192.168.1.31).", field: "source" };
      var port = $("fPort").value.trim() || "8081", token = $("fToken").value.trim();
      if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) return { error: "Port invalide.", field: "source" };
      return { source: "http://" + host + ":" + port + "/video" + (token ? "?token=" + encodeURIComponent(token) : "") };
    }
    var url = $("fUrl").value.trim();
    if (!url) return editing ? { keep: true } : { error: "Indiquez l'adresse du flux.", field: "source" };
    var scheme = $("fType").value === "rtsp" ? /^rtsps?:\/\//i : /^https?:\/\//i;
    if (!scheme.test(url)) return { error: $("fType").value === "rtsp" ? "L'adresse doit commencer par rtsp://" : "L'adresse doit commencer par http:// ou https://", field: "source" };
    return { source: url };
  }

  function num(id) { var v = $(id).value.trim(); return v === "" ? null : Number(v); }

  function readForm() {
    clearErrors();
    var problems = [], payload = {};
    var label = $("fLabel").value.trim();
    if (!label) problems.push(["label", "Le nom est obligatoire."]);
    payload.label = label;
    payload.location = $("fLocation").value.trim();
    var src = readSource();
    if (src.error) problems.push([src.field, src.error]);
    else if (!src.keep) payload.source = src.source;
    payload.enabled = $("fEnabled").value === "1";
    var editing = state.editing !== null;
    [["capture_width", "fCapW"], ["capture_height", "fCapH"], ["process_fps", "fFps"], ["x", "fX"], ["y", "fY"]].forEach(function (p) {
      var v = num(p[1]);
      if (v !== null && isNaN(v)) problems.push([p[0], "Nombre attendu."]);
      else if (v !== null || editing) payload[p[0]] = v;     // en modification, vide = effacer le réglage
    });
    if ($("fType").value !== "webcam") { delete payload.capture_width; delete payload.capture_height; }
    problems.forEach(function (p) { showError(p[0], p[1]); });
    return problems.length ? null : payload;
  }

  function submit(ev) {
    ev.preventDefault();
    var payload = readForm();
    if (!payload) return;
    var editing = state.editing, save = $("btnSave");
    save.disabled = true;
    var request = editing ? api("PUT", "/api/admin/cameras/" + encodeURIComponent(editing), payload) : api("POST", "/api/admin/cameras", payload);
    request.then(function (res) {
      save.disabled = false;
      if (!res.ok) { var e = errorOf(res); showError(e.field || "_", e.message); return; }
      Shell.toast((editing ? "Modifications enregistrées : " : "Caméra ajoutée : ") + res.data.label);
      closeForm();
      load();
    }).catch(function () { save.disabled = false; showError("_", "Le serveur ne répond pas."); });
  }

  // ---- test de connexion --------------------------------------------------------------------------------------
  function runTest() {
    clearErrors();
    var src = readSource(), body = {};
    if (src.error) { showError(src.field, src.error); return; }
    if (src.keep) body.camera_id = state.editing; else body.source = src.source;
    if ($("fType").value === "webcam") {
      var w = num("fCapW"), h = num("fCapH");
      if (w && h) { body.capture_width = w; body.capture_height = h; }
    }
    var btn = $("btnTest"), box = $("testBox");
    btn.disabled = true; withIcon(btn, "refresh", "Test en cours…");
    box.hidden = false; box.className = "result-box"; box.replaceChildren(el("span", "Connexion à la source… (jusqu'à 10 secondes)", "muted"));
    api("POST", "/api/admin/cameras/test", body).then(function (res) {
      btn.disabled = false; withIcon(btn, "search", "Tester la connexion");
      if (!res.ok) { box.className = "result-box fail"; box.replaceChildren(el("b", "Test impossible"), el("span", errorOf(res).message)); return; }
      renderTest(res.data);
    }).catch(function () {
      btn.disabled = false; withIcon(btn, "search", "Tester la connexion");
      box.className = "result-box fail"; box.replaceChildren(el("b", "Test impossible"), el("span", "Le serveur ne répond pas."));
    });
  }

  function renderTest(r) {
    var box = $("testBox");
    box.className = "result-box " + (r.ok ? "ok" : "fail");
    var parts = [el("b", r.ok ? "Connexion réussie" : "Échec de la connexion")];
    if (!r.ok) parts.push(el("span", r.error || "Aucune image reçue."));
    if (r.already_running) parts.push(el("span", "Cette source est déjà utilisée par la caméra « " + r.camera + " » : l'image affichée est celle en service.", "muted"));
    if (r.ok || r.width) {
      var m = el("div", null, "metrics");
      m.append(el("span", "Résolution : " + (r.width ? r.width + " × " + r.height : "—")),
               el("span", "Images/s : " + (r.fps ? r.fps : "—")),
               el("span", "Première image : " + (r.first_frame_ms !== null && r.first_frame_ms !== undefined ? r.first_frame_ms + " ms" : "—")));
      parts.push(m);
    }
    if (r.preview) { var img = el("img", null, "preview-img"); img.alt = "Aperçu de la caméra testée"; img.src = r.preview; parts.push(img); }
    box.replaceChildren.apply(box, parts);
  }

  // ---- détection ------------------------------------------------------------------------------------------------
  function showFound(title, items, note) {
    $("foundCard").hidden = false;
    $("foundTitle").textContent = title;
    var list = $("foundList");
    list.replaceChildren.apply(list, items.length ? items : [el("p", "Rien trouvé.", "muted")]);
    $("foundNote").textContent = note || "";
  }
  function foundItem(title, sub, label, onUse, disabled) {
    var row = el("div", null, "found-item");
    var left = el("div"); left.append(el("div", title, "cell-title"), el("div", sub, "cell-sub"));
    var b = actionButton("plus", label, "", onUse, disabled);
    row.append(left, b);
    return row;
  }

  function detectWebcams() {
    var b = $("btnWebcams"); b.disabled = true; withIcon(b, "refresh", "Détection…");
    api("GET", "/api/admin/cameras/webcams").then(function (res) {
      b.disabled = false; withIcon(b, "camera", "Détecter les webcams");
      if (!res.ok) { Shell.toast("Détection impossible.", "warning"); return; }
      var items = res.data.webcams.map(function (w) {
        return foundItem("Webcam " + w.index, w.width + " × " + w.height, "Utiliser", function () {
          openForm(null, this); $("fType").value = "webcam"; applyType(); $("fIndex").value = String(w.index); $("fLabel").value = "Webcam " + w.index;
        });
      });
      var used = res.data.in_use.length ? "Déjà utilisées par NiifYéén : " + res.data.in_use.join(", ") + " (non rouvertes)." : "";
      showFound("Webcams disponibles sur ce serveur", items, used);
    });
  }

  function discoverOnvif() {
    var b = $("btnOnvif"); b.disabled = true; withIcon(b, "refresh", "Recherche… (3 s)");
    api("POST", "/api/admin/cameras/discover").then(function (res) {
      b.disabled = false; withIcon(b, "search", "Rechercher des caméras ONVIF");
      if (!res.ok) { Shell.toast("Recherche impossible.", "warning"); return; }
      var items = res.data.devices.map(function (d) {
        var sub = d.address + (d.hardware ? " — " + d.hardware : "") + (d.configured ? " — déjà configurée" : "");
        return foundItem(d.name || "Caméra ONVIF", sub, "Utiliser", function () {
          openForm(null, this); $("fType").value = "rtsp"; applyType();
          $("fUrl").value = d.suggested_source; $("fLabel").value = d.name || ("Caméra " + d.address);
        });
      });
      showFound("Caméras ONVIF trouvées sur le réseau", items, res.data.note);
    });
  }

  // ---- temps réel ------------------------------------------------------------------------------------------------
  function connectLive() {
    var proto = location.protocol === "https:" ? "wss" : "ws", delay = 500;
    (function open() {
      var ws = new WebSocket(proto + "://" + location.host + "/ws/live");
      ws.onopen = function () { delay = 500; Shell.setLink(true, "Temps réel : connecté"); };
      ws.onmessage = function (m) {
        var d; try { d = JSON.parse(m.data); } catch (e) { return; }
        state.live = d.cameras_status || {};
        if (state.version !== null && d.cameras_version !== undefined && d.cameras_version !== state.version) { state.version = d.cameras_version; load(); }
        else updateLive();
      };
      ws.onclose = function () { Shell.setLink(false, "Temps réel : déconnecté"); setTimeout(open, delay); delay = Math.min(5000, delay * 2); };
      ws.onerror = function () { try { ws.close(); } catch (e) { /* ignoré */ } };
    })();
  }

  // ---- initialisation ------------------------------------------------------------------------------------------------
  Shell.mount({ active: "admin-cameras" });
  withIcon($("btnAdd"), "plus", "Ajouter une caméra");
  withIcon($("btnWebcams"), "camera", "Détecter les webcams");
  withIcon($("btnOnvif"), "search", "Rechercher des caméras ONVIF");
  withIcon($("btnTest"), "search", "Tester la connexion");
  $("btnAdd").addEventListener("click", function () { openForm(null, this); });
  $("btnWebcams").addEventListener("click", detectWebcams);
  $("btnOnvif").addEventListener("click", discoverOnvif);
  $("btnTest").addEventListener("click", runTest);
  $("btnCancel").addEventListener("click", closeForm);
  $("btnClose").addEventListener("click", closeForm);
  $("form").addEventListener("submit", submit);
  $("fType").addEventListener("change", function () { applyType(); resetTest(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("modal").hidden) closeForm(); });
  applyType();
  load().then(connectLive);
})();
