/*
 * Autorités : qui est prévenu, sur quel téléphone, pour quelles caméras ; journal des envois.
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 */
(function () {
  "use strict";

  var state = { authorities: [], cameras: [], telegram: false, cooldown: 120, editing: null, inbox: {} };

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
  function errorOf(res) {
    var d = res.data && res.data.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) return { message: d.message || "Erreur.", field: d.field || null };
    if (Array.isArray(d)) return { message: "Requête invalide.", field: null };
    return { message: "Erreur " + res.status, field: null };
  }
  function camLabel(id) {
    var c = state.cameras.filter(function (x) { return x.id === id; })[0];
    return c ? c.label : id;
  }

  // ---- liste ---------------------------------------------------------------------------------------
  function load() {
    return api("GET", "/api/authorities").then(function (res) {
      if (!res.ok) { Shell.toast("Liste des autorités indisponible.", "warning"); return; }
      state.authorities = res.data.authorities || [];
      state.cameras = res.data.cameras || [];
      state.telegram = !!res.data.telegram_configured;
      state.cooldown = res.data.cooldown_sec || 120;
      state.inbox = res.data.inbox || {};
      $("scope").checked = !!res.data.enabled;
      render();
    });
  }

  function render() {
    $("chipMode").replaceChildren(Shell.icon("bell"), el("span", "Canal"), el("b", state.telegram ? "Interne + Telegram" : "Interne"));
    $("modeNote").textContent = state.inbox.enabled === false
      ? "La boîte de réception interne est désactivée (NIIFYEEN_INBOX_ENABLED=0) : les messages ne sont visibles que dans le journal ci-dessous."
      : "Chaque autorité lit ses alertes dans sa boîte de réception, avec le lien secret de la colonne de droite, sans service externe." +
        (state.telegram ? " Telegram est en plus utilisé pour les autorités qui ont un identifiant Telegram." : "");
    $("lanToggle").checked = !!state.inbox.lan_open;
    $("lanToggle").disabled = !!state.inbox.lan_forced;   // imposé par NIIFYEEN_INBOX_HOST : se change au lancement
    var ips = (state.inbox.lan_ips || []).join(", ");
    $("lanNote").textContent = state.inbox.lan_open
      ? "Boîte de réception ouverte au réseau local (port " + state.inbox.port + "). Le lien secret est le seul contrôle d'accès, et la connexion n'est pas chiffrée : utilisez un réseau isolé ou de confiance." +
        (ips ? " Adresses de cet ordinateur : " + ips + ". Le lien utilise la première ; si le téléphone ne se connecte pas, essayez les autres." : "")
      : "Attention : la boîte de réception n'est joignable que depuis cet ordinateur, donc les liens en 127.0.0.1 ne marcheront pas sur un téléphone. Relancez le serveur avec lancer.bat (ou NIIFYEEN_INBOX_HOST=0.0.0.0)" +
        (state.inbox.lan_ip ? ". Adresse de cet ordinateur sur le réseau : " + state.inbox.lan_ip + "." : ".");
    var rows = $("rows"); rows.replaceChildren();
    $("emptyMsg").hidden = state.authorities.length > 0;
    state.authorities.forEach(function (a) {
      var tr = el("tr");
      var name = el("td"); name.append(el("b", a.name)); if (a.service) { name.append(el("br"), el("span", a.service, "muted")); }
      var cams = a.cameras.length ? a.cameras.map(camLabel).join(", ") : "—";
      var pos = (a.x === null || a.y === null) ? "—" : a.x.toFixed(2) + " ; " + a.y.toFixed(2);
      var chan = a.enabled ? (a.channel === "telegram" ? "Interne + Telegram" : "Interne") : "Désactivée";
      var link = el("td");
      if (a.inbox_url) {
        var field = el("input"); field.readOnly = true; field.value = a.inbox_url; field.setAttribute("aria-label", "Lien de la boîte de réception de " + a.name);
        field.addEventListener("focus", function () { field.select(); });
        var copy = el("button", "Copier", "btn small"); copy.type = "button";
        copy.addEventListener("click", function () {
          field.select();
          var done = function () { Shell.toast("Lien copié."); };
          if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(a.inbox_url).then(done, function () { document.execCommand("copy"); done(); });
          else { document.execCommand("copy"); done(); }
        });
        var renew = el("button", "Nouveau lien", "btn small"); renew.type = "button";
        renew.addEventListener("click", function () { newLink(a); });
        link.append(field, el("br"), copy, document.createTextNode(" "), renew);
      } else link.textContent = "—";
      tr.append(name, el("td", a.phone || "—"), el("td", cams), el("td", pos), el("td", chan), link);
      var act = el("td");
      [["Essai", function () { test(a); }], ["Modifier", function () { openForm(a); }], ["Supprimer", function () { remove(a); }]].forEach(function (p) {
        var b = el("button", p[0], p[0] === "Supprimer" ? "btn small danger" : "btn small"); b.type = "button";
        b.addEventListener("click", p[1]); act.append(b, document.createTextNode(" "));
      });
      tr.append(act); rows.append(tr);
    });
    loadLog();
  }

  function loadLog() {
    return api("GET", "/api/dispatches?limit=30").then(function (res) {
      var body = $("logRows"); body.replaceChildren();
      var list = res.ok ? res.data.dispatches : [];
      $("logEmpty").hidden = list.length > 0;
      list.forEach(function (d) {
        var tr = el("tr");
        var msg = el("td"); var det = el("details"); det.append(el("summary", "Voir"), el("pre", d.message, "msg-pre")); msg.append(det);
        tr.append(el("td", new Date(d.created_at).toLocaleString("fr-FR")), el("td", d.authority_name), el("td", d.target_label || "—"),
                  el("td", d.camera_id ? camLabel(d.camera_id) : "—"), el("td", d.channel), el("td", d.status), msg);
        body.append(tr);
      });
    });
  }

  function test(a) {
    api("POST", "/api/authorities/" + a.id + "/test").then(function (res) {
      Shell.toast(res.ok ? "Essai : " + res.data.status + " (" + res.data.channel + ") — visible dans la boîte de réception." : errorOf(res).message, res.ok ? "info" : "warning");
      loadLog();
    });
  }
  function newLink(a) {
    api("POST", "/api/authorities/" + a.id + "/inbox-token").then(function (res) {
      Shell.toast(res.ok ? "Nouveau lien créé : l'ancien ne fonctionne plus." : errorOf(res).message, res.ok ? "info" : "warning");
      if (res.ok) load();
    });
  }
  function remove(a) {
    api("DELETE", "/api/authorities/" + a.id).then(function (res) {
      Shell.toast(res.ok ? a.name + " supprimée." : errorOf(res).message, res.ok ? "info" : "warning");
      if (res.ok) load();
    });
  }

  // ---- formulaire ----------------------------------------------------------------------------------
  function clearErrors() {
    document.querySelectorAll("#form .error").forEach(function (n) { n.textContent = ""; });
    $("formError").hidden = true;
  }
  function openForm(a) {
    state.editing = a || null; clearErrors();
    $("modalTitle").textContent = a ? "Modifier l'autorité" : "Ajouter une autorité";
    $("fName").value = a ? a.name : ""; $("fService").value = a ? a.service : ""; $("fPhone").value = a ? a.phone : "";
    $("fChat").value = a ? a.telegram_chat_id : "";
    $("fX").value = a && a.x !== null ? a.x : ""; $("fY").value = a && a.y !== null ? a.y : "";
    $("fEnabled").value = !a || a.enabled ? "1" : "0";
    var box = $("camBoxes"); box.replaceChildren();
    state.cameras.forEach(function (c) {
      var lab = el("label", null, "check"); var cb = document.createElement("input"); cb.type = "checkbox"; cb.value = c.id;
      cb.checked = !!(a && a.cameras.indexOf(c.id) >= 0);
      lab.append(cb, document.createTextNode(" " + c.label)); box.append(lab);
    });
    $("modal").hidden = false; $("fName").focus();
  }
  function closeForm() { $("modal").hidden = true; }

  $("form").addEventListener("submit", function (ev) {
    ev.preventDefault(); clearErrors();
    var num = function (id) { var v = $(id).value.trim(); return v === "" ? null : Number(v); };
    var body = {
      name: $("fName").value, service: $("fService").value, phone: $("fPhone").value, telegram_chat_id: $("fChat").value,
      x: num("fX"), y: num("fY"), enabled: $("fEnabled").value === "1",
      cameras: Array.prototype.map.call($("camBoxes").querySelectorAll("input:checked"), function (i) { return i.value; })
    };
    var req = state.editing ? api("PUT", "/api/authorities/" + state.editing.id, body) : api("POST", "/api/authorities", body);
    req.then(function (res) {
      if (!res.ok) {
        var e = errorOf(res), holder = e.field && document.querySelector('#form [data-f="' + e.field + '"] .error');
        if (holder) holder.textContent = e.message; else { $("formError").textContent = e.message; $("formError").hidden = false; }
        return;
      }
      closeForm(); Shell.toast("Autorité enregistrée : " + res.data.name); load();
    });
  });
  $("btnAdd").addEventListener("click", function () { openForm(null); });
  $("btnClose").addEventListener("click", closeForm);
  $("btnCancel").addEventListener("click", closeForm);
  $("lanToggle").addEventListener("change", function () {
    var on = $("lanToggle").checked;
    api("PUT", "/api/inbox-lan", { enabled: on }).then(function (res) {
      if (!res.ok) { $("lanToggle").checked = !on; Shell.toast("Réglage non enregistré.", "warning"); return; }
      Shell.toast(on ? "Boîte de réception ouverte au réseau local. Recopiez les liens." : "Boîte de réception limitée à cet ordinateur.", "success");
      load();
    });
  });

  $("scope").addEventListener("change", function () {
    var on = $("scope").checked;
    api("PUT", "/api/authorities-scope", { enabled: on }).then(function (res) {
      if (!res.ok) { $("scope").checked = !on; Shell.toast("Réglage non enregistré.", "warning"); return; }
      Shell.toast(on ? "Envoi aux autorités activé." : "Envoi aux autorités désactivé.");
    });
  });

  Shell.mount({ active: "authorities" });
  load();
  setInterval(loadLog, 5000);
})();
