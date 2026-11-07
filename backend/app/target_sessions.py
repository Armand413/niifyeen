"""Cibles choisies dans la base de personnes, et sauvegarde / reprise d'une session de cibles.

Seuls les **identifiants de personnes enrôlées** sont sauvegardés : jamais d'empreinte ni de photo d'une cible
envoyée à la volée (donnée biométrique d'une personne qui n'a pas consenti). Les cibles non enrôlées ne sont
donc pas reprises ; la réponse le précise.
"""
import json
from typing import Any, Dict, List, Optional

import numpy as np

from . import config
from .db import EmbeddingRef, Participant, Setting, to_iso_utc, utcnow
from .people import PersonNotFound, bytes_to_embedding

SESSION_KEY = "targets_session"
CAPACITY_KEY = "targets_capacity"


def person_embedding(db, participant_id: int) -> np.ndarray:
    """Empreinte moyenne (normalisée) des photos de référence d'une personne."""
    if db.get(Participant, participant_id) is None:
        raise PersonNotFound("Personne introuvable.")
    rows = db.query(EmbeddingRef.embedding).filter(EmbeddingRef.participant_id == participant_id).all()
    if not rows:
        raise PersonNotFound("Cette personne n'a aucune photo de référence exploitable.")
    mean = np.mean(np.stack([bytes_to_embedding(r[0]) for r in rows]), axis=0).astype(np.float32)
    return mean / (np.linalg.norm(mean) + 1e-9)


def person_match(db, participant_id: int, embedding: np.ndarray) -> Dict[str, Any]:
    """Même structure que ``target_profile.find_match`` : la correspondance est celle de la personne choisie."""
    from .people import rank_participants
    ranked = rank_participants(db, embedding)
    own = next((r for r in ranked if r["participant_id"] == participant_id), None)
    others = [{"nom": r["nom"], "score": round(r["score"], 3)} for r in ranked
              if r["participant_id"] != participant_id and r["score"] >= config.PROFILE_THRESHOLD][:3]
    score = own["score"] if own else 1.0
    return {"matched": True, "participant_id": participant_id, "score": score, "threshold": config.PROFILE_THRESHOLD,
            "best_score": score, "enrolled_count": len(ranked), "others": others, "chosen": True}


# ---- réglages ----------------------------------------------------------------------
def _get(db, key: str) -> Optional[str]:
    row = db.get(Setting, key)
    return row.value if row else None


def _put(db, key: str, value: Optional[str]) -> None:
    row = db.get(Setting, key)
    if value is None:
        if row:
            db.delete(row)
    elif row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def load_capacity(db) -> Optional[int]:
    raw = _get(db, CAPACITY_KEY)
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value and 1 <= value <= config.TARGETS_HARD_LIMIT else None


def save_capacity(db, value: int) -> None:
    _put(db, CAPACITY_KEY, str(int(value)))


# ---- session -----------------------------------------------------------------------
def save_session(db, participant_ids: List[int], unenrolled: int) -> Dict[str, Any]:
    data = {"saved_at": to_iso_utc(utcnow()), "participant_ids": list(dict.fromkeys(participant_ids)),
            "unenrolled_skipped": int(unenrolled)}
    _put(db, SESSION_KEY, json.dumps(data))
    return data


def load_session(db) -> Optional[Dict[str, Any]]:
    raw = _get(db, SESSION_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        ids = [int(i) for i in data.get("participant_ids", [])]
    except (ValueError, TypeError, AttributeError):
        return None
    return {"saved_at": data.get("saved_at"), "participant_ids": ids,
            "unenrolled_skipped": int(data.get("unenrolled_skipped", 0))}


def forget_session(db) -> bool:
    existed = _get(db, SESSION_KEY) is not None
    _put(db, SESSION_KEY, None)
    return existed
