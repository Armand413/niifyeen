/*
 * Calculs purs de la page Investigation (testables sans navigateur) : requête de filtres, disposition de la
 * chronologie (une ligne par caméra), graduations du temps, libellés.
 */
(function (root) {
  "use strict";

  var KIND_LABELS = { event: "Événement", passage: "Passage", move: "Étape de cible" };
  var CHIP_TYPES = { targets: "Cibles", people: "Personnes", cameras: "Caméras", analytics: "Lignes et zones", passage: "Passages" };

  function kindLabel(k) { return KIND_LABELS[k] || String(k || ""); }

  /** Requête de l'API (valeurs vides omises). `f.types` est un tableau. */
  function buildQuery(f) {
    var parts = [];
    function add(k, v) { if (v !== undefined && v !== null && String(v).trim() !== "") parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v).trim())); }
    f = f || {};
    if (f.start || f.end) { add("start", f.start); add("end", f.end); } else { add("minutes", f.minutes); }
    add("camera_id", f.camera_id);
    add("types", (f.types || []).join(","));
    add("participant_id", f.participant_id);
    add("target_id", f.target_id);
    add("session", f.session);
    add("q", f.q);
    if (f.only_alerts) add("only_alerts", "true");
    return parts.length ? "?" + parts.join("&") : "";
  }

  /** Graduations : pas choisi pour obtenir 4 à 10 repères. */
  function ticks(sinceMs, untilMs, width) {
    var span = untilMs - sinceMs;
    if (!(span > 0) || !(width > 0)) return [];
    var steps = [60e3, 5 * 60e3, 10 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 86400e3, 2 * 86400e3, 7 * 86400e3];
    var step = steps[steps.length - 1];
    for (var i = 0; i < steps.length; i++) { if (span / steps[i] <= 10) { step = steps[i]; break; } }
    var out = [], first = Math.ceil(sinceMs / step) * step, pad = function (n) { return (n < 10 ? "0" : "") + n; };
    for (var t = first; t <= untilMs && out.length < 40; t += step) {
      var d = new Date(t);
      var label = pad(d.getHours()) + ":" + pad(d.getMinutes());
      if (step >= 12 * 3600e3) label = pad(d.getDate()) + "/" + pad(d.getMonth() + 1);
      else if (span > 36 * 3600e3) label = pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + " " + pad(d.getHours()) + "h";
      out.push({ x: Math.round((t - sinceMs) / span * width * 10) / 10, label: label });
    }
    return out;
  }

  /**
   * Disposition de la chronologie : une ligne par caméra (les éléments sans caméra vont dans « Autres »).
   * Retourne { lanes: [{id, y}], marks: [{key, x, w, y, lane, kind, type, severity}], height }.
   */
  function layout(items, sinceIso, untilIso, width, laneHeight) {
    var since = Date.parse(sinceIso), until = Date.parse(untilIso), span = until - since;
    var lh = laneHeight || 26, order = [], seen = {}, marks = [];
    if (!(span > 0)) return { lanes: [], marks: [], height: 0 };
    (items || []).forEach(function (it) {
      var lane = it.camera_id || "";
      if (!seen.hasOwnProperty(lane)) { seen[lane] = true; order.push(lane); }
    });
    order.sort(function (a, b) { return a === "" ? 1 : (b === "" ? -1 : (a < b ? -1 : (a > b ? 1 : 0))); });
    var index = {};
    order.forEach(function (id, i) { index[id] = i; });
    (items || []).forEach(function (it) {
      var t0 = Date.parse(it.ts);
      if (isNaN(t0)) return;
      var t1 = it.end_ts ? Date.parse(it.end_ts) : t0;
      var x = Math.max(0, Math.min(width, (t0 - since) / span * width));
      var x2 = Math.max(x, Math.min(width, ((isNaN(t1) ? t0 : t1) - since) / span * width));
      marks.push({ key: it.key, x: Math.round(x * 10) / 10, w: Math.max(4, Math.round((x2 - x) * 10) / 10),
                   y: index[it.camera_id || ""] * lh + lh / 2, lane: it.camera_id || "", kind: it.kind, type: it.type,
                   severity: it.severity || "info" });
    });
    return { lanes: order.map(function (id, i) { return { id: id, y: i * lh + lh / 2 }; }), marks: marks, height: order.length * lh };
  }

  /** Ajoute / retire un type de la sélection (sans doublon, ordre stable). */
  function toggleType(types, type, on) {
    var out = (types || []).filter(function (t) { return t !== type; });
    if (on) out.push(type);
    return out;
  }

  function duration(sec) {
    if (!(sec >= 0)) return "";
    if (sec < 1) return "< 1 s";
    if (sec < 60) return Math.round(sec) + " s";
    var m = Math.floor(sec / 60);
    return sec < 3600 ? m + " min " + Math.round(sec % 60) + " s" : Math.floor(sec / 3600) + " h " + (m % 60) + " min";
  }

  root.InvestigationLogic = { kindLabel: kindLabel, buildQuery: buildQuery, ticks: ticks, layout: layout,
                              toggleType: toggleType, duration: duration, CHIP_TYPES: CHIP_TYPES };
})(typeof window !== "undefined" ? window : globalThis);
