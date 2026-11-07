"""Enregistrement vidéo optionnel et relecture (Module 9).

- **Désactivé par défaut.** Un interrupteur général l'active ; ``NIIFYEEN_RECORDING_DISABLED=1`` l'interdit totalement.
- **Trois déclencheurs** : manuel (une caméra, pour une durée), sur événement (avec une mémoire tampon pour garder les
  secondes qui précèdent), sur plage horaire (jours et heures locales du serveur).
- **Format** : fichiers ``.nyr`` maison (suite d'images JPEG horodatées), lus par l'application elle-même. Aucun codec
  vidéo n'est nécessaire à l'enregistrement ; l'export produit un AVI (Motion-JPEG) lisible par les lecteurs usuels.
- **Images brutes** (sans cadres ni noms incrustés) à la résolution du flux (640x360 par défaut) et 1 à 10 images par seconde.
- **Ne bloque pas l'analyse** : tout se passe dans un thread à part qui ne fait que copier des images déjà encodées.
- **Rétention** : durée et espace disque maximum configurables ; arrêt si l'espace libre devient trop faible.
"""
import json
import logging
import os
import re
import shutil
import struct
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from . import config, events as events_mod
from .db import Recording, Setting, SessionLocal, epoch_to_utc, to_iso_utc, utcnow

log = logging.getLogger(__name__)

MAGIC = b"NYR1"
_REC = struct.Struct("<dI")                       # horodatage (secondes Unix), taille de l'image
MAX_FRAME = 4 * 1024 * 1024
SETTINGS_KEY = "recording_settings"
EVENT_TYPES = tuple(t for fam in events_mod.FAMILIES.values() for t in fam)

DEFAULTS: Dict[str, Any] = {
    "enabled": False, "fps": 2, "pre_roll_sec": 5, "post_roll_sec": 20, "retention_days": 7, "max_storage_mb": 2000,
    "allow_export": True, "event_types": ["zone_intrusion", "zone_overcrowded", "person_inactive_seen", "target_seen"],
    "cameras": [], "schedules": [],
}
MAX_SCHEDULES = 20
MAX_MANUAL_MINUTES = 240
MAX_EXPORT_MINUTES = 15
MAX_EXPORT_FRAMES = 4000


class RecordingError(Exception):
    def __init__(self, message: str, status: int = 422, field: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.field = field


# ---- réglages -----------------------------------------------------------------------------------------------------------
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _int(data, key, lo, hi, label):
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise RecordingError(f"{label} : entier de {lo} à {hi} attendu.", field=key)
    return v


def validate_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise RecordingError("Réglages invalides.")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise RecordingError(f"Réglage inconnu : {sorted(unknown)[0]}", field=sorted(unknown)[0])
    out: Dict[str, Any] = {}
    for key in ("enabled", "allow_export"):
        if key in data:
            if not isinstance(data[key], bool):
                raise RecordingError("Vrai ou faux attendu.", field=key)
            out[key] = data[key]
    if "fps" in data:
        out["fps"] = _int(data, "fps", 1, 10, "Images par seconde")
    if "pre_roll_sec" in data:
        out["pre_roll_sec"] = _int(data, "pre_roll_sec", 0, 30, "Secondes avant l'événement")
    if "post_roll_sec" in data:
        out["post_roll_sec"] = _int(data, "post_roll_sec", 5, 300, "Secondes après l'événement")
    if "retention_days" in data:
        out["retention_days"] = _int(data, "retention_days", 1, 365, "Conservation (jours)")
    if "max_storage_mb" in data:
        out["max_storage_mb"] = _int(data, "max_storage_mb", 100, 200000, "Espace maximum (Mo)")
    if "event_types" in data:
        v = data["event_types"]
        if not isinstance(v, list) or len(v) > 40 or any(t not in EVENT_TYPES for t in v):
            raise RecordingError("Types d'événements invalides.", field="event_types")
        out["event_types"] = list(dict.fromkeys(v))
    if "cameras" in data:
        v = data["cameras"]
        if not isinstance(v, list) or len(v) > 64 or any(not isinstance(c, str) or not 0 < len(c) <= 100 for c in v):
            raise RecordingError("Liste de caméras invalide.", field="cameras")
        out["cameras"] = list(dict.fromkeys(v))
    if "schedules" in data:
        v = data["schedules"]
        if not isinstance(v, list) or len(v) > MAX_SCHEDULES:
            raise RecordingError(f"{MAX_SCHEDULES} plages au plus.", field="schedules")
        clean = []
        for sch in v:
            if not isinstance(sch, dict) or set(sch) - {"camera_id", "days", "start", "end"}:
                raise RecordingError("Plage horaire invalide.", field="schedules")
            cam, days, start, end = sch.get("camera_id"), sch.get("days"), sch.get("start"), sch.get("end")
            if not isinstance(cam, str) or not 0 < len(cam) <= 100:
                raise RecordingError("Caméra de la plage invalide (« * » pour toutes).", field="schedules")
            if (not isinstance(days, list) or not days or len(set(days)) != len(days)
                    or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days)):
                raise RecordingError("Jours invalides (0 = lundi … 6 = dimanche).", field="schedules")
            if not (isinstance(start, str) and _HHMM.match(start) and isinstance(end, str) and _HHMM.match(end)) or start == end:
                raise RecordingError("Heures invalides (HH:MM, début différent de la fin).", field="schedules")
            clean.append({"camera_id": cam, "days": sorted(days), "start": start, "end": end})
        out["schedules"] = clean
    return out


def load_settings(db) -> Dict[str, Any]:
    s = json.loads(json.dumps(DEFAULTS))
    row = db.get(Setting, SETTINGS_KEY)
    if row and row.value:
        try:
            saved = json.loads(row.value)
            if isinstance(saved, dict):
                s.update(validate_settings(saved))
        except (ValueError, RecordingError):
            log.warning("Réglages d'enregistrement illisibles : valeurs par défaut")
    return s


def save_settings(db, changes: Dict[str, Any]) -> Dict[str, Any]:
    clean = validate_settings(changes)
    if not clean:
        raise RecordingError("Aucun réglage à enregistrer.")
    if clean.get("enabled") and config.RECORDING_HARD_DISABLED:
        raise RecordingError("L'enregistrement est interdit par l'administrateur (NIIFYEEN_RECORDING_DISABLED).", 403, "enabled")
    s = load_settings(db)
    s.update(clean)
    row = db.get(Setting, SETTINGS_KEY)
    if row is None:
        db.add(Setting(key=SETTINGS_KEY, value=json.dumps(s)))
    else:
        row.value = json.dumps(s)
    db.commit()
    return s


def minutes_of(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def schedule_active(schedules: List[Dict[str, Any]], camera_id: str, now_local: datetime) -> bool:
    """Une plage est active pour la caméra à cet instant (heure locale). Une plage « 22:00-06:00 » passe minuit."""
    minute = now_local.hour * 60 + now_local.minute
    today, yesterday = now_local.weekday(), (now_local.weekday() - 1) % 7
    for sch in schedules:
        if sch["camera_id"] not in ("*", camera_id):
            continue
        a, b = minutes_of(sch["start"]), minutes_of(sch["end"])
        if a < b:
            if today in sch["days"] and a <= minute < b:
                return True
        elif (today in sch["days"] and minute >= a) or (yesterday in sch["days"] and minute < b):
            return True
    return False


# ---- fichiers -------------------------------------------------------------------------------------------------------------
def safe_name(camera_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", camera_id)[:40] or "cam"


class SegmentWriter:
    def __init__(self, path: Path):
        self.path = path
        self.frames = 0
        self.bytes = 0
        self.first_ts: Optional[float] = None
        self.last_ts: Optional[float] = None
        self._f = open(path, "wb")
        self._f.write(MAGIC)
        self.bytes = len(MAGIC)

    def write(self, ts: float, jpeg: bytes):
        if len(jpeg) > MAX_FRAME:
            return
        self._f.write(_REC.pack(ts, len(jpeg)))
        self._f.write(jpeg)
        self._f.flush()
        self.frames += 1
        self.bytes += _REC.size + len(jpeg)
        self.first_ts = ts if self.first_ts is None else self.first_ts
        self.last_ts = ts

    def close(self):
        try:
            self._f.close()
        except OSError:
            pass


_index_cache: Dict[str, Tuple[Tuple[int, float], List[Tuple[float, int, int]]]] = {}
_index_lock = threading.Lock()


def read_index(path: Path) -> List[Tuple[float, int, int]]:
    """[(horodatage, position, taille)] ; une fin de fichier tronquée est ignorée."""
    try:
        st = path.stat()
    except OSError:
        return []
    stamp = (st.st_size, st.st_mtime)
    key = str(path)
    with _index_lock:
        hit = _index_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    out: List[Tuple[float, int, int]] = []
    try:
        with open(path, "rb") as f:
            if f.read(4) != MAGIC:
                return []
            size = st.st_size
            pos = 4
            while pos + _REC.size <= size:
                f.seek(pos)
                head = f.read(_REC.size)
                if len(head) < _REC.size:
                    break
                ts, n = _REC.unpack(head)
                if n > MAX_FRAME or pos + _REC.size + n > size:
                    break
                out.append((ts, pos + _REC.size, n))
                pos += _REC.size + n
    except OSError:
        return []
    with _index_lock:
        if len(_index_cache) > 64:
            _index_cache.clear()
        _index_cache[key] = (stamp, out)
    return out


def read_frame(path: Path, entry: Tuple[float, int, int]) -> bytes:
    with open(path, "rb") as f:
        f.seek(entry[1])
        return f.read(entry[2])


def resolve_path(directory: Path, name: str) -> Path:
    """Chemin d'un segment, toujours à l'intérieur du dossier des enregistrements."""
    p = (directory / name).resolve()
    if directory.resolve() not in p.parents:
        raise RecordingError("Chemin d'enregistrement invalide.", 400)
    return p


# ---- lecture (base) -----------------------------------------------------------------------------------------------------------
def view(r: Recording) -> Dict[str, Any]:
    dur = ((r.ended_at - r.started_at).total_seconds() if r.ended_at else None)
    return {"id": r.id, "camera_id": r.camera_id, "started_at": to_iso_utc(r.started_at),
            "ended_at": to_iso_utc(r.ended_at) if r.ended_at else None, "duration_sec": round(dur, 1) if dur is not None else None,
            "trigger": r.trigger, "event_id": r.event_id, "reason": r.reason, "frames": r.frames, "bytes": r.bytes,
            "fps": r.fps, "status": r.status}


def list_recordings(db, camera_id: str = "", since: Optional[datetime] = None, until: Optional[datetime] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
    q = db.query(Recording)
    if camera_id:
        q = q.filter(Recording.camera_id == camera_id)
    if since:
        q = q.filter((Recording.ended_at.is_(None)) | (Recording.ended_at >= since))
    if until:
        q = q.filter(Recording.started_at <= until)
    return [view(r) for r in q.order_by(Recording.started_at.desc(), Recording.id.desc()).limit(max(1, min(limit, 500))).all()]


def get_recording(db, rec_id: int) -> Recording:
    r = db.get(Recording, rec_id)
    if r is None:
        raise RecordingError("Enregistrement introuvable.", 404)
    return r


def detail(db, rec_id: int, directory: Path) -> Dict[str, Any]:
    r = get_recording(db, rec_id)
    index = read_index(resolve_path(directory, r.path))
    t0 = index[0][0] if index else None
    out = view(r)
    out["frames"] = len(index) if index else r.frames
    out["t"] = [round(e[0] - t0, 2) for e in index] if index else []
    out["t0"] = to_iso_utc(epoch_to_utc(t0)) if t0 is not None else None
    return out


def frame_bytes(db, rec_id: int, n: int, directory: Path) -> bytes:
    r = get_recording(db, rec_id)
    path = resolve_path(directory, r.path)
    index = read_index(path)
    if not 0 <= n < len(index):
        raise RecordingError("Image introuvable.", 404)
    try:
        return read_frame(path, index[n])
    except OSError:
        raise RecordingError("Fichier d'enregistrement illisible.", 410)


def find_for_time(db, camera_id: str, when: datetime, directory: Path) -> Optional[Dict[str, Any]]:
    """Enregistrement de la caméra qui couvre cet instant, avec la position (en secondes) dans le segment."""
    rows = (db.query(Recording).filter(Recording.camera_id == camera_id, Recording.started_at <= when + timedelta(seconds=1))
            .filter((Recording.ended_at.is_(None)) | (Recording.ended_at >= when - timedelta(seconds=1)))
            .order_by(Recording.started_at.desc()).limit(3).all())
    for r in rows:
        offset = max(0.0, (when - r.started_at).total_seconds())
        return {"recording_id": r.id, "offset_sec": round(offset, 1), "started_at": to_iso_utc(r.started_at)}
    return None


def delete_recording(db, rec_id: int, directory: Path, active_ids=()) -> None:
    r = get_recording(db, rec_id)
    if r.id in active_ids or r.status == "recording":
        raise RecordingError("Cet enregistrement est en cours : arrêtez-le d'abord.", 409)
    try:
        resolve_path(directory, r.path).unlink()
    except (OSError, RecordingError):
        pass
    db.delete(r)
    db.commit()


def disk_usage(db, directory: Path) -> Dict[str, Any]:
    from sqlalchemy import func
    used = db.query(func.coalesce(func.sum(Recording.bytes), 0)).scalar() or 0
    probe = directory if directory.exists() else directory.parent
    try:
        free = shutil.disk_usage(probe if probe.exists() else Path(".")).free
    except OSError:
        free = None
    return {"used_bytes": int(used), "free_bytes": free, "segments": db.query(Recording).count()}


# ---- export AVI ---------------------------------------------------------------------------------------------------------------
def export_avi(db, camera_id: str, since: datetime, until: datetime, directory: Path) -> str:
    """Écrit un AVI Motion-JPEG des images de la caméra entre deux instants ; retourne le chemin temporaire."""
    import cv2
    import numpy as np
    if until <= since:
        raise RecordingError("La fin doit suivre le début.", field="end")
    if until - since > timedelta(minutes=MAX_EXPORT_MINUTES):
        raise RecordingError(f"Export limité à {MAX_EXPORT_MINUTES} minutes.", field="end")
    rows = (db.query(Recording).filter(Recording.camera_id == camera_id, Recording.started_at <= until)
            .filter((Recording.ended_at.is_(None)) | (Recording.ended_at >= since)).order_by(Recording.started_at).all())
    t0 = (since - datetime(1970, 1, 1)).total_seconds()
    t1 = (until - datetime(1970, 1, 1)).total_seconds()
    wanted: List[Tuple[Path, Tuple[float, int, int]]] = []
    for r in rows:
        path = resolve_path(directory, r.path)
        wanted += [(path, e) for e in read_index(path) if t0 <= e[0] <= t1]
    if not wanted:
        raise RecordingError("Aucune image enregistrée sur cette période.", 404)
    if len(wanted) > MAX_EXPORT_FRAMES:
        raise RecordingError("Trop d'images : réduisez la période.", field="end")
    span = max(1e-6, wanted[-1][1][0] - wanted[0][1][0])
    fps = max(1.0, min(10.0, (len(wanted) - 1) / span)) if len(wanted) > 1 else 1.0
    fd, tmp = tempfile.mkstemp(suffix=".avi", prefix="niifyeen-export-")
    os.close(fd)
    writer = None
    try:
        size = None
        for path, entry in wanted:
            img = cv2.imdecode(np.frombuffer(read_frame(path, entry), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            if size is None:
                size = (img.shape[1], img.shape[0])
                writer = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
                if not writer.isOpened():
                    raise RecordingError("Impossible de créer le fichier vidéo.", 500)
            elif (img.shape[1], img.shape[0]) != size:
                img = cv2.resize(img, size)
            writer.write(img)
        if writer is None:
            raise RecordingError("Aucune image lisible sur cette période.", 404)
    except Exception:
        if writer is not None:
            writer.release()
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    writer.release()
    return tmp


# ---- enregistreur -----------------------------------------------------------------------------------------------------------------
class _Session:
    def __init__(self, trigger: str, until: Optional[float], event_id: Optional[int], reason: str):
        self.trigger, self.until, self.event_id, self.reason = trigger, until, event_id, reason


class Recorder:
    """Thread d'enregistrement : échantillonne les images brutes, tient la mémoire tampon et écrit les segments."""

    def __init__(self, get_cams: Callable[[], Dict[str, Any]], feed, directory: Optional[Path] = None,
                 tick: float = 0.1, segment_sec: float = 600.0, retention_every_sec: float = 300.0,
                 min_free_bytes: Optional[int] = None, clock: Callable[[], float] = time.time,
                 now_local: Callable[[], datetime] = datetime.now, hard_disabled: Optional[bool] = None):
        self._get_cams = get_cams
        self.feed = feed
        self.directory = Path(directory or config.RECORDINGS_DIR)
        self.tick = tick
        self.segment_sec = segment_sec
        self.retention_every = retention_every_sec
        self.min_free = config.RECORDING_MIN_FREE_MB * 1024 * 1024 if min_free_bytes is None else min_free_bytes
        self._clock = clock
        self._now_local = now_local
        self.hard_disabled = config.RECORDING_HARD_DISABLED if hard_disabled is None else hard_disabled
        self._lock = threading.RLock()
        self._settings: Optional[Dict[str, Any]] = None
        self._rings: Dict[str, Deque[Tuple[float, bytes]]] = {}
        self._last_sample: Dict[str, float] = {}
        self._last_seq: Dict[str, Any] = {}
        self._sessions: Dict[str, _Session] = {}
        self._writers: Dict[str, Dict[str, Any]] = {}
        self._viewers: Dict[str, Any] = {}
        self._blocked_until: Dict[str, float] = {}
        self._last_retention = 0.0
        self.last_error: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- réglages
    def settings(self) -> Dict[str, Any]:
        with self._lock:
            if self._settings is None:
                db = SessionLocal()
                try:
                    self._settings = load_settings(db)
                finally:
                    db.close()
            return json.loads(json.dumps(self._settings))

    def reload(self):
        with self._lock:
            self._settings = None

    def enabled(self) -> bool:
        return bool(self.settings()["enabled"]) and not self.hard_disabled

    def allowed(self, camera_id: str, s: Optional[Dict[str, Any]] = None) -> bool:
        s = s or self.settings()
        return not s["cameras"] or camera_id in s["cameras"]

    # -- déclencheurs
    def start_manual(self, camera_id: str, minutes: int) -> Dict[str, Any]:
        if self.hard_disabled:
            raise RecordingError("L'enregistrement est interdit par l'administrateur.", 403)
        if not self.settings()["enabled"]:
            raise RecordingError("L'enregistrement est désactivé : activez-le dans les réglages.", 409)
        if camera_id not in (self._get_cams() or {}):
            raise RecordingError("Caméra inconnue ou désactivée.", 404, "camera_id")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_MANUAL_MINUTES:
            raise RecordingError(f"Durée : 1 à {MAX_MANUAL_MINUTES} minutes.", field="minutes")
        with self._lock:
            self._sessions[camera_id] = _Session("manual", self._clock() + minutes * 60, None, "Enregistrement manuel")
        return self.session_info(camera_id)

    def stop_manual(self, camera_id: str) -> bool:
        with self._lock:
            sess = self._sessions.get(camera_id)
            if sess is None or sess.trigger != "manual":
                return False
            del self._sessions[camera_id]
            return True

    def session_info(self, camera_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            sess = self._sessions.get(camera_id)
            if sess is None:
                return None
            return {"trigger": sess.trigger, "until": to_iso_utc(epoch_to_utc(sess.until)) if sess.until else None, "reason": sess.reason}

    def on_event(self, ev: Dict[str, Any]):
        """Écouteur du fil d'événements : un événement choisi démarre ou prolonge un enregistrement."""
        try:
            cam = ev.get("camera_id")
            if not cam or not self.enabled():
                return
            s = self.settings()
            if ev.get("type") not in s["event_types"] or not self.allowed(cam, s):
                return
            until = self._clock() + s["post_roll_sec"]
            with self._lock:
                cur = self._sessions.get(cam)
                if cur is None:
                    self._sessions[cam] = _Session("event", until, ev.get("id"), (ev.get("title") or ev.get("type") or "")[:200])
                elif cur.until is not None and cur.until < until:
                    cur.until = until
        except Exception:
            log.exception("Enregistrement sur événement")

    # -- boucle
    def _raw_viewer(self, cam_id: str, cam: Any, want: bool):
        has = self._viewers.get(cam_id) is cam
        try:
            if want and not has:
                cam.add_raw_viewer(); self._viewers[cam_id] = cam
            elif not want and has:
                cam.remove_raw_viewer(); del self._viewers[cam_id]
        except Exception:
            self._viewers.pop(cam_id, None)

    def step(self, now: Optional[float] = None):
        now = self._clock() if now is None else now
        cams = self._get_cams() or {}
        with self._lock:
            enabled = self.enabled()
            s = self.settings()
            for cam_id in [c for c in {*self._writers, *self._viewers, *self._rings, *self._sessions} if c not in cams or not enabled]:
                self._close(cam_id, now)
                self._raw_viewer(cam_id, self._viewers.get(cam_id), False) if cam_id in self._viewers else None
                self._rings.pop(cam_id, None); self._last_seq.pop(cam_id, None)
                if not enabled or cam_id not in cams:
                    self._sessions.pop(cam_id, None)
            if not enabled:
                return
            buffer = s["pre_roll_sec"] > 0 and bool(s["event_types"])
            for cam_id, cam in list(cams.items()):
                try:
                    self._step_camera(cam_id, cam, s, buffer, now)
                except OSError as exc:
                    self.last_error = f"{type(exc).__name__}"
                    log.error("Enregistrement de %s interrompu : %s", cam_id, type(exc).__name__)
                    self._close(cam_id, now, error=True)
                    self._blocked_until[cam_id] = now + 30
                except Exception:
                    log.exception("Enregistrement de %s", cam_id)
            if now - self._last_retention >= self.retention_every:
                self._last_retention = now
                try:
                    self.apply_retention(now)
                except Exception:
                    log.exception("Rétention des enregistrements")

    def _step_camera(self, cam_id: str, cam: Any, s: Dict[str, Any], buffer: bool, now: float):
        if not self.allowed(cam_id, s):
            self._close(cam_id, now); self._raw_viewer(cam_id, cam, False); self._rings.pop(cam_id, None)
            self._sessions.pop(cam_id, None)
            return
        sess = self._sessions.get(cam_id)
        if sess and sess.until is not None and now > sess.until:
            self._sessions.pop(cam_id, None); sess = None
        scheduled = schedule_active(s["schedules"], cam_id, self._now_local())
        recording = sess is not None or scheduled
        if now < self._blocked_until.get(cam_id, 0):
            recording = False
        self._raw_viewer(cam_id, cam, recording or buffer)
        # échantillonnage : une nouvelle image brute, au plus `fps` par seconde
        seq = getattr(cam, "raw_seq", None)
        fresh = (cam.status(now) == "online" and getattr(cam, "latest_jpeg_raw", None) is not None
                 and seq == getattr(cam, "frame_seq", None) and seq != self._last_seq.get(cam_id)
                 and now - self._last_sample.get(cam_id, 0) >= 1.0 / s["fps"])
        frame = None
        if fresh:
            self._last_seq[cam_id] = seq
            self._last_sample[cam_id] = now
            frame = (getattr(cam, "last_frame_ts", None) or now, cam.latest_jpeg_raw)
            if buffer:
                ring = self._rings.setdefault(cam_id, deque())
                ring.append(frame)
                while ring and ring[0][0] < frame[0] - s["pre_roll_sec"] - 1:
                    ring.popleft()
        w = self._writers.get(cam_id)
        if w is not None and (not recording or now - w["opened"] >= self.segment_sec):
            self._close(cam_id, now)
            w = None
        if recording and w is None and (frame is not None or self._rings.get(cam_id)):
            trigger = sess.trigger if sess else "schedule"
            w = self._open(cam_id, trigger, sess.event_id if sess else None, sess.reason if sess else "Plage horaire", s, now)
            if w is not None and trigger == "event":           # secondes précédant l'événement
                for ts, jpg in list(self._rings.get(cam_id, ())):
                    if frame is None or ts < frame[0]:
                        w["writer"].write(ts, jpg)
        if w is not None and frame is not None:
            w["writer"].write(frame[0], frame[1])

    def _open(self, cam_id: str, trigger: str, event_id: Optional[int], reason: str, s: Dict[str, Any], now: float):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.directory).free
        except OSError:
            raise
        if free < self.min_free:
            self.last_error = "espace disque insuffisant"
            log.error("Espace disque insuffisant : enregistrement de %s non démarré", cam_id)
            self._blocked_until[cam_id] = now + 60
            return None
        started = epoch_to_utc(now)
        db = SessionLocal()
        try:
            rec = Recording(camera_id=cam_id, started_at=started, trigger=trigger, event_id=event_id, reason=reason,
                            path="pending", fps=float(s["fps"]), status="recording")
            db.add(rec); db.flush()
            name = f"{safe_name(cam_id)}_{started:%Y%m%d_%H%M%S}_{rec.id}.nyr"
            rec.path = name
            writer = SegmentWriter(self.directory / name)
            db.commit()
            rec_id = rec.id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        w = {"id": rec_id, "writer": writer, "opened": now}
        self._writers[cam_id] = w
        return w

    def _close(self, cam_id: str, now: float, error: bool = False):
        w = self._writers.pop(cam_id, None)
        if w is None:
            return
        w["writer"].close()
        db = SessionLocal()
        try:
            rec = db.get(Recording, w["id"])
            if rec is not None:
                wr = w["writer"]
                rec.frames, rec.bytes = wr.frames, wr.bytes
                rec.started_at = epoch_to_utc(wr.first_ts) if wr.first_ts else rec.started_at
                rec.ended_at = epoch_to_utc(wr.last_ts) if wr.last_ts else epoch_to_utc(now)
                rec.status = "error" if error else "done"
                db.commit()
        except Exception:
            db.rollback()
            log.exception("Fermeture d'un enregistrement")
        finally:
            db.close()

    def active(self) -> Dict[str, int]:
        with self._lock:
            return {cam: w["id"] for cam, w in self._writers.items()}

    def status(self) -> Dict[str, Any]:
        s = self.settings()
        db = SessionLocal()
        try:
            usage = disk_usage(db, self.directory)
        finally:
            db.close()
        with self._lock:
            sessions = {c: self.session_info(c) for c in list(self._sessions)}
            recording = list(self._writers)
        return {"enabled": s["enabled"] and not self.hard_disabled, "hard_disabled": self.hard_disabled,
                "recording": recording, "sessions": sessions, "last_error": self.last_error, **usage,
                "max_bytes": s["max_storage_mb"] * 1024 * 1024}

    # -- rétention
    def apply_retention(self, now: Optional[float] = None) -> int:
        s = self.settings()
        cutoff = epoch_to_utc(now or self._clock()) - timedelta(days=s["retention_days"])
        active = set(self.active().values())
        removed = 0
        db = SessionLocal()
        try:
            def drop(r):
                try:
                    resolve_path(self.directory, r.path).unlink()
                except (OSError, RecordingError):
                    pass
                db.delete(r)

            for r in db.query(Recording).filter(Recording.id.notin_(active) if active else True,
                                                Recording.status != "recording",
                                                Recording.started_at < cutoff).all():
                drop(r); removed += 1
            db.commit()
            limit = s["max_storage_mb"] * 1024 * 1024
            from sqlalchemy import func
            total = db.query(func.coalesce(func.sum(Recording.bytes), 0)).scalar() or 0
            if total > limit:
                for r in db.query(Recording).filter(Recording.status != "recording").order_by(Recording.started_at).all():
                    if r.id in active:
                        continue
                    total -= r.bytes
                    drop(r); removed += 1
                    if total <= limit:
                        break
                db.commit()
        finally:
            db.close()
        return removed

    def recover(self):
        """Après un arrêt brutal : les segments restés « en cours » sont refermés à partir de leur contenu."""
        db = SessionLocal()
        try:
            for r in db.query(Recording).filter(Recording.status == "recording").all():
                try:
                    path = resolve_path(self.directory, r.path)
                    index = read_index(path)
                except RecordingError:
                    index = []
                if index:
                    r.frames, r.bytes = len(index), index[-1][1] + index[-1][2]
                    r.started_at, r.ended_at = epoch_to_utc(index[0][0]), epoch_to_utc(index[-1][0])
                    r.status = "done"
                else:
                    r.status, r.ended_at = "error", r.started_at
            db.commit()
        finally:
            db.close()

    # -- cycle de vie
    def start(self):
        try:
            self.recover()
        except Exception:
            log.exception("Reprise des enregistrements")
        self.feed.add_listener(self.on_event)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self):
        self.feed.remove_listener(self.on_event)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            now = self._clock()
            for cam_id in list(self._writers):
                self._close(cam_id, now)
            for cam_id, cam in list(self._viewers.items()):
                self._raw_viewer(cam_id, cam, False)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("Enregistreur")
            self._stop.wait(self.tick)
