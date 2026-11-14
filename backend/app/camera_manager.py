"""Gestion des caméras : validation, persistance (table ``cameras``) et pilotage à chaud.

Depuis NiifYéén 2.0, la base est la source de vérité. ``cameras.json`` n'est lu qu'**une fois**, au tout
premier démarrage, pour importer la configuration existante ; ensuite, tout se fait depuis l'interface.

Les modifications sont appliquées sans redémarrer le serveur : seule la caméra concernée est arrêtée
puis relancée, et uniquement si un paramètre qui l'exige a changé (source, résolution, cadence, activation).
"""
import logging
import re
import threading
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import urlsplit

from . import config, events
from .db import Camera, Floor, SessionLocal, Setting, to_iso_utc, utcnow
from .supervision import mask_source

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https", "rtsp", "rtsps")
MAX_WEBCAM_INDEX = 31
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SEEDED_KEY = "cameras_seeded"
# Champs dont la modification impose de relancer le flux.
RESTART_FIELDS = ("source", "capture_width", "capture_height", "process_fps", "enabled")


class CameraError(ValueError):
    """Erreur de saisie ou d'état. ``status`` est le code HTTP à renvoyer."""

    def __init__(self, message: str, field: Optional[str] = None, status: int = 422):
        super().__init__(message)
        self.message, self.field, self.status = message, field, status


class CameraNotFound(CameraError):
    def __init__(self, camera_id: str):
        super().__init__(f"Caméra introuvable : {camera_id}", status=404)


class CameraConflict(CameraError):
    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message, field, status=409)


# ---- sources ---------------------------------------------------------------------------------
def normalize_source(raw: Any) -> Union[int, str]:
    """Index de webcam (int) ou URL (str). Refuse tout le reste, notamment les chemins de fichiers locaux."""
    if isinstance(raw, bool) or raw is None:
        raise CameraError("La source est obligatoire.", "source")
    if isinstance(raw, int):
        index = raw
    else:
        text = str(raw).strip()
        if not text:
            raise CameraError("La source est obligatoire.", "source")
        if text.isdigit():
            index = int(text)
        else:
            if len(text) > 500 or any(ch.isspace() or ord(ch) < 32 for ch in text):
                raise CameraError("Adresse de source invalide (espaces ou caractères de contrôle).", "source")
            try:
                parts = urlsplit(text)
                port = parts.port                      # lève ValueError si le port est invalide
            except ValueError:
                raise CameraError("Adresse de source invalide.", "source")
            if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
                raise CameraError("Source non prise en charge. Indiquez un index de webcam (0, 1…) ou une adresse "
                                  "commençant par http://, https://, rtsp:// ou rtsps://.", "source")
            if port is not None and not 0 < port < 65536:
                raise CameraError("Port invalide dans l'adresse de la source.", "source")
            return text
    if not 0 <= index <= MAX_WEBCAM_INDEX:
        raise CameraError(f"Index de webcam invalide (0 à {MAX_WEBCAM_INDEX}).", "source")
    return index


def source_kind(source: Union[int, str]) -> str:
    """webcam | http | rtsp"""
    if isinstance(source, int):
        return "webcam"
    return "rtsp" if urlsplit(str(source)).scheme.lower() in ("rtsp", "rtsps") else "http"


def has_secret(source: Union[int, str]) -> bool:
    """Vrai si l'adresse contient des identifiants ou des paramètres (jeton…)."""
    if isinstance(source, int):
        return False
    parts = urlsplit(str(source))
    return bool(parts.username or parts.password or parts.query)


def slugify(label: str) -> str:
    text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:32].strip("-")
    return text or "camera"


# ---- saisie ----------------------------------------------------------------------------------
def _number(value: Any, field: str, low: float, high: float, integer: bool = False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CameraError("Un nombre est attendu.", field)
    if not low <= value <= high:
        raise CameraError(f"Valeur hors limites ({low:g} à {high:g}).", field)
    return int(value) if integer else float(value)


def clean_input(data: Dict[str, Any], creating: bool) -> Dict[str, Any]:
    """Valide et normalise les champs fournis. Seuls les champs présents dans ``data`` sont retournés."""
    out: Dict[str, Any] = {}
    if "label" in data or creating:
        label = (data.get("label") or "").strip() if isinstance(data.get("label"), (str, type(None))) else None
        if not label:
            raise CameraError("Le nom de la caméra est obligatoire.", "label")
        if len(label) > 40:
            raise CameraError("Le nom est limité à 40 caractères.", "label")
        out["label"] = label
    if "location" in data:
        location = data.get("location")
        if location is not None and not isinstance(location, str):
            raise CameraError("La zone doit être un texte.", "location")
        location = (location or "").strip()
        if len(location) > 80:
            raise CameraError("La zone est limitée à 80 caractères.", "location")
        out["location"] = location
    if "source" in data or creating:
        out["source"] = normalize_source(data.get("source"))
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise CameraError("« enabled » doit être vrai ou faux.", "enabled")
        out["enabled"] = data["enabled"]
    for axis in ("x", "y"):
        if axis in data:
            out[axis] = _number(data[axis], axis, 0.0, 1.0)
    if "capture_width" in data or "capture_height" in data:
        width = _number(data.get("capture_width"), "capture_width", 160, 7680, integer=True)
        height = _number(data.get("capture_height"), "capture_height", 120, 4320, integer=True)
        if (width is None) != (height is None):
            raise CameraError("Indiquez la largeur et la hauteur, ou aucune des deux.", "capture_width")
        out["capture_width"], out["capture_height"] = width, height
    if "process_fps" in data:
        out["process_fps"] = _number(data["process_fps"], "process_fps", 0.5, 60.0)
    if "floor_id" in data:
        out["floor_id"] = _number(data["floor_id"], "floor_id", 1, 10 ** 9, integer=True)
    if "heading" in data:
        heading = _number(data["heading"], "heading", 0.0, 360.0)
        out["heading"] = None if heading is None else heading % 360.0
    if "fov" in data:
        out["fov"] = _number(data["fov"], "fov", 10.0, 360.0)
    if "view_range" in data:
        out["view_range"] = _number(data["view_range"], "view_range", 0.02, 1.0)
    if creating and "id" in data and data["id"] is not None:
        if not isinstance(data["id"], str) or not ID_RE.match(data["id"]):
            raise CameraError("Identifiant invalide (lettres, chiffres, - et _ ; 32 caractères maximum).", "id")
        out["id"] = data["id"]
    return out


# ---- gestionnaire ------------------------------------------------------------------------------
class CameraManager:
    def __init__(self, cams: Dict[str, Any], meta: Dict[str, Dict[str, Any]], sources: Dict[str, Any],
                 make_pipeline: Callable[[Dict[str, Any]], Any], known: Callable[[], list],
                 targets: Callable[[], list], feed: Optional["events.EventFeed"] = None,
                 session_factory=SessionLocal):
        self.cams, self.meta, self.sources = cams, meta, sources     # modifiés *en place* : d'autres modules les partagent
        self._make_pipeline, self._known, self._targets, self.feed = make_pipeline, known, targets, feed
        self._session = session_factory
        self._lock = threading.RLock()
        self.version = 0                  # augmente à chaque changement : les pages rechargent la liste
        self.errors: Dict[str, str] = {}  # caméras activées mais impossibles à démarrer

    # ---- démarrage ----------------------------------------------------------------------------
    def seed_if_needed(self, cameras: List[Dict[str, Any]]) -> int:
        """Importe ``cameras.json`` au premier démarrage seulement (un réglage mémorise que c'est fait)."""
        db = self._session()
        try:
            if db.get(Setting, SEEDED_KEY) is not None:
                return 0
            created = 0
            if db.query(Camera).count() == 0:
                for i, cam in enumerate(cameras):
                    db.add(Camera(id=cam["id"], label=cam.get("label") or cam["id"], location=cam.get("location") or "",
                                  source=str(cam["source"]), enabled=True, x=cam.get("x"), y=cam.get("y"),
                                  position=i))
                    created += 1
            db.add(Setting(key=SEEDED_KEY, value="1"))
            db.commit()
            return created
        finally:
            db.close()

    def load_and_start(self):
        rows = self._load_rows()
        self._rebuild_meta(rows)
        for row in rows:
            if row.enabled:
                self._start(row)
        self.version += 1

    # ---- lecture --------------------------------------------------------------------------------
    def _load_rows(self) -> List[Camera]:
        db = self._session()
        try:
            rows = db.query(Camera).order_by(Camera.position, Camera.id).all()
            db.expunge_all()
            return rows
        finally:
            db.close()

    def _check_floor(self, floor_id):
        if floor_id is None:
            return
        db = self._session()
        try:
            if db.get(Floor, floor_id) is None:
                raise CameraError("Cet étage n'existe pas.", "floor_id")
        finally:
            db.close()

    def refresh_meta(self):
        """Recalcule les métadonnées d'affichage (après un changement d'étage, par exemple)."""
        with self._lock:
            self._rebuild_meta(self._load_rows())
            self.version += 1

    def _get_row(self, camera_id: str) -> Camera:
        db = self._session()
        try:
            row = db.get(Camera, camera_id)
            if row is None:
                raise CameraNotFound(camera_id)
            db.expunge(row)
            return row
        finally:
            db.close()

    def admin_view(self, row: Camera) -> Dict[str, Any]:
        source = normalize_source(row.source)
        cam = self.cams.get(row.id)
        if cam is not None:
            runtime = cam.stats()
        else:
            runtime = {"status": "disabled" if not row.enabled else "error", "fps": 0.0, "latency_ms": None,
                       "persons": 0, "width": 0, "height": 0, "age_sec": None, "reconnects": 0}
        return {
            "id": row.id, "site_id": row.site_id, "label": row.label, "location": row.location or "",
            "kind": source_kind(source), "source_display": mask_source(source), "has_secret": has_secret(source),
            "enabled": bool(row.enabled), "x": row.x, "y": row.y,
            "floor_id": row.floor_id, "heading": row.heading, "fov": row.fov, "view_range": row.view_range,
            "capture_width": row.capture_width, "capture_height": row.capture_height,
            "process_fps": row.process_fps, "position": row.position,
            "created_at": to_iso_utc(row.created_at) if row.created_at else None,
            "updated_at": to_iso_utc(row.updated_at) if row.updated_at else None,
            "error": self.errors.get(row.id), **runtime,
        }

    def list_admin(self) -> List[Dict[str, Any]]:
        return [self.admin_view(r) for r in self._load_rows()]

    def get_admin(self, camera_id: str) -> Dict[str, Any]:
        return self.admin_view(self._get_row(camera_id))

    def source_of(self, camera_id: str) -> Union[int, str]:
        """Source enregistrée (en clair) : réservée à un usage interne, jamais renvoyée au navigateur."""
        return normalize_source(self._get_row(camera_id).source)

    def configured_hosts(self) -> set:
        """Adresses (hôtes) déjà configurées, pour signaler les caméras découvertes qui existent déjà."""
        hosts = set()
        for row in self._load_rows():
            source = normalize_source(row.source)
            if isinstance(source, str):
                hosts.add(urlsplit(source).hostname)
        return hosts

    def find_running(self, source: Union[int, str]):
        """(identifiant, libellé, caméra) si une caméra en marche utilise déjà cette source."""
        labels = {r.id: r.label for r in self._load_rows()}
        for camera_id, cam in list(self.cams.items()):
            if self.sources.get(camera_id) == source:
                return camera_id, labels.get(camera_id, camera_id), cam
        return None

    # ---- pipeline -----------------------------------------------------------------------------------
    def _pipeline_config(self, row: Camera) -> Dict[str, Any]:
        return {"id": row.id, "source": normalize_source(row.source), "capture_width": row.capture_width,
                "capture_height": row.capture_height, "process_fps": row.process_fps}

    def _start(self, row: Camera) -> bool:
        try:
            cam = self._make_pipeline(self._pipeline_config(row))
            cam.set_known(self._known())
            cam.set_targets(self._targets())
            cam.start()
        except Exception as exc:
            log.exception("Impossible de démarrer la caméra %s", row.id)
            self.errors[row.id] = f"{type(exc).__name__}: {exc}"[:200]
            return False
        self.errors.pop(row.id, None)
        self.cams[row.id] = cam
        return True

    def refresh_meta(self):
        """Relit noms, emplacements et position sur la carte depuis la base (modifiés hors de l'application).
        Ne touche ni aux flux ni aux sources : seuls les libellés affichés sont mis à jour."""
        with self._lock:
            rows = [r for r in self._load_rows() if r.enabled and r.id in self.cams]
            new_meta = config.camera_meta([{"id": r.id, "label": r.label, "location": r.location, "x": r.x, "y": r.y,
                                            "floor_id": r.floor_id, "heading": r.heading, "fov": r.fov,
                                            "view_range": r.view_range} for r in rows])
            for key in [k for k in self.meta if k not in new_meta]:
                del self.meta[key]
            self.meta.update(new_meta)

    def _stop(self, camera_id: str):
        cam = self.cams.pop(camera_id, None)
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                log.exception("Arrêt de la caméra %s", camera_id)

    def _rebuild_meta(self, rows: List[Camera]):
        enabled = [r for r in rows if r.enabled]
        new_meta = config.camera_meta([{"id": r.id, "label": r.label, "location": r.location, "x": r.x, "y": r.y,
                                        "floor_id": r.floor_id, "heading": r.heading, "fov": r.fov,
                                        "view_range": r.view_range} for r in enabled])
        new_sources = {r.id: normalize_source(r.source) for r in enabled}
        for target, new in ((self.meta, new_meta), (self.sources, new_sources)):
            for key in [k for k in target if k not in new]:
                del target[key]
            target.update(new)

    def _emit(self, type_: str, text: str, camera_id: str):
        if self.feed is not None:
            self.feed.emit(type_, text, camera_id=camera_id)

    # ---- actions ---------------------------------------------------------------------------------------
    def _check_unique_source(self, rows: List[Camera], source: Union[int, str], ignore_id: Optional[str] = None):
        for row in rows:
            if row.id != ignore_id and normalize_source(row.source) == source:
                raise CameraConflict(f"Cette source est déjà utilisée par la caméra « {row.label} ».", "source")

    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            fields = clean_input(data, creating=True)
            self._check_floor(fields.get("floor_id"))
            rows = self._load_rows()
            if len(rows) >= config.MAX_CAMERAS:
                raise CameraConflict(f"Nombre maximal de caméras atteint ({config.MAX_CAMERAS}).")
            self._check_unique_source(rows, fields["source"])
            taken = {r.id for r in rows}
            if "id" in fields:
                if fields["id"] in taken:
                    raise CameraConflict("Cet identifiant est déjà utilisé.", "id")
                camera_id = fields["id"]
            else:
                base = slugify(fields["label"])
                camera_id, n = base, 2
                while camera_id in taken:
                    camera_id = f"{base[:28]}-{n}"
                    n += 1
            row = Camera(id=camera_id, label=fields["label"], location=fields.get("location", ""),
                         source=str(fields["source"]), enabled=fields.get("enabled", True),
                         x=fields.get("x"), y=fields.get("y"), floor_id=fields.get("floor_id"),
                         heading=fields.get("heading"), fov=fields.get("fov"), view_range=fields.get("view_range"),
                         capture_width=fields.get("capture_width"),
                         capture_height=fields.get("capture_height"), process_fps=fields.get("process_fps"),
                         position=max([r.position for r in rows], default=-1) + 1)
            db = self._session()
            try:
                db.add(row)
                db.commit()
                db.refresh(row)
                db.expunge(row)
            finally:
                db.close()
            if row.enabled and not self._start(row):
                self._delete_row(camera_id)             # on ne garde pas une caméra qui ne peut pas démarrer
                raise CameraError(f"Impossible de démarrer la caméra : {self.errors.pop(camera_id, 'erreur inconnue')}",
                                  status=500)
            self._rebuild_meta(self._load_rows())
            self.version += 1
            self._emit(events.CAMERA_ADDED, f"Caméra ajoutée : {row.label}", camera_id)
            return self.admin_view(self._get_row(camera_id))

    def update(self, camera_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            row = self._get_row(camera_id)
            fields = clean_input(data, creating=False)
            fields.pop("id", None)
            self._check_floor(fields.get("floor_id"))
            if "source" in fields:
                self._check_unique_source(self._load_rows(), fields["source"], ignore_id=camera_id)
            was_enabled = bool(row.enabled)
            before = {k: (normalize_source(row.source) if k == "source" else getattr(row, k)) for k in RESTART_FIELDS}
            for key, value in fields.items():
                setattr(row, key, str(value) if key == "source" else value)
            row.updated_at = utcnow()
            db = self._session()
            try:
                db.merge(row)
                db.commit()
            finally:
                db.close()
            row = self._get_row(camera_id)
            after = {k: (normalize_source(row.source) if k == "source" else getattr(row, k)) for k in RESTART_FIELDS}
            changed = {k for k in RESTART_FIELDS if before[k] != after[k]}

            if "enabled" in changed and not row.enabled:
                self._stop(camera_id)
                self._emit(events.CAMERA_DISABLED, f"Caméra désactivée : {row.label}", camera_id)
            elif row.enabled and (changed or camera_id not in self.cams):
                self._stop(camera_id)
                self._start(row)
                if "enabled" in changed and not was_enabled:
                    self._emit(events.CAMERA_ENABLED, f"Caméra activée : {row.label}", camera_id)
            self._rebuild_meta(self._load_rows())
            self.version += 1
            return self.admin_view(row)

    def _delete_row(self, camera_id: str):
        db = self._session()
        try:
            db.query(Camera).filter(Camera.id == camera_id).delete()
            db.commit()
        finally:
            db.close()

    def delete(self, camera_id: str) -> None:
        with self._lock:
            row = self._get_row(camera_id)
            self._stop(camera_id)
            self._delete_row(camera_id)
            self.errors.pop(camera_id, None)
            self._rebuild_meta(self._load_rows())
            self.version += 1
            self._emit(events.CAMERA_REMOVED, f"Caméra supprimée : {row.label}", camera_id)

    def restart(self, camera_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._get_row(camera_id)
            if not row.enabled:
                raise CameraConflict("Cette caméra est désactivée : activez-la d'abord.")
            self._stop(camera_id)
            self._start(row)
            self.version += 1
            return self.admin_view(row)

    def stop_all(self):
        with self._lock:
            for camera_id in list(self.cams):
                self._stop(camera_id)
