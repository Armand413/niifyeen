/*
 * Calculs purs de la page Statistiques (testables sans navigateur) : formats, couleurs de carte de chaleur,
 * géométrie des histogrammes et du graphe de transitions.
 */
(function (root) {
  "use strict";

  var PERIODS = [{ minutes: 60, label: "1 h" }, { minutes: 1440, label: "24 h" }, { minutes: 10080, label: "7 j" }, { minutes: 43200, label: "30 j" }];
  var WEEKDAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."];

  function formatDuration(sec) {
    if (typeof sec !== "number" || !(sec >= 0)) return "—";
    var total = Math.round(sec);
    if (total < 60) return total + " s";
    var m = Math.floor(total / 60), s = total % 60;
    return m + " min " + (s < 10 ? "0" : "") + s + " s";
  }

  /** « +50 % », « −12,5 % », « = » ; « n/a » quand la référence est nulle ou inconnue. */
  function formatDelta(d) {
    if (!d || d.pct === null || d.pct === undefined) return (d && d.diff > 0) ? "nouveau" : "n/a";
    if (d.pct === 0) return "=";
    var v = String(Math.abs(d.pct)).replace(".", ",");
    return (d.pct > 0 ? "+" : "−") + v + " %";
  }

  /** Intensité 0..1 (échelle racine : les petites valeurs restent visibles). */
  function intensity(v, max) {
    if (!(v > 0) || !(max > 0)) return 0;
    return Math.min(1, Math.sqrt(v / max));
  }

  /** Couleur rgba d'une cellule de chaleur : transparent -> jaune -> rouge. */
  function heatColor(v, max) {
    var k = intensity(v, max);
    if (k === 0) return "rgba(0,0,0,0)";
    var g = Math.round(220 - 170 * k), a = (0.25 + 0.65 * k).toFixed(2);
    return "rgba(255," + g + ",30," + a + ")";
  }

  /** Barres d'un histogramme (valeurs >= 0) dans un cadre w x h ; hauteur minimale 1 pour une valeur non nulle. */
  function bars(values, w, h) {
    if (!values || !values.length) return { max: 0, bars: [] };
    var max = Math.max.apply(null, values.map(function (v) { return v > 0 ? v : 0; }));
    var bw = w / values.length;
    return { max: max, bars: values.map(function (v, i) {
      var n = v > 0 ? v : 0, hh = max ? Math.max(n ? 1 : 0, Math.round(n / max * h)) : 0;
      return { i: i, v: n, x: Math.round(i * bw * 10) / 10, w: Math.max(1, Math.round((bw - 1) * 10) / 10), h: hh, y: h - hh };
    }) };
  }

  /** Disposition circulaire des caméras et courbes des transitions (largeur selon le nombre de passages). */
  function graph(nodes, edges, w, h) {
    var ids = [];
    (nodes || []).forEach(function (n) { if (ids.indexOf(n.camera_id) < 0) ids.push(n.camera_id); });
    (edges || []).forEach(function (e) { [e.from, e.to].forEach(function (c) { if (ids.indexOf(c) < 0) ids.push(c); }); });
    var cx = w / 2, cy = h / 2, r = Math.max(10, Math.min(w, h) / 2 - 36), pos = {};
    ids.forEach(function (id, i) {
      var a = ids.length === 1 ? 0 : (-Math.PI / 2 + i * 2 * Math.PI / ids.length);
      pos[id] = { id: id, x: Math.round((cx + (ids.length === 1 ? 0 : r * Math.cos(a))) * 10) / 10, y: Math.round((cy + (ids.length === 1 ? 0 : r * Math.sin(a))) * 10) / 10 };
    });
    var maxCount = 0;
    (edges || []).forEach(function (e) { if (e.count > maxCount) maxCount = e.count; });
    var out = (edges || []).map(function (e) {
      var a = pos[e.from], b = pos[e.to], dx = b.x - a.x, dy = b.y - a.y, len = Math.sqrt(dx * dx + dy * dy) || 1;
      // courbe vers la droite du sens de parcours : les deux sens d'une paire ne se superposent pas
      var qx = (a.x + b.x) / 2 + dy / len * 28, qy = (a.y + b.y) / 2 - dx / len * 28;
      return { from: e.from, to: e.to, count: e.count, width: Math.round((1 + 5 * (maxCount ? e.count / maxCount : 0)) * 10) / 10,
               d: "M" + a.x + " " + a.y + " Q" + Math.round(qx * 10) / 10 + " " + Math.round(qy * 10) / 10 + " " + b.x + " " + b.y,
               lx: Math.round(((a.x + 2 * qx + b.x) / 4) * 10) / 10, ly: Math.round(((a.y + 2 * qy + b.y) / 4) * 10) / 10 };
    });
    return { nodes: ids.map(function (id) { return pos[id]; }), edges: out };
  }

  /** Paramètres communs des requêtes : période, caméra, décalage du fuseau du navigateur. */
  function query(minutes, cameraId, tzOffsetMin) {
    var q = "minutes=" + encodeURIComponent(minutes);
    if (cameraId) q += "&camera_id=" + encodeURIComponent(cameraId);
    if (typeof tzOffsetMin === "number" && isFinite(tzOffsetMin)) q += "&tz_offset_min=" + Math.round(tzOffsetMin);
    return q;
  }

  /** Décalage à envoyer au serveur : minutes À AJOUTER à l'UTC pour obtenir l'heure locale (inverse de getTimezoneOffset). */
  function tzOffsetFromDate(d) { return -d.getTimezoneOffset(); }

  root.StatisticsLogic = { PERIODS: PERIODS, WEEKDAYS: WEEKDAYS, formatDuration: formatDuration, formatDelta: formatDelta, intensity: intensity,
                           heatColor: heatColor, bars: bars, graph: graph, query: query, tzOffsetFromDate: tzOffsetFromDate };
})(typeof window !== "undefined" ? window : globalThis);
