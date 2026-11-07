/*
 * Page « Personnes » : liste, fiche individuelle (informations, références, activité), ajout avec détection de doublon.
 * Les personnes sont identifiées par leur numéro de fiche, jamais par leur nom : renommer quelqu'un ne casse pas son historique.
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 */
(function () {
  "use strict";

  var PAGE_SIZE = 25;
  var STATUS_TEXT = { probable: "Correspondance probable", a_verifier: "À vérifier" };
  var STATUS_CLASS = { probable: "ok", a_verifier: "warn" };
  var state = { items: [], total: 0, page: 1, q: "", statut: "", selected: null, detail: null, tab: "info",
                blocked: false, cameras: {}, recognition: null };
  var searchTimer = null, thumbUrls = [];

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function when(iso) { var d = new Date(iso); return isNaN(d.getTime()) ? "—" : d.toLocaleString(); }
  function duration(sec) {
    if (sec < 1) return "< 1 s";
    if (sec < 60) return Math.round(sec) + " s";
    var m = Math.floor(sec / 60);
    return sec < 3600 ? m + " min " + Math.round(sec % 60) + " s" : Math.floor(sec / 3600) + " h " + (m % 60) + " min";
  }
  function cameraLabel(id) { return (state.cameras[id] && state.cameras[id].label) || id || "—"; }

  function api(method, path, body) {
    var options = { method: method, headers: {} };
    if (body instanceof FormData) options.body = body;
    else if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
    });
  }
  function errorOf(res) {
    var d = res.data && res.data.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) return d;
    if (typeof d === "string") return { message: d, field: null };
    if (Array.isArray(d)) return { message: "Saisie invalide : " + d.map(function (x) { return (x.loc || []).slice(-1) + " " + x.msg; }).join(" ; "), field: null };
    return { message: "Erreur " + res.status, field: null };
  }
  function tag(text, kind) { return el("span", text, "tag" + (kind ? " " + kind : "")); }

  // ---- liste ---------------------------------------------------------------------------------------
  function loadList() {
    var params = new URLSearchParams({ page: state.page, page_size: PAGE_SIZE });
    if (state.q) params.set("q", state.q);
    if (state.statut) params.set("statut", state.statut);
    return api("GET", "/api/people?" + params).then(function (res) {
      if (res.status === 403) { state.blocked = true; state.items = []; state.total = 0; renderList(); showBlocked(errorOf(res).message); return; }
      if (!res.ok) { Shell.toast("Liste indisponible.", "warning"); return; }
      state.blocked = false; showBlocked("");
      state.items = res.data.items; state.total = res.data.total; state.recognition = res.data.recognition;
      renderList();
    });
  }

  function showBlocked(text) { var b = $("blocked"); b.hidden = !text; b.textContent = text || ""; }

  function renderList() {
    var body = $("rows");
    body.replaceChildren.apply(body, state.items.map(function (p) {
      var tr = el("tr", null, "selectable" + (p.id === state.selected ? " selected" : ""));
      tr.tabIndex = 0;
      tr.setAttribute("data-id", String(p.id));
      var name = el("td"); name.append(el("div", p.display_name, "cell-title"), el("div", p.matricule ? "Réf. " + p.matricule : "Fiche n°" + p.id, "cell-sub"));
      var status = el("td"); status.appendChild(tag(p.actif ? "Active" : "Désactivée", p.actif ? "ok" : "off"));
      var last = el("td"); last.textContent = p.last_seen ? when(p.last_seen) + " — " + cameraLabel(p.last_camera) : "Jamais détectée";
      tr.append(name, status, el("td", String(p.references), "num"), last);
      tr.addEventListener("click", function () { select(p.id, true); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") select(p.id, true); });
      return tr;
    }));
    var pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    $("pageLabel").textContent = "Page " + state.page + " / " + pages + " · " + state.total + " personne(s)";
    $("prev").disabled = state.page <= 1; $("next").disabled = state.page >= pages;
    var empty = $("emptyMsg");
    empty.hidden = state.items.length > 0 || state.blocked;
    empty.textContent = state.q || state.statut ? "Aucune personne ne correspond à cette recherche." : "Aucune personne enrôlée. Cliquez sur « Ajouter une personne ».";
    $("chipTotal").replaceChildren(Shell.icon("users"), el("span", "Personnes"), el("b", String(state.total)));
  }

  // ---- fiche ------------------------------------------------------------------------------------------
  function select(id, push) {
    state.selected = id; state.tab = "info";
    if (push && window.history && history.pushState) history.pushState({ id: id }, "", "/people/" + id);
    renderList();
    return api("GET", "/api/people/" + id).then(function (res) {
      if (!res.ok) { state.detail = null; renderFiche(errorOf(res).message); return; }
      state.detail = res.data; renderFiche();
    });
  }

  function reloadDetail() { return state.selected ? select(state.selected, false) : Promise.resolve(); }

  function field(id, label, input, hint) {
    var f = el("div", null, "field"); f.setAttribute("data-f", id);
    var l = el("label", label); l.setAttribute("for", input.id);
    f.append(l, input);
    if (hint) f.appendChild(el("span", hint, "hint"));
    f.appendChild(el("span", "", "error"));
    return f;
  }
  function showFieldError(root, name, message) {
    var f = root.querySelector('[data-f="' + name + '"]');
    if (f) { f.classList.add("invalid"); var e = f.querySelector(".error"); if (e) e.textContent = message; return true; }
    return false;
  }
  function clearFieldErrors(root) {
    Array.prototype.forEach.call(root.querySelectorAll(".field.invalid"), function (f) { f.classList.remove("invalid"); });
    Array.prototype.forEach.call(root.querySelectorAll(".field .error"), function (e) { e.textContent = ""; });
  }

  function renderFiche(problem) {
    var box = $("fiche");
    var d = state.detail;
    if (problem) { box.replaceChildren(el("p", problem, "notice danger")); return; }
    if (!d) { box.replaceChildren(el("p", state.blocked ? "" : "Sélectionnez une personne pour voir sa fiche.", "muted")); return; }

    var head = el("div", null, "fiche-head");
    var title = el("div"); title.append(el("h2", d.display_name), tag(d.actif ? "Active" : "Désactivée", d.actif ? "ok" : "off"));
    var actions = el("div", null, "row-actions");
    var toggle = el("button", d.actif ? "Désactiver" : "Activer", "btn small"); toggle.type = "button";
    toggle.title = "Une personne désactivée reste reconnue : sa détection déclenche un avertissement dans la supervision.";
    toggle.addEventListener("click", function () { update({ actif: !d.actif }, d.display_name + (d.actif ? " désactivée." : " activée.")); });
    var del = el("button", "Supprimer", "btn small danger"); del.type = "button";
    del.addEventListener("click", function () { remove(d); });
    actions.append(toggle, del);
    head.append(title, actions);

    var tabs = el("div", null, "fiche-tabs"); tabs.setAttribute("role", "tablist");
    var panes = {};
    [["info", "Informations"], ["refs", "Références (" + d.references.length + ")"], ["activity", "Activité"]].forEach(function (t) {
      var b = el("button", t[1]); b.type = "button"; b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(state.tab === t[0]));
      b.addEventListener("click", function () {
        state.tab = t[0];
        Array.prototype.forEach.call(tabs.children, function (x) { x.setAttribute("aria-selected", String(x === b)); });
        Object.keys(panes).forEach(function (k) { panes[k].hidden = k !== t[0]; });
      });
      tabs.appendChild(b);
    });
    panes.info = infoPane(d); panes.refs = refsPane(d); panes.activity = activityPane(d);
    Object.keys(panes).forEach(function (k) { panes[k].hidden = k !== state.tab; });
    box.replaceChildren(head, tabs, panes.info, panes.refs, panes.activity);
  }

  function infoPane(d) {
    var form = el("form"); form.noValidate = true;
    var nom = el("input"); nom.id = "eNom"; nom.value = d.nom; nom.maxLength = 100;
    var prenom = el("input"); prenom.id = "ePrenom"; prenom.value = d.prenom; prenom.maxLength = 60;
    var mat = el("input"); mat.id = "eMat"; mat.value = d.matricule; mat.maxLength = 40;
    var infos = el("textarea"); infos.id = "eInfos"; infos.value = d.infos; infos.maxLength = 500;
    var grid = el("div", null, "form-grid");
    var wide = field("infos", "Informations", infos); wide.classList.add("wide");
    grid.append(field("nom", "Nom", nom), field("prenom", "Prénom", prenom), field("matricule", "Référence / matricule", mat, "Facultatif ; unique."), wide);
    var save = el("button", "Enregistrer les modifications", "btn primary"); save.type = "submit";
    var dates = el("dl", null, "kv"); dates.style.marginTop = "14px";
    [["Fiche n°", String(d.id)], ["Enrôlée le", when(d.date_enrolement)], ["Consentement déclaré le", when(d.date_consentement)],
     ["Dernière modification", when(d.updated_at)]].forEach(function (r) { dates.append(el("dt", r[0]), el("dd", r[1])); });
    form.append(grid, save, dates);
    form.addEventListener("submit", function (e) {
      e.preventDefault(); clearFieldErrors(form);
      update({ nom: nom.value, prenom: prenom.value, matricule: mat.value, infos: infos.value }, "Fiche enregistrée.", form);
    });
    return form;
  }

  function refsPane(d) {
    var box = el("div");
    box.appendChild(el("p", "Seules des empreintes faciales sont conservées, jamais les photos. Plusieurs angles et éclairages améliorent la reconnaissance.", "muted"));
    var list = el("ul", null, "list-plain");
    d.references.forEach(function (r) {
      var li = el("li"); li.appendChild(el("span", "Référence n°" + r.id + " — " + r.source));
      var b = el("button", "Supprimer", "btn small danger"); b.type = "button";
      b.disabled = d.references.length <= 1; b.title = b.disabled ? "Une personne doit garder au moins une référence." : "";
      b.addEventListener("click", function () {
        api("DELETE", "/api/people/" + d.id + "/references/" + r.id).then(function (res) {
          if (!res.ok) { Shell.toast(errorOf(res).message, "warning"); return; }
          Shell.toast("Référence supprimée."); refresh();
        });
      });
      li.appendChild(b); list.appendChild(li);
    });
    var file = el("input"); file.type = "file"; file.accept = "image/*"; file.multiple = true; file.id = "eFiles";
    file.setAttribute("aria-label", "Ajouter des photos de référence");
    var add = el("button", "Ajouter ces photos", "btn small"); add.type = "button";
    add.addEventListener("click", function () {
      if (!file.files.length) { Shell.toast("Choisissez au moins une photo.", "warning"); return; }
      var fd = new FormData();
      Array.prototype.forEach.call(file.files, function (f) { fd.append("photos", f); });
      add.disabled = true;
      api("POST", "/api/people/" + d.id + "/references", fd).then(function (res) {
        add.disabled = false;
        if (!res.ok) { Shell.toast(errorOf(res).message, "warning"); return; }
        Shell.toast(res.data.added + " référence(s) ajoutée(s)" + (res.data.ignored_photos ? " — " + res.data.ignored_photos + " photo(s) sans visage ignorée(s)." : "."));
        refresh();
      });
    });
    var row = el("div"); row.style.cssText = "display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap";
    row.append(file, add);
    box.append(list, row);
    return box;
  }

  function activityPane(d) {
    var a = d.activity, box = el("div");
    if (!a.passages) { box.appendChild(el("p", "Aucune détection enregistrée pour cette personne sur la période conservée.", "muted")); }
    else {
      var stats = el("div", null, "stat-grid");
      [[a.passages, "passages"], [a.observations, "observations"], [a.probable_passages, "correspondances probables"],
       [a.to_verify_passages, "à vérifier"]].forEach(function (s) { var c = el("div", null, "stat"); c.append(el("b", String(s[0])), el("span", s[1])); stats.appendChild(c); });
      box.appendChild(stats);
      var kv = el("dl", null, "kv");
      [["Première détection", when(a.first_seen)], ["Dernière détection", when(a.last_seen)], ["Dernière caméra", a.last_camera_label || a.last_camera]]
        .forEach(function (r) { kv.append(el("dt", r[0]), el("dd", r[1])); });
      box.appendChild(kv);

      box.appendChild(el("div", "Zones visitées", "section-title"));
      var zones = el("ul", null, "list-plain");
      a.zones.forEach(function (z) { var li = el("li"); li.append(el("span", z.zone), el("span", z.passages + " passage(s) · dernier : " + when(z.last_seen), "muted")); zones.appendChild(li); });
      box.appendChild(zones);

      box.appendChild(el("div", "Par caméra", "section-title"));
      var cams = el("ul", null, "list-plain");
      a.by_camera.forEach(function (c) { var li = el("li"); li.append(el("span", c.camera_label), el("span", c.passages + " passage(s) · " + c.observations + " observation(s)", "muted")); cams.appendChild(li); });
      box.appendChild(cams);
    }

    box.appendChild(el("div", "Chronologie des passages", "section-title"));
    var tl = el("ul", null, "list-plain");
    d.timeline.items.forEach(function (p) {
      var li = el("li"), left = el("span", when(p.first_seen) + " — " + p.camera_label + " — " + duration(p.duration_sec));
      li.appendChild(left);
      if (p.match_status) li.appendChild(tag(STATUS_TEXT[p.match_status] || p.match_status, STATUS_CLASS[p.match_status] || ""));
      tl.appendChild(li);
    });
    box.appendChild(tl);
    if (d.timeline.total > d.timeline.items.length) box.appendChild(el("p", "… et " + (d.timeline.total - d.timeline.items.length) + " passage(s) plus ancien(s).", "muted"));
    if (!d.timeline.items.length) tl.appendChild(el("li", "Aucun passage."));

    box.appendChild(el("div", "Événements de supervision", "section-title"));
    var ev = el("ul", null, "list-plain");
    d.events.forEach(function (e) { var li = el("li"); li.append(el("span", when(e.timestamp) + " — " + e.title), tag(e.severity === "warning" ? "Avertissement" : "Information", e.severity === "warning" ? "warn" : "")); ev.appendChild(li); });
    if (!d.events.length) ev.appendChild(el("li", "Aucun événement."));
    box.appendChild(ev);
    box.appendChild(el("p", "Une correspondance est une estimation : « probable » ou « à vérifier », jamais une identité certaine. Les passages pendant lesquels la personne était la cible d'une recherche sont enregistrés comme « cible » et ne sont pas comptés ici.", "muted"));
    return box;
  }

  // ---- actions ------------------------------------------------------------------------------------------------
  function refresh() { return Promise.all([loadList(), reloadDetail()]); }

  function update(fields, successText, form) {
    var id = state.selected;
    api("PUT", "/api/people/" + id, fields).then(function (res) {
      if (!res.ok) {
        var e = errorOf(res);
        if (!(form && e.field && showFieldError(form, e.field, e.message))) Shell.toast(e.message, "warning");
        return;
      }
      Shell.toast(successText); refresh();
    });
  }

  function remove(d) {
    if (!window.confirm("Supprimer définitivement la fiche de « " + d.display_name + " » ?\n\nSes empreintes sont effacées et son nom est retiré " +
                        "de l'historique et des événements. Cette action est irréversible.")) return;
    api("DELETE", "/api/people/" + d.id).then(function (res) {
      if (!res.ok) { Shell.toast(errorOf(res).message, "warning"); return; }
      Shell.toast("Fiche supprimée.");
      state.selected = null; state.detail = null;
      if (window.history && history.pushState) history.pushState({}, "", "/people");
      renderFiche(); loadList();
    });
  }

  // ---- ajout ---------------------------------------------------------------------------------------------------
  function clearThumbs() { thumbUrls.forEach(function (u) { URL.revokeObjectURL(u); }); thumbUrls = []; $("aThumbs").replaceChildren(); }

  function openForm() {
    $("form").reset(); clearThumbs(); clearFieldErrors($("form"));
    $("dupBox").hidden = true; $("formError").hidden = true;
    $("modal").hidden = false; $("aNom").focus();
  }
  function closeForm() { $("modal").hidden = true; clearThumbs(); }

  function submitForm(e) {
    e.preventDefault();
    var root = $("form");
    clearFieldErrors(root); $("formError").hidden = true;
    var problems = [];
    if (!$("aNom").value.trim()) problems.push(["nom", "Le nom est obligatoire."]);
    var files = $("aPhotos").files;
    if (!files.length) problems.push(["photos", "Ajoutez au moins une photo."]);
    else if (files.length > 10) problems.push(["photos", "10 photos au maximum."]);
    if (!$("aConsent").checked) problems.push(["consentement", "Le consentement de la personne est obligatoire."]);
    if (problems.length) { problems.forEach(function (p) { showFieldError(root, p[0], p[1]); }); return; }

    var fd = new FormData();
    fd.append("nom", $("aNom").value); fd.append("prenom", $("aPrenom").value); fd.append("matricule", $("aMatricule").value);
    fd.append("infos", $("aInfos").value); fd.append("actif", $("aActif").value); fd.append("consentement", "true");
    if ($("dupConfirm").checked) fd.append("confirmer_doublon", "true");
    Array.prototype.forEach.call(files, function (f) { fd.append("photos", f); });
    var save = $("btnSave"); save.disabled = true;
    api("POST", "/participants/enroll", fd).then(function (res) {
      save.disabled = false;
      if (res.ok) {
        Shell.toast(res.data.display_name + " enrôlée.");
        closeForm();
        loadList().then(function () { select(res.data.participant_id, true); });
        return;
      }
      var err = errorOf(res);
      if (res.status === 409 && err.needs_confirmation) {
        $("dupText").textContent = err.message + (err.existing_name ? " Fiche existante : " + err.existing_name + " (n°" + err.existing_id + ")." : "");
        $("dupBox").hidden = false; return;
      }
      if (!(err.field && showFieldError(root, err.field, err.message))) { $("formError").textContent = err.message; $("formError").hidden = false; }
    }).catch(function () { save.disabled = false; $("formError").textContent = "Le serveur ne répond pas."; $("formError").hidden = false; });
  }

  // ---- initialisation --------------------------------------------------------------------------------------------
  function idFromPath() { var m = /^\/people\/(\d+)\/?$/.exec(location.pathname); return m ? Number(m[1]) : null; }

  Shell.mount({ active: "people" });
  $("btnAdd").replaceChildren(Shell.icon("plus"), el("span", "Ajouter une personne"));
  $("btnAdd").addEventListener("click", openForm);
  $("btnClose").addEventListener("click", closeForm);
  $("btnCancel").addEventListener("click", closeForm);
  $("form").addEventListener("submit", submitForm);
  $("aPhotos").addEventListener("change", function () {
    clearThumbs();
    Array.prototype.slice.call($("aPhotos").files, 0, 10).forEach(function (f) {
      var url = URL.createObjectURL(f); thumbUrls.push(url);
      var img = el("img"); img.src = url; img.alt = f.name; $("aThumbs").appendChild(img);
    });
  });
  $("search").addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { state.q = $("search").value.trim(); state.page = 1; loadList(); }, 300);
  });
  $("statut").addEventListener("change", function () { state.statut = $("statut").value; state.page = 1; loadList(); });
  $("prev").addEventListener("click", function () { state.page = Math.max(1, state.page - 1); loadList(); });
  $("next").addEventListener("click", function () { state.page += 1; loadList(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("modal").hidden) closeForm(); });
  window.addEventListener("popstate", function () {
    var id = idFromPath();
    if (id) select(id, false); else { state.selected = null; state.detail = null; renderFiche(); renderList(); }
  });

  renderFiche();
  fetch("/api/cameras").then(function (r) { return r.ok ? r.json() : { camera_meta: {} }; })
    .then(function (j) { state.cameras = j.camera_meta || {}; }).catch(function () { /* libellés = identifiants */ })
    .then(loadList)
    .then(function () { var id = idFromPath(); if (id) select(id, false); });
})();
