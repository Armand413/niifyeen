/*
 * Calculs purs de la page Carte (testables sans navigateur) : géométrie du plan, champ de vision,
 * analyse des déplacements, position pendant le rejeu.
 */
(function (root) {
  "use strict";

  var BASE_W = 900, MIN_H = 300, MAX_H = 1800;
  var IMAGE_URL = /^\/api\/floors\/[0-9]+\/image$/;
  var DEFAULT_PLAN = "/static/site-plan.svg";

  /** Taille du repère (largeur fixe à 900) pour un plan de proportions w:h. */
  function planSize(width, height) {
    if (!(width > 0) || !(height > 0)) return { width: BASE_W, height: 510 };
    var h = Math.round(BASE_W * height / width);
    return { width: BASE_W, height: Math.min(MAX_H, Math.max(MIN_H, h)) };
  }

  /** Adresse d'image acceptée : seulement nos propres routes (jamais une URL fournie par un tiers). */
  function safeImage(url) { return IMAGE_URL.test(url || "") ? url : DEFAULT_PLAN; }

  /** Caméras visibles sur un étage. Sans étage défini, le plan illustratif montre toutes les caméras. */
  function camerasOn(meta, floorId) {
    var out = {};
    Object.keys(meta || {}).forEach(function (id) {
      var m = meta[id];
      if (floorId === null || floorId === undefined) out[id] = m;
      else if (m.floor_id === floorId && m.placed) out[id] = m;
    });
    return out;
  }

  /** Caméras actives sans position sur un étage (à placer). */
  function unplaced(meta, hasFloors) {
    if (!hasFloors) return [];
    return Object.keys(meta || {}).filter(function (id) { return !meta[id].floor_id || !meta[id].placed; });
  }

  function point(cx, cy, r, deg) {
    var a = deg * Math.PI / 180;               // 0° = haut du plan, sens horaire
    return { x: cx + r * Math.sin(a), y: cy - r * Math.cos(a) };
  }
  function fx(n) { return (Math.round(n * 10) / 10).toString(); }

  /** Secteur du champ de vision : centre (cx, cy), portée r, direction `heading`, ouverture `fov` (degrés). */
  function wedgePath(cx, cy, r, heading, fov) {
    if (!(r > 0) || !(fov > 0)) return "";
    if (fov >= 359.5) {
      return "M" + fx(cx - r) + " " + fx(cy) + "a" + fx(r) + " " + fx(r) + " 0 1 0 " + fx(2 * r) + " 0a" +
             fx(r) + " " + fx(r) + " 0 1 0 " + fx(-2 * r) + " 0Z";
    }
    var a = point(cx, cy, r, heading - fov / 2), b = point(cx, cy, r, heading + fov / 2);
    return "M" + fx(cx) + " " + fx(cy) + "L" + fx(a.x) + " " + fx(a.y) + "A" + fx(r) + " " + fx(r) + " 0 " +
           (fov > 180 ? 1 : 0) + " 1 " + fx(b.x) + " " + fx(b.y) + "Z";
  }

  function clamp01(v) { return Math.min(1, Math.max(0, v)); }

  /** Position de la souris -> fractions du plan (0-1), bornées. `rect` = getBoundingClientRect() du SVG. */
  function toFraction(rect, clientX, clientY) {
    if (!rect || !(rect.width > 0) || !(rect.height > 0)) return null;
    return { x: clamp01((clientX - rect.left) / rect.width), y: clamp01((clientY - rect.top) / rect.height) };
  }

  /** Direction (0-360°, 0 = haut) du point p vu depuis c. */
  function headingFrom(cx, cy, px, py) {
    var d = Math.atan2(px - cx, -(py - cy)) * 180 / Math.PI;
    return Math.round(((d % 360) + 360) % 360);
  }

  /**
   * Analyse de trajets : passages et durée cumulée par caméra, transitions entre caméras.
   * La durée d'une étape = délai jusqu'à l'étape suivante ; la dernière étape n'a pas de durée connue.
   */
  function analyse(trajectories) {
    var cams = {}, trans = {}, count = 0;
    (trajectories || []).forEach(function (t) {
      var moves = (t.moves || []).filter(function (m) { return m && m.camera_id && typeof m.ts === "number"; })
                                 .sort(function (a, b) { return a.ts - b.ts; });
      if (!moves.length) return;
      count++;
      moves.forEach(function (m, i) {
        var c = cams[m.camera_id] = cams[m.camera_id] || { camera_id: m.camera_id, visits: 0, dwell_sec: 0 };
        c.visits++;
        var next = moves[i + 1];
        if (next) {
          c.dwell_sec += Math.max(0, next.ts - m.ts);
          if (next.camera_id !== m.camera_id) {
            var key = m.camera_id + "\u0000" + next.camera_id;
            trans[key] = trans[key] || { from: m.camera_id, to: next.camera_id, count: 0 };
            trans[key].count++;
          }
        }
      });
    });
    var byVisits = Object.keys(cams).map(function (k) { return cams[k]; })
      .sort(function (a, b) { return b.visits - a.visits || a.camera_id.localeCompare(b.camera_id); });
    var byCount = Object.keys(trans).map(function (k) { return trans[k]; })
      .sort(function (a, b) { return b.count - a.count || a.from.localeCompare(b.from) || a.to.localeCompare(b.to); });
    return { trajectories: count, cameras: byVisits, transitions: byCount };
  }

  /** Bornes de temps d'un trajet [début, fin] (secondes), ou null s'il est vide. */
  function span(moves) {
    var ts = (moves || []).map(function (m) { return m.ts; }).filter(function (v) { return typeof v === "number"; });
    return ts.length ? [Math.min.apply(null, ts), Math.max.apply(null, ts)] : null;
  }

  /** Étapes déjà atteintes à l'instant t, dans l'ordre. */
  function movesUntil(moves, t) {
    return (moves || []).filter(function (m) { return m.ts <= t; }).sort(function (a, b) { return a.ts - b.ts; });
  }

  root.MapLogic = { planSize: planSize, safeImage: safeImage, camerasOn: camerasOn, unplaced: unplaced,
                    wedgePath: wedgePath, toFraction: toFraction, headingFrom: headingFrom, analyse: analyse,
                    span: span, movesUntil: movesUntil, DEFAULT_PLAN: DEFAULT_PLAN };
})(typeof window !== "undefined" ? window : globalThis);
