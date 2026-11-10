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
DB_THRESHOLD = _env_float("NIIFYEEN_DB_THRESHOLD", 0.35)          # personne enrôlée : en dessous, « inconnu »
TARGET_THRESHOLD = _env_float("NIIFYEEN_TARGET_THRESHOLD", 0.40)  # cible recherchée
# Durée pendant laquelle une identité reconnue (cible ou personne enrôlée) est conservée tant que la même silhouette est
# suivie, même si le visage n'est plus visible (de dos, de profil, tête baissée). Au-delà, il faut revoir le visage.
IDENTITY_HOLD_SEC = max(1.0, min(300.0, _env_float("NIIFYEEN_IDENTITY_HOLD_SEC", 20.0)))
REID_THRESHOLD = _env_float("NIIFYEEN_REID_THRESHOLD", 0.45)      # même personne entre caméras

# Statuts de correspondance avec une personne enrôlée. Une similarité n'est jamais une identité certaine :
#   >= DB_PROBABLE_THRESHOLD                  -> « correspondance probable »
#   entre DB_THRESHOLD et DB_PROBABLE_THRESHOLD -> « à vérifier »
#   < DB_THRESHOLD                            -> « inconnu »
# Deux personnes enrôlées à moins de DB_AMBIGUITY_MARGIN l'une de l'autre rendent aussi la correspondance « à vérifier ».
# Valeurs non validées sur des données réelles : à calibrer.
DB_PROBABLE_THRESHOLD = max(_env_float("NIIFYEEN_DB_PROBABLE_THRESHOLD", 0.50), DB_THRESHOLD)
DB_AMBIGUITY_MARGIN = _env_float("NIIFYEEN_DB_AMBIGUITY_MARGIN", 0.05)
# Une nouvelle personne dont le visage ressemble à ce point à une personne déjà enrôlée est signalée (doublon probable).
PERSON_DUPLICATE_THRESHOLD = _env_float("NIIFYEEN_PERSON_DUPLICATE_THRESHOLD", 0.60)

# Similarité minimale pour afficher la fiche d'une personne enrôlée à partir d'une photo.
# Par défaut égale au seuil de la cible : afficher des données personnelles exige au moins autant de confiance.
PROFILE_THRESHOLD = _env_float("NIIFYEEN_PROFILE_THRESHOLD", TARGET_THRESHOLD)

# ---- Caméras --------------------------------------------------------------------
MAX_CAMERAS = _env_int("NIIFYEEN_MAX_CAMERAS", 32)
PROBE_TIMEOUT_SEC = 6.0           # délai maximal d'un test de connexion à une source
DEFAULT_CAPTURE_SIZE = (1280, 720)

# ---- Flux vidéo -----------------------------------------------------------------
# Taille des images diffusées (supervision, plein écran). Plus grand = plus net sur un grand moniteur,
# mais plus de bande passante et d'encodage JPEG ; la détection travaille sur l'image réduite à cette taille.
STREAM_SIZE = (_env_int("NIIFYEEN_STREAM_WIDTH", 640), _env_int("NIIFYEEN_STREAM_HEIGHT", 360))

# ---- Cibles multiples -----------------------------------------------------------
# Capacité par défaut ; modifiable à chaud (page Cibles) entre 1 et TARGETS_HARD_LIMIT.
TARGETS_HARD_LIMIT = 32
MAX_TARGETS = max(1, min(TARGETS_HARD_LIMIT, _env_int("NIIFYEEN_MAX_TARGETS", 8)))
# Une couleur par cible (plan, liste, cadre vidéo). Distinctes du bleu-vert des caméras.
TARGET_COLORS = ["#d92d20", "#7a5af8", "#f79009", "#ee46bc", "#2e5bff", "#8a4b08", "#3b8f0a", "#1d2939"]
# Une nouvelle photo aussi proche d'une cible existante est refusée (même personne ajoutée deux fois).
TARGET_DUPLICATE_THRESHOLD = _env_float("NIIFYEEN_TARGET_DUPLICATE_THRESHOLD", 0.60)
TARGET_THUMBNAIL_PX = 200

# ---- Suivi de la cible --------------------------------------------------------
CAM_SWITCH_CONFIRM_SEC = 0.8
TARGET_TIMEOUT_SEC = 3.0
TARGET_PATH_MAX = 200

# ---- Journalisation en base ---------------------------------------------------
LOG_EVERY_SEC = 0.8  # par (caméra, piste, mode)
# Durée de conservation des observations, en jours. 0 = conservation illimitée.
# Enregistrement vidéo (Module 9) : désactivé tant que l'exploitant ne l'active pas dans l'interface ;
# NIIFYEEN_RECORDING_DISABLED=1 l'interdit totalement (l'interface ne peut pas le réactiver).
RECORDINGS_DIR = Path(os.getenv("NIIFYEEN_RECORDINGS_DIR") or PROJECT_ROOT / "recordings")
RECORDING_HARD_DISABLED = os.getenv("NIIFYEEN_RECORDING_DISABLED", "0").strip().lower() in ("1", "true", "oui", "yes")
RECORDING_MIN_FREE_MB = _env_int("NIIFYEEN_RECORDING_MIN_FREE_MB", 500)
RETENTION_DAYS = _env_int("NIIFYEEN_RETENTION_DAYS", 30)

# ---- Historique ---------------------------------------------------------------
# Deux détections de la même piste espacées de plus de N secondes forment deux passages.
HISTORY_GAP_SEC = _env_float("NIIFYEEN_HISTORY_GAP_SEC", 10.0)
# Si faux, l'historique et l'export remplacent les noms par « Personne enrôlée » / « Cible ».
HISTORY_SHOW_IDENTITIES = os.getenv("NIIFYEEN_HISTORY_IDENTITIES", "1").strip().lower() not in ("0", "false", "non", "no")
HISTORY_MAX_SPAN_DAYS = 90
HISTORY_CSV_MAX_ROWS = 50_000

# ---- Limites des envois de fichiers -------------------------------------------
MAX_UPLOAD_BYTES = _env_int("NIIFYEEN_MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
MAX_ENROLL_PHOTOS = 10
MAX_SEARCH_PHOTOS = 10
MAX_NAME_LENGTH = 100
MAX_INFOS_LENGTH = 500
MAX_PERSON_NAME = 60          # nom ou prénom
MAX_MATRICULE = 40
MAX_REFERENCES = 30           # photos de référence (empreintes) par personne

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
        label = item.get("label")
        if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 40):
            raise ValueError(f"caméra {cam_id} : « label » doit être un texte de 40 caractères maximum")
        location = item.get("location")
        if location is not None and (not isinstance(location, str) or len(location) > 80):
            raise ValueError(f"caméra {cam_id} : « location » doit être un texte de 80 caractères maximum")
        for axis in ("x", "y"):
            value = item.get(axis)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not 0 <= value <= 1):
                raise ValueError(f"caméra {cam_id} : « {axis} » doit être un nombre entre 0 et 1")
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


# Emplacements par défaut sur le plan du site (fractions de la largeur / hauteur du plan) :
# entrée, salle, sortie. Les caméras suivantes sont alignées le long du couloir.
_PLAN_SLOTS = [(0.11, 0.72), (0.63, 0.30), (0.89, 0.72)]


def _default_position(index: int):
    if index < len(_PLAN_SLOTS):
        return _PLAN_SLOTS[index]
    return (min(0.25 + 0.12 * (index - len(_PLAN_SLOTS)), 0.95), 0.72)


def camera_meta(cameras: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Métadonnées d'affichage (libellé et position sur le plan), indexées par l'identifiant de caméra.

    ``x`` et ``y`` sont des fractions entre 0 et 1 de la largeur et de la hauteur du plan.
    """
    meta = {}
    for i, cam in enumerate(cameras):
        dx, dy = _default_position(i)
        meta[cam["id"]] = {
            "label": cam.get("label") or cam["id"],
            "location": cam.get("location") or "",
            "x": cam["x"] if cam.get("x") is not None else dx,
            "y": cam["y"] if cam.get("y") is not None else dy,
            "placed": cam.get("x") is not None and cam.get("y") is not None,
            "floor_id": cam.get("floor_id"),
            "heading": cam.get("heading"),
            "fov": cam.get("fov"),
            "view_range": cam.get("view_range"),
        }
    return meta
