/* Boîte de réception d'une autorité : alertes à traiter, photos, accusé de prise en charge, son facultatif.
 * Tout le texte venant du serveur est inséré avec textContent : rien n'est interprété comme du HTML. */
(function () {
  "use strict";
  var base = location.pathname.replace(/\/+$/, "");           // /a/<lien secret>
  var data = { messages: [] }, seen = {}, lastSig = "", first = true, filter = "todo", soundOn = false, audio = null, failures = 0;

  function $(id) { return document.getElementById(id); }
  function el(tag, text, cls) { var n = document.createElement(tag); if (text) n.textContent = text; if (cls) n.className = cls; return n; }
  function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; } }

  function ago(iso) {
    var s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return "à l'instant";
    if (s < 3600) return "il y a " + Math.floor(s / 60) + " min";
    if (s < 86400) return "il y a " + Math.floor(s / 3600) + " h";
    return "il y a " + Math.floor(s / 86400) + " j";
  }
  function field(text, label) {                              // « Label : valeur » dans le texte du message
    var m = new RegExp("^" + label + " : (.+)$", "m").exec(text);
    return m ? m[1].trim() : null;
  }

  // ---- son ----------------------------------------------------------------------------------------
  // Les navigateurs interdisent tout son avant un premier geste de l'utilisateur : la bannière demande ce geste une fois,
  // puis le choix est mémorisé. Tant qu'une alerte n'est pas prise en charge, un rappel sonore est rejoué.
  var REMINDER_MS = 15000, unlocked = false;
  function ctx() {
    try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audio = null; }
    return audio;
  }
  function showBanner() { $("soundBanner").hidden = !soundOn || unlocked; }
  function unlock() {                                         // à appeler dans un geste de l'utilisateur
    var a = ctx(); if (!a) return;
    var done = function () { unlocked = a.state === "running"; showBanner(); };
    if (a.state === "suspended") a.resume().then(done, done); else done();
  }
  function tone(freq, start, dur) {
    var a = ctx(); if (!a) return;
    var o = a.createOscillator(), g = a.createGain();
    o.type = "square"; o.frequency.value = freq; g.gain.value = 0.2;
    o.connect(g); g.connect(a.destination); o.start(a.currentTime + start); o.stop(a.currentTime + start + dur);
  }
  function alarm() { [880, 660, 880].forEach(function (f, i) { tone(f, i * 0.25, 0.18); }); }
  function beep() { if (soundOn && unlocked) alarm(); }
  function setSound(on) {
    soundOn = on; $("sound").setAttribute("aria-pressed", on ? "true" : "false"); $("sound").textContent = "Son : " + (on ? "oui" : "non");
    store("niifyeen.inbox.sound", on ? "1" : "0");
    if (on) unlock(); else showBanner();
  }
  $("sound").addEventListener("click", function () { setSound(!soundOn); if (soundOn) alarm(); });
  $("soundTest").addEventListener("click", function () { unlock(); alarm(); });
  $("soundBanner").addEventListener("click", function () { unlock(); alarm(); });
  ["click", "touchstart", "keydown"].forEach(function (name) {   // le premier geste n'importe où débloque le son
    document.addEventListener(name, function () { if (soundOn && !unlocked) unlock(); }, { passive: true });
  });
  // Actif par défaut (désactivable) ; il ne sonne réellement qu'après un premier geste, d'où la bannière.
  soundOn = store("niifyeen.inbox.sound") !== "0";
  $("sound").setAttribute("aria-pressed", soundOn ? "true" : "false"); $("sound").textContent = "Son : " + (soundOn ? "oui" : "non");
  showBanner();
  // Tant qu'une alerte n'est pas prise en charge, le son est rejoué (rappel).
  setInterval(function () {
    var pending = data.messages.some(function (m) { return !m.acknowledged && m.target_label !== "essai"; });
    if (pending && soundOn && unlocked) alarm();
  }, REMINDER_MS);

  // ---- photos en grand ----------------------------------------------------------------------------
  function openPhoto(src, caption) {
    $("lbImg").src = src; $("lbImg").alt = caption; $("lbCap").textContent = caption;
    $("lightbox").hidden = false; $("lbClose").focus();
  }
  function closePhoto() { $("lightbox").hidden = true; $("lbImg").removeAttribute("src"); }
  $("lbClose").addEventListener("click", closePhoto);
  $("lightbox").addEventListener("click", function (ev) { if (ev.target === $("lightbox")) closePhoto(); });
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closePhoto(); });

  // ---- affichage ----------------------------------------------------------------------------------
  function card(m, isNew) {
    var c = el("article", null, "card " + (m.acknowledged ? "done" : "todo") + (isNew ? " new" : ""));
    var head = el("div", null, "head"), left = el("div");
    left.append(el("span", m.acknowledged ? "Pris en charge" : (m.target_label === "essai" ? "Message d'essai" : "Alerte"), "badge"),
                el("p", m.target_label && m.target_label !== "essai" ? m.target_label : "Test de la liaison", "target"));
    var ageEl = el("b", ago(m.created_at)); ageEl.setAttribute("data-ts", m.created_at);
    var when = el("div", null, "when"); when.append(ageEl,
      document.createTextNode(new Date(m.created_at).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })));
    head.append(left, when); c.append(head);

    var place = el("p", null, "place");
    if (m.camera_label) { place.append(document.createTextNode("Vue par la caméra "), el("b", m.camera_label)); if (m.camera_place) place.append(document.createTextNode(" — " + m.camera_place)); c.append(place); }

    var chips = el("div", null, "chips"), conf = field(m.message, "Confiance du système"), name = field(m.message, "  Nom");
    if (name) chips.append(el("span", "Personne reconnue : " + name, "chip"));
    if (conf) chips.append(el("span", "Confiance " + conf.split(" ")[0], "chip"));
    if (/identité inconnue/.test(m.message)) chips.append(el("span", "Identité inconnue", "chip"));
    if (chips.childNodes.length) c.append(chips);

    if (m.photos.length) {
      var g = el("div", null, "gallery");
      m.photos.forEach(function (caption, i) {
        var src = base + "/api/photo/" + m.id + "/" + i;
        var fig = el("figure", null, "shot"), btn = el("button"), img = document.createElement("img");
        btn.type = "button"; btn.setAttribute("aria-label", "Agrandir : " + caption);
        img.src = src; img.alt = caption; img.loading = "lazy";
        btn.append(img); btn.addEventListener("click", function () { openPhoto(src, caption); });
        fig.append(btn, el("figcaption", caption)); g.append(fig);
      });
      c.append(g);
    } else if (m.target_label !== "essai") {
      c.append(el("p", "Photos non disponibles (conservées une heure en mémoire seulement).", "nophoto"));
    }

    var det = el("details"); det.append(el("summary", "Détails complets"), el("pre", m.message)); c.append(det);
    var act = el("div", null, "actions");
    if (!m.acknowledged) {
      var b = el("button", "Pris en charge", "ack"); b.type = "button";
      b.addEventListener("click", function () { b.disabled = true; fetch(base + "/api/ack/" + m.id, { method: "POST" }).then(refresh); });
      act.append(b);
    }
    var del = el("button", "Supprimer", "del"); del.type = "button";
    del.addEventListener("click", function () {
      if (!window.confirm("Supprimer cette alerte de votre boîte de réception ?")) return;
      del.disabled = true; fetch(base + "/api/messages/" + m.id, { method: "DELETE" }).then(refresh);
    });
    act.append(del); c.append(act);
    return c;
  }

  var clearAll = $("clearAll");
  if (clearAll) clearAll.addEventListener("click", function () {
    if (!data.messages.length || !window.confirm("Supprimer toutes les alertes de votre boîte de réception ?")) return;
    fetch(base + "/api/messages", { method: "DELETE" }).then(refresh);
  });

  function render() {
    var todo = data.messages.filter(function (m) { return !m.acknowledged; });
    $("nTodo").textContent = todo.length; $("nAll").textContent = data.messages.length;
    $("tabTodo").setAttribute("aria-selected", filter === "todo"); $("tabAll").setAttribute("aria-selected", filter === "all");
    var shown = filter === "todo" ? todo : data.messages, list = $("list");
    list.replaceChildren();
    shown.forEach(function (m) { list.append(card(m, m._new)); });
    var empty = $("empty");
    empty.hidden = shown.length > 0;
    empty.textContent = filter === "todo" ? "Aucune alerte à traiter. Cette page se met à jour toute seule." : "Aucune alerte reçue pour l'instant.";
    document.title = (todo.length ? "(" + todo.length + ") " : "") + "NiifYéén — Réception";
  }

  function ingest(payload) {
    $("who").textContent = payload.authority; $("service").textContent = payload.service || "";
    var fresh = 0;
    payload.messages.forEach(function (m) {
      m._new = !first && !seen[m.id];
      if (m._new && !m.acknowledged) fresh++;
      seen[m.id] = true;
    });
    var sig = payload.messages.map(function (m) { return m.id + ":" + m.acknowledged + ":" + m.photos.length; }).join("|") +
              payload.authority + payload.service;
    var changed = sig !== lastSig; lastSig = sig;
    data = payload; first = false;
    if (fresh) {
      $("flash").hidden = false; $("flash").textContent = fresh > 1 ? fresh + " nouvelles alertes" : "Nouvelle alerte";
      setTimeout(function () { $("flash").hidden = true; }, 8000);
      if (navigator.vibrate) { try { navigator.vibrate([300, 120, 300]); } catch (e) { /* ignoré */ } }
      beep();
    }
    if (changed) render();            // sans changement, on ne reconstruit pas : les « Détails » ouverts et le défilement restent
  }

  function setLive(ok) {
    $("live").className = "live" + (ok ? "" : " bad");
    $("state").textContent = ok ? "En direct" : "Connexion perdue";
  }
  function refresh() {
    return fetch(base + "/api/messages", { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    }).then(function (payload) { failures = 0; setLive(true); ingest(payload); })
      .catch(function () { failures++; if (failures >= 2) setLive(false); });
  }

  $("tabTodo").addEventListener("click", function () { filter = "todo"; render(); });
  $("tabAll").addEventListener("click", function () { filter = "all"; render(); });
  refresh();
  setInterval(refresh, 3000);
  setInterval(function () {                                                  // met à jour « il y a … » sans reconstruire
    document.querySelectorAll("[data-ts]").forEach(function (n) { n.textContent = ago(n.getAttribute("data-ts")); });
  }, 30000);
})();
