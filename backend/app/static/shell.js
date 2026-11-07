/*
 * Coque commune de l'interface NiifYéén : barre latérale, thème clair/sombre, icônes,
 * notifications et préférences. Tout le texte est inséré avec textContent.
 */
(function (root) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var PREFIX = "niifyeen.";

  // ---- préférences (localStorage, sans jamais planter si le stockage est indisponible) --------
  var prefs = {
    get: function (key, fallback) {
      try {
        var raw = root.localStorage.getItem(PREFIX + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (e) { return fallback; }
    },
    set: function (key, value) {
      try { root.localStorage.setItem(PREFIX + key, JSON.stringify(value)); } catch (e) { /* ignoré */ }
    }
  };

  // ---- icônes ------------------------------------------------------------------------------
  var ICONS = {
    monitor: [["rect", { x: 3, y: 4, width: 18, height: 12, rx: 2 }], ["path", { d: "M8 20h8M12 16v4" }]],
    users: [["circle", { cx: 9, cy: 8, r: 3.5 }], ["path", { d: "M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6" }],
            ["path", { d: "M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.3c2.2.7 3.5 2.6 3.5 5.7" }]],
    route: [["circle", { cx: 6, cy: 18, r: 2.5 }], ["circle", { cx: 18, cy: 6, r: 2.5 }],
            ["path", { d: "M8.5 18H15a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h6.5" }]],
    history: [["path", { d: "M3 12a9 9 0 1 0 3-6.7L3 8" }], ["path", { d: "M3 3v5h5M12 7v5l3 2" }]],
    list: [["path", { d: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" }]],
    sun: [["circle", { cx: 12, cy: 12, r: 4 }],
          ["path", { d: "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }]],
    moon: [["path", { d: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" }]],
    expand: [["path", { d: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" }]],
    eye: [["path", { d: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" }], ["circle", { cx: 12, cy: 12, r: 3 }]],
    eyeoff: [["path", { d: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" }], ["circle", { cx: 12, cy: 12, r: 3 }],
             ["path", { d: "M3 3l18 18" }]],
    user: [["circle", { cx: 12, cy: 8, r: 4 }], ["path", { d: "M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7" }]],
    target: [["circle", { cx: 12, cy: 12, r: 9 }], ["circle", { cx: 12, cy: 12, r: 4 }],
             ["path", { d: "M12 1v4M12 19v4M1 12h4M19 12h4" }]],
    camera: [["rect", { x: 2, y: 6, width: 14, height: 12, rx: 2 }], ["path", { d: "M16 10l6-3v10l-6-3z" }]],
    chart: [["path", { d: "M4 20V4M4 20h16M8 16v-5M12 16V8M16 16v-3" }]],
    menu: [["path", { d: "M4 6h16M4 12h16M4 18h16" }]],
    bell: [["path", { d: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10 21a2 2 0 0 0 4 0" }]],
    left: [["path", { d: "m15 6-6 6 6 6" }]],
    right: [["path", { d: "m9 6 6 6-6 6" }]],
    plus: [["path", { d: "M12 5v14M5 12h14" }]],
    edit: [["path", { d: "M4 20h4L19 9l-4-4L4 16z" }], ["path", { d: "m14 6 4 4" }]],
    trash: [["path", { d: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" }]],
    refresh: [["path", { d: "M20 11a8 8 0 0 0-14.5-3.5M4 4v4h4M4 13a8 8 0 0 0 14.5 3.5M20 20v-4h-4" }]],
    power: [["path", { d: "M12 3v9M6.3 6.3a8 8 0 1 0 11.4 0" }]],
    search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "m20 20-4-4" }]],
    volume: [["path", { d: "M11 5 6 9H3v6h3l5 4zM15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13" }]],
    mute: [["path", { d: "M11 5 6 9H3v6h3l5 4zM22 9l-6 6M16 9l6 6" }]]
  };

  function icon(name) {
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    (ICONS[name] || []).forEach(function (part) {
      var node = document.createElementNS(NS, part[0]);
      Object.keys(part[1]).forEach(function (k) { node.setAttribute(k, part[1][k]); });
      svg.appendChild(node);
    });
    return svg;
  }

  // ---- thème ---------------------------------------------------------------------------------
  function theme() { return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function setTheme(value) {
    document.documentElement.setAttribute("data-theme", value === "light" ? "light" : "dark");
    prefs.set("theme", value === "light" ? "light" : "dark");
  }

  // ---- notifications et son -----------------------------------------------------------------
  function toast(text, severity, ms) {
    var box = document.getElementById("toasts");
    if (!box) {
      box = document.createElement("div");
      box.id = "toasts"; box.className = "toasts";
      box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite");
      document.body.appendChild(box);
    }
    var t = document.createElement("div");
    t.className = "toast" + (severity && severity !== "info" ? " " + severity : "");
    t.textContent = text;
    box.appendChild(t);
    while (box.children.length > 4) box.removeChild(box.firstChild);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, ms || 4500);
  }

  var audio = null;
  function beep(freq, ms) {
    try {
      var Ctx = root.AudioContext || root.webkitAudioContext;
      if (!Ctx) return;
      audio = audio || new Ctx();
      var osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = freq || 880; gain.gain.value = 0.05;
      osc.connect(gain); gain.connect(audio.destination);
      osc.start(); osc.stop(audio.currentTime + (ms || 160) / 1000);
    } catch (e) { /* son indisponible : ignoré */ }
  }

  // ---- barre latérale -------------------------------------------------------------------------
  var NAV = [
    { href: "/supervision", icon: "monitor", label: "Supervision", key: "supervision" },
    { href: "/alerts", icon: "bell", label: "Alertes", key: "alerts" },
    { href: "/people", icon: "users", label: "Personnes", key: "people" },
    { href: "/map", icon: "route", label: "Carte", key: "map" },
    { href: "/zones", icon: "target", label: "Lignes et zones", key: "zones" },
    { href: "/admin/cameras", icon: "camera", label: "Gestion des caméras", key: "admin-cameras" },
    { href: "/", icon: "users", label: "Cibles et personnes", key: "home" },
    { href: "/cameras", icon: "route", label: "Plan et cheminement", key: "cameras" },
    { href: "/investigation", icon: "search", label: "Investigation", key: "investigation" },
    { href: "/statistics", icon: "chart", label: "Statistiques", key: "statistics" },
    { href: "/reports", icon: "list", label: "Rapports", key: "reports" },
    { href: "/recordings", icon: "camera", label: "Enregistrements", key: "recordings" },
    { href: "/history", icon: "history", label: "Historique", key: "history" },
    { href: "/logs", icon: "list", label: "Journal agrégé", key: "logs" }
  ];

  function mount(options) {
    options = options || {};
    var side = document.getElementById("sidebar");
    if (!side) return null;
    side.replaceChildren();
    side.setAttribute("data-collapsed", String(!!prefs.get("sidebarCollapsed", false)));

    var brand = document.createElement("div");
    brand.className = "brand";
    var mark = document.createElement("div"); mark.className = "brand-mark"; mark.appendChild(icon("target"));
    var name = document.createElement("span"); name.className = "brand-name"; name.textContent = "NiifYéén";
    brand.append(mark, name);
    side.appendChild(brand);

    var nav = document.createElement("nav"); nav.className = "nav"; nav.setAttribute("aria-label", "Navigation principale");
    NAV.forEach(function (item) {
      var a = document.createElement("a");
      a.href = item.href; a.title = item.label;
      if (item.key === options.active) a.setAttribute("aria-current", "page");
      var label = document.createElement("span"); label.className = "label"; label.textContent = item.label;
      a.append(icon(item.icon), label);
      if (item.key === "alerts") {
        var badge = document.createElement("span"); badge.className = "nav-badge"; badge.id = "alertBadge"; badge.hidden = true;
        a.appendChild(badge);
      }
      nav.appendChild(a);
    });
    side.appendChild(nav);

    var spacer = document.createElement("div"); spacer.className = "side-spacer"; side.appendChild(spacer);

    var status = document.createElement("div"); status.className = "side-status";
    var dot = document.createElement("span"); dot.className = "dot"; dot.id = "linkDot";
    var statusText = document.createElement("span"); statusText.className = "label"; statusText.id = "linkText";
    statusText.textContent = "Connexion…";
    status.append(dot, statusText);
    side.appendChild(status);

    var themeBtn = document.createElement("button");
    themeBtn.type = "button"; themeBtn.className = "side-btn"; themeBtn.id = "themeBtn";
    var themeLabel = document.createElement("span"); themeLabel.className = "label";
    function paintTheme() {
      themeBtn.replaceChildren(icon(theme() === "dark" ? "sun" : "moon"), themeLabel);
      themeLabel.textContent = theme() === "dark" ? "Mode clair" : "Mode sombre";
      themeBtn.title = themeLabel.textContent;
    }
    themeBtn.addEventListener("click", function () { setTheme(theme() === "dark" ? "light" : "dark"); paintTheme(); });
    paintTheme();
    side.appendChild(themeBtn);

    var collapse = document.createElement("button");
    collapse.type = "button"; collapse.className = "side-btn"; collapse.title = "Réduire / agrandir le menu";
    var collapseLabel = document.createElement("span"); collapseLabel.className = "label"; collapseLabel.textContent = "Réduire le menu";
    collapse.append(icon("menu"), collapseLabel);
    collapse.addEventListener("click", function () {
      var next = side.getAttribute("data-collapsed") !== "true";
      side.setAttribute("data-collapsed", String(next));
      prefs.set("sidebarCollapsed", next);
    });
    side.appendChild(collapse);
    watchAlerts();
    markDemo(nav, options.active);
    return side;
  }

  /** Mode démonstration : bandeau permanent + entrée de menu, pour qu'on ne confonde jamais avec des données réelles. */
  function markDemo(nav, active) {
    if (typeof fetch !== "function") return;
    fetch("/api/demo").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d || !d.demo || document.getElementById("demoBanner")) return;
      var bar = document.createElement("div");
      bar.id = "demoBanner"; bar.className = "demo-banner"; bar.setAttribute("role", "status");
      bar.textContent = "MODE DÉMONSTRATION — caméras, personnes et données simulées";
      document.body.insertBefore(bar, document.body.firstChild);
      document.body.classList.add("demo");
      document.title = "[DÉMO] " + document.title;
      var a = document.createElement("a"); a.href = "/demo"; a.title = "Démonstration";
      if (active === "demo") a.setAttribute("aria-current", "page");
      var label = document.createElement("span"); label.className = "label"; label.textContent = "Démonstration";
      a.append(icon("eye"), label); nav.insertBefore(a, nav.firstChild);
    }).catch(function () { /* hors démonstration ou serveur injoignable */ });
  }


  // ---- alertes : pastille, notification et son dans toutes les pages -----------------------------------------------
  var RANK = { low: 0, medium: 1, high: 2, critical: 3 };

  /** Son à jouer pour des alertes nouvelles : null (rien) ou { freq, count } selon la plus haute priorité sonore. */
  function soundFor(fresh, enabled) {
    if (!enabled) return null;
    var best = -1;
    (fresh || []).forEach(function (a) { if (a && a.sound && RANK.hasOwnProperty(a.priority)) best = Math.max(best, RANK[a.priority]); });
    if (best < 0) return null;
    return { freq: [520, 660, 880, 1040][best], count: best >= 3 ? 3 : (best === 2 ? 2 : 1) };
  }

  var watching = false;
  function watchAlerts() {
    if (watching || typeof fetch !== "function") return;
    watching = true;
    var lastId = null;
    function tick() {
      var url = "/api/alerts/summary" + (lastId === null ? "" : "?after_id=" + lastId);
      fetch(url).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
        if (!d) return;
        var badge = document.getElementById("alertBadge");
        if (badge) { badge.hidden = !d["new"]; badge.textContent = d["new"] > 99 ? "99+" : String(d["new"]);
                     badge.className = "nav-badge" + (d.new_urgent ? " urgent" : ""); }
        if (lastId !== null) {
          (d.fresh || []).slice(-3).forEach(function (a) {
            toast(a.type_label + " : " + a.title, a.priority === "high" || a.priority === "critical" ? "warning" : "info", 6000);
          });
          var snd = soundFor(d.fresh, prefs.get("alertSound", true) !== false);
          if (snd) for (var i = 0; i < snd.count; i++) (function (k) { setTimeout(function () { beep(snd.freq, 140); }, k * 220); })(i);
        }
        lastId = d.last_id;
      }).catch(function () { /* serveur injoignable : on réessaie au prochain tour */ });
    }
    tick();
    setInterval(tick, 5000);
  }

  /** État de la liaison temps réel affiché en bas de la barre latérale. */
  function setLink(ok, text) {
    var dot = document.getElementById("linkDot"), label = document.getElementById("linkText");
    if (dot) dot.className = "dot " + (ok ? "ok" : "bad");
    if (label) label.textContent = text;
  }

  root.Shell = { prefs: prefs, icon: icon, theme: theme, setTheme: setTheme, toast: toast, beep: beep,
                 mount: mount, setLink: setLink, soundFor: soundFor };
})(typeof window !== "undefined" ? window : globalThis);
