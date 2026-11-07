"""Historique et investigation (Module 8).

Une **chronologie** réunit, avec des filtres combinables, trois sortes d'éléments :
- ``event`` : événements de supervision (table ``events``) ;
- ``passage`` : détections regroupées par personne et par caméra (voir ``history.py``) ;
- ``move`` : étapes du trajet d'une cible, quand on filtre sur une session de cible.

Un **dossier d'événement** garde des *références* vers des événements ou des alertes choisis par l'opérateur
(jamais une copie) : un nom effacé par la suppression d'une personne ou la purge disparaît donc aussi du dossier.
"""
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from sqlalchemy import or_

from . import alerts as alerts_mod
from . import config, events as events_mod, history
from .db import Alert, Dossier, DossierItem, Event, TargetMove, to_iso_utc, utcnow

log = logging.getLogger(__name__)

MAX_ITEMS = 500                 # éléments renvoyés par une recherche
MAX_PER_SOURCE = 500
MAX_DOSSIERS = 100
MAX_DOSSIER_ITEMS = 300
MAX_TITLE = 120
MAX_NOTE = 2000
SESSION_MARGIN = timedelta(seconds=30)
EVENT_TYPES = tuple(t for fam in events_mod.FAMILIES.values() for t in fam)
PSEUDO_TYPES = ("passage", "move")
KINDS_FR = {"event": "Événement", "passage": "Passage", "move": "Étape de cible"}


class InvestigationError(Exception):
    def __init__(self, message: str, status: int = 422, field: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.field = field


def parse_types(raw: Optional[str]) -> List[str]:
    """``types`` = liste séparée par des virgules d'événements, de familles (``targets``…) ou de ``passage`` / ``move``."""
    out: List[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part in events_mod.FAMILIES:
            out += list(events_mod.FAMILIES[part])
        elif part in EVENT_TYPES or part in PSEUDO_TYPES:
            out.append(part)
        else:
            raise InvestigationError(f"Type inconnu : {part[:40]}", field="types")
    return list(dict.fromkeys(out))


def _t(iso_or_dt) -> datetime:
    if isinstance(iso_or_dt, datetime):
        return iso_or_dt
    return datetime.fromisoformat(str(iso_or_dt).replace("Z", "+00:00")).replace(tzinfo=None)


# ---- chronologie -------------------------------------------------------------------------------------------------------
def _json_like(field: str, value: int) -> Tuple[str, str]:
    return f'%"{field}": {int(value)},%', f'%"{field}": {int(value)}}}%'


def _event_item(e: Event, alert: Optional[Alert]) -> Dict[str, Any]:
    try:
        payload = json.loads(e.payload) if e.payload else {}
    except ValueError:
        payload = {}
    return {"key": f"event:{e.id}", "kind": "event", "id": e.id, "ts": to_iso_utc(e.timestamp), "end_ts": None,
            "type": e.type, "severity": e.severity, "camera_id": e.camera_id, "title": e.title,
            "participant_id": payload.get("participant_id") if config.HISTORY_SHOW_IDENTITIES else None,
            "target_id": payload.get("target_id"), "zone_id": payload.get("zone_id"),
            "alert": {"id": alert.id, "status": alert.status, "priority": alert.priority} if alert else None,
            "_t": e.timestamp}


def _query_events(db, since, until, camera_id, types, participant_id, target_id, q, only_alerts, limit):
    query = db.query(Event).filter(Event.timestamp >= since, Event.timestamp <= until)
    wanted = [t for t in types if t in EVENT_TYPES]
    if types and not wanted:
        return []                                     # seuls des types non « événement » sont demandés
    if wanted:
        query = query.filter(Event.type.in_(wanted))
    if camera_id:
        query = query.filter(Event.camera_id == camera_id)
    if participant_id is not None:
        a, b = _json_like("participant_id", participant_id)
        query = query.filter(or_(Event.payload.like(a), Event.payload.like(b)))
    if target_id is not None:
        a, b = _json_like("target_id", target_id)
        query = query.filter(or_(Event.payload.like(a), Event.payload.like(b)))
    if q:
        like = "%" + history._like_escape(q.strip()[:100]) + "%"
        query = query.filter(Event.title.like(like, escape="\\"))
    if only_alerts:
        query = query.filter(Event.id.in_(db.query(Alert.event_id).filter(Alert.event_id.isnot(None))))
    rows = query.order_by(Event.timestamp.desc(), Event.id.desc()).limit(limit).all()
    ids = [r.id for r in rows]
    by_event: Dict[int, Alert] = {}
    if ids:
        for a in db.query(Alert).filter(Alert.event_id.in_(ids)).order_by(Alert.id).all():
            by_event.setdefault(a.event_id, a)
    return [_event_item(r, by_event.get(r.id)) for r in rows]


def _query_passages(db, since, until, camera_id, types, participant_id, q, mode, limit):
    if types and "passage" not in types:
        return []
    data = history.read_episodes(db, since, until, camera_id or "", mode or "", q or "", 1, limit, participant_id)
    items = []
    for ep in data["items"]:
        kind = {"TARGET": "cible", "DB": "personne enrôlée", "UNK": "personne inconnue"}.get(ep["mode"], ep["mode"])
        who = ep["label"] or kind
        items.append({"key": f"passage:{ep['camera_id']}:{ep['track_id']}:{ep['first_seen']}", "kind": "passage",
                      "id": None, "ts": ep["first_seen"], "end_ts": ep["last_seen"], "type": "passage",
                      "severity": "info", "camera_id": ep["camera_id"],
                      "title": f"{who} — {ep['observations']} observation(s), {ep['duration_sec']} s",
                      "participant_id": ep["participant_id"], "target_id": None, "zone_id": None, "alert": None,
                      "mode": ep["mode"], "_t": _t(ep["first_seen"])})
    return items


def _query_moves(db, session: int, camera_id, types):
    if types and "move" not in types:
        return []
    q = db.query(TargetMove).filter(TargetMove.session == session)
    if camera_id:
        q = q.filter(TargetMove.camera_id == camera_id)
    items = []
    for m in q.order_by(TargetMove.timestamp, TargetMove.id).limit(MAX_PER_SOURCE).all():
        items.append({"key": f"move:{m.id}", "kind": "move", "id": m.id, "ts": to_iso_utc(m.timestamp), "end_ts": None,
                      "type": "move", "severity": "info", "camera_id": m.camera_id,
                      "title": f"{history.public_label('TARGET', m.label) or 'Cible'} — étape",
                      "participant_id": None, "target_id": None, "zone_id": None, "alert": None, "_t": m.timestamp})
    return items


def session_range(db, session: int) -> Tuple[datetime, datetime]:
    first = db.query(TargetMove).filter(TargetMove.session == session).order_by(TargetMove.timestamp).first()
    last = db.query(TargetMove).filter(TargetMove.session == session).order_by(TargetMove.timestamp.desc()).first()
    if first is None:
        raise InvestigationError("Session de cible introuvable.", 404, "session")
    return first.timestamp - SESSION_MARGIN, last.timestamp + SESSION_MARGIN


def timeline(db, since: datetime, until: datetime, camera_id: str = "", types: Optional[Sequence[str]] = None,
             participant_id: Optional[int] = None, target_id: Optional[int] = None, session: Optional[int] = None,
             q: str = "", only_alerts: bool = False, limit: int = MAX_ITEMS) -> Dict[str, Any]:
    """Éléments des plus récents aux plus anciens. ``truncated`` indique que la limite a coupé des résultats."""
    types = list(types or [])
    if participant_id is not None and not config.HISTORY_SHOW_IDENTITIES:
        raise InvestigationError("Les identités sont masquées : la recherche par personne est désactivée.", 403)
    limit = max(1, min(limit, MAX_ITEMS))
    if session is not None:
        since, until = session_range(db, session)
    items: List[Dict[str, Any]] = []
    try:
        items += _query_events(db, since, until, camera_id, types, participant_id, target_id, q, only_alerts, MAX_PER_SOURCE)
        if not only_alerts and target_id is None:
            mode = "TARGET" if session is not None else ""
            items += _query_passages(db, since, until, camera_id, types, participant_id, q, mode, MAX_PER_SOURCE)
        if session is not None:
            items += _query_moves(db, session, camera_id, types)
    except history.HistoryError as exc:
        raise InvestigationError(str(exc))
    if session is not None:                      # une session de cible : on ne garde que ce qui la concerne
        items = [i for i in items if i["kind"] != "event" or i["type"] in events_mod.FAMILIES["targets"]]
    items.sort(key=lambda i: (i["_t"], i["key"]), reverse=True)
    truncated = len(items) > limit
    items = items[:limit]
    counts: Dict[str, int] = {}
    for i in items:
        counts[i["type"]] = counts.get(i["type"], 0) + 1
        i.pop("_t", None)
    return {"items": items, "total": len(items), "truncated": truncated, "counts": counts,
            "since": to_iso_utc(since), "until": to_iso_utc(until), "identities_visible": config.HISTORY_SHOW_IDENTITIES}


def event_detail(db, event_id: int, context_sec: int = 60, recordings_dir=None) -> Dict[str, Any]:
    e = db.get(Event, event_id)
    if e is None:
        raise InvestigationError("Événement introuvable.", 404)
    item = _event_item(e, None)
    item.pop("_t", None)
    try:
        item["payload"] = json.loads(e.payload) if e.payload else {}
    except ValueError:
        item["payload"] = {}
    if not config.HISTORY_SHOW_IDENTITIES:
        for k in ("person", "participant_id"):
            item["payload"].pop(k, None)
    linked = db.query(Alert).filter(Alert.event_id == e.id).order_by(Alert.id).all()
    if linked:
        item["alert"] = {"id": linked[0].id, "status": linked[0].status, "priority": linked[0].priority}
    item["alerts"] = [alerts_mod.view(a) for a in linked]
    near = db.query(Event).filter(Event.id != e.id, Event.timestamp >= e.timestamp - timedelta(seconds=context_sec),
                                  Event.timestamp <= e.timestamp + timedelta(seconds=context_sec))
    if e.camera_id:
        near = near.filter(Event.camera_id == e.camera_id)
    else:
        near = near.filter(Event.camera_id.is_(None))
    item["context"] = [{"id": n.id, "ts": to_iso_utc(n.timestamp), "type": n.type, "title": n.title}
                       for n in near.order_by(Event.timestamp, Event.id).limit(10).all()]
    # Un extrait n'existe que si l'enregistrement était actif à ce moment-là : sinon, seule l'observation est conservée.
    hit = None
    if recordings_dir is not None and e.camera_id:
        from . import recordings
        hit = recordings.find_for_time(db, e.camera_id, e.timestamp, recordings_dir)
    if hit:
        item["video"] = {"available": True, "note": "Un extrait vidéo existe pour cet instant.", **hit}
    else:
        item["video"] = {"available": False, "note": "Aucun extrait vidéo : seule une observation est enregistrée."}
    return item


# ---- export CSV -------------------------------------------------------------------------------------------------------
CSV_HEADER = ["debut_utc", "fin_utc", "nature", "type", "gravite", "camera", "description", "alerte", "etat_alerte"]


def iter_csv(result: Dict[str, Any]) -> Iterator[str]:
    def line(cells):
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="\r\n").writerow([history.csv_safe(c) for c in cells])
        return buf.getvalue()

    yield "﻿" + line(CSV_HEADER)
    for i in result["items"]:
        a = i.get("alert") or {}
        yield line([i["ts"], i.get("end_ts") or "", KINDS_FR.get(i["kind"], i["kind"]), i["type"], i["severity"],
                    i.get("camera_id") or "", i["title"], a.get("id", ""), alerts_mod.STATUS_LABELS.get(a.get("status"), "")])


# ---- dossiers --------------------------------------------------------------------------------------------------------------
def _clean_text(value: Any, field: str, limit: int, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise InvestigationError("Ce champ est obligatoire.", field=field)
        return None
    if not isinstance(value, str):
        raise InvestigationError("Texte attendu.", field=field)
    value = value.strip()
    if required and not value:
        raise InvestigationError("Ce champ est obligatoire.", field=field)
    if len(value) > limit:
        raise InvestigationError(f"Trop long ({limit} caractères au plus).", field=field)
    return value or None


def dossier_view(d: Dossier, count: int) -> Dict[str, Any]:
    return {"id": d.id, "title": d.title, "note": d.note, "created_at": to_iso_utc(d.created_at),
            "updated_at": to_iso_utc(d.updated_at), "items_count": count}


def list_dossiers(db) -> List[Dict[str, Any]]:
    from sqlalchemy import func
    counts = dict(db.query(DossierItem.dossier_id, func.count(DossierItem.id)).group_by(DossierItem.dossier_id).all())
    return [dossier_view(d, counts.get(d.id, 0)) for d in db.query(Dossier).order_by(Dossier.updated_at.desc(), Dossier.id.desc()).all()]


def create_dossier(db, title: Any, note: Any = None) -> Dict[str, Any]:
    title = _clean_text(title, "title", MAX_TITLE, required=True)
    note = _clean_text(note, "note", MAX_NOTE)
    if db.query(Dossier).count() >= MAX_DOSSIERS:
        raise InvestigationError(f"Limite de {MAX_DOSSIERS} dossiers atteinte.", 409)
    d = Dossier(title=title, note=note)
    db.add(d); db.commit()
    return dossier_view(d, 0)


def update_dossier(db, dossier_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
    d = _get_dossier(db, dossier_id)
    if "title" in data:
        d.title = _clean_text(data["title"], "title", MAX_TITLE, required=True)
    if "note" in data:
        d.note = _clean_text(data["note"], "note", MAX_NOTE)
    d.updated_at = utcnow()
    db.commit()
    return dossier_view(d, db.query(DossierItem).filter(DossierItem.dossier_id == d.id).count())


def delete_dossier(db, dossier_id: int) -> None:
    d = _get_dossier(db, dossier_id)
    db.query(DossierItem).filter(DossierItem.dossier_id == d.id).delete(synchronize_session=False)
    db.delete(d); db.commit()


def _get_dossier(db, dossier_id: int) -> Dossier:
    d = db.get(Dossier, dossier_id)
    if d is None:
        raise InvestigationError("Dossier introuvable.", 404)
    return d


def add_item(db, dossier_id: int, kind: Any, ref_id: Any, note: Any = None) -> Dict[str, Any]:
    d = _get_dossier(db, dossier_id)
    if kind not in ("event", "alert"):
        raise InvestigationError("Type d'élément inconnu (event ou alert).", field="kind")
    if isinstance(ref_id, bool) or not isinstance(ref_id, int) or ref_id < 1:
        raise InvestigationError("Identifiant invalide.", field="ref_id")
    note = _clean_text(note, "note", MAX_NOTE)
    target = db.get(Event if kind == "event" else Alert, ref_id)
    if target is None:
        raise InvestigationError("Élément introuvable.", 404, "ref_id")
    existing = db.query(DossierItem).filter_by(dossier_id=d.id, kind=kind, ref_id=ref_id).first()
    if existing:
        raise InvestigationError("Cet élément est déjà dans le dossier.", 409)
    if db.query(DossierItem).filter(DossierItem.dossier_id == d.id).count() >= MAX_DOSSIER_ITEMS:
        raise InvestigationError(f"Un dossier contient {MAX_DOSSIER_ITEMS} éléments au plus.", 409)
    item = DossierItem(dossier_id=d.id, kind=kind, ref_id=ref_id, note=note)
    d.updated_at = utcnow()
    db.add(item); db.commit()
    return {"id": item.id}


def update_item(db, dossier_id: int, item_id: int, note: Any) -> None:
    item = db.query(DossierItem).filter_by(id=item_id, dossier_id=dossier_id).first()
    if item is None:
        raise InvestigationError("Élément introuvable.", 404)
    item.note = _clean_text(note, "note", MAX_NOTE)
    _get_dossier(db, dossier_id).updated_at = utcnow()
    db.commit()


def remove_item(db, dossier_id: int, item_id: int) -> None:
    item = db.query(DossierItem).filter_by(id=item_id, dossier_id=dossier_id).first()
    if item is None:
        raise InvestigationError("Élément introuvable.", 404)
    db.delete(item)
    _get_dossier(db, dossier_id).updated_at = utcnow()
    db.commit()


def get_dossier(db, dossier_id: int) -> Dict[str, Any]:
    """Le dossier avec ses éléments **résolus** à l'instant T, du plus ancien au plus récent."""
    d = _get_dossier(db, dossier_id)
    rows = db.query(DossierItem).filter(DossierItem.dossier_id == d.id).all()
    out = []
    for r in rows:
        entry: Dict[str, Any] = {"item_id": r.id, "kind": r.kind, "ref_id": r.ref_id, "note": r.note,
                                 "added_at": to_iso_utc(r.added_at), "missing": False}
        if r.kind == "event":
            e = db.get(Event, r.ref_id)
            if e is None:
                entry.update(missing=True, ts=None, title="Événement supprimé (purge ou anonymisation)", type=None,
                             camera_id=None, severity="info")
            else:
                entry.update(ts=to_iso_utc(e.timestamp), type=e.type, severity=e.severity, camera_id=e.camera_id, title=e.title)
        else:
            a = db.get(Alert, r.ref_id)
            if a is None:
                entry.update(missing=True, ts=None, title="Alerte supprimée (purge)", type=None, camera_id=None, severity="info")
            else:
                entry.update(ts=to_iso_utc(a.created_at), type=a.type, severity="warning" if a.priority in ("high", "critical") else "info",
                             camera_id=a.camera_id, title=a.title, alert_status=a.status, priority=a.priority)
        out.append(entry)
    out.sort(key=lambda x: (x["ts"] is None, x["ts"] or "", x["item_id"]))
    res = dossier_view(d, len(out))
    res["items"] = out
    return res


# ---- rapports PDF ---------------------------------------------------------------------------------------------------------
def _fmt_ts(iso: Optional[str]) -> str:
    return _t(iso).strftime("%d/%m/%Y %H:%M:%S") if iso else "-"


def timeline_pdf(result: Dict[str, Any], labels: Dict[str, str], description: str, title: str = "Rapport d'investigation") -> bytes:
    from . import pdf_report
    cam = lambda c: labels.get(c or "", c or "-")
    by_cam: Dict[str, int] = {}
    for i in result["items"]:
        by_cam[cam(i.get("camera_id"))] = by_cam.get(cam(i.get("camera_id")), 0) + 1
    type_fr = lambda t: alerts_mod.TYPES.get(t, (t,))[0] if t not in ("passage", "move") else {"passage": "Passage", "move": "Étape de cible"}[t]
    summary = [f"Période (UTC) : du {_fmt_ts(result['since'])} au {_fmt_ts(result['until'])}.", description,
               f"{result['total']} élément(s)" + (" - résultats tronqués : affinez les filtres pour voir le reste." if result["truncated"] else "") + "."]
    sections: List[Dict[str, Any]] = [{"heading": "Résumé", "paragraphs": summary}]
    if result["counts"]:
        sections.append({"heading": "Répartition",
                         "table": {"header": ["Type", "Nombre"], "widths": [4, 1],
                                   "rows": [[type_fr(t), n] for t, n in sorted(result["counts"].items(), key=lambda x: -x[1])]}})
        sections.append({"heading": "Par caméra",
                         "table": {"header": ["Caméra", "Nombre"], "widths": [4, 1],
                                   "rows": [[c, n] for c, n in sorted(by_cam.items(), key=lambda x: -x[1])]}})
    chrono = sorted(result["items"], key=lambda i: i["ts"])
    sections.append({"heading": "Chronologie", "table": {
        "header": ["Heure (UTC)", "Nature", "Caméra", "Description", "Alerte"], "widths": [2.2, 1.8, 2, 6, 1.4],
        "rows": [[_fmt_ts(i["ts"]), KINDS_FR.get(i["kind"], i["kind"]), cam(i.get("camera_id")), i["title"],
                  f"n°{i['alert']['id']}" if i.get("alert") else ""] for i in chrono]}})
    return pdf_report.build_pdf(title, "Historique et investigation", sections)


def dossier_pdf(dossier: Dict[str, Any], labels: Dict[str, str]) -> bytes:
    from . import pdf_report
    cam = lambda c: labels.get(c or "", c or "-")
    paragraphs = [f"Créé le {_fmt_ts(dossier['created_at'])} (UTC), mis à jour le {_fmt_ts(dossier['updated_at'])}.",
                  f"{dossier['items_count']} élément(s) choisi(s) par l'opérateur."]
    if dossier.get("note"):
        paragraphs.append("Note : " + dossier["note"])
    missing = sum(1 for i in dossier["items"] if i["missing"])
    if missing:
        paragraphs.append(f"{missing} élément(s) ne sont plus disponibles (purge ou anonymisation) et apparaissent sans contenu.")
    rows = [[_fmt_ts(i["ts"]), "Alerte" if i["kind"] == "alert" else "Événement", cam(i.get("camera_id")), i["title"], i.get("note") or ""]
            for i in dossier["items"]]
    sections = [{"heading": "Résumé", "paragraphs": paragraphs},
                {"heading": "Chronologie", "table": {"header": ["Heure (UTC)", "Nature", "Caméra", "Description", "Note de l'opérateur"],
                                                    "widths": [2.2, 1.4, 2, 5, 4], "rows": rows}}]
    return pdf_report.build_pdf("Dossier : " + dossier["title"], "Dossier d'événement", sections)
