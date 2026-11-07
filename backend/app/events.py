"""Fil d'événements de supervision, persistant (table ``events``).

Un événement est un fait horodaté : cible repérée, changement de caméra, caméra hors ligne,
personne enrôlée reconnue… Les pages interrogent ``recent()`` ; le WebSocket ne diffuse que le
dernier identifiant (``last_id``) et les pages viennent chercher les nouveautés (``after_id``).
"""
import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func

from .db import Event, SessionLocal, to_iso_utc

log = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "critical")

# Types d'événements émis par la supervision.
TARGET_SEEN = "target_seen"
TARGET_MOVED = "target_moved"
TARGET_LOST = "target_lost"
PERSON_RECOGNIZED = "person_recognized"
PERSON_INACTIVE = "person_inactive_seen"      # personne dont la fiche est désactivée, reconnue par une caméra
CAMERA_OFFLINE = "camera_offline"
CAMERA_ONLINE = "camera_online"
CAMERA_ADDED = "camera_added"
CAMERA_REMOVED = "camera_removed"
CAMERA_ENABLED = "camera_enabled"
CAMERA_DISABLED = "camera_disabled"
CAMERA_OBSTRUCTED = "camera_obstructed"      # image uniforme (objectif couvert, écran noir…)
CAMERA_CLEARED = "camera_cleared"
CAMERA_FROZEN = "camera_frozen"              # images strictement identiques (flux figé)
LINE_CROSSED = "line_crossed"
ZONE_OVERCROWDED = "zone_overcrowded"
ZONE_CLEARED = "zone_cleared"
ZONE_INTRUSION = "zone_intrusion"
ZONE_LOITERING = "zone_loitering"            # présence prolongée dans une zone

# Familles utilisées par le filtre de l'interface.
FAMILIES = {
    "targets": (TARGET_SEEN, TARGET_MOVED, TARGET_LOST),
    "people": (PERSON_RECOGNIZED, PERSON_INACTIVE),
    "cameras": (CAMERA_OFFLINE, CAMERA_ONLINE, CAMERA_ADDED, CAMERA_REMOVED, CAMERA_ENABLED, CAMERA_DISABLED,
                CAMERA_OBSTRUCTED, CAMERA_CLEARED, CAMERA_FROZEN),
    "analytics": (LINE_CROSSED, ZONE_OVERCROWDED, ZONE_CLEARED, ZONE_INTRUSION, ZONE_LOITERING),
}


def _to_dict(row: Event) -> Dict[str, Any]:
    try:
        payload = json.loads(row.payload) if row.payload else {}
    except ValueError:
        payload = {}
    return {"id": row.id, "timestamp": to_iso_utc(row.timestamp), "type": row.type, "severity": row.severity,
            "camera_id": row.camera_id, "title": row.title, "payload": payload}


class EventFeed:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_id = 0
        self._listeners = []
        self.reload_last_id()

    def add_listener(self, fn):
        """Appelle ``fn(event_dict)`` après chaque événement enregistré (ex. centre d'alertes). Les erreurs sont ignorées."""
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def reload_last_id(self) -> int:
        db = SessionLocal()
        try:
            last = db.query(func.max(Event.id)).scalar() or 0
        finally:
            db.close()
        with self._lock:
            self._last_id = max(self._last_id, last)
        return self._last_id

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._last_id

    def emit(self, type: str, title: str, severity: str = "info", camera_id: Optional[str] = None,
             timestamp: Optional[datetime] = None, **payload) -> Optional[Dict[str, Any]]:
        """Enregistre un événement. Ne lève jamais : un échec d'écriture ne doit pas bloquer la supervision."""
        if severity not in SEVERITIES:
            severity = "info"
        db = SessionLocal()
        try:
            row = Event(type=type, severity=severity, camera_id=camera_id, title=title[:300],
                        payload=json.dumps(payload, ensure_ascii=False, default=str) if payload else None)
            if timestamp is not None:
                row.timestamp = timestamp
            db.add(row)
            db.commit()
            db.refresh(row)
            with self._lock:
                self._last_id = max(self._last_id, row.id)
                listeners = list(self._listeners)
            out = _to_dict(row)
        except Exception:
            db.rollback()
            log.exception("Événement non enregistré : %s", title)
            return None
        finally:
            db.close()
        for fn in listeners:
            try:
                fn(out)
            except Exception:
                log.exception("Écouteur d'événements en échec")
        return out

    def recent(self, limit: int = 50, after_id: Optional[int] = None, before_id: Optional[int] = None,
               family: Optional[str] = None, types: Optional[Sequence[str]] = None,
               camera_id: Optional[str] = None, severity: Optional[str] = None) -> List[Dict[str, Any]]:
        """Événements, du plus récent au plus ancien."""
        wanted = list(types or [])
        if family:
            wanted += list(FAMILIES.get(family, ()))
        db = SessionLocal()
        try:
            q = db.query(Event)
            if after_id is not None:
                q = q.filter(Event.id > after_id)
            if before_id is not None:
                q = q.filter(Event.id < before_id)
            if wanted:
                q = q.filter(Event.type.in_(wanted))
            if camera_id:
                q = q.filter(Event.camera_id == camera_id)
            if severity:
                q = q.filter(Event.severity == severity)
            return [_to_dict(r) for r in q.order_by(Event.id.desc()).limit(limit).all()]
        finally:
            db.close()
