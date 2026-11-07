/*
 * Plan du site : caméras, caméras où les cibles sont identifiées, et trajectoires.
 *
 * Dessine par-dessus site-plan.svg (même repère 900 x 510). Les positions viennent de
 * `camera_meta` (x et y sont des fractions de la largeur / hauteur du plan).
 * Plusieurs cibles peuvent être affichées en même temps, chacune dans sa couleur.
 * Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML.
 */
(function (root) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var W = 900, H = 510;          // taille du plan courant ; ``render`` accepte ``opts.width`` / ``opts.height``
  var TEAL = "#186b82", AMBER = "#b54708";
  var MAX_MOVES = 20;          // nombre maximal de déplacements dessinés par cible
  var CAM_R = 19;              // rayon du pictogramme de caméra
  var FONT = "Segoe UI,Arial,sans-serif";

  function el(doc, name, attrs, text) {
    var node = doc.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function position(meta, camId) {
    var m = meta && meta[camId];
    if (!m || typeof m.x !== "number" || typeof m.y !== "number") return null;
    return { x: m.x * W, y: m.y * H };
  }

  function labelOf(meta, camId) {
    return (meta && meta[camId] && meta[camId].label) || camId;
  }

  /** Liste des passages successifs, sans doublon consécutif : [{camera_id, ts, score}]. */
  function visits(path) {
    var out = [];
    (path || []).forEach(function (p) {
      if (!p || !p.camera_id) return;
      if (out.length && out[out.length - 1].camera_id === p.camera_id) return;
      out.push(p);
    });
    return out;
  }

  /**
   * Segments à dessiner entre caméras. Chaque segment est une courbe légère qui s'arrête avant
   * les pictogrammes (pour que la flèche reste visible) ; A->B et B->A se courbent de part et
   * d'autre. `bendExtra` écarte davantage la courbe : les trajets de deux cibles qui empruntent
   * le même couloir ne se superposent pas.
   */
  function buildSegments(path, meta, maxMoves, bendExtra) {
    var seq = visits(path).filter(function (v) { return position(meta, v.camera_id); });
    var limit = (maxMoves || MAX_MOVES) + 1;
    var firstNumber = Math.max(0, seq.length - limit) + 1;   // numéro de la 1re étape affichée
    var shown = seq.slice(-limit);
    var segments = [];
    for (var i = 1; i < shown.length; i++) {
      var a = position(meta, shown[i - 1].camera_id), b = position(meta, shown[i].camera_id);
      var dx = b.x - a.x, dy = b.y - a.y, len = Math.sqrt(dx * dx + dy * dy);
      if (len < 2 * CAM_R + 10) continue;
      var ux = dx / len, uy = dy / len, nx = -uy, ny = ux;
      var bend = Math.min(40, len * 0.16) + (bendExtra || 0);
      var sx = a.x + ux * (CAM_R + 6), sy = a.y + uy * (CAM_R + 6);
      var ex = b.x - ux * (CAM_R + 9), ey = b.y - uy * (CAM_R + 9);
      var cx = (a.x + b.x) / 2 + nx * bend, cy = (a.y + b.y) / 2 + ny * bend;
      segments.push({
        d: "M" + sx.toFixed(1) + " " + sy.toFixed(1) + " Q" + cx.toFixed(1) + " " + cy.toFixed(1) +
           " " + ex.toFixed(1) + " " + ey.toFixed(1),
        from: shown[i - 1].camera_id, to: shown[i].camera_id, ts: shown[i].ts,
        step: firstNumber + i,
        age: (i - 1) / Math.max(1, shown.length - 2)       // 0 = le plus ancien, 1 = le plus récent
      });
    }
    var numbers = {};     // camera_id -> numéros d'étapes
    shown.forEach(function (v, idx) {
      (numbers[v.camera_id] = numbers[v.camera_id] || []).push(firstNumber + idx);
    });
    return { segments: segments, numbers: numbers, last: shown.length ? shown[shown.length - 1].camera_id : null };
  }

  function darken(hex) {
    var m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
    if (!m) return "#7a1d12";
    return "#" + [1, 2, 3].map(function (i) {
      var v = Math.round(parseInt(m[i], 16) * 0.72).toString(16);
      return v.length < 2 ? "0" + v : v;
    }).join("");
  }
  function safeColor(c) { return /^#[0-9a-f]{6}$/i.test(c || "") ? c : "#d92d20"; }

  function drawCamera(doc, parent, pos, fill, rings, camId) {
    var g = el(doc, "g", { transform: "translate(" + pos.x.toFixed(1) + " " + pos.y.toFixed(1) + ")" });
    if (camId) { g.setAttribute("data-camera", camId); g.setAttribute("class", "plan-camera"); }
    rings.forEach(function (ring, j) {
      if (ring.kind === "active") {
        g.appendChild(el(doc, "circle", { r: CAM_R + 4 + 5 * j, "class": "plan-pulse", style: "stroke:" + ring.color }));
      } else {
        g.appendChild(el(doc, "circle", { r: CAM_R + 6 + 5 * j, fill: "none", stroke: ring.color,
                                          "stroke-width": 3, "stroke-dasharray": "5 4" }));
      }
    });
    g.appendChild(el(doc, "circle", { r: CAM_R, fill: fill, stroke: "#fff", "stroke-width": 3 }));
    g.appendChild(el(doc, "rect", { x: -9, y: -6, width: 12, height: 11, rx: 2, fill: "#fff" }));
    g.appendChild(el(doc, "path", { d: "M4-3L10-7V7L4 3Z", fill: "#fff" }));
    parent.appendChild(g);
  }

  function drawText(doc, parent, x, y, text, opts) {
    var common = { x: x.toFixed(1), y: y.toFixed(1), "text-anchor": opts.anchor || "middle",
                   "font-size": opts.size || 14, "font-weight": 700, "font-family": FONT };
    // Halo blanc dessous, texte dessus : lisible sur n'importe quel fond, dans tous les navigateurs.
    parent.appendChild(el(doc, "text", Object.assign({ fill: "#fff", stroke: "#fff", "stroke-width": 5,
                                                       "stroke-linejoin": "round" }, common), text));
    parent.appendChild(el(doc, "text", Object.assign({ fill: opts.fill }, common), text));
  }

  /** Pastille colorée avec les numéros d'étapes, centrée au-dessus de la caméra (`level` = rang d'empilement). */
  function drawStepBadge(doc, parent, pos, text, color, level, title) {
    var w = 14 + 7.5 * text.length, h = 20;
    var x0 = Math.min(Math.max(pos.x - w / 2, 4), W - w - 4), y0 = pos.y - CAM_R - h - 3 - level * (h + 3);
    var rect = el(doc, "rect", { x: x0.toFixed(1), y: y0.toFixed(1), width: w.toFixed(1), height: h,
                                 rx: 10, fill: darken(color), stroke: "#fff", "stroke-width": 2 });
    if (title) rect.appendChild(el(doc, "title", {}, title));
    parent.appendChild(rect);
    parent.appendChild(el(doc, "text", { x: (x0 + w / 2).toFixed(1), y: (y0 + 14.5).toFixed(1),
                                         "text-anchor": "middle", "font-size": 13, "font-weight": 700,
                                         fill: "#fff", "font-family": FONT }, text));
  }

  function formatTime(ts) {
    var d = new Date((ts || 0) * 1000);
    return isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
  }

  function lastCamera(target) {
    var seq = visits(target && target.path);
    return seq.length ? seq[seq.length - 1].camera_id : null;
  }

  /** Résumé en une ligne de l'état de toutes les cibles. */
  function statusText(meta, targets) {
    targets = (targets || []).filter(Boolean);
    if (!targets.length) return { kind: "idle", text: "Aucune cible définie." };
    var anyActive = false, anySeen = false;
    var parts = targets.map(function (t) {
      var name = t.label || "Cible";
      var seq = visits(t.path);
      if (t.active && t.camera_id) {
        anyActive = true; anySeen = true;
        var score = typeof t.score === "number" ? " (score " + t.score.toFixed(2) + ")" : "";
        return name + " identifiée sur « " + labelOf(meta, t.camera_id) + " »" + score +
               (seq.length > 1 ? " · " + (seq.length - 1) + " déplacement(s)" : "");
      }
      if (seq.length) {
        anySeen = true;
        return name + " perdue de vue — dernière caméra : « " + labelOf(meta, seq[seq.length - 1].camera_id) + " »";
      }
      return name + " : pas encore repérée";
    });
    var kind = anyActive ? "active" : (anySeen ? "lost" : "idle");
    var prefix = targets.length > 1 ? targets.length + " cibles — " : "";
    return { kind: kind, text: prefix + parts.join(" · ") };
  }

  var lastSignature = null;

  /**
   * Redessine le calque. Ne fait rien si l'état n'a pas changé : l'animation des caméras actives
   * n'est donc pas relancée à chaque message du WebSocket (10 par seconde).
   * `targets` : [{id, label, color, active, camera_id, score, path:[{camera_id, ts, score}]}]
   */
  function render(svg, statusEl, meta, targets, opts) {
    opts = opts || {};
    W = opts.width || 900; H = opts.height || 510;
    var doc = svg.ownerDocument;
    targets = (targets || []).filter(Boolean);
    var signature = JSON.stringify([opts.signature || null, W, H, meta, targets.map(function (t) {
      return [t.id, t.label, t.color, !!t.active, t.active ? t.camera_id : null,
              t.active && typeof t.score === "number" ? t.score.toFixed(2) : null,
              visits(t.path).map(function (v) { return [v.camera_id, v.ts]; })];
    })]);
    if (signature === lastSignature && !opts.force) return false;
    lastSignature = signature;

    var st = statusText(meta, targets);
    if (statusEl) {
      statusEl.textContent = st.text;
      statusEl.className = "plan-status is-" + st.kind;
    }

    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (opts.before) opts.before(svg, doc, { width: W, height: H });     // fond, champs de vision…

    // Une pointe de flèche par couleur.
    var defs = el(doc, "defs");
    targets.forEach(function (t, i) {
      var marker = el(doc, "marker", { id: "planArrow" + i, viewBox: "0 0 10 10", refX: 7, refY: 5,
                                       markerWidth: 4.5, markerHeight: 4.5, orient: "auto" });
      marker.appendChild(el(doc, "path", { d: "M0 0L10 5L0 10z", fill: safeColor(t.color) }));
      defs.appendChild(marker);
    });
    svg.appendChild(defs);

    var built = targets.map(function (t, i) { return buildSegments(t.path, meta, MAX_MOVES, i * 11); });

    // 1. trajectoires (sous les caméras)
    var pathLayer = el(doc, "g", { fill: "none" });
    targets.forEach(function (t, i) {
      var color = safeColor(t.color);
      built[i].segments.forEach(function (seg) {
        var line = el(doc, "path", {
          d: seg.d, stroke: color, "stroke-width": 4, "stroke-linecap": "round",
          "marker-end": "url(#planArrow" + i + ")", opacity: (0.35 + 0.65 * seg.age).toFixed(2)
        });
        line.appendChild(el(doc, "title", {}, (t.label || "Cible") + " — étape " + seg.step + " : " +
          labelOf(meta, seg.from) + " → " + labelOf(meta, seg.to) + (seg.ts ? " à " + formatTime(seg.ts) : "")));
        pathLayer.appendChild(line);
      });
    });
    svg.appendChild(pathLayer);

    // 2. caméras, libellés, numéros d'étapes
    Object.keys(meta || {}).forEach(function (camId) {
      var pos = position(meta, camId);
      if (!pos) return;

      var here = [], gone = [];         // cibles identifiées ici / dont c'est la dernière caméra
      targets.forEach(function (t, i) {
        if (t.active && t.camera_id === camId) here.push({ t: t, color: safeColor(t.color), kind: "active" });
        else if (!t.active && built[i].last === camId) gone.push({ t: t, color: safeColor(t.color), kind: "lastseen" });
      });

      drawCamera(doc, svg, pos, here.length ? here[0].color : TEAL, here.concat(gone), camId);

      var anchor = pos.x < 90 ? "start" : (pos.x > W - 90 ? "end" : "middle");
      var lx = anchor === "start" ? pos.x - CAM_R : (anchor === "end" ? pos.x + CAM_R : pos.x);
      drawText(doc, svg, lx, pos.y + CAM_R + 20, labelOf(meta, camId),
               { anchor: anchor, size: 15, fill: here.length ? darken(here[0].color) : TEAL });

      var line = 0;
      here.forEach(function (h) {
        drawText(doc, svg, lx, pos.y + CAM_R + 38 + 16 * line++, (h.t.label || "Cible") + " ici",
                 { anchor: anchor, size: 13, fill: darken(h.color) });
      });
      gone.forEach(function (h) {
        drawText(doc, svg, lx, pos.y + CAM_R + 38 + 16 * line++, (h.t.label || "Cible") + " : dernière vue",
                 { anchor: anchor, size: 12, fill: AMBER });
      });

      var level = 0;
      targets.forEach(function (t, i) {
        var nums = built[i].numbers[camId];
        if (nums && nums.length) {
          drawStepBadge(doc, svg, pos, nums.slice(-3).join(" · ") + (nums.length > 3 ? " …" : ""),
                        safeColor(t.color), level++, (t.label || "Cible") + " — passage(s) : " + nums.join(", "));
        }
      });
    });
    if (opts.after) opts.after(svg, doc, { width: W, height: H });
    return true;
  }

  function reset() { lastSignature = null; }

  root.SitePlan = { render: render, buildSegments: buildSegments, visits: visits, statusText: statusText,
                    reset: reset };
})(typeof window !== "undefined" ? window : globalThis);
