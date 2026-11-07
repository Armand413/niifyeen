"""Registre des cibles : plusieurs cibles peuvent être suivies en même temps.

Chaque cible a son identifiant, son libellé (« Cible 2 »), sa couleur, sa vignette (conservée en
mémoire uniquement, jamais écrite sur disque ni en base), sa correspondance éventuelle avec une
personne enrôlée, et son propre suivi de trajet.
"""
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from . import config
from .target_path import TargetTracker
from .vision import cosine_similarity


class TargetLimitError(Exception):
    """Nombre maximal de cibles atteint."""


class DuplicateTargetError(Exception):
    """La personne est déjà une cible."""

    def __init__(self, existing: "Target", score: float):
        super().__init__(existing.label)
        self.existing = existing
        self.score = score


@dataclass
class Target:
    id: int
    label: str
    color: str
    embedding: np.ndarray
    photo: Optional[bytes]            # vignette JPEG, en mémoire uniquement
    created_at: float
    session: int                      # identifiant du trajet enregistré en base
    match: Optional[Dict[str, Any]]   # résultat de target_profile.find_match
    tracker: TargetTracker
    source: str = "photo"            # « photo » (envoyée) ou « person » (choisie dans la base)


def make_thumbnail(img: np.ndarray, max_px: int = config.TARGET_THUMBNAIL_PX) -> Optional[bytes]:
    """Réduit une image (BGR) en vignette JPEG. Retourne None si l'encodage échoue."""
    h, w = img.shape[:2]
    scale = min(1.0, max_px / float(max(h, w)))
    if scale < 1.0:
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    return buf.tobytes() if ok else None


def hex_to_bgr(color: str) -> Tuple[int, int, int]:
    color = color.lstrip("#")
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    return (b, g, r)


class TargetRegistry:
    def __init__(self, max_targets: int = config.MAX_TARGETS, colors: Optional[List[str]] = None,
                 on_move: Optional[Callable[["Target", Dict[str, Any]], None]] = None):
        self.max_targets = max_targets
        self.colors = list(colors or config.TARGET_COLORS)
        self._on_move = on_move
        self._lock = threading.RLock()
        self._targets: Dict[int, Target] = {}
        self._next_id = 1
        self._last_session = 0
        self.version = 0      # augmente à chaque ajout / retrait : les pages savent qu'elles doivent recharger la liste

    # ---- ajout / retrait --------------------------------------------------------
    def _new_session(self) -> int:
        self._last_session = max(int(time.time() * 1000), self._last_session + 1)
        return self._last_session

    def _pick_color(self) -> str:
        used = {t.color for t in self._targets.values()}
        for color in self.colors:
            if color not in used:
                return color
        return self.colors[(self._next_id - 1) % len(self.colors)]

    def add(self, embedding: np.ndarray, photo: Optional[bytes] = None, match: Optional[Dict[str, Any]] = None,
            duplicate_threshold: Optional[float] = None, source: str = "photo") -> Target:
        threshold = config.TARGET_DUPLICATE_THRESHOLD if duplicate_threshold is None else duplicate_threshold
        embedding = np.asarray(embedding, dtype=np.float32)
        with self._lock:
            if len(self._targets) >= self.max_targets:
                raise TargetLimitError(f"Nombre maximal de cibles atteint ({self.max_targets}). "
                                       "Retirez une cible pour en ajouter une autre.")
            for existing in self._targets.values():
                score = cosine_similarity(embedding, existing.embedding)
                if score >= threshold:
                    raise DuplicateTargetError(existing, score)
            target_id = self._next_id
            self._next_id += 1
            target = Target(id=target_id, label=f"Cible {target_id}", color=self._pick_color(),
                            embedding=embedding, photo=photo, created_at=time.time(),
                            session=self._new_session(), match=match, tracker=TargetTracker(),
                            source=source)
            if self._on_move:
                target.tracker.on_move = lambda move, t=target: self._on_move(t, move)
            self._targets[target_id] = target
            self.version += 1
            return target

    def set_capacity(self, value: int) -> int:
        """Change le nombre maximal de cibles. Refuse de descendre sous le nombre de cibles en cours."""
        value = int(value)
        if not 1 <= value <= config.TARGETS_HARD_LIMIT:
            raise ValueError(f"La capacité doit être comprise entre 1 et {config.TARGETS_HARD_LIMIT}.")
        with self._lock:
            if value < len(self._targets):
                raise TargetLimitError(f"{len(self._targets)} cibles sont en cours : retirez-en avant de "
                                       f"descendre à {value}.")
            self.max_targets = value
            self.version += 1
            return value

    def participant_ids(self) -> List[int]:
        """Personnes enrôlées actuellement suivies (dans l'ordre d'ajout)."""
        with self._lock:
            return [t.match["participant_id"] for t in self._targets.values()
                    if t.match and t.match.get("matched") and t.match.get("participant_id")]

    def remove(self, target_id: int) -> bool:
        with self._lock:
            if self._targets.pop(target_id, None) is None:
                return False
            self.version += 1
            return True

    def clear(self) -> int:
        with self._lock:
            n = len(self._targets)
            self._targets.clear()
            self._next_id = 1           # la numérotation repart de « Cible 1 »
            self.version += 1
            return n

    # ---- lecture -----------------------------------------------------------------
    def get(self, target_id: int) -> Optional[Target]:
        with self._lock:
            return self._targets.get(target_id)

    def items(self) -> List[Target]:
        with self._lock:
            return list(self._targets.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._targets)

    def for_cameras(self) -> List[Dict[str, Any]]:
        """Ce dont les caméras ont besoin pour reconnaître les cibles."""
        return [{"id": t.id, "label": t.label, "embedding": t.embedding,
                 "threshold": config.TARGET_THRESHOLD, "color_bgr": hex_to_bgr(t.color)}
                for t in self.items()]

    def snapshot(self) -> List[Dict[str, Any]]:
        """État en direct de chaque cible (envoyé par WebSocket)."""
        out = []
        for t in self.items():
            snap = t.tracker.snapshot()
            status = snap["target"]
            out.append({"id": t.id, "label": t.label, "color": t.color,
                        "active": status["active"], "camera_id": status["camera_id"],
                        "score": status["score"], "last_seen_ts": status["last_seen_ts"],
                        "path": snap["target_path"]})
        return out
