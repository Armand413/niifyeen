"""Rapports d'activité paramétrables (Module 12).

Un rapport assemble des sections choisies (synthèse, caméras, profil horaire, lignes et zones, alertes, incidents,
transitions, comparaison) sur une période et une caméra. Il est **anonyme** : uniquement des totaux et des titres
d'incidents techniques (pannes, zones), jamais de nom, d'identifiant de fiche ni d'image.
Le même objet sert à l'aperçu (JSON) et au PDF.
"""
import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import pdf_report, stats
from .db import Alert, Event, Setting, to_iso_utc

SETTINGS_KEY = "report_templates"
MAX_TEMPLATES = 20
MAX_INCIDENTS = 30
DEFAULT_TITLE = "Rapport d'activité"
SECTIONS = {
    "summary": "Synthèse",
    "cameras": "Activité par caméra",
    "hours": "Passages par heure de la journée",
    "zones": "Lignes et zones",
    "alerts": "Alertes",
    "incidents": "Incidents techniques et de zone",
    "transitions": "Transitions entre caméras",
    "comparison": "Comparaison avec la période précédente",
}
INCIDENT_TYPES = ("camera_offline", "camera_obstructed", "camera_frozen", "zone_intrusion", "zone_overcrowded")
PRIORITY_FR = {"low": "Basse", "medium": "Moyenne", "high": "Haute", "critical": "Critique"}
STATUS_FR = {"new": "Nouvelle", "seen": "Vue", "acknowledged": "Prise en compte", "closed": "Clôturée"}


class ReportError(ValueError):
    pass


def parse_params(title: str = "", camera_id: str = "", sections: Optional[str] = None, tz_offset_min: int = 0) -> Dict[str, Any]:
    title = " ".join((title or "").split())[:100] or DEFAULT_TITLE
    if sections is None or not str(sections).strip():
        chosen = list(SECTIONS)
    else:
        chosen = []
        for name in str(sections).split(","):
            name = name.strip()
            if not name:
                continue
            if name not in SECTIONS:
                raise ReportError(f"Section inconnue : {name[:30]}.")
            if name not in chosen:
                chosen.append(name)
        if not chosen:
            raise ReportError("Choisissez au moins une section.")
    # l'ordre est toujours celui du rapport, quel que soit l'ordre demandé
    chosen = [s for s in SECTIONS if s in chosen]
    return {"title": title, "camera_id": (camera_id or "").strip()[:100], "sections": chosen,
            "tz_offset_min": max(-840, min(840, int(tz_offset_min)))}


def _fmt(dt: datetime, off: timedelta) -> str:
    return (dt + off).strftime("%d/%m/%Y %H:%M")


def _fmt_dur(sec: Optional[float]) -> str:
    if sec is None:
        return "-"
    total = int(round(sec))
    return f"{total} s" if total < 60 else f"{total // 60} min {total % 60:02d} s"


def build(db, since: datetime, until: datetime, params: Dict[str, Any], camera_labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    labels = camera_labels or {}
    name = lambda c: labels.get(c) or c           # noqa: E731
    cam = params["camera_id"]
    off = timedelta(minutes=params["tz_offset_min"])
    wanted = params["sections"]
    ov = stats.overview(db, since, until, cam, params["tz_offset_min"]) if set(wanted) & {"summary", "cameras", "hours", "zones"} else None
    out: List[Dict[str, Any]] = []

    for sid in wanted:
        sec: Dict[str, Any] = {"id": sid, "heading": SECTIONS[sid], "paragraphs": []}
        if sid == "summary":
            sec["paragraphs"] = [
                f"{ov['passages']} passage(s) et {ov['observations']} observation(s) sur la période.",
                f"Personnes enrôlées distinctes : {ov['unique_enrolled']}. Passages d'inconnus : {ov['unknown_passages']} "
                f"(les inconnus ne sont pas dédoublonnés). Passages d'une cible : {ov['by_mode']['TARGET']}.",
                f"Durée moyenne d'un passage : {_fmt_dur(ov['avg_duration_sec'])} (médiane : {_fmt_dur(ov['median_duration_sec'])}).",
                "Une observation est une ligne de détection ; un passage est une personne visible sans interruption sur une caméra.",
            ]
        elif sid == "cameras":
            sec["table"] = {"header": ["Caméra", "Observations", "Passages", "Personnes enrôlées", "Durée moyenne"],
                            "rows": [[name(c["camera_id"]), c["observations"], c["passages"], c["unique_enrolled"], _fmt_dur(c["avg_duration_sec"])]
                                     for c in ov["by_camera"]], "widths": [3, 2, 2, 2, 2]}
            if not ov["by_camera"]:
                sec["paragraphs"] = ["Aucune détection sur la période."]
        elif sid == "hours":
            sec["bars"] = {"labels": [f"{h:02d}" for h in range(24)], "values": ov["by_hour"]}
            sec["paragraphs"] = ["Passages par heure locale de début de passage." if sum(ov["by_hour"]) else "Aucun passage sur la période."]
        elif sid == "zones":
            sec["table"] = {"header": ["Ligne / zone", "Caméra", "Entrées", "Sorties", "Occupation max."],
                            "rows": [[z["name"], name(z["camera_id"]), z["entries"], z["exits"], z["occupancy_max"]] for z in ov["zones"]],
                            "widths": [4, 3, 1.5, 1.5, 2]}
            if not ov["zones"]:
                sec["paragraphs"] = ["Aucune ligne ni zone définie."]
        elif sid == "alerts":
            q = db.query(Alert).filter(Alert.created_at >= since, Alert.created_at <= until)
            if cam:
                q = q.filter(Alert.camera_id == cam)
            rows = q.all()
            by_prio = Counter(a.priority for a in rows)
            by_status = Counter(a.status for a in rows)
            by_type = Counter(a.type for a in rows)
            from .alerts import TYPES
            sec["paragraphs"] = [f"{len(rows)} alerte(s) créée(s) ({sum(a.count for a in rows)} événement(s) regroupé(s)). "
                                 + "Priorités : " + (", ".join(f"{PRIORITY_FR.get(k, k)} {v}" for k, v in sorted(by_prio.items())) or "aucune") + ". "
                                 + "États : " + (", ".join(f"{STATUS_FR.get(k, k)} {v}" for k, v in sorted(by_status.items())) or "aucun") + "."]
            sec["table"] = {"header": ["Type d'alerte", "Nombre"],
                            "rows": [[TYPES.get(t, (t, ""))[0], n] for t, n in by_type.most_common()], "widths": [5, 1]}
        elif sid == "incidents":
            q = db.query(Event).filter(Event.timestamp >= since, Event.timestamp <= until, Event.type.in_(INCIDENT_TYPES))
            if cam:
                q = q.filter(Event.camera_id == cam)
            total = q.count()
            rows = q.order_by(Event.timestamp.desc()).limit(MAX_INCIDENTS).all()
            sec["paragraphs"] = [f"{total} incident(s)" + (f", {MAX_INCIDENTS} plus récents affichés." if total > MAX_INCIDENTS else ".")]
            sec["table"] = {"header": ["Date", "Caméra", "Incident"],
                            "rows": [[_fmt(e.timestamp, off), name(e.camera_id) if e.camera_id else "-", e.title] for e in rows], "widths": [2, 2, 6]}
        elif sid == "transitions":
            t = stats.transitions(db, since, until)
            sec["table"] = {"header": ["De", "Vers", "Nombre", "Cibles", "Enrôlées", "Délai médian"],
                            "rows": [[name(e["from"]), name(e["to"]), e["count"], e["target"], e["enrolled"], _fmt_dur(e["median_gap_sec"])] for e in t["edges"]],
                            "widths": [3, 3, 1.5, 1.5, 1.5, 2]}
            sec["paragraphs"] = [t["note"]] if t["edges"] else ["Aucune transition sur la période. " + t["note"]]
        elif sid == "comparison":
            span = until - since
            c = stats.compare(db, since, until, since - span, since, cam)
            labels_fr = [("passages", "Passages"), ("observations", "Observations"), ("unique_enrolled", "Personnes enrôlées"),
                         ("unknown_passages", "Passages d'inconnus"), ("avg_duration_sec", "Durée moyenne (s)")]

            def pct(d):
                return "n/a" if d["pct"] is None else f"{d['pct']:+g} %".replace(".", ",")
            sec["table"] = {"header": ["Mesure", "Période", "Référence", "Écart"],
                            "rows": [[lab, c["delta"][k]["a"] if c["delta"][k]["a"] is not None else "-",
                                      c["delta"][k]["b"] if c["delta"][k]["b"] is not None else "-", pct(c["delta"][k])] for k, lab in labels_fr],
                            "widths": [4, 2, 2, 2]}
            sec["paragraphs"] = [f"Référence : {_fmt(since - span, off)} à {_fmt(since, off)}. « n/a » : référence à zéro."]
        out.append(sec)

    scope = f"caméra {name(cam)}" if cam else "toutes les caméras"
    return {"title": params["title"], "subtitle": f"Période du {_fmt(since, off)} au {_fmt(until, off)} - {scope}",
            "since": to_iso_utc(since), "until": to_iso_utc(until), "camera_id": cam or None, "sections": out, "anonymous": True}


def render_pdf(report: Dict[str, Any]) -> bytes:
    return pdf_report.build_pdf(report["title"], report["subtitle"] + ". Rapport anonyme : aucun nom ni image.", report["sections"])


# ---- modèles de rapport (réglages enregistrés) -------------------------------------------------------------------------------------
def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 40:
        raise ReportError("Le nom du modèle doit faire de 1 à 40 caractères.")
    return name


def list_templates(db) -> Dict[str, Dict[str, Any]]:
    row = db.get(Setting, SETTINGS_KEY)
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_template(db, name: str, minutes: int, params: Dict[str, Any]) -> Dict[str, Any]:
    name = _clean_name(name)
    templates = list_templates(db)
    if name not in templates and len(templates) >= MAX_TEMPLATES:
        raise ReportError(f"Au plus {MAX_TEMPLATES} modèles : supprimez-en un.")
    templates[name] = {"title": params["title"], "camera_id": params["camera_id"], "sections": params["sections"], "minutes": int(minutes)}
    row = db.get(Setting, SETTINGS_KEY)
    if row is None:
        row = Setting(key=SETTINGS_KEY, value="")
        db.add(row)
    row.value = json.dumps(templates, ensure_ascii=False)
    db.commit()
    return templates


def delete_template(db, name: str) -> bool:
    templates = list_templates(db)
    if name not in templates:
        return False
    del templates[name]
    db.get(Setting, SETTINGS_KEY).value = json.dumps(templates, ensure_ascii=False)
    db.commit()
    return True
