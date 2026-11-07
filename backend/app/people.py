"""Personnes enrôlées : validation, création, modification, références (photos), activité et fiche individuelle.

Principe central de NiifYéén 2.0 : une personne est identifiée par son **identifiant**, jamais par son nom.
Les détections et les événements portent ``participant_id`` ; le nom affiché est toujours le nom *actuel*
de la fiche. Renommer quelqu'un ne casse donc pas son historique, et deux homonymes ne sont plus confondus.

Seules des empreintes faciales (« références ») sont conservées, jamais les photos.
"""
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy import func, text

from . import config, history
from .db import (Detection, EmbeddingRef, Event, Participant, PERSON_EVENT_TYPES, display_name, to_iso_utc, utcnow)
from .vision import cosine_similarity

STATUSES = ("probable", "a_verifier")
_SINGLE_LINE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MULTI_LINE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PersonError(ValueError):
    def __init__(self, message: str, field: Optional[str] = None, status: int = 422, **extra):
        super().__init__(message)
        self.message, self.field, self.status, self.extra = message, field, status, extra


class PersonNotFound(PersonError):
    def __init__(self, person_id: int):
        super().__init__("Personne introuvable.", status=404)


class PersonConflict(PersonError):
    def __init__(self, message: str, field: Optional[str] = None, **extra):
        super().__init__(message, field, status=409, **extra)


# ---- empreintes ------------------------------------------------------------------------------------
def embedding_to_bytes(emb: np.ndarray) -> bytes:
    return np.asarray(emb, dtype=np.float32).tobytes()


def bytes_to_embedding(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def rank_participants(db, embedding: np.ndarray) -> List[Dict[str, Any]]:
    """Similarité de l'empreinte avec chaque personne enrôlée (meilleure de ses références), décroissante."""
    best: Dict[int, Dict[str, Any]] = {}
    rows = (db.query(EmbeddingRef, Participant)
            .join(Participant, Participant.id == EmbeddingRef.participant_id).all())
    for ref, person in rows:
        stored = bytes_to_embedding(ref.embedding)
        if stored.shape != embedding.shape:
            continue                                  # empreinte d'un autre modèle : ignorée
        score = cosine_similarity(embedding, stored)
        current = best.get(person.id)
        if current is None or score > current["score"]:
            best[person.id] = {"participant_id": person.id, "nom": person.display_name, "score": score,
                               "actif": bool(person.actif)}
    return sorted(best.values(), key=lambda r: -r["score"])


def find_duplicate(db, embeddings: Sequence[np.ndarray], exclude_id: Optional[int] = None,
                   threshold: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Personne déjà enrôlée dont le visage ressemble à ces empreintes (doublon probable), sinon None."""
    if not embeddings:
        return None
    threshold = config.PERSON_DUPLICATE_THRESHOLD if threshold is None else threshold
    mean = np.mean(np.stack([np.asarray(e, dtype=np.float32) for e in embeddings]), axis=0)
    mean = mean / (np.linalg.norm(mean) + 1e-9)
    for row in rank_participants(db, mean.astype(np.float32)):
        if exclude_id is not None and row["participant_id"] == exclude_id:
            continue
        return row if row["score"] >= threshold else None
    return None


# ---- saisie ------------------------------------------------------------------------------------------
def _text(value: Any, field: str, max_len: int, multiline: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PersonError("Un texte est attendu.", field)
    value = value.strip()
    if len(value) > max_len:
        raise PersonError(f"Limité à {max_len} caractères.", field)
    if (_MULTI_LINE_CONTROL if multiline else _SINGLE_LINE_CONTROL).search(value):
        raise PersonError("Caractères de contrôle interdits.", field)
    return value


def clean_person_input(data: Dict[str, Any], creating: bool) -> Dict[str, Any]:
    """Valide et normalise les champs fournis (seuls ceux présents dans ``data`` sont retournés)."""
    out: Dict[str, Any] = {}
    if "nom" in data or creating:
        nom = _text(data.get("nom"), "nom", config.MAX_NAME_LENGTH)
        if not nom:
            raise PersonError("Le nom est obligatoire.", "nom")
        out["nom"] = nom
    if "prenom" in data:
        out["prenom"] = _text(data["prenom"], "prenom", config.MAX_PERSON_NAME) or None
    if "matricule" in data:
        out["matricule"] = _text(data["matricule"], "matricule", config.MAX_MATRICULE) or None
    if "infos" in data:
        out["infos"] = _text(data["infos"], "infos", config.MAX_INFOS_LENGTH, multiline=True)
    if "actif" in data:
        if not isinstance(data["actif"], bool):
            raise PersonError("« actif » doit être vrai ou faux.", "actif")
        out["actif"] = data["actif"]
    return out


def _check_matricule(db, matricule: Optional[str], exclude_id: Optional[int] = None):
    if not matricule:
        return
    query = db.query(Participant).filter(func.lower(Participant.matricule) == matricule.lower())
    if exclude_id is not None:
        query = query.filter(Participant.id != exclude_id)
    other = query.first()
    if other is not None:
        raise PersonConflict(f"Ce matricule est déjà attribué à la fiche n°{other.id}.", "matricule")


# ---- écriture ----------------------------------------------------------------------------------------
def _get(db, person_id: int) -> Participant:
    person = db.get(Participant, person_id)
    if person is None:
        raise PersonNotFound(person_id)
    return person


def create_person(db, fields: Dict[str, Any], references: Sequence[Tuple[np.ndarray, Optional[str]]]) -> Participant:
    """Crée la fiche et ses références. ``fields`` doit venir de ``clean_person_input(..., creating=True)``."""
    if not references:
        raise PersonError("Au moins une photo de référence avec un visage exploitable est nécessaire.", "photos")
    if len(references) > config.MAX_REFERENCES:
        raise PersonError(f"Maximum {config.MAX_REFERENCES} références par personne.", "photos")
    _check_matricule(db, fields.get("matricule"))
    now = utcnow()
    person = Participant(nom=fields["nom"], prenom=fields.get("prenom"), matricule=fields.get("matricule"),
                         infos=fields.get("infos", ""), actif=fields.get("actif", True),
                         date_enrolement=now, date_consentement=now, updated_at=now)
    db.add(person)
    db.flush()
    for emb, source in references:
        db.add(EmbeddingRef(participant_id=person.id, embedding=embedding_to_bytes(emb), source=source))
    db.commit()
    return person


def update_person(db, person_id: int, fields: Dict[str, Any]) -> Participant:
    person = _get(db, person_id)
    if "matricule" in fields:
        _check_matricule(db, fields["matricule"], exclude_id=person_id)
    for key, value in fields.items():
        setattr(person, key, value)
    person.updated_at = utcnow()
    db.commit()
    return person


def reference_count(db, person_id: int) -> int:
    return db.query(EmbeddingRef).filter(EmbeddingRef.participant_id == person_id).count()


def add_references(db, person_id: int, references: Sequence[Tuple[np.ndarray, Optional[str]]]) -> int:
    person = _get(db, person_id)
    if not references:
        raise PersonError("Aucun visage exploitable dans les photos envoyées.", "photos")
    if reference_count(db, person_id) + len(references) > config.MAX_REFERENCES:
        raise PersonConflict(f"Maximum {config.MAX_REFERENCES} références par personne.", "photos")
    for emb, source in references:
        db.add(EmbeddingRef(participant_id=person_id, embedding=embedding_to_bytes(emb), source=source))
    person.updated_at = utcnow()
    db.commit()
    return len(references)


def delete_reference(db, person_id: int, reference_id: int) -> None:
    _get(db, person_id)
    ref = db.get(EmbeddingRef, reference_id)
    if ref is None or ref.participant_id != person_id:
        raise PersonError("Référence introuvable.", status=404)
    if reference_count(db, person_id) <= 1:
        raise PersonConflict("Une personne doit garder au moins une photo de référence : supprimez la fiche pour la retirer.")
    db.delete(ref)
    db.query(Participant).filter(Participant.id == person_id).update({Participant.updated_at: utcnow()})
    db.commit()


# ---- lecture : liste -----------------------------------------------------------------------------------
def _like(value: str) -> str:
    return "%" + value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _summary_rows(db, ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
    out = {i: {"references": 0, "detections": 0, "last_seen": None, "last_camera": None} for i in ids}
    if not ids:
        return out
    for pid, n in db.query(EmbeddingRef.participant_id, func.count(EmbeddingRef.id)) \
            .filter(EmbeddingRef.participant_id.in_(ids)).group_by(EmbeddingRef.participant_id):
        out[pid]["references"] = n
    last_ids = []
    for pid, n, last, last_id in db.query(Detection.participant_id, func.count(Detection.id), func.max(Detection.timestamp),
                                          func.max(Detection.id)) \
            .filter(Detection.participant_id.in_(ids)).group_by(Detection.participant_id):
        out[pid].update(detections=n, last_seen=to_iso_utc(last) if last else None)
        last_ids.append(last_id)
    if last_ids:
        for pid, cam in db.query(Detection.participant_id, Detection.camera_id).filter(Detection.id.in_(last_ids)):
            out[pid]["last_camera"] = cam
    return out


def _brief(p: Participant) -> Dict[str, Any]:
    return {"id": p.id, "nom": p.nom, "prenom": p.prenom or "", "display_name": p.display_name,
            "matricule": p.matricule or "", "actif": bool(p.actif),
            "date_enrolement": to_iso_utc(p.date_enrolement) if p.date_enrolement else None,
            "date_consentement": to_iso_utc(p.date_consentement) if p.date_consentement else None}


def list_people(db, q: str = "", statut: str = "", page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    query = db.query(Participant)
    q = (q or "").strip()
    if q:
        pattern = _like(q)
        full = func.lower(func.coalesce(Participant.prenom + " ", "") + Participant.nom)
        query = query.filter(
            func.lower(Participant.nom).like(pattern, escape="\\") | func.lower(func.coalesce(Participant.prenom, "")).like(pattern, escape="\\")
            | func.lower(func.coalesce(Participant.matricule, "")).like(pattern, escape="\\") | full.like(pattern, escape="\\"))
    if statut == "actif":
        query = query.filter(Participant.actif.is_(True))
    elif statut == "inactif":
        query = query.filter(Participant.actif.is_(False))
    total = query.count()
    rows = query.order_by(func.lower(Participant.nom), func.lower(func.coalesce(Participant.prenom, "")), Participant.id) \
        .offset((page - 1) * page_size).limit(page_size).all()
    summary = _summary_rows(db, [p.id for p in rows])
    return {"items": [{**_brief(p), **summary[p.id]} for p in rows], "total": total, "page": page, "page_size": page_size}


# ---- lecture : activité et fiche --------------------------------------------------------------------------
def _camera_info(camera_meta: Dict[str, Dict[str, Any]], camera_id: str) -> Tuple[str, str]:
    meta = camera_meta.get(camera_id) or {}
    label = meta.get("label", camera_id)
    return label, (meta.get("location") or label)


def person_activity(db, participant_id: int, camera_meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Passages détectés pour cette personne (liaison par identifiant), sur toute la période conservée.

    Les passages pendant lesquels la personne était *la cible* sont enregistrés comme « cible » (sans identifiant)
    et ne sont pas comptés ici : relier une cible à une personne repose sur une estimation, qu'on ne grave pas dans
    l'historique.
    """
    cte, params, where = history._episode_query(datetime(1970, 1, 1), utcnow() + timedelta(days=1), "", "", "",
                                                participant_id)
    rows = db.execute(text(
        cte + "SELECT camera_id, COUNT(*) AS passages, SUM(observations) AS observations, "
              "SUM(CASE WHEN match_status = 'probable' THEN 1 ELSE 0 END) AS probable, "
              "MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen "
              f"FROM episodes{where} AND mode = 'DB' GROUP BY camera_id"), params).fetchall()
    empty = {"passages": 0, "observations": 0, "probable_passages": 0, "to_verify_passages": 0, "first_seen": None,
             "last_seen": None, "last_camera": None, "last_camera_label": None, "by_camera": [], "zones": []}
    if not rows:
        return empty

    by_camera, zones = [], {}
    for r in rows:
        label, zone = _camera_info(camera_meta, r.camera_id)
        last = to_iso_utc(datetime.fromisoformat(r.last_seen))
        by_camera.append({"camera_id": r.camera_id, "camera_label": label, "zone": zone, "passages": r.passages,
                          "observations": r.observations, "last_seen": last})
        z = zones.setdefault(zone, {"zone": zone, "passages": 0, "observations": 0, "last_seen": last})
        z["passages"] += r.passages
        z["observations"] += r.observations
        z["last_seen"] = max(z["last_seen"], last)
    by_camera.sort(key=lambda c: c["last_seen"], reverse=True)
    last_row = max(rows, key=lambda r: r.last_seen)
    total = sum(r.passages for r in rows)
    probable = sum(r.probable for r in rows)
    return {"passages": total, "observations": sum(r.observations for r in rows),
            "probable_passages": probable, "to_verify_passages": total - probable,
            "first_seen": to_iso_utc(datetime.fromisoformat(min(r.first_seen for r in rows))),
            "last_seen": to_iso_utc(datetime.fromisoformat(last_row.last_seen)),
            "last_camera": last_row.camera_id, "last_camera_label": _camera_info(camera_meta, last_row.camera_id)[0],
            "by_camera": by_camera, "zones": sorted(zones.values(), key=lambda z: z["last_seen"], reverse=True)}


def person_events(db, participant_id: int, limit: int = 15) -> List[Dict[str, Any]]:
    needle_a, needle_b = f'%"participant_id": {int(participant_id)},%', f'%"participant_id": {int(participant_id)}}}%'
    rows = (db.query(Event).filter(Event.type.in_(PERSON_EVENT_TYPES))
            .filter(Event.payload.like(needle_a) | Event.payload.like(needle_b))
            .order_by(Event.id.desc()).limit(limit * 3).all())
    out = []
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except ValueError:
            continue
        if payload.get("participant_id") != participant_id:
            continue
        out.append({"id": row.id, "timestamp": to_iso_utc(row.timestamp), "type": row.type, "severity": row.severity,
                    "camera_id": row.camera_id, "title": row.title, "status": payload.get("status")})
        if len(out) >= limit:
            break
    return out


def person_detail(db, person_id: int, camera_meta: Dict[str, Dict[str, Any]], passages: int = 20) -> Dict[str, Any]:
    """Fiche individuelle : enrôlement, références, activité, zones visitées, chronologie."""
    person = _get(db, person_id)
    refs = db.query(EmbeddingRef).filter(EmbeddingRef.participant_id == person_id).order_by(EmbeddingRef.id).all()
    since = utcnow() - timedelta(days=max(config.RETENTION_DAYS, 1) if config.RETENTION_DAYS > 0 else 3650)
    timeline = history.read_episodes(db, since, utcnow() + timedelta(seconds=1), page=1, page_size=passages,
                                     participant_id=person_id)
    for item in timeline["items"]:
        item["camera_label"] = _camera_info(camera_meta, item["camera_id"])[0]
    return {**_brief(person), "infos": person.infos or "",
            "updated_at": to_iso_utc(person.updated_at) if person.updated_at else None,
            "references": [{"id": r.id, "source": r.source or "—"} for r in refs],
            "activity": person_activity(db, person_id, camera_meta),
            "timeline": {"items": timeline["items"], "total": timeline["total"]},
            "events": person_events(db, person_id)}
