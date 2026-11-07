/*
 * Page « Centre d'alertes » : liste filtrable, détail avec historique et notes, changements d'état
 * (un par un ou par lot), règles et état des notifications externes. Tout le texte est inséré avec textContent.
 */
(function () {
  "use strict";

  var L = window.AlertsLogic;
  var PAGE = 50;
  var state = { status: "open", alerts: [], counts: {}, selected: null, picked: [], meta: {}, rules: [], priorities: [],
                more: false, lastSummary: 0, sound: true };

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
  function cameraLabel(id) { return id ? ((state.meta[id] && state.meta[id].label) || id) : "—"; }
  function when(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "" : d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function localToIso(v) {
    if (!v) return "";
    var d = new Date(v);
    return isNaN(d.getTime()) ? "" : d.toISOString();
  }

  // ---- filtres et liste ----------------------------------------------------------------------------
  function filters(extra) {
    var f = { status: state.status, priority: $("fPriority").value, category: $("fCategory").value, camera_id: $("fCamera").value,
              q: $("fQ").value, since: localToIso($("fSince").value), until: localToIso($("fUntil").value), limit: PAGE };
    Object.keys(extra || {}).forEach(function (k) { f[k] = extra[k]; });
    return f;
  }

  function loadList(append) {
    var extra = {};
    if (append && state.alerts.length) extra.before_id = state.alerts[state.alerts.length - 1].id;
    return api("GET", "/api/alerts" + L.buildQuery(filters(extra))).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      var items = res.data.alerts || [];
      state.alerts = append ? state.alerts.concat(items) : items;
      state.more = items.length === PAGE;
      state.counts = res.data.counts || {};
      var ids = state.alerts.map(function (a) { return a.id; });
      state.picked = state.picked.filter(function (id) { return ids.indexOf(id) >= 0; });
      renderCounts(); renderList();
    });
  }

  function renderCounts() {
    var c = state.counts;
    $("cNew").textContent = c["new"] || 0;
    $("cOpen").textContent = (c["new"] || 0) + (c.seen || 0) + (c.acknowledged || 0);
    $("cAck").textContent = c.acknowledged || 0;
    $("cClosed").textContent = c.closed || 0;
  }

  function renderList() {
    var body = $("rows");
    body.replaceChildren();
    state.alerts.forEach(function (a) {
      var tr = el("tr");
      if (a.id === state.selected) tr.setAttribute("aria-selected", "true");
      tr.setAttribute("data-alert", String(a.id));
      var c0 = el("td"), box = el("input"); box.type = "checkbox"; box.checked = state.picked.indexOf(a.id) >= 0;
      box.setAttribute("aria-label", "Sélectionner l'alerte " + a.id);
      box.addEventListener("click", function (ev) { ev.stopPropagation(); });
      box.addEventListener("change", function () { state.picked = L.toggleId(state.picked, a.id, box.checked); renderBulk(); });
      c0.appendChild(box);
      var c1 = el("td"); c1.appendChild(el("span", L.priorityLabel(a.priority), "tag alert-" + L.priorityClass(a.priority)));
      var c2 = el("td"); c2.appendChild(el("div", a.type_label, "cell-title"));
      c2.appendChild(el("div", a.title + (a.count > 1 ? "  (×" + a.count + ")" : ""), "muted"));
      var c3 = el("td", cameraLabel(a.camera_id));
      var c4 = el("td", when(a.last_at)); c4.title = L.ageText(a.last_at); c4.className = "num";
      var c5 = el("td"); c5.appendChild(el("span", L.statusLabel(a.status), "tag" + (a.status === "new" ? " warn" : (a.status === "closed" ? " off" : " ok"))));
      tr.append(c0, c1, c2, c3, c4, c5);
      tr.addEventListener("click", function () { openAlert(a.id, true); });
      body.appendChild(tr);
    });
    $("empty").hidden = state.alerts.length > 0;
    $("table").hidden = state.alerts.length === 0;
    $("btnMore").hidden = !state.more;
    $("listInfo").textContent = state.alerts.length ? state.alerts.length + " alerte(s) affichée(s)" : "";
    $("selAll").checked = state.alerts.length > 0 && state.picked.length === state.alerts.length;
    renderBulk();
  }

  function renderBulk() {
    var n = state.picked.length;
    $("selInfo").textContent = n ? n + " sélectionnée(s)" : "";
    ["bulkSeen", "bulkAck", "bulkClose"].forEach(function (id) { $(id).disabled = n === 0; });
  }

  function bulk(status) {
    if (!state.picked.length) return;
    api("POST", "/api/alerts/bulk", { ids: state.picked, status: status }).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      var d = res.data;
      Shell.toast(d.changed + " alerte(s) modifiée(s)" + (d.skipped ? ", " + d.skipped + " ignorée(s) (changement impossible)." : "."));
      state.picked = [];
      loadList(false).then(function () { if (state.selected) openAlert(state.selected, false); });
    });
  }

  // ---- détail ---------------------------------------------------------------------------------------
  var ACTION_LABELS = { created: "Créée", seen: "Consultée", acknowledged: "Acquittée", closed: "Clôturée", reopened: "Rouverte",
                        note: "Note", notified: "Notification" };

  function openAlert(id, markSeen) {
    state.selected = id;
    return api("GET", "/api/alerts/" + id).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); state.selected = null; renderDetail(null); return; }
      var a = res.data;
      if (markSeen && a.status === "new") {                    // ouvrir une alerte nouvelle = la consulter
        return api("POST", "/api/alerts/" + id + "/status", { status: "seen" }).then(function (r2) {
          renderDetail(r2.ok ? r2.data : a); loadList(false);
        });
      }
      renderDetail(a); renderList();
    });
  }

  function setStatus(id, status) {
    api("POST", "/api/alerts/" + id + "/status", { status: status }).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      renderDetail(res.data); loadList(false);
    });
  }

  function renderDetail(a) {
    $("detail").hidden = !a; $("noDetail").hidden = !!a;
    if (!a) return;
    $("dTitle").textContent = a.type_label + " — " + L.priorityLabel(a.priority);
    $("dMeta").textContent = "Alerte n° " + a.id + " · " + a.category_label + " · " + cameraLabel(a.camera_id) + " · " + L.statusLabel(a.status);
    $("dText").textContent = a.title + (a.count > 1 ? " — répétée " + a.count + " fois, dernière " + L.ageText(a.last_at) : " — " + when(a.created_at));
    var box = $("dActions"); box.replaceChildren();
    L.actionsFor(a.status).forEach(function (act) {
      var b = el("button", act[1], "btn small" + (act[0] === "closed" ? "" : " primary")); b.type = "button";
      b.addEventListener("click", function () { setStatus(a.id, act[0]); });
      box.appendChild(b);
    });
    var hist = $("dHistory"); hist.replaceChildren();
    (a.history || []).forEach(function (h) {
      var li = el("li"); li.appendChild(el("span", (ACTION_LABELS[h.action] || h.action) + (h.note ? " : " + h.note : "")));
      li.appendChild(el("span", when(h.timestamp), "muted"));
      hist.appendChild(li);
    });
    var n = a.notified || {}, keys = Object.keys(n);
    $("dNotifTitle").hidden = keys.length === 0;
    $("dNotif").textContent = keys.map(function (k) { return k + " : " + n[k]; }).join(" · ");
    $("dNote").value = "";
  }

  function saveNote() {
    var note = $("dNote").value.trim();
    if (!state.selected || !note) return;
    api("POST", "/api/alerts/" + state.selected + "/note", { note: note }).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      renderDetail(res.data); Shell.toast("Note enregistrée.");
    });
  }

  // ---- règles ----------------------------------------------------------------------------------------
  function checkbox(value, label) {
    var c = el("input"); c.type = "checkbox"; c.checked = !!value; c.setAttribute("aria-label", label); return c;
  }

  function renderRules() {
    var body = $("rulesBody"); body.replaceChildren();
    state.rules.forEach(function (r) {
      var tr = el("tr"); tr.setAttribute("data-rule", r.type);
      var on = checkbox(r.enabled, "Actif : " + r.label), snd = checkbox(r.sound, "Son : " + r.label), ext = checkbox(r.notify, "Notification externe : " + r.label);
      var pr = el("select"); pr.setAttribute("aria-label", "Priorité : " + r.label);
      state.priorities.forEach(function (p) { var o = el("option", L.priorityLabel(p)); o.value = p; pr.appendChild(o); });
      pr.value = r.priority;
      var dd = el("input"); dd.type = "number"; dd.min = "0"; dd.max = "86400"; dd.value = String(r.dedup_sec); dd.setAttribute("aria-label", "Regroupement : " + r.label);
      dd.className = "alerts-num";
      [[el("td", r.label)], [on], [pr], [dd], [snd], [ext]].forEach(function (cell, i) {
        var td = cell[0].tagName === "TD" ? cell[0] : el("td"); if (i) td.appendChild(cell[0]); tr.appendChild(td);
      });
      tr._get = function () {
        return { enabled: on.checked, priority: pr.value, dedup_sec: Number(dd.value), sound: snd.checked, notify: ext.checked };
      };
      body.appendChild(tr);
    });
  }

  function saveRules() {
    var rules = {}, bad = false;
    document.querySelectorAll("#rulesBody tr").forEach(function (tr) {
      var r = tr._get();
      if (!(Number.isInteger(r.dedup_sec) && r.dedup_sec >= 0 && r.dedup_sec <= 86400)) bad = true;
      rules[tr.getAttribute("data-rule")] = r;
    });
    if (bad) { Shell.toast("Le regroupement doit être un nombre entier de secondes (0 à 86400).", "warning"); return; }
    api("PUT", "/api/alerts/rules", { rules: rules }).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.rules = res.data.rules; renderRules(); Shell.toast("Règles enregistrées.");
    });
  }

  function loadRules() {
    return api("GET", "/api/alerts/rules").then(function (res) {
      if (!res.ok) return;
      state.rules = res.data.rules; state.priorities = res.data.priorities; renderRules();
    });
  }

  // ---- canaux externes ---------------------------------------------------------------------------------
  function loadChannels() {
    return api("GET", "/api/alerts/channels").then(function (res) {
      if (!res.ok) return;
      var d = res.data, parts = [];
      parts.push("E-mail : " + (d.email.configured ? "configuré (" + d.email.recipients + " destinataire(s))" : "non configuré"));
      parts.push("Telegram : " + (d.telegram.configured ? "configuré" : "non configuré"));
      $("chInfo").textContent = parts.join(" · ") + ". Envoi à partir de la priorité « " + L.priorityLabel(d.min_priority) + " », " +
        d.per_minute + " messages par minute au plus, " + (d.identities ? "avec" : "sans") + " détail d'identité. " +
        (d.email.configured || d.telegram.configured ? "" : "Voir le README (variables NIIFYEEN_SMTP_*, NIIFYEEN_TELEGRAM_*).");
      $("testEmail").disabled = !d.email.configured; $("testTelegram").disabled = !d.telegram.configured;
    });
  }

  function testChannel(channel) {
    $("chResult").textContent = "Envoi en cours…";
    api("POST", "/api/alerts/channels/test", { channel: channel }).then(function (res) {
      $("chResult").textContent = res.ok ? (channel === "email" ? "E-mail" : "Telegram") + " : " + res.data.result : errorText(res);
    });
  }

  // ---- son ----------------------------------------------------------------------------------------------
  function paintSound() {
    var b = $("btnSound");
    b.replaceChildren(Shell.icon(state.sound ? "volume" : "mute"), document.createTextNode(state.sound ? " Son activé" : " Son coupé"));
    b.setAttribute("aria-pressed", String(state.sound));
  }

  // ---- démarrage -------------------------------------------------------------------------------------------
  function init() {
    Shell.mount({ active: "alerts" });
    state.sound = Shell.prefs.get("alertSound", true) !== false;
    paintSound();
    $("btnSound").addEventListener("click", function () {
      state.sound = !state.sound; Shell.prefs.set("alertSound", state.sound); paintSound();
      if (state.sound) Shell.beep(660, 120);
    });
    document.querySelectorAll("#statusSeg button").forEach(function (b) {
      b.addEventListener("click", function () {
        state.status = b.getAttribute("data-status");
        document.querySelectorAll("#statusSeg button").forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
        loadList(false);
      });
    });
    var timer = null;
    function later() { clearTimeout(timer); timer = setTimeout(function () { loadList(false); }, 300); }
    ["fQ", "fPriority", "fCategory", "fCamera", "fSince", "fUntil"].forEach(function (id) {
      $(id).addEventListener(id === "fQ" ? "input" : "change", later);
    });
    $("selAll").addEventListener("change", function () {
      state.picked = $("selAll").checked ? state.alerts.map(function (a) { return a.id; }) : [];
      renderList();
    });
    $("bulkSeen").addEventListener("click", function () { bulk("seen"); });
    $("bulkAck").addEventListener("click", function () { bulk("acknowledged"); });
    $("bulkClose").addEventListener("click", function () { bulk("closed"); });
    $("btnMore").addEventListener("click", function () { loadList(true); });
    $("dNoteBtn").addEventListener("click", saveNote);
    $("dNote").addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); saveNote(); } });
    $("rulesSave").addEventListener("click", saveRules);
    $("testEmail").addEventListener("click", function () { testChannel("email"); });
    $("testTelegram").addEventListener("click", function () { testChannel("telegram"); });

    var cat = $("fCategory");
    [["target", "Cibles"], ["people", "Personnes"], ["camera", "Caméras"], ["analytics", "Lignes et zones"]].forEach(function (c) {
      var o = el("option", c[1]); o.value = c[0]; cat.appendChild(o);
    });
    api("GET", "/api/cameras").then(function (res) {
      state.meta = res.ok ? (res.data.camera_meta || {}) : {};
      var sel = $("fCamera");
      (res.ok ? res.data.cameras : []).forEach(function (id) { var o = el("option", cameraLabel(id)); o.value = id; sel.appendChild(o); });
      loadList(false);
    });
    loadRules(); loadChannels();
    setInterval(function () { loadList(false).then(function () { if (state.selected) { var s = state.selected; api("GET", "/api/alerts/" + s).then(function (r) { if (r.ok && state.selected === s) renderDetail(r.data); }); } }); }, 5000);
  }

  window.AlertsPage = { state: state, init: init };
  init();
})();
