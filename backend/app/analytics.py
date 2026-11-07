"""Événements vidéo : lignes virtuelles, zones, occupation, affluence, caméra masquée ou figée.

Le moteur lit les pistes déjà produites par le suivi (``cam.latest_events``) : il ne relance aucun modèle.
Référence de position d'une personne = le point au sol (milieu du bas de son cadre).
Les compteurs sont des **estimations** : une personne mal suivie (piste perdue, identifiant qui change) peut
être comptée deux fois ou manquée ; ils ne remplacent pas un compteur de passage dédié.
"""
import json
import logging
import threading
import time
import zlib  # noqa: F401  (utilisé par le pipeline ; gardé ici pour la documentation des mesures)
from datetime import datetime, timedelta

from sqlalchemy.orm.exc import StaleDataError
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, events
from .db import SessionLocal, Zone, ZoneStat, to_iso_utc, utcnow
from .geometry import crossing, is_simple_polygon, point_in_polygon, polygon_area, side

log = logging.getLogger(__name__)

MAX_ZONES_PER_CAMERA = 20
MAX_NAME = 40
MAX_LABEL = 30
BUCKET_SEC = 300


class ZoneError(ValueError):
    def __init__(self, message: str, field: Optional[str] = None, status: int = 400):
        super().__init__(message)
        self.field, self.status = field, status


# ---- saisie --------------------------------------------------------------------------------------
def _text(value: Any, field: str, limit: int, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise ZoneError("Ce champ est obligatoire.", field)
        return None
    if not isinstance(value, str):
        raise ZoneError("Un texte est attendu.", field)
    value = value.strip()
    if not value and required:
        raise ZoneError("Ce champ est obligatoire.", field)
    if len(value) > limit:
        raise ZoneError(f"Limité à {limit} caractères.", field)
    return value or None


def clean_points(raw: Any, kind: str) -> List[List[float]]:
    if not isinstance(raw, list) or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in raw):
        raise ZoneError("Les points doivent être une liste de paires [x, y].", "points")
    pts = []
    for p in raw:
        for v in p:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0 or v != v:
                raise ZoneError("Chaque coordonnée doit être comprise entre 0 et 1.", "points")
        pts.append([round(float(p[0]), 4), round(float(p[1]), 4)])
    if kind == "line":
        if len(pts) != 2:
            raise ZoneError("Une ligne a exactement 2 points.", "points")
        if abs(pts[0][0] - pts[1][0]) + abs(pts[0][1] - pts[1][1]) < 0.02:
            raise ZoneError("La ligne est trop courte.", "points")
    else:
        if not 3 <= len(pts) <= 12:
            raise ZoneError("Une zone a de 3 à 12 points.", "points")
        tup = [tuple(p) for p in pts]
        if polygon_area(tup) < 0.001:
            raise ZoneError("La zone est trop petite ou aplatie.", "points")
        if not is_simple_polygon(tup):
            raise ZoneError("Les côtés de la zone ne doivent pas se croiser.", "points")
    return pts


def clean_zone(data: Dict[str, Any], creating: bool, current_kind: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    kind = data.get("kind", current_kind)
    if creating or "kind" in data:
        if kind not in ("line", "zone"):
            raise ZoneError("Le type doit être « line » ou « zone ».", "kind")
        if not creating and kind != current_kind:
            raise ZoneError("Le type d'un tracé ne peut pas être modifié.", "kind")
        out["kind"] = kind
    kind = kind or current_kind
    if creating or "name" in data:
        out["name"] = _text(data.get("name"), "name", MAX_NAME, required=True)
    if creating or "points" in data:
        out["points"] = clean_points(data.get("points"), kind)
    if "label_in" in data or "label_out" in data:
        if kind != "line":
            raise ZoneError("Les libellés de passage ne concernent que les lignes.", "label_in")
        out["label_in"] = _text(data.get("label_in"), "label_in", MAX_LABEL)
        out["label_out"] = _text(data.get("label_out"), "label_out", MAX_LABEL)
    if "capacity" in data:
        if kind != "zone":
            raise ZoneError("La capacité ne concerne que les zones.", "capacity")
        cap = data["capacity"]
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 500):
            raise ZoneError("La capacité doit être un entier de 1 à 500.", "capacity")
        out["capacity"] = cap
    if "restricted" in data:
        if kind != "zone":
            raise ZoneError("« Zone interdite » ne concerne que les zones.", "restricted")
        if not isinstance(data["restricted"], bool):
            raise ZoneError("« restricted » doit être vrai ou faux.", "restricted")
        out["restricted"] = data["restricted"]
    if "max_dwell_sec" in data:
        if kind != "zone":
            raise ZoneError("La présence maximale ne concerne que les zones.", "max_dwell_sec")
        d = data["max_dwell_sec"]
        if d is not None and (isinstance(d, bool) or not isinstance(d, int) or not 5 <= d <= 86400):
            raise ZoneError("La présence maximale doit être un entier de 5 à 86400 secondes.", "max_dwell_sec")
        out["max_dwell_sec"] = d
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ZoneError("« enabled » doit être vrai ou faux.", "enabled")
        out["enabled"] = data["enabled"]
    if creating and "camera_id" not in data:
        raise ZoneError("La caméra est obligatoire.", "camera_id")
    return out


def view(z: Zone) -> Dict[str, Any]:
    return {"id": z.id, "camera_id": z.camera_id, "kind": z.kind, "name": z.name, "points": json.loads(z.points),
            "label_in": z.label_in or "", "label_out": z.label_out or "", "capacity": z.capacity,
            "restricted": bool(z.restricted), "max_dwell_sec": z.max_dwell_sec, "enabled": bool(z.enabled),
            "created_at": to_iso_utc(z.created_at) if z.created_at else None}


def list_zones(db, camera_id: Optional[str] = None) -> List[Dict[str, Any]]:
    q = db.query(Zone)
    if camera_id:
        q = q.filter(Zone.camera_id == camera_id)
    return [view(z) for z in q.order_by(Zone.camera_id, Zone.id).all()]


def create_zone(db, data: Dict[str, Any], camera_ids: List[str]) -> Zone:
    fields = clean_zone(data, creating=True)
    camera_id = data.get("camera_id")
    if camera_id not in camera_ids:
        raise ZoneError("Caméra inconnue.", "camera_id", 404)
    if db.query(Zone).filter(Zone.camera_id == camera_id).count() >= MAX_ZONES_PER_CAMERA:
        raise ZoneError(f"Maximum {MAX_ZONES_PER_CAMERA} tracés par caméra.", status=409)
    row = Zone(camera_id=camera_id, kind=fields["kind"], name=fields["name"], points=json.dumps(fields["points"]),
               label_in=fields.get("label_in"), label_out=fields.get("label_out"),
               capacity=fields.get("capacity") if fields["kind"] == "zone" else None,
               restricted=bool(fields.get("restricted", False)) if fields["kind"] == "zone" else False,
               max_dwell_sec=fields.get("max_dwell_sec") if fields["kind"] == "zone" else None,
               enabled=fields.get("enabled", True))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_zone(db, zone_id: int) -> Zone:
    row = db.get(Zone, zone_id)
    if row is None:
        raise ZoneError("Tracé introuvable.", status=404)
    return row


def update_zone(db, zone_id: int, data: Dict[str, Any]) -> Zone:
    row = get_zone(db, zone_id)
    if "camera_id" in data and data["camera_id"] != row.camera_id:
        raise ZoneError("Un tracé ne peut pas changer de caméra : supprimez-le et recréez-le.", "camera_id")
    fields = clean_zone(data, creating=False, current_kind=row.kind)
    for key, value in fields.items():
        if key == "points":
            value = json.dumps(value)
        if key == "kind":
            continue
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


def delete_zone(db, zone_id: int) -> None:
    row = get_zone(db, zone_id)
    db.query(ZoneStat).filter(ZoneStat.zone_id == zone_id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()


def bucket_start(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % (BUCKET_SEC // 60), second=0, microsecond=0)


def series(db, zone_id: int, since: datetime, until: datetime) -> List[Dict[str, Any]]:
    rows = (db.query(ZoneStat).filter(ZoneStat.zone_id == zone_id, ZoneStat.bucket >= since, ZoneStat.bucket <= until)
            .order_by(ZoneStat.bucket).all())
    return [{"bucket": to_iso_utc(r.bucket), "entries": r.entries, "exits": r.exits,
             "occupancy_avg": round(r.occupancy_sum / r.samples, 2) if r.samples else 0.0,
             "occupancy_max": r.occupancy_max} for r in rows]


def overview(db, since: datetime, until: datetime) -> List[Dict[str, Any]]:
    """Totaux par tracé sur la période (le total d'un jour est la somme de ses tranches)."""
    out = []
    stats: Dict[int, Dict[str, Any]] = {}
    for r in db.query(ZoneStat).filter(ZoneStat.bucket >= since, ZoneStat.bucket <= until).all():
        s = stats.setdefault(r.zone_id, {"entries": 0, "exits": 0, "occupancy_max": 0, "peak_bucket": None})
        s["entries"] += r.entries
        s["exits"] += r.exits
        if r.occupancy_max > s["occupancy_max"]:
            s["occupancy_max"], s["peak_bucket"] = r.occupancy_max, r.bucket
    for z in db.query(Zone).order_by(Zone.camera_id, Zone.id).all():
        s = stats.get(z.id, {"entries": 0, "exits": 0, "occupancy_max": 0, "peak_bucket": None})
        out.append({"zone_id": z.id, "camera_id": z.camera_id, "kind": z.kind, "name": z.name,
                    "entries": s["entries"], "exits": s["exits"], "occupancy_max": s["occupancy_max"],
                    "peak_bucket": to_iso_utc(s["peak_bucket"]) if s["peak_bucket"] else None})
    return out


# ---- moteur ---------------------------------------------------------------------------------------
class AnalyticsEngine:
    def __init__(self, get_cams: Callable[[], Dict[str, Any]], feed: "events.EventFeed",
                 get_meta: Callable[[], Dict[str, Dict[str, Any]]], session_factory=SessionLocal,
                 interval: float = 0.2, lost_after_sec: float = 2.0, confirm_frames: int = 2,
                 over_sec: float = 3.0, line_cooldown_sec: float = 2.0, intrusion_cooldown_sec: float = 20.0,
                 obstruct_std: float = 4.0, obstruct_after_sec: float = 10.0, frozen_after_sec: float = 15.0,
                 recover_after_sec: float = 3.0, flush_every_sec: float = 10.0):
        self._get_cams, self.feed, self._get_meta = get_cams, feed, get_meta
        self._session = session_factory
        self.interval, self.lost_after, self.confirm_frames = interval, lost_after_sec, confirm_frames
        self.over_sec, self.line_cooldown, self.intrusion_cooldown = over_sec, line_cooldown_sec, intrusion_cooldown_sec
        self.obstruct_std, self.obstruct_after, self.frozen_after = obstruct_std, obstruct_after_sec, frozen_after_sec
        self.recover_after, self.flush_every = recover_after_sec, flush_every_sec
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()                  # un seul enregistrement d'affluence à la fois
        self._zones: Dict[int, Dict[str, Any]] = {}          # id -> définition
        self._state: Dict[int, Dict[str, Any]] = {}          # id -> état d'exécution
        self._seq: Dict[str, Any] = {}                        # caméra -> dernière image traitée
        self._cam_health: Dict[str, Dict[str, Any]] = {}
        self._buckets: Dict[Tuple[int, datetime], Dict[str, float]] = {}
        self._last_flush = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- définitions ------------------------------------------------------------------------------
    def reload(self):
        db = self._session()
        try:
            rows = [view(z) for z in db.query(Zone).filter(Zone.enabled.is_(True)).all()]
        finally:
            db.close()
        with self._lock:
            self._zones = {z["id"]: z for z in rows}
            self._state = {k: v for k, v in self._state.items() if k in self._zones}
            self._buckets = {k: v for k, v in self._buckets.items() if k[0] in self._zones}   # tracé supprimé : rien à écrire
            for zid in self._zones:
                self._state.setdefault(zid, self._new_state(self._zones[zid]))
            for zid, z in self._zones.items():           # un tracé modifié repart de zéro (ancien état faussé)
                if self._state[zid].get("sig") != (z["kind"], json.dumps(z["points"])):
                    self._state[zid] = self._new_state(z)

    @staticmethod
    def _new_state(z: Dict[str, Any]) -> Dict[str, Any]:
        return {"sig": (z["kind"], json.dumps(z["points"])), "tracks": {}, "count_in": 0, "count_out": 0,
                "inside": {}, "occupancy": 0, "over_since": None, "ok_since": None, "over_announced": False,
                "last_intrusion": -1e9, "dwell_total": 0.0, "dwell_n": 0, "since": time.time()}

    def _label(self, cam_id: str) -> str:
        return ((self._get_meta() or {}).get(cam_id) or {}).get("label", cam_id)

    def _add_bucket(self, zid: int, now: float, entries=0, exits=0, occupancy: Optional[int] = None):
        start = bucket_start(datetime.utcfromtimestamp(now))
        b = self._buckets.setdefault((zid, start), {"entries": 0, "exits": 0, "samples": 0, "sum": 0.0, "max": 0})
        b["entries"] += entries
        b["exits"] += exits
        if occupancy is not None:
            b["samples"] += 1
            b["sum"] += occupancy
            b["max"] = max(b["max"], occupancy)

    # ---- une passe -----------------------------------------------------------------------------------
    def step(self, now: Optional[float] = None):
        now = time.time() if now is None else now
        cams = self._get_cams()
        with self._lock:
            for cam_id in [c for c in {*self._seq, *self._cam_health} if c not in cams]:
                self._seq.pop(cam_id, None)
                self._cam_health.pop(cam_id, None)
            for cam_id, cam in list(cams.items()):
                try:
                    self._check_health(cam_id, cam, now)
                    self._process_camera(cam_id, cam, now)
                except Exception:
                    log.exception("Analyse de la caméra %s", cam_id)
            if now - self._last_flush >= self.flush_every:
                self._flush()
                self._last_flush = now

    def _process_camera(self, cam_id: str, cam: Any, now: float):
        zones = [z for z in self._zones.values() if z["camera_id"] == cam_id]
        if not zones or cam.status(now) != "online":
            return
        seq = getattr(cam, "frame_seq", None)
        if seq is not None and self._seq.get(cam_id) == seq:
            return                                           # même image que la fois précédente : rien de neuf
        self._seq[cam_id] = seq
        persons = []
        for ev in list(cam.latest_events or []):
            tid = ev.get("track_id")
            fx, fy = ev.get("fx"), ev.get("fy")
            if tid is None or fx is None or fy is None:
                continue
            persons.append((tid, (float(fx), float(fy)), ev))
        for z in zones:
            if z["kind"] == "line":
                self._process_line(z, cam_id, persons, now)
            else:
                self._process_zone(z, cam_id, persons, now)

    def _process_line(self, z: Dict[str, Any], cam_id: str, persons, now: float):
        st = self._state[z["id"]]
        a, b = tuple(z["points"][0]), tuple(z["points"][1])
        seen = set()
        for tid, pt, ev in persons:
            seen.add(tid)
            tr = st["tracks"].setdefault(tid, {"ref": None, "cool": -1e9, "seen": now})
            tr["seen"] = now
            if side(a, b, pt) == 0:
                continue                                      # pile sur la ligne : on garde l'ancienne référence
            if tr["ref"] is not None:
                way = crossing(a, b, tr["ref"], pt)
                if way and now - tr["cool"] >= self.line_cooldown:
                    tr["cool"] = now
                    self._count_line(z, cam_id, way, tid, now)
            tr["ref"] = pt
        for tid in [t for t, v in st["tracks"].items() if now - v["seen"] > self.lost_after * 3]:
            del st["tracks"][tid]

    def _count_line(self, z: Dict[str, Any], cam_id: str, way: int, tid: Any, now: float):
        st = self._state[z["id"]]
        if way > 0:
            st["count_in"] += 1
            self._add_bucket(z["id"], now, entries=1)
            name = z["label_in"] or "Sens 1"
        else:
            st["count_out"] += 1
            self._add_bucket(z["id"], now, exits=1)
            name = z["label_out"] or "Sens 2"
        self.feed.emit(events.LINE_CROSSED, f"{z['name']} : {name} ({self._label(cam_id)})", camera_id=cam_id,
                       zone_id=z["id"], zone=z["name"], direction=name, way=way, track_id=tid)

    def _process_zone(self, z: Dict[str, Any], cam_id: str, persons, now: float):
        st = self._state[z["id"]]
        poly = [tuple(p) for p in z["points"]]
        present = {}
        for tid, pt, ev in persons:
            if point_in_polygon(pt, poly):
                present[tid] = ev
        for tid, ev in present.items():
            rec = st["inside"].get(tid)
            if rec is None:
                st["inside"][tid] = {"frames": 1, "since": now, "last": now, "confirmed": False, "ev": ev}
                rec = st["inside"][tid]
            else:
                rec["frames"] += 1
                rec["last"] = now
                rec["ev"] = ev
            if not rec["confirmed"] and rec["frames"] >= self.confirm_frames:
                rec["confirmed"] = True
                st["count_in"] += 1
                self._add_bucket(z["id"], now, entries=1)
                if z["restricted"] and now - st["last_intrusion"] >= self.intrusion_cooldown:
                    st["last_intrusion"] = now
                    extra = {}
                    if ev.get("mode") == "DB" and config.HISTORY_SHOW_IDENTITIES and ev.get("participant_id") is not None:
                        extra = {"participant_id": ev["participant_id"], "match_status": ev.get("match_status")}
                    self.feed.emit(events.ZONE_INTRUSION, f"Intrusion : {z['name']} ({self._label(cam_id)})",
                                   severity="warning", camera_id=cam_id, zone_id=z["id"], zone=z["name"],
                                   mode=ev.get("mode"), **extra)
        limit = z.get("max_dwell_sec")
        if limit:
            for tid, rec in st["inside"].items():
                if (rec["confirmed"] and not rec.get("loitered") and tid in present
                        and now - rec["since"] >= limit):
                    rec["loitered"] = True       # une seule annonce par séjour
                    self.feed.emit(events.ZONE_LOITERING,
                                   f"Présence prolongée : {z['name']} ({int(now - rec['since'])} s, seuil {limit} s) — "
                                   f"{self._label(cam_id)}", severity="warning", camera_id=cam_id, zone_id=z["id"],
                                   zone=z["name"], dwell_sec=int(now - rec["since"]), max_dwell_sec=limit,
                                   mode=rec["ev"].get("mode"))
        for tid in [t for t, r in st["inside"].items() if t not in present and now - r["last"] > self.lost_after]:
            rec = st["inside"].pop(tid)
            if rec["confirmed"]:
                st["count_out"] += 1
                st["dwell_total"] += max(0.0, rec["last"] - rec["since"])
                st["dwell_n"] += 1
                self._add_bucket(z["id"], now, exits=1)
        confirmed = sum(1 for r in st["inside"].values() if r["confirmed"] and r["last"] == now)
        st["occupancy"] = confirmed
        self._add_bucket(z["id"], now, occupancy=confirmed)
        cap = z["capacity"]
        if cap:
            if confirmed > cap:
                st["ok_since"] = None
                st["over_since"] = st["over_since"] or now
                if not st["over_announced"] and now - st["over_since"] >= self.over_sec:
                    st["over_announced"] = True
                    self.feed.emit(events.ZONE_OVERCROWDED,
                                   f"Affluence : {z['name']} ({confirmed} personnes pour {cap}) — {self._label(cam_id)}",
                                   severity="warning", camera_id=cam_id, zone_id=z["id"], zone=z["name"],
                                   occupancy=confirmed, capacity=cap)
            else:
                st["over_since"] = None
                st["ok_since"] = st["ok_since"] or now
                if st["over_announced"] and now - st["ok_since"] >= self.over_sec:
                    st["over_announced"] = False
                    self.feed.emit(events.ZONE_CLEARED, f"Affluence revenue à la normale : {z['name']}",
                                   camera_id=cam_id, zone_id=z["id"], zone=z["name"], occupancy=confirmed, capacity=cap)

    # ---- caméra masquée / figée --------------------------------------------------------------------
    def _check_health(self, cam_id: str, cam: Any, now: float):
        stats = getattr(cam, "frame_stats", None) or {}
        if cam.status(now) != "online" or not stats or now - stats.get("ts", 0) > 3.0:
            self._cam_health.pop(cam_id, None)
            return
        h = self._cam_health.setdefault(cam_id, {"dark_since": None, "ok_since": None, "crc": None, "same_since": now,
                                                  "obstructed": False, "frozen": False})
        bad = stats["std"] < self.obstruct_std
        if bad:
            h["ok_since"] = None
            h["dark_since"] = h["dark_since"] or now
            if not h["obstructed"] and now - h["dark_since"] >= self.obstruct_after:
                h["obstructed"] = True
                self.feed.emit(events.CAMERA_OBSTRUCTED,
                               f"Caméra masquée ou image uniforme : {self._label(cam_id)}", severity="warning",
                               camera_id=cam_id, brightness=round(stats["mean"]), contrast=round(stats["std"], 1))
        else:
            h["dark_since"] = None
            if h["obstructed"]:
                h["ok_since"] = h["ok_since"] or now
                if now - h["ok_since"] >= self.recover_after:
                    h["obstructed"] = False
                    self.feed.emit(events.CAMERA_CLEARED, f"Image rétablie : {self._label(cam_id)}", camera_id=cam_id)
        crc = stats.get("crc")
        if crc is not None and crc == h["crc"]:
            if not h["frozen"] and now - h["same_since"] >= self.frozen_after:
                h["frozen"] = True
                self.feed.emit(events.CAMERA_FROZEN, f"Flux figé (images identiques) : {self._label(cam_id)}",
                               severity="warning", camera_id=cam_id)
        else:
            h["crc"], h["same_since"] = crc, now
            if h["frozen"]:
                h["frozen"] = False
                self.feed.emit(events.CAMERA_CLEARED, f"Flux de nouveau actif : {self._label(cam_id)}", camera_id=cam_id)

    # ---- lecture / persistance ------------------------------------------------------------------------
    def snapshot(self) -> Dict[int, Dict[str, Any]]:
        with self._lock:
            out = {}
            for zid, z in self._zones.items():
                st = self._state[zid]
                item = {"zone_id": zid, "kind": z["kind"], "count_in": st["count_in"], "count_out": st["count_out"],
                        "since": st["since"]}
                if z["kind"] == "zone":
                    item.update(occupancy=st["occupancy"], capacity=z["capacity"], over=st["over_announced"],
                                avg_dwell_sec=round(st["dwell_total"] / st["dwell_n"], 1) if st["dwell_n"] else None)
                out[zid] = item
            health = {cid: {"obstructed": h["obstructed"], "frozen": h["frozen"]} for cid, h in self._cam_health.items()}
            return {"zones": out, "cameras": health}

    def _flush(self):
        with self._flush_lock:
            self._flush_locked()

    def _flush_locked(self):
        with self._lock:
            pending, self._buckets = self._buckets, {}
        if not pending:
            return
        db = self._session()
        try:
            for (zid, start), b in pending.items():
                row = db.query(ZoneStat).filter(ZoneStat.zone_id == zid, ZoneStat.bucket == start).first()
                if row is None:
                    row = ZoneStat(zone_id=zid, bucket=start, entries=0, exits=0, samples=0, occupancy_sum=0.0, occupancy_max=0)
                    db.add(row)
                row.entries += int(b["entries"])
                row.exits += int(b["exits"])
                row.samples += int(b["samples"])
                row.occupancy_sum += float(b["sum"])
                row.occupancy_max = max(row.occupancy_max or 0, int(b["max"]))
            db.commit()
        except StaleDataError:
            db.rollback()                                    # tracé supprimé pendant l'écriture : rien à conserver
            log.debug("Affluence ignorée : tracé supprimé entre-temps")
        except Exception:
            db.rollback()
            log.exception("Enregistrement de l'affluence")
        finally:
            db.close()

    def flush(self):
        self._flush()

    # ---- fil d'exécution -------------------------------------------------------------------------------
    def start(self):
        self.reload()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="analytics", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._flush()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                log.exception("Moteur d'analyse")
            self._stop.wait(self.interval)
