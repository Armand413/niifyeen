"""Configuration centralisée.

Tout ce qui dépend de la machine ou de l'installation (chemins, caméras,
seuils, limites) est défini ici plutôt que dispersé dans le code.

Les valeurs peuvent être surchargées par des variables d'environnement
(préfixe ``NIIFYEEN_``). Les caméras sont lues dans ``cameras.json``
(ignoré par Git, voir ``cameras.example.json``).
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)

# Racine du projet (le dossier qui contient ``backend/``). Tous les chemins
# sont absolus : le serveur peut être lancé depuis n'importe quel dossier.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r n'est pas un nombre, valeur par défaut %s utilisée", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


# ---- Fichiers ---------------------------------------------------------------
DB_PATH = Path(os.getenv("NIIFYEEN_DB") or PROJECT_ROOT / "niifyeen.db")
DB_URL = f"sqlite:///{DB_PATH.as_posix()}"
MODELS_DIR = Path(os.getenv("NIIFYEEN_MODELS_DIR") or PROJECT_ROOT / "models_insightface")
CAMERAS_FILE = Path(os.getenv("NIIFYEEN_CAMERAS_FILE") or PROJECT_ROOT / "cameras.json")

_local_weights = PROJECT_ROOT / "yolov8n.pt"
# Si le fichier n'est pas présent, Ultralytics le télécharge au premier lancement.
YOLO_WEIGHTS = str(_local_weights) if _local_weights.exists() else "yolov8n.pt"

# ---- Seuils de similarité (cosinus) -----------------------------------------
# Ces valeurs n'ont pas été validées sur un jeu de test : les ajuster avec
# des données réelles avant de s'y fier.
DB_THRESHOLD = _env_float("NIIFYEEN_DB_THRESHOLD", 0.35)          # personne enrôlée
TARGET_THRESHOLD = _env_float("NIIFYEEN_TARGET_THRESHOLD", 0.40)  # cible recherchée
REID_THRESHOLD = _env_float("NIIFYEEN_REID_THRESHOLD", 0.45)      # même personne entre caméras

# ---- Suivi de la cible --------------------------------------------------------
CAM_SWITCH_CONFIRM_SEC = 0.8
TARGET_TIMEOUT_SEC = 3.0
TARGET_PATH_MAX = 200

# ---- Journalisation en base ---------------------------------------------------
LOG_EVERY_SEC = 0.8  # par (caméra, piste, mode)
# Durée de conservation des observations, en jours. 0 = conservation illimitée.
RETENTION_DAYS = _env_int("NIIFYEEN_RETENTION_DAYS", 30)

# ---- Limites des envois de fichiers -------------------------------------------
MAX_UPLOAD_BYTES = _env_int("NIIFYEEN_MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
MAX_ENROLL_PHOTOS = 10
MAX_SEARCH_PHOTOS = 10
MAX_NAME_LENGTH = 100
MAX_INFOS_LENGTH = 500

# ---- Caméras ------------------------------------------------------------------
DEFAULT_CAMERAS: List[Dict[str, Any]] = [{"id": "entre", "source": 0}]
_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _validate_cameras(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("le fichier doit contenir une liste de caméras")
    cameras: List[Dict[str, Any]] = []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"caméra n°{i + 1} : un objet est attendu")
        cam_id = item.get("id")
        if not isinstance(cam_id, str) or not _CAMERA_ID_RE.match(cam_id):
            raise ValueError(f"caméra n°{i + 1} : « id » invalide (lettres, chiffres, - et _ ; 32 max)")
        if cam_id in seen:
            raise ValueError(f"identifiant de caméra en double : {cam_id}")
        source = item.get("source")
        if isinstance(source, bool) or not isinstance(source, (int, str)) or source == "":
            raise ValueError(f"caméra {cam_id} : « source » doit être un index ou une URL")
        seen.add(cam_id)
        cameras.append(item)
    return cameras


def load_cameras(path: Path = None) -> List[Dict[str, Any]]:
    """Lit la liste des caméras. Retombe sur la webcam 0 si le fichier n'existe pas."""
    path = Path(path) if path else CAMERAS_FILE
    if not path.exists():
        log.warning(
            "%s introuvable : seule la webcam d'index 0 est utilisée. "
            "Copier cameras.example.json vers cameras.json pour configurer les caméras.", path)
        return [dict(c) for c in DEFAULT_CAMERAS]
    with path.open(encoding="utf-8-sig") as f:
        return _validate_cameras(json.load(f))


def camera_meta(cameras: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Métadonnées d'affichage, indexées par le *même* identifiant que les caméras."""
    n = len(cameras)
    meta = {}
    for i, cam in enumerate(cameras):
        frac = i / (n - 1) if n > 1 else 0.5
        meta[cam["id"]] = {
            "label": cam.get("label", cam["id"]),
            "x": cam.get("x", round(0.2 + 0.6 * frac, 2)),
            "y": cam.get("y", round(0.8 - 0.6 * frac, 2)),
        }
    return meta
