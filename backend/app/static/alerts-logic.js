/*
 * Calculs purs du centre d'alertes (testables sans navigateur) : libellés, requête de filtres,
 * actions possibles selon l'état, âge relatif, sélection.
 */
(function (root) {
  "use strict";

  var PRIORITY_LABELS = { low: "Faible", medium: "Moyenne", high: "Haute", critical: "Critique" };
  var STATUS_LABELS = { "new": "Nouvelle", seen: "Consultée", acknowledged: "Acquittée", closed: "Clôturée" };
  var RANK = { low: 0, medium: 1, high: 2, critical: 3 };

  function priorityLabel(p) { return PRIORITY_LABELS[p] || String(p || ""); }
  function statusLabel(s) { return STATUS_LABELS[s] || String(s || ""); }
  function priorityClass(p) { return RANK.hasOwnProperty(p) ? "p-" + p : "p-low"; }

  /** Requête de l'API à partir des filtres de la page (les valeurs vides sont omises). */
  function buildQuery(f) {
    var keys = ["status", "priority", "category", "camera_id", "q", "since", "until", "before_id", "limit"], parts = [];
    keys.forEach(function (k) {
      var v = f && f[k];
      if (v === undefined || v === null || String(v).trim() === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v).trim()));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  /** Changements d'état proposés à l'opérateur pour une alerte (cf. alerts.py : on n'avance que vers l'avant). */
  function actionsFor(status) {
    if (status === "new") return [["seen", "Marquer consultée"], ["acknowledged", "Acquitter"], ["closed", "Clôturer"]];
    if (status === "seen") return [["acknowledged", "Acquitter"], ["closed", "Clôturer"]];
    if (status === "acknowledged") return [["closed", "Clôturer"]];
    if (status === "closed") return [["seen", "Rouvrir"]];
    return [];
  }

  /** « à l'instant », « il y a 5 min », « il y a 3 h », « il y a 2 j ». */
  function ageText(iso, nowMs) {
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var s = Math.max(0, Math.round(((nowMs === undefined ? Date.now() : nowMs) - t) / 1000));
    if (s < 45) return "à l'instant";
    if (s < 3600) return "il y a " + Math.round(s / 60) + " min";
    if (s < 86400) return "il y a " + Math.round(s / 3600) + " h";
    return "il y a " + Math.round(s / 86400) + " j";
  }

  /** Sélection : ajoute/retire un identifiant (sans doublon). */
  function toggleId(ids, id, on) {
    var out = (ids || []).filter(function (x) { return x !== id; });
    if (on) out.push(id);
    return out;
  }

  root.AlertsLogic = { priorityLabel: priorityLabel, statusLabel: statusLabel, priorityClass: priorityClass,
                       buildQuery: buildQuery, actionsFor: actionsFor, ageText: ageText, toggleId: toggleId };
})(typeof window !== "undefined" ? window : globalThis);
