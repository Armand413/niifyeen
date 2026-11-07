/*
 * Page « Investigation » : recherche à filtres combinables, chronologie, détail d'un événement (plan, alertes,
 * contexte), exports et dossiers d'événement. Tout le texte est inséré avec textContent.
 */
(function () {
  "use strict";

  var L = window.InvestigationLogic, NS = "http://www.w3.org/2000/svg";
  var TL_W = 1000, LABEL_W = 110;
  var state = { result: null, selected: null, picked: [], types: [], cameras: [], meta: {}, floors: [], dossiers: [], dossier: null, detail: null };

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
  function cameraLabel(id) { return id ? ((state.meta[id] && state.meta[id].label) || id) : "—"; }
  function when(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "" : d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function localToIso(v) { if (!v) return ""; var d = new Date(v); return isNaN(d.getTime()) ? "" : d.toISOString(); }

  // ---- filtres ---------------------------------------------------------------------------------------
  function filters() {
    var custom = $("fPeriod").value === "custom";
    return { minutes: custom ? "" : $("fPeriod").value, start: custom ? localToIso($("fFrom").value) : "", end: custom ? localToIso($("fTo").value) : "",
             camera_id: $("fCamera").value, types: state.types, participant_id: $("fPerson").value, session: $("fSession").value,
             q: $("fQ").value, only_alerts: $("fAlerts").checked };
  }

  function renderChips() {
    var box = $("chips"); box.replaceChildren();
    Object.keys(L.CHIP_TYPES).forEach(function (t) {
      var b = el("button", L.CHIP_TYPES[t]); b.type = "button"; b.setAttribute("data-type", t);
      b.setAttribute("aria-pressed", String(state.types.indexOf(t) >= 0));
      b.addEventListener("click", function () { state.types = L.toggleType(state.types, t, state.types.indexOf(t) < 0); renderChips(); later(); });
      box.appendChild(b);
    });
  }

  var timer = null;
  function later() { clearTimeout(timer); timer = setTimeout(search, 300); }

  function search() {
    var f = filters(), qs = L.buildQuery(f);
    $("fHint").textContent = f.session ? "Une session de cible limite la recherche à la durée de cette session (±30 s) et aux éléments qui la concernent." : "";
    $("btnCsv").href = "/api/investigation/timeline.csv" + qs;
    $("btnPdf").href = "/api/investigation/report.pdf" + qs;
    $("btnPdf").textContent = f.session ? "Rapport de session (PDF)" : "Rapport PDF";
    return api("GET", "/api/investigation/timeline" + qs).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      state.result = res.data;
      var keys = res.data.items.map(function (i) { return i.key; });
      state.picked = state.picked.filter(function (k) { return keys.indexOf(k) >= 0; });
      if (state.selected && keys.indexOf(state.selected) < 0) state.selected = null;
      render();
    });
  }

  // ---- affichage des résultats ------------------------------------------------------------------------
  function render() { renderSummary(); renderTimeline(); renderRows(); }

  function renderSummary() {
    var r = state.result;
    $("summary").textContent = r.total + " élément(s)" + (r.truncated ? " (liste tronquée : affinez les filtres)" : "");
  }

  function renderTimeline() {
    var r = state.result, svg = $("tl"), lay = L.layout(r.items, r.since, r.until, TL_W - LABEL_W);
    svg.replaceChildren();
    var H = Math.max(40, lay.height) + 26;
    svg.setAttribute("viewBox", "0 0 " + TL_W + " " + H);
    L.ticks(Date.parse(r.since), Date.parse(r.until), TL_W - LABEL_W).forEach(function (t) {
      svg.appendChild(s("line", { x1: LABEL_W + t.x, x2: LABEL_W + t.x, y1: 0, y2: H - 20, "class": "tick" }));
      svg.appendChild(s("text", { x: LABEL_W + t.x, y: H - 6, "text-anchor": "middle" }, t.label));
    });
    lay.lanes.forEach(function (lane) {
      svg.appendChild(s("line", { x1: LABEL_W, x2: TL_W, y1: lane.y, y2: lane.y, "class": "lane" }));
      svg.appendChild(s("text", { x: 4, y: lane.y + 4 }, lane.id ? cameraLabel(lane.id).slice(0, 16) : "Autres"));
    });
    lay.marks.forEach(function (m) {
      var cls = "mark k-" + m.kind + (m.severity !== "info" ? " s-" + m.severity : "") + (m.key === state.selected ? " is-selected" : "");
      var rect = s("rect", { x: LABEL_W + m.x - (m.w < 6 ? 3 : 0), y: m.y - 8, width: Math.max(6, m.w), height: 16, rx: 3, "class": cls, "data-key": m.key });
      var title = s("title", {}, m.type); rect.appendChild(title);
      rect.addEventListener("click", function () { choose(m.key); });
      svg.appendChild(rect);
    });
    $("tlInfo").textContent = r.items.length ? "Une ligne par caméra. Vert : passages (détections regroupées) ; bleu : événements ; orange / rouge : événements importants ; violet : étapes de cible." : "";
  }

  function renderRows() {
    var body = $("rows"); body.replaceChildren();
    state.result.items.forEach(function (it) {
      var tr = el("tr"); tr.setAttribute("data-key", it.key);
      if (it.key === state.selected) tr.setAttribute("aria-selected", "true");
      var c0 = el("td");
      if (it.kind === "event") {
        var box = el("input"); box.type = "checkbox"; box.checked = state.picked.indexOf(it.key) >= 0;
        box.setAttribute("aria-label", "Sélectionner pour le dossier");
        box.addEventListener("click", function (ev) { ev.stopPropagation(); });
        box.addEventListener("change", function () { state.picked = L.toggleType(state.picked, it.key, box.checked); renderBulk(); });
        c0.appendChild(box);
      }
      var c1 = el("td", when(it.ts), "num");
      var c2 = el("td"); c2.appendChild(el("span", L.kindLabel(it.kind), "tag"));
      var c3 = el("td", it.title);
      var c4 = el("td", cameraLabel(it.camera_id));
      var c5 = el("td");
      if (it.alert) c5.appendChild(el("span", "n°" + it.alert.id, "tag " + (it.alert.status === "new" ? "warn" : "ok")));
      tr.append(c0, c1, c2, c3, c4, c5);
      tr.addEventListener("click", function () { choose(it.key); });
      body.appendChild(tr);
    });
    $("empty").hidden = state.result.items.length > 0;
    $("table").hidden = state.result.items.length === 0;
    $("selAll").checked = false;
    renderBulk();
  }

  function renderBulk() {
    $("selInfo").textContent = state.picked.length ? state.picked.length + " sélectionné(s)" : "";
    $("btnAddMany").disabled = !state.picked.length || !state.dossier;
    $("btnAddMany").title = state.dossier ? "" : "Choisissez d'abord un dossier";
  }

  // ---- détail d'un événement ------------------------------------------------------------------------------
  function find(key) { return (state.result.items || []).filter(function (i) { return i.key === key; })[0] || null; }

  function choose(key) {
    state.selected = key; renderTimeline(); renderRows();
    var it = find(key);
    if (!it || it.kind !== "event") { renderDetail(null); return; }
    api("GET", "/api/investigation/events/" + it.id).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      renderDetail(res.data);
    });
  }

  function renderDetail(d) {
    state.detail = d;
    $("detail").hidden = !d; $("noDetail").hidden = !!d;
    if (!d) return;
    $("dTitle").textContent = d.title;
    $("dMeta").textContent = when(d.ts) + " · " + d.type + " · " + cameraLabel(d.camera_id) + " · gravité " + d.severity;
    $("dVideo").textContent = d.video.available ? "Extrait vidéo disponible." : d.video.note;
    var link = d.video.available && typeof RecordingsLogic !== "undefined" ? RecordingsLogic.clipLink(d.video.recording_id, d.video.offset_sec) : "";
    $("dVideoLink").hidden = !link; if (link) $("dVideoLink").href = link;
    var box = $("dAlerts"); box.replaceChildren();
    (d.alerts || []).forEach(function (a) {
      box.appendChild(el("p", "Alerte n°" + a.id + " — " + a.priority + " — " + a.status + (a.count > 1 ? " (×" + a.count + ")" : ""), "tag"));
    });
    var ctx = $("dContext"); ctx.replaceChildren();
    (d.context || []).forEach(function (c) { var li = el("li"); li.appendChild(el("span", c.title)); li.appendChild(el("span", when(c.ts), "muted")); ctx.appendChild(li); });
    if (!(d.context || []).length) ctx.appendChild(el("li", "Rien d'autre sur cette caméra à ce moment."));
    drawPlan(d.camera_id);
  }

  function drawPlan(camId) {
    var svg = $("dPlan"), m = state.meta[camId], floor = null;
    svg.replaceChildren();
    if (!camId || !m || !m.placed) {
      $("dPlanInfo").textContent = camId ? "Cette caméra n'est pas placée sur un plan (page Carte)." : "Événement sans caméra.";
      return;
    }
    state.floors.forEach(function (f) { if (f.id === m.floor_id) floor = f; });
    var size = floor ? MapLogic.planSize(floor.width, floor.height) : MapLogic.planSize(0, 0);
    var one = {}; one[camId] = m;
    var image = MapLogic.safeImage(floor ? floor.image_url : null);
    svg.setAttribute("viewBox", "0 0 " + size.width + " " + size.height);
    SitePlan.render(svg, null, one, [{ id: 1, label: "Événement", color: "#d92d20", active: true, camera_id: camId, score: null,
                                      path: [{ camera_id: camId, ts: Date.now() / 1000 }] }],
      { width: size.width, height: size.height, force: true, signature: "inv" + camId + image,
        before: function (root, doc, dim) {
          var bg = doc.createElementNS(NS, "image");
          bg.setAttribute("href", image); bg.setAttribute("x", "0"); bg.setAttribute("y", "0");
          bg.setAttribute("width", String(dim.width)); bg.setAttribute("height", String(dim.height));
          bg.setAttribute("preserveAspectRatio", "none"); root.appendChild(bg);
        } });
    $("dPlanInfo").textContent = "Caméra : " + cameraLabel(camId) + (floor ? " — " + floor.name : "") + ".";
  }

  // ---- dossiers ----------------------------------------------------------------------------------------------
  function loadDossiers(selectId) {
    return api("GET", "/api/dossiers").then(function (res) {
      state.dossiers = res.ok ? res.data.dossiers : [];
      var sel = $("dossierSel"); sel.replaceChildren();
      var none = el("option", "— aucun —"); none.value = ""; sel.appendChild(none);
      state.dossiers.forEach(function (d) { var o = el("option", d.title + " (" + d.items_count + ")"); o.value = String(d.id); sel.appendChild(o); });
      var want = selectId || (state.dossier && state.dossier.id) || "";
      sel.value = String(want);
      if (sel.value !== String(want)) sel.value = "";
      return openDossier(sel.value);
    });
  }

  function openDossier(id) {
    if (!id) { state.dossier = null; $("dossierBox").hidden = true; renderBulk(); return Promise.resolve(); }
    return api("GET", "/api/dossiers/" + id).then(function (res) {
      if (!res.ok) { state.dossier = null; $("dossierBox").hidden = true; renderBulk(); return; }
      state.dossier = res.data; renderDossier();
    });
  }

  function renderDossier() {
    var d = state.dossier;
    $("dossierBox").hidden = !d; if (!d) return;
    $("dossierNote").value = d.note || "";
    $("dossierPdf").href = "/api/dossiers/" + d.id + "/report.pdf";
    var ul = $("dossierItems"); ul.replaceChildren();
    d.items.forEach(function (it) {
      var li = el("li"); li.setAttribute("data-item", String(it.item_id));
      var left = el("div");
      left.appendChild(el("div", (it.kind === "alert" ? "Alerte : " : "") + it.title, it.missing ? "muted" : "cell-title"));
      left.appendChild(el("div", it.ts ? when(it.ts) + " · " + cameraLabel(it.camera_id) : "", "muted"));
      var note = el("input"); note.value = it.note || ""; note.maxLength = 2000; note.placeholder = "Note"; note.setAttribute("aria-label", "Note de l'élément");
      note.addEventListener("change", function () {
        api("PUT", "/api/dossiers/" + d.id + "/items/" + it.item_id, { note: note.value }).then(function (r) { if (!r.ok) Shell.toast(errorText(r), "warning"); });
      });
      left.appendChild(note);
      var rm = el("button", "Retirer", "btn small"); rm.type = "button";
      rm.addEventListener("click", function () {
        api("DELETE", "/api/dossiers/" + d.id + "/items/" + it.item_id).then(function (r) { if (!r.ok) Shell.toast(errorText(r), "warning"); loadDossiers(d.id); });
      });
      li.append(left, rm); ul.appendChild(li);
    });
    if (!d.items.length) ul.appendChild(el("li", "Dossier vide : sélectionnez des événements puis « Ajouter au dossier »."));
    renderBulk();
  }

  function createDossier() {
    var title = $("dossierNew").value.trim();
    if (!title) { Shell.toast("Donnez un titre au dossier.", "warning"); return; }
    api("POST", "/api/dossiers", { title: title }).then(function (res) {
      if (!res.ok) { Shell.toast(errorText(res), "warning"); return; }
      $("dossierNew").value = ""; loadDossiers(res.data.id);
    });
  }

  function addEvents(ids) {
    if (!state.dossier) { Shell.toast("Choisissez ou créez d'abord un dossier.", "warning"); return Promise.resolve(); }
    var added = 0, dup = 0, failed = 0, did = state.dossier.id;
    return ids.reduce(function (chain, id) {
      return chain.then(function () {
        return api("POST", "/api/dossiers/" + did + "/items", { kind: "event", ref_id: id }).then(function (r) {
          if (r.ok) added++; else if (r.status === 409 && /déjà/.test(String(r.data.detail))) dup++; else failed++;
        });
      });
    }, Promise.resolve()).then(function () {
      Shell.toast(added + " ajouté(s)" + (dup ? ", " + dup + " déjà présent(s)" : "") + (failed ? ", " + failed + " refusé(s)" : "") + ".", failed ? "warning" : "info");
      return loadDossiers(did);
    });
  }

  function pickedEventIds() {
    return state.picked.map(function (k) { var it = find(k); return it && it.id; }).filter(function (x) { return x; });
  }

  // ---- démarrage ----------------------------------------------------------------------------------------------
  function init() {
    Shell.mount({ active: "investigation" });
    renderChips();
    $("fPeriod").addEventListener("change", function () {
      var c = $("fPeriod").value === "custom"; $("fFromBox").hidden = !c; $("fToBox").hidden = !c;
      if (c && !$("fTo").value) {
        var now = new Date(), pad = function (n) { return (n < 10 ? "0" : "") + n; }, fmt = function (d) {
          return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()); };
        $("fTo").value = fmt(now); $("fFrom").value = fmt(new Date(now.getTime() - 3600e3));
      }
      later();
    });
    ["fFrom", "fTo", "fCamera", "fPerson", "fAlerts"].forEach(function (id) { $(id).addEventListener("change", later); });
    $("fSession").addEventListener("change", later);
    $("fQ").addEventListener("input", later);
    $("selAll").addEventListener("change", function () {
      state.picked = $("selAll").checked ? state.result.items.filter(function (i) { return i.kind === "event"; }).map(function (i) { return i.key; }) : [];
      renderRows(); $("selAll").checked = state.picked.length > 0;
    });
    $("btnAddMany").addEventListener("click", function () { addEvents(pickedEventIds()); });
    $("dAdd").addEventListener("click", function () { if (state.detail) addEvents([state.detail.id]); });
    $("dossierCreate").addEventListener("click", createDossier);
    $("dossierNew").addEventListener("keydown", function (ev) { if (ev.key === "Enter") { ev.preventDefault(); createDossier(); } });
    $("dossierSel").addEventListener("change", function () { openDossier($("dossierSel").value); });
    $("dossierSave").addEventListener("click", function () {
      if (!state.dossier) return;
      api("PUT", "/api/dossiers/" + state.dossier.id, { note: $("dossierNote").value }).then(function (r) {
        Shell.toast(r.ok ? "Note du dossier enregistrée." : errorText(r), r.ok ? "info" : "warning");
      });
    });
    $("dossierDelete").addEventListener("click", function () {
      var d = state.dossier;
      if (!d || !confirm("Supprimer le dossier « " + d.title + " » ? Les événements eux-mêmes ne sont pas supprimés.")) return;
      api("DELETE", "/api/dossiers/" + d.id).then(function () { state.dossier = null; loadDossiers(""); });
    });

    var pending = [
      api("GET", "/api/cameras").then(function (res) {
        state.cameras = res.ok ? res.data.cameras : []; state.meta = res.ok ? (res.data.camera_meta || {}) : {};
        state.cameras.forEach(function (id) { var o = el("option", cameraLabel(id)); o.value = id; $("fCamera").appendChild(o); });
      }),
      api("GET", "/api/floors").then(function (res) { state.floors = res.ok ? (res.data.floors || []) : []; }),
      api("GET", "/api/people?page_size=200").then(function (res) {
        if (!res.ok) { $("fPerson").disabled = true; $("fPerson").title = "Identités masquées ou indisponibles"; return; }
        res.data.items.forEach(function (p) { var o = el("option", p.display_name + (p.actif ? "" : " (inactif)")); o.value = String(p.id); $("fPerson").appendChild(o); });
      }),
      api("GET", "/api/history/trajectories?minutes=43200").then(function (res) {
        (res.ok ? res.data.items : []).forEach(function (t) { var o = el("option", (t.label || "Cible") + " — " + when(t.started) + " (" + t.cameras + " caméra(s))"); o.value = String(t.session); $("fSession").appendChild(o); });
      })
    ];
    Promise.all(pending).then(function () { return Promise.all([search(), loadDossiers("")]); });
  }

  window.InvestigationPage = { state: state, init: init };
  init();
})();
