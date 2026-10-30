"""Suivi du parcours de la cible d'une caméra à l'autre.

Logique pure, sans dépendance à FastAPI ni aux modèles : facile à tester.
Elle est alimentée par un thread dédié (voir ``main.py``) et non plus par la
boucle WebSocket : le parcours est donc suivi même sans navigateur connecté.
"""
import copy
import threading
from typing import Any, Dict, Iterable, List, Optional

from . import config


class TargetTracker:
    def __init__(self,
                 switch_confirm_sec: float = config.CAM_SWITCH_CONFIRM_SEC,
                 timeout_sec: float = config.TARGET_TIMEOUT_SEC,
                 path_max: int = config.TARGET_PATH_MAX):
        self.switch_confirm_sec = switch_confirm_sec
        self.timeout_sec = timeout_sec
        self.path_max = path_max
        self._lock = threading.Lock()
        self._reset_unlocked()

    # ---- état -----------------------------------------------------------
    def _reset_unlocked(self):
        self._status: Dict[str, Any] = {
            "active": False, "camera_id": None, "track_id": None,
            "global_id": None, "score": None, "last_seen_ts": 0.0,
        }
        self._path: List[Dict[str, Any]] = []
        self._pending_cam: Optional[str] = None
        self._pending_since = 0.0
        self._was_active = False

    def reset(self):
        with self._lock:
            self._reset_unlocked()

    def snapshot(self) -> Dict[str, Any]:
        """Copie cohérente de l'état, sûre à sérialiser depuis un autre thread."""
        with self._lock:
            return {"target": copy.deepcopy(self._status),
                    "target_path": copy.deepcopy(self._path)}

    # ---- sélection de la meilleure détection ----------------------------
    @staticmethod
    def pick_best(events: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        best = None
        for ev in events:
            if ev.get("mode") != "TARGET":
                continue
            score = ev.get("target_score")
            score = float(score) if score is not None else 0.0
            if best is None or score > best["score"]:
                best = {"camera_id": ev.get("camera_id"), "track_id": ev.get("track_id"),
                        "global_id": ev.get("global_id"), "score": score}
        return best

    # ---- mise à jour ------------------------------------------------------
    def update(self, best: Optional[Dict[str, Any]], now: float):
        with self._lock:
            self._update_unlocked(best, now)

    def _append(self, cam: str, now: float, score):
        self._path.append({"camera_id": cam, "ts": now, "score": score})
        self._path = self._path[-self.path_max:]

    def _update_unlocked(self, best, now):
        status = self._status

        if best is None:
            ts = status.get("last_seen_ts", 0.0)
            if ts and (now - ts) > self.timeout_sec:
                status.update(active=False, camera_id=None, track_id=None,
                              global_id=None, score=None)
            return

        status.update(active=True, camera_id=best["camera_id"],
                      track_id=best.get("track_id"), global_id=best.get("global_id"),
                      score=best.get("score"), last_seen_ts=now)
        current_cam = best["camera_id"]

        if not self._was_active:
            self._append(current_cam, now, best.get("score"))
            self._was_active = True
            self._pending_cam = None
            self._pending_since = 0.0
            return

        last_cam = self._path[-1]["camera_id"] if self._path else None

        if self._pending_cam is None:
            if current_cam != last_cam:
                self._pending_cam = current_cam
                self._pending_since = now
            return

        if current_cam != self._pending_cam:
            self._pending_cam = current_cam
            self._pending_since = now
            return

        # Changement de caméra confirmé après ``switch_confirm_sec`` de présence continue.
        if (now - self._pending_since) >= self.switch_confirm_sec:
            self._append(self._pending_cam, now, best.get("score"))
            self._pending_cam = None
            self._pending_since = 0.0


class TargetWorker:
    """Thread qui alimente le ``TargetTracker`` à partir des événements des caméras."""

    def __init__(self, tracker: TargetTracker, get_cams, interval: float = 0.1):
        self.tracker = tracker
        self._get_cams = get_cams
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def step(self, now: float):
        events = []
        for cam in list(self._get_cams().values()):
            events.extend(list(cam.latest_events or []))
        self.tracker.update(self.tracker.pick_best(events), now)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self):
        import logging
        import time
        while not self._stop.is_set():
            try:
                self.step(time.time())
            except Exception:
                logging.getLogger(__name__).exception("Suivi de la cible : erreur")
            self._stop.wait(self.interval)
