/*
 * Calculs purs de la page Zones (testables sans navigateur) : flèche de sens d'un passage, centre d'une zone,
 * barres de l'affluence, validation du tracé en cours.
 */
(function (root) {
  "use strict";

  /** Flèche indiquant le « sens 1 » d'une ligne a->b : au milieu, dirigée selon (-dy, dx) (cf. geometry.py). */
  function sideArrow(a, b, length) {
    var dx = b[0] - a[0], dy = b[1] - a[1], n = Math.sqrt(dx * dx + dy * dy);
    if (!(n > 0)) return null;
    var ux = -dy / n, uy = dx / n, mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2, l = length || 0.05;
    return { from: [mx, my], to: [mx + ux * l, my + uy * l], normal: [ux, uy] };
  }

  function centroid(points) {
    if (!points || !points.length) return null;
    var x = 0, y = 0;
    points.forEach(function (p) { x += p[0]; y += p[1]; });
    return [x / points.length, y / points.length];
  }

  /** Le tracé en cours peut-il être terminé ? (ligne : 2 points ; zone : 3 à 12) */
  function canFinish(kind, points) {
    var n = (points || []).length;
    return kind === "line" ? n === 2 : (n >= 3 && n <= 12);
  }

  /** Ajoute un point à un tracé en cours ; une ligne s'arrête à 2 points, une zone à 12. Ignore un point confondu avec le précédent. */
  function addPoint(kind, points, p) {
    var pts = (points || []).slice();
    var max = kind === "line" ? 2 : 12;
    if (pts.length >= max) return pts;
    var last = pts[pts.length - 1];
    if (last && Math.abs(last[0] - p[0]) + Math.abs(last[1] - p[1]) < 0.01) return pts;
    pts.push([Math.round(p[0] * 10000) / 10000, Math.round(p[1] * 10000) / 10000]);
    return pts;
  }

  /**
   * Barres d'un histogramme : une barre par tranche, hauteur proportionnelle à `key`.
   * Retourne { bars: [{x, y, w, h, value, bucket}], max }.
   */
  function bars(series, key, width, height) {
    var list = series || [];
    var max = list.reduce(function (m, s) { return Math.max(m, s[key] || 0); }, 0);
    if (!list.length) return { bars: [], max: 0 };
    var slot = width / list.length, w = Math.max(1, slot * 0.7);
    return {
      max: max,
      bars: list.map(function (s, i) {
        var v = s[key] || 0, h = max > 0 ? Math.round(v / max * height * 10) / 10 : 0;
        return { x: Math.round((i * slot + (slot - w) / 2) * 10) / 10, y: Math.round((height - h) * 10) / 10,
                 w: Math.round(w * 10) / 10, h: h, value: v, bucket: s.bucket };
      })
    };
  }

  /** Libellé court d'une tranche ISO (HH:MM en heure locale). */
  function bucketLabel(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  root.ZonesLogic = { sideArrow: sideArrow, centroid: centroid, canFinish: canFinish, addPoint: addPoint,
                      bars: bars, bucketLabel: bucketLabel };
})(typeof window !== "undefined" ? window : globalThis);
