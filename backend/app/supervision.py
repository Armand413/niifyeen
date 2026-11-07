"""Centre de supervision : état des caméras, personnes visibles et génération des événements.

Le ``SupervisionMonitor`` observe l'état brut (caméras, cibles, personnes reconnues) et en tire des
événements lisibles, sans répétition : une caméra qui clignote ne produit pas cent alertes.
"""
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from . import config, events, history

log = logging.getLogger(__name__)

NETWORK_SCHEMES = ("http", "https", "rtsp", "rtsps", "rtmp", "udp", "tcp")


def mask_source(source: Any) -> str:
    """Source de caméra affichable : jamais d'identifiants ni de jeton (ils sont retirés de l'URL)."""
    if isinstance(source, bool) or source is None:
        return "—"
    if isinstance(source, int):
        return f"Webcam {source}"
    text = str(source)
    try:
        parts = urlsplit(text)
    except ValueError:
        return "source réseau"
    if parts.scheme.lower() in NETWORK_SCHEMES and parts.hostname:
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        return f"{parts.scheme.lower()}://{host}{parts.path}" + ("?…" if parts.query else "")
    return os.path.basename(text.replace("\\", "/")) or "fichier"


def describe_cameras(cams: Dict[str, Any], sources: Dict[str, Any], meta: Dict[str, Dict[str, Any]],
                     targets: List[Dict[str, Any]], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Une fiche par caméra : identité, localisation, état de fonctionnement, cibles présentes."""
    out = []
    for cam_id, cam in list(cams.items()):
        m = meta.get(cam_id) or {}
        out.append({
            "id": cam_id, "label": m.get("label", cam_id), "location": m.get("location", ""),
            "x": m.get("x"), "y": m.get("y"), "source": mask_source(sources.get(cam_id)),
            **cam.stats(now),
            "targets_here": [t["id"] for t in targets if t.get("active") and t.get("camera_id") == cam_id],
        })
    return out


def camera_stats(cams: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """État de fonctionnement léger, diffusé en continu par le WebSocket."""
    return {cam_id: cam.stats(now) for cam_id, cam in list(cams.items())}


STATUS_RANK = {"probable": 2, "a_verifier": 1}


def visible_people(cams: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Personnes enrôlées reconnues en ce moment (une ligne par caméra et par personne).

    Chaque ligne porte l'identifiant de la fiche et le **statut de correspondance** : une similarité n'est jamais
    présentée comme une identité certaine (« probable » ou « à vérifier »).
    """
    show = config.HISTORY_SHOW_IDENTITIES
    seen: Dict[tuple, Dict[str, Any]] = {}
    for cam_id, cam in list(cams.items()):
        for ev in list(cam.latest_events or []):
            if ev.get("mode") != "DB":
                continue
            pid = ev.get("participant_id")
            key = (cam_id, pid if pid is not None else ev.get("label"))
            score = ev.get("db_score")
            if key not in seen or (score or 0) > (seen[key]["score"] or 0):
                seen[key] = {"camera_id": cam_id, "participant_id": pid if show else None,
                             "label": history.public_label("DB", ev.get("label")),
                             "score": round(score, 3) if score is not None else None,
                             "status": ev.get("match_status"), "active": ev.get("participant_active", True),
                             "ambiguous": bool(ev.get("match_ambiguous"))}
    return list(seen.values())


class SupervisionMonitor:
    def __init__(self, get_cams: Callable[[], Dict[str, Any]], get_targets: Callable[[], List[Any]],
                 feed: "events.EventFeed", get_meta: Callable[[], Dict[str, Dict[str, Any]]],
                 interval: float = 0.5, offline_after_sec: float = 5.0, never_online_after_sec: float = 15.0,
                 lost_after_sec: float = 8.0, recognized_cooldown_sec: float = 60.0):
        self._get_cams, self._get_targets, self.feed, self._get_meta = get_cams, get_targets, feed, get_meta
        self.interval = interval
        self.offline_after_sec = offline_after_sec
        self.never_online_after_sec = never_online_after_sec
        self.lost_after_sec = lost_after_sec
        self.recognized_cooldown_sec = recognized_cooldown_sec
        self._cam_state: Dict[str, Dict[str, Any]] = {}
        self._target_state: Dict[int, Dict[str, Any]] = {}
        self._recognized: Dict[tuple, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- utilitaires --------------------------------------------------------------------
    def _cam_label(self, cam_id: Optional[str]) -> str:
        return ((self._get_meta() or {}).get(cam_id) or {}).get("label", cam_id or "?")

    # ---- déplacement d'une cible (appelé par le suivi, hors verrou) ------------------------
    def on_target_move(self, target, move: Dict[str, Any]):
        path = target.tracker.snapshot()["target_path"]
        cam = move["camera_id"]
        common = dict(target_id=target.id, target_label=target.label, color=target.color,
                      score=move.get("score"), camera=cam)
        if len(path) <= 1:
            self.feed.emit(events.TARGET_SEEN, f"{target.label} repérée : {self._cam_label(cam)}",
                           camera_id=cam, **common)
        else:
            previous = path[-2]["camera_id"]
            self.feed.emit(events.TARGET_MOVED,
                           f"{target.label} : {self._cam_label(previous)} → {self._cam_label(cam)}",
                           camera_id=cam, from_camera=previous, **common)

    # ---- une passe d'observation --------------------------------------------------------------
    def step(self, now: Optional[float] = None):
        now = time.time() if now is None else now
        self._check_cameras(now)
        self._check_people(now)
        self._check_targets(now)

    def _check_cameras(self, now: float):
        current = self._get_cams()
        for cam_id in [c for c in self._cam_state if c not in current]:
            del self._cam_state[cam_id]                  # caméra retirée : on oublie son état
        for cam_id, cam in list(current.items()):
            st = self._cam_state.setdefault(cam_id, {"announced": None, "bad_since": None})
            status = cam.status(now)
            if status == "online":
                st["bad_since"] = None
                if st["announced"] == "offline":
                    self.feed.emit(events.CAMERA_ONLINE, f"Caméra rétablie : {self._cam_label(cam_id)}",
                                   camera_id=cam_id)
                st["announced"] = "online"
                continue
            if status == "stopped":
                continue
            if st["bad_since"] is None:
                st["bad_since"] = now
            threshold = self.offline_after_sec if cam.last_frame_ts else self.never_online_after_sec
            if st["announced"] != "offline" and now - st["bad_since"] >= threshold:
                st["announced"] = "offline"
                self.feed.emit(events.CAMERA_OFFLINE, f"Caméra hors ligne : {self._cam_label(cam_id)}",
                               severity="warning", camera_id=cam_id, status=status)

    def _check_people(self, now: float):
        show = config.HISTORY_SHOW_IDENTITIES
        for cam_id, cam in list(self._get_cams().items()):
            for ev in list(cam.latest_events or []):
                if ev.get("mode") != "DB":
                    continue
                pid, status = ev.get("participant_id"), ev.get("match_status")
                inactive = ev.get("participant_active") is False
                key = (cam_id, pid if pid is not None else ev.get("label"), inactive and status == "probable")
                if now - self._recognized.get(key, -1e9) < self.recognized_cooldown_sec:
                    continue
                self._recognized[key] = now
                name = history.public_label("DB", ev.get("label"))
                where = self._cam_label(cam_id)
                payload = dict(status=status, score=ev.get("db_score"), ambiguous=bool(ev.get("match_ambiguous")))
                if show:
                    payload.update(person=name, participant_id=pid)
                if inactive and status == "probable":
                    # Fiche désactivée mais reconnue avec une correspondance probable : à signaler à l'opérateur.
                    self.feed.emit(events.PERSON_INACTIVE, f"Personne désactivée reconnue : {name} — {where}",
                                   severity="warning", camera_id=cam_id, **payload)
                elif status == "probable":
                    self.feed.emit(events.PERSON_RECOGNIZED, f"Personne reconnue : {name} — {where}",
                                   camera_id=cam_id, **payload)
                else:
                    self.feed.emit(events.PERSON_RECOGNIZED, f"Correspondance à vérifier : {name} — {where}",
                                   camera_id=cam_id, **payload)
        if len(self._recognized) > 500:                        # évite une croissance sans fin
            self._recognized = {k: t for k, t in self._recognized.items()
                                if now - t < self.recognized_cooldown_sec}

    def _check_targets(self, now: float):
        current = {t.id: t for t in self._get_targets()}
        for tid in [i for i in self._target_state if i not in current]:
            del self._target_state[tid]                        # cible retirée
        for tid, target in current.items():
            snap = target.tracker.snapshot()
            active = snap["target"]["active"]
            st = self._target_state.setdefault(tid, {"was_active": False, "lost_since": None, "announced_lost": False})
            if active:
                if st["announced_lost"]:
                    cam = snap["target"]["camera_id"]
                    self.feed.emit(events.TARGET_SEEN, f"{target.label} de nouveau détectée : {self._cam_label(cam)}",
                                   camera_id=cam, target_id=tid, target_label=target.label, color=target.color,
                                   camera=cam, reappeared=True)
                st.update(was_active=True, lost_since=None, announced_lost=False)
            elif st["was_active"]:
                if st["lost_since"] is None:
                    st["lost_since"] = now
                if not st["announced_lost"] and now - st["lost_since"] >= self.lost_after_sec:
                    st["announced_lost"] = True
                    path = snap["target_path"]
                    last = path[-1]["camera_id"] if path else None
                    self.feed.emit(events.TARGET_LOST,
                                   f"{target.label} perdue de vue (dernière caméra : {self._cam_label(last)})",
                                   camera_id=last, target_id=tid, target_label=target.label, color=target.color,
                                   camera=last)

    # ---- thread ------------------------------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("Supervision : erreur")
            self._stop.wait(self.interval)
