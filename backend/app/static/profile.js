/*
 * Fiche de la cible : affiche ce que la base contient sur la personne qui correspond à la cible.
 * Le texte (noms, informations libres, noms de fichiers) est inséré avec textContent : aucune
 * donnée n'est interprétée comme du HTML.
 */
(function (root) {
  "use strict";

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function when(iso) {
    var d = new Date(iso);
    return isNaN(d.getTime()) ? "—" : d.toLocaleString();
  }
  function sim(x) { return typeof x === "number" ? x.toFixed(2) : "—"; }

  function row(dl, label, value) {
    dl.appendChild(el("dt", label));
    var dd = el("dd");
    if (value instanceof Node) dd.appendChild(value); else dd.textContent = value;
    dl.appendChild(dd);
  }

  function message(container, title, detail, kind) {
    var box = el("div", null, "profile-card profile-" + kind);
    box.appendChild(el("div", title, "profile-title"));
    if (detail) box.appendChild(el("p", detail, "profile-note"));
    container.replaceChildren(box);
  }

  /** data = réponse de /search/by-image ou de /target/profile : { target_set, profile }. */
  function render(container, data) {
    if (!data || !data.target_set || !data.profile) {
      message(container, "Aucune cible définie", "Envoyez une ou plusieurs photos pour définir la cible.", "idle");
      return;
    }
    var p = data.profile;

    if (!p.matched) {
      if (p.reason === "deleted") {
        message(container, "La personne correspondante a été supprimée de la base",
                "La cible reste suivie sur les caméras, mais sa fiche n'existe plus.", "none");
      } else {
        message(container, "Cible non enregistrée dans la base",
                p.enrolled_count
                  ? "Aucune des " + p.enrolled_count + " personne(s) enrôlée(s) ne ressemble assez à la cible (similarité la plus proche : " +
                    sim(p.best_score) + ", seuil : " + sim(p.threshold) + "). La cible est quand même suivie sur les caméras."
                  : "Aucune personne n'est enrôlée pour l'instant. La cible est quand même suivie sur les caméras.",
                "none");
      }
      return;
    }

    if (p.hidden) {
      message(container, "Personne enrôlée reconnue",
              "Similarité " + sim(p.score) + ". Les informations sont masquées par la configuration du serveur (NIIFYEEN_HISTORY_IDENTITIES=0).",
              "match");
      return;
    }

    var box = el("div", null, "profile-card profile-match");
    var head = el("div", null, "profile-head");
    head.appendChild(el("div", p.participant.nom, "profile-name"));
    head.appendChild(el("span", "Dans la base", "profile-badge"));
    box.appendChild(head);
    box.appendChild(el("div", "Similarité " + sim(p.score) + " (seuil " + sim(p.threshold) + ")", "profile-score"));

    var dl = el("dl", null, "profile-dl");
    row(dl, "Identifiant", "#" + p.participant.id);
    row(dl, "Nom", p.participant.nom);
    row(dl, "Informations", p.participant.infos || "—");
    row(dl, "Consentement enregistré le", p.participant.date_consentement ? when(p.participant.date_consentement) : "—");
    row(dl, "Empreintes faciales", p.enrolment.embeddings + (p.enrolment.sources.length ? " (photos : " + p.enrolment.sources.join(", ") + ")" : ""));

    var a = p.activity;
    if (a.passages) {
      row(dl, "Détections enregistrées", a.passages + " passage(s), " + a.observations + " observation(s)");
      row(dl, "Première détection", when(a.first_seen));
      row(dl, "Dernière détection", when(a.last_seen));
      row(dl, "Dernière caméra", a.last_camera_label || a.last_camera);
    } else {
      row(dl, "Détections enregistrées", "Aucune sous ce nom (sur la période conservée).");
    }
    box.appendChild(dl);

    if (a.passages) {
      var table = el("table", null, "profile-table");
      var thead = table.createTHead().insertRow();
      ["Caméra", "Passages", "Observations", "Dernière détection"].forEach(function (h) {
        var th = document.createElement("th"); th.scope = "col"; th.textContent = h; thead.appendChild(th);
      });
      var body = table.createTBody();
      a.by_camera.forEach(function (c) {
        var tr = body.insertRow();
        [c.camera_label || c.camera_id, c.passages, c.observations, when(c.last_seen)].forEach(function (v) {
          tr.insertCell().textContent = String(v);
        });
      });
      box.appendChild(table);
    }

    if (p.other_candidates && p.other_candidates.length) {
      box.appendChild(el("p", "Attention : d'autres personnes enrôlées sont aussi proches — " +
        p.other_candidates.map(function (o) { return o.nom + " (" + sim(o.score) + ")"; }).join(", ") +
        ". Vérifier manuellement avant de conclure.", "profile-warn"));
    }
    container.replaceChildren(box);
  }

  root.ProfileView = { render: render };
})(typeof window !== "undefined" ? window : globalThis);
