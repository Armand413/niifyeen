/*
 * Calculs purs de la page Enregistrements (testables sans navigateur) : tailles, recherche d'image dans un segment,
 * cadence de lecture, libellés des déclencheurs et des plages horaires, lien direct depuis une alerte ou un événement.
 */
(function (root) {
  "use strict";

  var DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."];
  var TRIGGERS = { manual: "Manuel", event: "Sur événement", schedule: "Plage horaire" };
  var STATUSES = { recording: "En cours", done: "Terminé", error: "Interrompu" };

  function formatBytes(n) {
    if (typeof n !== "number" || !(n >= 0)) return "";
    if (n < 1024) return n + " o";
    if (n < 1048576) return (Math.round(n / 102.4) / 10) + " Ko";
    if (n < 1073741824) return (Math.round(n / 104857.6) / 10) + " Mo";
    return (Math.round(n / 107374182.4) / 10) + " Go";
  }

  /** Index de la dernière image dont l'instant (en secondes) est <= sec ; borné à la liste. */
  function frameAt(times, sec) {
    if (!times || !times.length) return -1;
    if (!(sec > times[0])) return 0;
    var lo = 0, hi = times.length - 1;
    while (lo < hi) {
      var mid = (lo + hi + 1) >> 1;
      if (times[mid] <= sec) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  /** Attente (ms) avant l'image suivante : écart réel entre images divisé par la vitesse, borné pour rester fluide. */
  function nextDelay(times, i, speed) {
    var s = speed > 0 ? speed : 1;
    if (!times || i < 0 || i + 1 >= times.length) return 0;
    var ms = (times[i + 1] - times[i]) * 1000 / s;
    return Math.max(30, Math.min(2000, ms));
  }

  function triggerLabel(t) { return TRIGGERS[t] || String(t || ""); }
  function statusLabel(s) { return STATUSES[s] || String(s || ""); }

  function scheduleLabel(s, cameraName) {
    var days = (s.days || []).map(function (d) { return DAYS[d] || "?"; }).join(", ");
    return days + " " + s.start + "–" + s.end + " · " + (s.camera_id === "*" ? "toutes les caméras" : (cameraName || s.camera_id));
  }

  function formatClock(sec) {
    if (!(sec >= 0)) return "0:00";
    var m = Math.floor(sec / 60), s = Math.floor(sec % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  /** Lien direct : ?id=12&t=34.5 -> { id: 12, t: 34.5 } ; valeurs invalides ignorées. */
  function parseDeepLink(search) {
    var out = { id: null, t: 0 };
    String(search || "").replace(/^\?/, "").split("&").forEach(function (part) {
      var kv = part.split("="), k = kv[0], v = decodeURIComponent(kv[1] || "");
      if (k === "id" && /^\d{1,9}$/.test(v)) out.id = Number(v);
      if (k === "t" && /^\d{1,6}(\.\d+)?$/.test(v)) out.t = Number(v);
    });
    return out;
  }

  /** Lien vers un extrait, à partir de nombres uniquement. */
  function clipLink(id, offset) {
    if (!(Number.isInteger(id) && id > 0)) return "";
    var t = offset > 0 ? Math.round(offset * 10) / 10 : 0;
    return "/recordings?id=" + id + "&t=" + t;
  }

  root.RecordingsLogic = { DAYS: DAYS, formatBytes: formatBytes, frameAt: frameAt, nextDelay: nextDelay, triggerLabel: triggerLabel,
                           statusLabel: statusLabel, scheduleLabel: scheduleLabel, formatClock: formatClock,
                           parseDeepLink: parseDeepLink, clipLink: clipLink };
})(typeof window !== "undefined" ? window : globalThis);
