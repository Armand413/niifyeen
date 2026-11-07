/*
 * Panneau de gestion des cibles : choisir une personne enrôlée, régler la capacité, sauvegarder / reprendre
 * une session. Aucun texte reçu du serveur n'est interprété comme du HTML (textContent uniquement).
 */
(function (root) {
  "use strict";

  function el(tag, text, className) {
    var n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (className) n.className = className;
    return n;
  }
  async function call(method, url, body) {
    var opts = { method: method };
    if (body !== undefined) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
    var res = await fetch(url, opts), data = null;
    try { data = await res.json(); } catch (e) { /* corps vide */ }
    if (!res.ok) throw new Error(data && typeof data.detail === "string" ? data.detail : "Erreur " + res.status);
    return data;
  }

  /** ids : { pick, add, max, apply, save, resume, forget, status, session } ; onChange() recharge la liste des cibles. */
  function init(ids, onChange) {
    var $ = function (k) { return document.getElementById(ids[k]); };
    var statusBox = $("status");
    function say(text, isError) { statusBox.textContent = text; statusBox.className = "hint" + (isError ? " error" : ""); }
    function changed() { if (onChange) return onChange(); }

    async function loadPeople() {
      var select = $("pick");
      select.replaceChildren(el("option", "Choisir une personne…"));
      select.firstChild.value = "";
      try {
        var data = await call("GET", "/api/people?statut=&page_size=200");
        (data.items || []).forEach(function (p) {
          var o = el("option", p.display_name + (p.matricule ? " (" + p.matricule + ")" : "") + (p.actif ? "" : " — inactive"));
          o.value = String(p.id);
          select.appendChild(o);
        });
        if (!(data.items || []).length) select.firstChild.textContent = "Aucune personne enrôlée";
      } catch (e) {
        select.firstChild.textContent = "Liste indisponible";
      }
    }

    async function loadSettings() {
      try {
        var s = await call("GET", "/api/targets/settings");
        $("max").value = String(s.max);
        $("max").max = String(s.hard_limit);
      } catch (e) { /* le champ garde sa valeur */ }
    }

    async function loadSession() {
      var box = $("session");
      try {
        var s = await call("GET", "/api/targets/session");
        $("resume").disabled = !s.saved;
        $("forget").disabled = !s.saved;
        if (!s.saved) { box.textContent = "Aucune session sauvegardée."; return; }
        var names = (s.people || []).map(function (p) { return p.nom || "(supprimée)"; });
        var when = s.saved_at ? new Date(s.saved_at).toLocaleString() : "";
        box.textContent = "Session du " + when + " : " + s.count + " personne(s)" +
          (names.length ? " — " + names.join(", ") : "") +
          (s.unenrolled_skipped ? ". " + s.unenrolled_skipped + " cible(s) non enrôlée(s) n'avaient pas été conservées." : ".");
      } catch (e) { box.textContent = "Session indisponible."; }
    }

    $("add").addEventListener("click", async function () {
      var id = Number($("pick").value);
      if (!id) { say("Choisissez d'abord une personne.", true); return; }
      try {
        var r = await call("POST", "/api/targets/from-person", { participant_id: id });
        say("Cible ajoutée — " + r.targets_count + " cible(s) suivie(s).");
        await changed();
      } catch (e) { say(e.message, true); }
    });

    $("apply").addEventListener("click", async function () {
      var v = Number($("max").value);
      try {
        var r = await call("PUT", "/api/targets/settings", { max: v });
        say("Capacité réglée à " + r.max + " cible(s).");
        await changed();
      } catch (e) { say(e.message, true); loadSettings(); }
    });

    $("save").addEventListener("click", async function () {
      try { await call("POST", "/api/targets/session/save"); say("Session sauvegardée."); }
      catch (e) { say(e.message, true); }
      loadSession();
    });

    $("resume").addEventListener("click", async function () {
      try {
        var r = await call("POST", "/api/targets/session/resume");
        var parts = [r.added + " ajoutée(s)"];
        if (r.already_tracked) parts.push(r.already_tracked + " déjà suivie(s)");
        if (r.missing) parts.push(r.missing + " personne(s) supprimée(s) de la base");
        if (r.not_added_capacity) parts.push(r.not_added_capacity + " non ajoutée(s) : capacité atteinte");
        say("Session reprise : " + parts.join(", ") + ".", !!(r.missing || r.not_added_capacity));
        await changed();
      } catch (e) { say(e.message, true); }
    });

    $("forget").addEventListener("click", async function () {
      if (!confirm("Oublier la session sauvegardée ?")) return;
      try { await call("DELETE", "/api/targets/session"); say("Session oubliée."); }
      catch (e) { say(e.message, true); }
      loadSession();
    });

    loadPeople(); loadSettings(); loadSession();
    return { reload: function () { loadPeople(); loadSettings(); loadSession(); } };
  }

  root.TargetsPanel = { init: init };
})(typeof window !== "undefined" ? window : globalThis);
