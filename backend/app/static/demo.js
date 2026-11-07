/* Page « Démonstration » : déroulé du scénario, horloge, relance et réinitialisation. Texte inséré avec textContent. */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) { var n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
  var info = null;

  function clock(sec) { var m = Math.floor(sec / 60), s = Math.floor(sec % 60); return m + ":" + (s < 10 ? "0" : "") + s; }
  function post(path) { return fetch(path, { method: "POST" }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { return { ok: r.ok, data: d }; }); }); }

  function paintClock(d) {
    $("clock").textContent = "Scénario " + clock(d.scenario_sec) + " / " + clock(d.period_sec) + " · boucle " + (d.loop + 1) + (d.speed !== 1 ? " · vitesse ×" + d.speed : "");
    var rows = $("script").children;
    Array.prototype.forEach.call(rows, function (tr, i) {
      var next = d.script[i + 1], now = d.script[i];
      var active = d.scenario_sec >= now.t && (!next || d.scenario_sec < next.t);
      if (active) tr.setAttribute("aria-selected", "true"); else tr.removeAttribute("aria-selected");
    });
  }

  function refresh() {
    return fetch("/api/demo").then(function (r) { return r.json(); }).then(function (d) {
      if (!d.demo) { $("off").hidden = false; $("content").hidden = true; return; }
      var first = !info; info = d;
      $("off").hidden = true; $("content").hidden = false; $("restart").hidden = false; $("reset").hidden = false;
      if (first) {
        $("period").textContent = String(d.period_sec);
        d.simulated.forEach(function (t) { $("simulated").appendChild(el("li", t)); });
        d.real.forEach(function (t) { $("real").appendChild(el("li", t)); });
        $("meta").textContent = "Base de données de démonstration : " + d.database + " · personnes fictives : " + d.people.join(", ");
        d.script.forEach(function (row) {
          var cam = (d.cameras.filter(function (c) { return c.id === row.camera_id; })[0] || {}).label || row.camera_id;
          var tr = el("tr"); tr.append(el("td", clock(row.t), "num"), el("td", cam), el("td", row.text)); $("script").appendChild(tr);
        });
      }
      paintClock(d);
    });
  }

  Shell.mount({ active: "demo" });
  $("restart").addEventListener("click", function () { post("/api/demo/restart").then(function () { Shell.toast("Scénario relancé."); refresh(); }); });
  $("reset").addEventListener("click", function () {
    if (!confirm("Effacer les données de démonstration produites jusqu'ici (alertes, détections, statistiques) et relancer ?")) return;
    post("/api/demo/reset").then(function (r) { Shell.toast(r.ok ? "Données de démonstration réinitialisées." : "Échec de la réinitialisation.", r.ok ? "info" : "warning"); refresh(); });
  });
  refresh();
  setInterval(refresh, 1000);
})();
