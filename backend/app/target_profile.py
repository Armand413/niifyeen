"""Fiche de la cible : correspondance avec les personnes enrôlées et informations enregistrées.

Quand une cible est envoyée (photo), son empreinte faciale est comparée à celles des personnes
enrôlées. Si l'une d'elles dépasse ``PROFILE_THRESHOLD``, la fiche regroupe tout ce que la base
contient sur elle : fiche d'enrôlement, empreintes, et activité détectée par les caméras.

Une correspondance est une estimation : elle peut être fausse (voir le seuil dans la configuration).
"""
from typing import Any, Dict, Optional

import numpy as np

from . import config
from .db import EmbeddingRef, Participant, to_iso_utc
from .people import person_activity, rank_participants

MAX_OTHER_CANDIDATES = 3


def find_match(db, embedding: np.ndarray, threshold: Optional[float] = None) -> Dict[str, Any]:
    """Résultat de la comparaison, à conserver pour reconstruire la fiche (``render_profile``)."""
    threshold = config.PROFILE_THRESHOLD if threshold is None else threshold
    ranked = rank_participants(db, embedding)
    best = ranked[0] if ranked else None
    if best is None or best["score"] < threshold:
        return {"matched": False, "participant_id": None, "score": None, "threshold": threshold,
                "best_score": round(best["score"], 3) if best else None, "enrolled_count": len(ranked),
                "others": []}
    others = [r for r in ranked[1:] if r["score"] >= threshold][:MAX_OTHER_CANDIDATES]
    return {"matched": True, "participant_id": best["participant_id"], "score": best["score"],
            "threshold": threshold, "best_score": best["score"], "enrolled_count": len(ranked),
            "others": [{"nom": o["nom"], "score": round(o["score"], 3)} for o in others]}


def render_profile(db, match: Optional[Dict[str, Any]], camera_meta: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Fiche à afficher, reconstruite à la demande (l'activité évolue, la personne peut être supprimée)."""
    if not match:
        return {"matched": False, "reason": "no_target"}
    base = {"threshold": match["threshold"], "enrolled_count": match["enrolled_count"]}
    if not match["matched"]:
        return {**base, "matched": False, "reason": "not_enrolled",
                "best_score": None if match["best_score"] is None else round(match["best_score"], 3)}

    person = db.get(Participant, match["participant_id"])
    if person is None:
        return {**base, "matched": False, "reason": "deleted"}
    score = round(match["score"], 3)
    if not config.HISTORY_SHOW_IDENTITIES:
        return {**base, "matched": True, "hidden": True, "score": score}

    sources = [s for (s,) in db.query(EmbeddingRef.source).filter(EmbeddingRef.participant_id == person.id).all()]
    return {
        **base, "matched": True, "hidden": False, "score": score,
        # « nom » = nom affiché (prénom + nom) : c'est la clé historique lue par l'interface.
        "participant": {"id": person.id, "nom": person.display_name, "matricule": person.matricule or "",
                        "actif": bool(person.actif), "infos": person.infos or "",
                        "date_enrolement": to_iso_utc(person.date_enrolement) if person.date_enrolement else None,
                        "date_consentement": to_iso_utc(person.date_consentement) if person.date_consentement else None},
        "enrolment": {"embeddings": len(sources), "sources": sorted({s for s in sources if s})},
        "activity": person_activity(db, person.id, camera_meta),
        "other_candidates": match["others"],
    }
