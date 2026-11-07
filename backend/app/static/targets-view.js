/*
 * Liste des cibles : photo, couleur, état en direct, fiche (via ProfileView) et bouton « Retirer ».
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 */
(function (root) {
  "use strict";

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function safeColor(c) { return /^#[0-9a-f]{6}$/i.test(c || "") ? c : "#d92d20"; }

  function matchText(profile, source) {
    if (!profile) return "";
    if (profile.matched && profile.hidden) return "Personne enrôlée reconnue";
    if (profile.matched) return (source === "person" ? "Choisie dans la base : " : "Dans la base : ") + profile.participant.nom;
    if (profile.reason === "deleted") return "Personne supprimée de la base";
    return "Non enregistrée dans la base";
  }

  function card(t, opts) {
    var wrap = el("div", null, "target-card");
    wrap.style.borderLeftColor = safeColor(t.color);
    wrap.setAttribute("data-target-id", String(t.id));

    var head = el("div", null, "target-head");
    if (t.photo_url) {
      var img = el("img", null, "target-photo");
      img.src = t.photo_url; img.alt = "Photo de " + t.label; img.width = 72; img.height = 72;
      head.appendChild(img);
    }
    var main = el("div", null, "target-main");
    var name = el("div", null, "target-name");
    var dot = el("span", null, "target-dot");
    dot.style.background = safeColor(t.color);
    name.append(dot, el("span", t.label));
    main.appendChild(name);
    main.appendChild(el("div", matchText(t.profile, t.source), "target-match"));
    main.appendChild(el("div", opts.live === false ? "" : "En attente de détection…", "target-live"));
    head.appendChild(main);

    if (opts.onRemove) {
      var btn = el("button", "Retirer", "btn-danger target-remove");
      btn.type = "button";
      btn.setAttribute("aria-label", "Retirer " + t.label);
      btn.addEventListener("click", function () { opts.onRemove(t); });
      head.appendChild(btn);
    }
    wrap.appendChild(head);

    var p = t.profile;
    if (p && p.matched && !p.hidden && p.participant && Number.isInteger(p.participant.id)) {
      var links = el("div", null, "target-links");
      var fiche = el("a", "Fiche de la personne", "btn small");
      fiche.href = "/people/" + p.participant.id;
      var hist = el("a", "Historique de ses passages", "btn small");
      hist.href = "/history?participant_id=" + p.participant.id;
      links.append(fiche, hist);
      wrap.appendChild(links);
    }

    if (root.ProfileView && t.profile) {
      var details = el("details");
      if (opts.openProfiles) details.open = true;
      details.appendChild(el("summary", "Fiche"));
      var box = el("div");
      root.ProfileView.render(box, { target_set: true, profile: t.profile });
      details.appendChild(box);
      wrap.appendChild(details);
    }
    return wrap;
  }

  /** data = réponse de GET /targets. opts : { onRemove(target), openProfiles, live }. */
  function render(container, data, opts) {
    opts = opts || {};
    container.replaceChildren();
    if (!data || !data.targets || !data.targets.length) {
      container.appendChild(el("div", "Aucune cible définie. Envoyez une ou plusieurs photos pour en ajouter.",
                               "profile-card profile-idle"));
      return;
    }
    var list = el("div", null, "targets-list");
    data.targets.forEach(function (t) { list.appendChild(card(t, opts)); });
    container.appendChild(list);
  }

  function liveText(t, meta) {
    var cam = function (id) { return (meta && meta[id] && meta[id].label) || id; };
    if (t.active && t.camera_id) {
      return { kind: "active", text: "Vue sur « " + cam(t.camera_id) + " »" + (typeof t.score === "number" ? " (" + t.score.toFixed(2) + ")" : "") };
    }
    var last = (t.path || []).length ? t.path[t.path.length - 1].camera_id : null;
    if (last) return { kind: "lost", text: "Perdue de vue — dernière caméra : « " + cam(last) + " »" };
    return { kind: "idle", text: "Pas encore repérée" };
  }

  /** Met à jour l'état en direct sans reconstruire la liste (garde les fiches ouvertes). */
  function updateLive(container, liveTargets, meta) {
    (liveTargets || []).forEach(function (t) {
      var span = container.querySelector('[data-target-id="' + String(t.id).replace(/[^0-9]/g, "") + '"] .target-live');
      if (!span) return;
      var s = liveText(t, meta);
      span.textContent = s.text;
      span.className = "target-live is-" + s.kind;
    });
  }

  root.TargetsView = { render: render, updateLive: updateLive, liveText: liveText };
})(typeof window !== "undefined" ? window : globalThis);
