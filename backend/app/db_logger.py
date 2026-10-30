"""Enregistrement périodique des observations en base, et purge selon la rétention."""
import logging
import threading
import time
from typing import Callable, Dict, Optional

from . import config
from .db import SessionLocal, Detection, utcnow, purge_old_detections

log = logging.getLogger(__name__)

PURGE_EVERY_SEC = 3600.0
STALE_KEY_SEC = 120.0


def _opt(ev, key, cast):
    value = ev.get(key)
    return cast(value) if value is not None else None


class DBLogger:
    def __init__(self, get_cams: Callable[[], Dict[str, object]],
                 log_every_sec: float = config.LOG_EVERY_SEC,
                 retention_days: int = config.RETENTION_DAYS):
        self._get_cams = get_cams
        self.log_every_sec = log_every_sec
        self.retention_days = retention_days
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.last: Dict[tuple, float] = {}  # (cam, track, mode) -> dernier enregistrement
        self._last_purge = 0.0

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=3.0)

    def log_once(self, now: Optional[float] = None) -> int:
        """Enregistre les événements courants (au plus un par clé et par intervalle)."""
        now = time.time() if now is None else now
        rows = []
        for cam in list(self._get_cams().values()):
            for ev in list(cam.latest_events or []):
                mode = ev.get("mode") or "UNK"
                key = (ev.get("camera_id"), ev.get("track_id"), mode)
                if now - self.last.get(key, 0.0) < self.log_every_sec:
                    continue
                self.last[key] = now
                rows.append(Detection(
                    camera_id=str(ev.get("camera_id")),
                    track_id=_opt(ev, "track_id", int),
                    global_id=_opt(ev, "global_id", int),
                    mode=str(mode),
                    label=ev.get("label"),
                    target_score=_opt(ev, "target_score", float),
                    db_score=_opt(ev, "db_score", float),
                    x=_opt(ev, "x", float),
                    y=_opt(ev, "y", float),
                    timestamp=utcnow(),
                ))
        if rows:
            db = SessionLocal()
            try:
                db.add_all(rows)
                db.commit()
            finally:
                db.close()

        # Évite que le dictionnaire grossisse indéfiniment (une clé par piste vue).
        if len(self.last) > 500:
            self.last = {k: t for k, t in self.last.items() if now - t < STALE_KEY_SEC}
        return len(rows)

    def purge_if_due(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        if self.retention_days <= 0 or now - self._last_purge < PURGE_EVERY_SEC:
            return 0
        self._last_purge = now
        deleted = purge_old_detections(self.retention_days)
        if deleted:
            log.info("Rétention : %d observation(s) de plus de %d jours supprimée(s)",
                     deleted, self.retention_days)
        return deleted

    def loop(self):
        while self.running:
            try:
                self.log_once()
                self.purge_if_due()
            except Exception:
                log.exception("Impossible d'enregistrer les observations")
            time.sleep(0.25)
