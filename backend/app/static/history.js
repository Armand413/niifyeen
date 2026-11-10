/* Page « Historique des détections ». Tout le texte est inséré avec textContent : aucune donnée n'est interprétée comme du HTML. */
(function () {
  "use strict";

  var PAGE_SIZE = 25;
  var SVG_NS = "http://www.w3.org/2000/svg";
  var MODE_FR = { TARGET: "Cible", DB: "Personne enrôlée", UNK: "Inconnu" };

  var $ = function (id) { return document.getElementById(id); };
  var COLORS = ["#d92d20", "#7a5af8", "#f79009", "#ee46bc", "#2e5bff", "#8a4b08", "#3b8f0a", "#1d2939"];
  var state = { participantId: null, page: 1, total: 0, busy: false, meta: {}, trajectories: [], selected: new Set(), timer: null };

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function svgEl(tag, attrs, text) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  // ---- formats ---------------------------------------------------------------------
  function fmtDateTime(iso) { var d = new Date(iso); return isNaN(d) ? "" : d.toLocaleString(); }
  function fmtTime(iso) { var d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString(); }
  function fmtDuration(sec) {
    if (sec < 1) return "< 1 s";
    if (sec < 60) return Math.round(sec) + " s";
    var m = Math.floor(sec / 60), s = Math.round(sec % 60);
    if (sec < 3600) return m + " min " + (s < 10 ? "0" : "") + s + " s";
    return Math.floor(sec / 3600) + " h " + (m % 60) + " min";
  }
  function cameraLabel(id) { return (state.meta[id] && state.meta[id].label) || id; }

  // ---- filtres ---------------------------------------------------------------------
  function localToIso(value) { return value ? new Date(value).toISOString() : ""; }

  function rangeParams() {
    var p = new URLSearchParams();
    if ($("period").value === "custom") {
      if (!$("start").value) throw new Error("Indiquer au moins la date de début.");
      p.set("start", localToIso($("start").value));
      if ($("end").value) p.set("end", localToIso($("end").value));
    } else {
      p.set("minutes", $("period").value);
    }
    return p;
  }

  function filterParams() {
    var p = rangeParams();
    if ($("camera").value) p.set("camera_id", $("camera").value);
    return p;
  }
  function episodeParams() {
    var p = filterParams();
    if ($("mode").value) p.set("mode", $("mode").value);
    var label = $("label").value.trim();
    if (label && !$("labelBox").classList.contains("hidden")) p.set("label", label);
    if (state.participantId) p.set("participant_id", state.participantId);
    return p;
  }

  async function api(path, params) {
    var res = await fetch(path + "?" + params);
    if (!res.ok) {
      var detail = "";
      try { var j = await res.json(); detail = typeof j.detail === "string" ? j.detail : ""; } catch (e) { /* ignore */ }
      throw new Error(detail || "Erreur " + res.status);
    }
    return res.json();
  }

  // ---- synthèse --------------------------------------------------------------------
  function statCard(value, label, extra) {
    var c = el("div", null, "card-stat" + (extra ? " " + extra : ""));
    c.append(el("b", String(value)), el("span", label));
    return c;
  }

  function renderSummary(s) {
    var cards = $("cards");
    cards.replaceChildren(
      statCard(s.passages, "passages"),
      statCard(s.observations, "observations enregistrées"),
      statCard(s.by_mode.TARGET, "passages de la cible", "t-target"),
      statCard(s.by_mode.DB, s.enrolled_people === null || s.enrolled_people === undefined
        ? "passages de personnes enrôlées"
        : "passages de personnes enrôlées (" + s.enrolled_people + " personne(s))"),
      statCard(s.by_mode.UNK, "passages d'inconnus"));

    var body = $("cams");
    body.replaceChildren();
    if (!s.by_camera.length) {
      var tr = body.insertRow(), td = tr.insertCell();
      td.colSpan = 5; td.textContent = "Aucun passage pour ces filtres.";
    }
    s.by_camera.forEach(function (c) {
      var tr = body.insertRow();
      [cameraLabel(c.camera_id), c.passages, c.TARGET, c.DB, c.UNK].forEach(function (v, i) {
        var td = tr.insertCell(); td.textContent = String(v); if (i) td.className = "num";
      });
    });
    renderChart(s);
  }

  function renderChart(s) {
    var svg = $("chart"), W = 900, H = 170, L = 38, B = 22, T = 8, R = 6;
    svg.replaceChildren();
    var tl = s.timeline, max = Math.max.apply(null, [1].concat(tl.map(function (b) { return b.observations; })));
    var bw = (W - L - R) / tl.length, ph = H - B - T;
    [0, 0.5, 1].forEach(function (f) {
      var y = T + ph * (1 - f);
      svg.appendChild(svgEl("line", { x1: L, x2: W - R, y1: y, y2: y, stroke: "#e2e8f0" }));
      svg.appendChild(svgEl("text", { x: L - 6, y: y + 4, "text-anchor": "end" }, String(Math.round(max * f))));
    });
    tl.forEach(function (b, i) {
      var h = ph * b.observations / max, x = L + i * bw + 1, w = Math.max(1, bw - 2);
      var bar = svgEl("rect", { x: x.toFixed(1), y: (T + ph - h).toFixed(1), width: w.toFixed(1),
                                height: Math.max(h, b.observations ? 1 : 0).toFixed(1), fill: "#7aaaff", rx: 1 });
      bar.appendChild(svgEl("title", {}, fmtDateTime(b.start) + " : " + b.observations + " observation(s), dont " +
                                         b.target + " de la cible"));
      svg.appendChild(bar);
      if (b.target) {
        var th = ph * b.target / max;
        svg.appendChild(svgEl("rect", { x: x.toFixed(1), y: (T + ph - th).toFixed(1), width: w.toFixed(1),
                                        height: th.toFixed(1), fill: "#d92d20", rx: 1 }));
      }
    });
    [0, Math.floor((tl.length - 1) / 2), tl.length - 1].forEach(function (i, k) {
      if (!tl[i] || (k && i === 0)) return;
      var d = new Date(tl[i].start);
      var text = s.bucket_minutes >= 1440 ? d.toLocaleDateString()
        : (d.toLocaleDateString(undefined, { day: "2-digit", month: "2-digit" }) + " " +
           d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }));
      svg.appendChild(svgEl("text", { x: L + i * bw + bw / 2, y: H - 6,
                                      "text-anchor": k === 0 ? "start" : (k === 2 ? "end" : "middle") }, text));
    });
    $("chartNote").textContent = "Observations par tranche de " + s.bucket_minutes + " min (bleu), dont celles de la cible (rouge).";
  }

  // ---- passages --------------------------------------------------------------------
  function renderEpisodes(data) {
    var body = $("rows");
    body.replaceChildren();
    var showIdentity = data.identities_visible !== false;
    $("labelBox").classList.toggle("hidden", !showIdentity);
    $("gap").textContent = String(data.gap_sec);
    if (!data.items.length) {
      var tr = body.insertRow(), td = tr.insertCell();
      td.colSpan = 7; td.textContent = "Aucun passage pour ces filtres.";
    }
    data.items.forEach(function (ep) {
      var tr = body.insertRow();
      tr.insertCell().textContent = fmtDateTime(ep.first_seen);
      var dur = tr.insertCell();
      dur.textContent = fmtDuration(ep.duration_sec); dur.className = "num";
      dur.title = "Jusqu'à " + fmtTime(ep.last_seen);
      tr.insertCell().textContent = cameraLabel(ep.camera_id);
      var type = tr.insertCell();
      type.appendChild(el("span", MODE_FR[ep.mode] || ep.mode, "mode m-" + ep.mode));
      tr.insertCell().textContent = ep.label || "—";
      var obs = tr.insertCell(); obs.textContent = String(ep.observations); obs.className = "num";
      var sc = tr.insertCell(); sc.textContent = ep.best_score === null ? "—" : ep.best_score.toFixed(2); sc.className = "num";
    });
    state.total = data.total;
    var pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
    $("page").textContent = "Page " + state.page + " / " + pages;
    $("previous").disabled = state.busy || state.page <= 1;
    $("next").disabled = state.busy || state.page >= pages;
  }

  // ---- trajets des cibles ----------------------------------------------------------
  function renderTrajectories(data) {
    state.trajectories = data.items;
    var ids = data.items.map(function (t) { return t.session; });
    // On ne garde que les trajets encore présents ; par défaut, le plus récent.
    state.selected = new Set(Array.from(state.selected).filter(function (id) { return ids.indexOf(id) >= 0; }));
    if (!state.selected.size && ids.length) state.selected.add(ids[0]);

    var list = $("trajList");
    list.replaceChildren();
    $("trajStatus").textContent = data.items.length
      ? data.total + " trajet(s) sur la période" + (data.total > data.items.length ? " (les plus récents)" : "") +
        " — cliquer pour afficher ou masquer sur le plan."
      : "Aucun trajet de cible enregistré sur la période.";
    data.items.forEach(function (t) {
      var li = el("li"), btn = el("button");
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(state.selected.has(t.session)));
      var title = el("span", (t.label ? t.label + " · " : "") + fmtDateTime(t.started), "when");
      btn.append(title,
                 el("span", t.moves.map(function (m) { return cameraLabel(m.camera_id); }).join(" → ")),
                 el("span", t.cameras + " caméra(s)"));
      btn.addEventListener("click", function () {
        if (state.selected.has(t.session)) state.selected.delete(t.session); else state.selected.add(t.session);
        renderTrajectories(data);
      });
      li.appendChild(btn);
      list.appendChild(li);
    });
    drawTrajectory();
  }

  function drawTrajectory() {
    var chosen = state.trajectories.filter(function (x) { return state.selected.has(x.session); });
    var targets = chosen.map(function (t, i) {
      return { id: t.session, label: t.label || "Trajet", color: COLORS[i % COLORS.length], active: false,
               path: t.moves.map(function (m) { return { camera_id: m.camera_id, ts: m.ts, score: m.score }; }) };
    });
    window.SitePlan.reset();
    window.SitePlan.render($("planOverlay"), null, state.meta, targets);
    $("planCaption").textContent = !chosen.length ? "Aucun trajet sélectionné."
      : chosen.map(function (t) {
          return (t.label || "Trajet") + " : " + t.moves.map(function (m) { return cameraLabel(m.camera_id) + " (" + fmtTime(m.timestamp) + ")"; }).join(" → ");
        }).join(" — ") + ". Le dernier point de chaque trajet est entouré en pointillé.";
  }

  // ---- chargement ------------------------------------------------------------------
  function setStatus(text, isError) {
    var s = $("status");
    s.textContent = text;
    s.className = "status" + (isError ? " error" : "");
  }

  async function load() {
    if (state.busy) return;
    var params, eparams;
    try { params = filterParams(); eparams = episodeParams(); }
    catch (e) { setStatus(e.message, true); return; }
    state.busy = true;
    $("refresh").disabled = true;
    $("previous").disabled = true; $("next").disabled = true;
    setStatus("Chargement…");
    eparams.set("page", state.page); eparams.set("page_size", PAGE_SIZE);
    $("export").href = "/api/history/episodes.csv?" + episodeParams();
    try {
      var results = await Promise.all([
        api("/api/history/summary", params),
        api("/api/history/episodes", eparams),
        api("/api/history/trajectories", rangeParams())
      ]);
      state.busy = false;
      renderSummary(results[0]);
      renderEpisodes(results[1]);
      renderTrajectories(results[2]);
      setStatus(results[1].total + " passage(s) · " + results[0].observations + " observation(s) · actualisé à " +
                new Date().toLocaleTimeString());
    } catch (e) {
      setStatus("Historique indisponible : " + e.message, true);
    } finally {
      state.busy = false;
      $("refresh").disabled = false;
    }
  }

  function applyAuto() {
    clearInterval(state.timer);
    var custom = $("period").value === "custom";
    $("auto").disabled = custom;
    if (custom) $("auto").checked = false;
    if ($("auto").checked) state.timer = setInterval(load, 10000);
  }

  function toggleCustom() {
    var custom = $("period").value === "custom";
    document.querySelectorAll(".custom").forEach(function (n) { n.classList.toggle("hidden", !custom); });
    if (custom && !$("start").value) {
      var now = new Date(), start = new Date(now.getTime() - 3600 * 1000);
      var local = function (d) { return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
      $("start").value = local(start); $("end").value = local(now);
    }
    applyAuto();
  }

  // Filtre « personne » (lien depuis la fiche d'une cible) : ne concerne que le tableau des passages.
  function showPersonFilter() {
    var on = !!state.participantId;
    $("personFilter").classList.toggle("hidden", !on);
    $("personFilterText").textContent = on
      ? "Passages filtrés sur la personne n°" + state.participantId + " (la synthèse et les trajets restent globaux)."
      : "";
  }

  async function init() {
    window.SitePlan.loadBase($("planImg"), $("planOverlay")).then(function (b) { if (b) drawTrajectory(); });
    var wanted = new URLSearchParams(location.search).get("participant_id");
    state.participantId = /^[1-9][0-9]{0,9}$/.test(wanted || "") ? Number(wanted) : null;
    showPersonFilter();
    $("personFilterClear").addEventListener("click", function () {
      state.participantId = null; showPersonFilter(); state.page = 1;
      history.replaceState(null, "", location.pathname); load();
    });
    try {
      var res = await fetch("/api/cameras");
      if (res.ok) {
        var data = await res.json();
        state.meta = data.camera_meta || {};
        (data.cameras || []).forEach(function (id) {
          var o = document.createElement("option");
          o.value = id; o.textContent = cameraLabel(id);
          $("camera").appendChild(o);
        });
      }
    } catch (e) { /* le filtre caméra restera réduit à « Toutes » */ }

    $("filters").addEventListener("submit", function (ev) { ev.preventDefault(); state.page = 1; load(); });
    ["camera", "mode", "period", "start", "end"].forEach(function (id) {
      $(id).addEventListener("change", function () { state.page = 1; if (id === "period") toggleCustom(); if (!(id === "period" && $("period").value === "custom")) load(); });
    });
    $("auto").addEventListener("change", applyAuto);
    $("previous").addEventListener("click", function () { state.page--; load(); });
    $("next").addEventListener("click", function () { state.page++; load(); });
    load();
  }

  init();
})();
