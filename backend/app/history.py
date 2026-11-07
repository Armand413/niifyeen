"""Historique des détections : passages, synthèse, trajets de la cible, export CSV.

Une *observation* est une ligne de la table ``detections`` (une toutes les ~0,8 s par personne
visible). Un *passage* regroupe les observations d'une même piste sur une même caméra, tant
que deux observations consécutives sont espacées de moins de ``HISTORY_GAP_SEC`` secondes.
Son type est le plus « fort » rencontré pendant le passage : CIBLE > personne enrôlée > inconnu.
"""
import csv
import io
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import text

from . import config
from .db import TargetMove, to_iso_utc, utcnow

MODES = ("TARGET", "DB", "UNK")
BUCKET_CHOICES_MIN = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720, 1440]
MAX_BUCKETS = 60

_EPISODES_CTE = """
WITH base AS (
  -- Le nom affiché est le nom ACTUEL de la personne (liaison par identifiant) : un renommage se répercute sur tout
  -- son historique. Sans identifiant (anciennes lignes non rattachées), on garde le libellé enregistré.
  SELECT d.camera_id, d.track_id, d.mode,
         COALESCE(NULLIF(TRIM(COALESCE(p.prenom || ' ', '') || p.nom), ''), d.label) AS label,
         d.global_id, d.target_score, d.db_score, d.timestamp, d.participant_id, d.match_status,
         (julianday(d.timestamp) - 2440587.5) * 86400.0 AS t,
         CASE d.mode WHEN 'TARGET' THEN 3 WHEN 'DB' THEN 2 ELSE 1 END AS prio
  FROM detections d LEFT JOIN participants p ON p.id = d.participant_id
  WHERE d.timestamp >= :since AND d.timestamp <= :until {camera_clause}
),
flagged AS (
  SELECT *, CASE WHEN LAG(t) OVER w IS NULL OR t - LAG(t) OVER w > :gap THEN 1 ELSE 0 END AS is_new
  FROM base
  WINDOW w AS (PARTITION BY camera_id, track_id ORDER BY t)
),
numbered AS (
  SELECT *, SUM(is_new) OVER (PARTITION BY camera_id, track_id ORDER BY t
                              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS ep
  FROM flagged
),
ranked AS (
  SELECT camera_id, track_id, ep, mode, label, participant_id, match_status,
         MIN(timestamp) OVER p AS first_seen, MAX(timestamp) OVER p AS last_seen,
         MIN(t) OVER p AS first_t, MAX(t) OVER p AS last_t,
         COUNT(*) OVER p AS observations,
         MAX(target_score) OVER p AS max_target_score,
         MAX(db_score) OVER p AS max_db_score,
         MAX(global_id) OVER p AS global_id,
         ROW_NUMBER() OVER (PARTITION BY camera_id, track_id, ep
                            ORDER BY prio DESC, COALESCE(target_score, db_score, 0) DESC, t DESC) AS rn
  FROM numbered
  WINDOW p AS (PARTITION BY camera_id, track_id, ep)
),
episodes AS (SELECT * FROM ranked WHERE rn = 1)
"""


class HistoryError(ValueError):
    """Paramètre de période ou de filtre invalide (message affichable)."""


# ---- Période ---------------------------------------------------------------------------
def parse_datetime(value: str) -> datetime:
    """ISO 8601 -> datetime UTC naïf. Sans fuseau, la valeur est considérée comme UTC."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HistoryError(f"Date invalide : « {value} ».")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def resolve_range(minutes: Optional[int] = None, start: Optional[str] = None,
                  end: Optional[str] = None, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    now = now or utcnow()
    if start or end:
        until = parse_datetime(end) if end else now
        since = parse_datetime(start) if start else until - timedelta(minutes=minutes or 60)
    else:
        until = now
        since = now - timedelta(minutes=minutes or 60)
    if since >= until:
        raise HistoryError("La date de début doit précéder la date de fin.")
    if until - since > timedelta(days=config.HISTORY_MAX_SPAN_DAYS):
        raise HistoryError(f"Période trop longue (maximum {config.HISTORY_MAX_SPAN_DAYS} jours).")
    return since, until


def _sql_dt(dt: datetime) -> str:
    return dt.isoformat(sep=" ")          # même format que celui stocké par SQLite


# ---- Identité --------------------------------------------------------------------------
def public_label(mode: str, label: Optional[str]) -> Optional[str]:
    """Nom affiché dans l'historique, ou libellé générique si les identités sont masquées."""
    if config.HISTORY_SHOW_IDENTITIES:
        return label
    return {"DB": "Personne enrôlée", "TARGET": "Cible"}.get(mode)


# ---- Passages --------------------------------------------------------------------------
def _check_sqlite():
    if sqlite3.sqlite_version_info < (3, 25):
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} trop ancien : l'historique exige la version 3.25 ou plus.")


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _episode_query(since, until, camera_id, mode, label, participant_id=None):
    """Fragment commun (CTE + filtres) et ses paramètres."""
    if mode and mode not in MODES:
        raise HistoryError("Type inconnu (attendu : TARGET, DB ou UNK).")
    params: Dict[str, Any] = {"since": _sql_dt(since), "until": _sql_dt(until), "gap": config.HISTORY_GAP_SEC}
    camera_clause = ""
    if camera_id:
        camera_clause = "AND d.camera_id = :camera_id"
        params["camera_id"] = camera_id
    where = []
    if mode:
        where.append("mode = :mode")
        params["mode"] = mode
    if participant_id is not None:
        where.append("participant_id = :participant_id")
        params["participant_id"] = int(participant_id)
    # La recherche par nom est désactivée quand les identités sont masquées (sinon elle les révèlerait).
    if label and config.HISTORY_SHOW_IDENTITIES:
        where.append("LOWER(label) LIKE :label ESCAPE '\\'")
        params["label"] = f"%{_like_escape(label.strip().lower())}%"
    sql = _EPISODES_CTE.format(camera_clause=camera_clause)
    return sql, params, (" WHERE " + " AND ".join(where)) if where else ""


def _row_to_episode(row) -> Dict[str, Any]:
    mode = row.mode
    first = datetime.fromisoformat(row.first_seen)
    last = datetime.fromisoformat(row.last_seen)
    score = row.max_target_score if mode == "TARGET" else row.max_db_score if mode == "DB" else None
    return {
        "camera_id": row.camera_id,
        "track_id": row.track_id,
        "global_id": row.global_id,
        "mode": mode,
        "label": public_label(mode, row.label),
        # L'identifiant ne sert qu'à ouvrir la fiche : on ne le diffuse pas quand les identités sont masquées.
        "participant_id": row.participant_id if config.HISTORY_SHOW_IDENTITIES else None,
        "match_status": row.match_status if mode == "DB" else None,
        "first_seen": to_iso_utc(first),
        "last_seen": to_iso_utc(last),
        "duration_sec": round(max(0.0, row.last_t - row.first_t), 1),
        "observations": row.observations,
        "best_score": round(score, 3) if score is not None else None,
    }


def read_episodes(db, since: datetime, until: datetime, camera_id: str = "", mode: str = "",
                  label: str = "", page: int = 1, page_size: int = 50,
                  participant_id: Optional[int] = None) -> Dict[str, Any]:
    _check_sqlite()
    cte, params, where = _episode_query(since, until, camera_id, mode, label, participant_id)
    total = db.execute(text(cte + f"SELECT COUNT(*) FROM episodes{where}"), params).scalar() or 0
    rows = db.execute(
        text(cte + f"SELECT * FROM episodes{where} ORDER BY first_seen DESC, camera_id, track_id "
                   "LIMIT :limit OFFSET :offset"),
        {**params, "limit": page_size, "offset": (page - 1) * page_size}).fetchall()
    return {"items": [_row_to_episode(r) for r in rows], "total": total, "page": page, "page_size": page_size,
            "since": to_iso_utc(since), "until": to_iso_utc(until), "gap_sec": config.HISTORY_GAP_SEC,
            "identities_visible": config.HISTORY_SHOW_IDENTITIES}


# ---- Synthèse --------------------------------------------------------------------------
def pick_bucket_minutes(since: datetime, until: datetime) -> int:
    span_min = (until - since).total_seconds() / 60
    for b in BUCKET_CHOICES_MIN:
        if span_min / b <= MAX_BUCKETS:
            return b
    return BUCKET_CHOICES_MIN[-1]


def read_summary(db, since: datetime, until: datetime, camera_id: str = "") -> Dict[str, Any]:
    _check_sqlite()
    cte, params, _ = _episode_query(since, until, camera_id, "", "")
    per = db.execute(text(cte + "SELECT camera_id, mode, COUNT(*) AS n, SUM(observations) AS obs, "
                                "COUNT(DISTINCT CASE WHEN mode = 'DB' THEN COALESCE(CAST(participant_id AS TEXT), label) END) AS people "
                                "FROM episodes GROUP BY camera_id, mode"), params).fetchall()
    by_mode = {m: 0 for m in MODES}
    by_camera: Dict[str, Dict[str, Any]] = {}
    observations = 0
    for r in per:
        by_mode[r.mode] += r.n
        observations += r.obs
        cam = by_camera.setdefault(r.camera_id, {"camera_id": r.camera_id, "passages": 0,
                                                  "TARGET": 0, "DB": 0, "UNK": 0})
        cam["passages"] += r.n
        cam[r.mode] += r.n
    people = db.execute(text(cte + "SELECT COUNT(DISTINCT COALESCE(CAST(participant_id AS TEXT), label)) "
                                  "FROM episodes WHERE mode = 'DB'"), params).scalar() or 0

    # Courbe d'activité : observations par tranche de temps.
    bucket = pick_bucket_minutes(since, until)
    bucket_sec = bucket * 60
    since_t = (since - datetime(1970, 1, 1)).total_seconds()
    n_buckets = max(1, math.ceil((until - since).total_seconds() / bucket_sec))
    clause = "AND camera_id = :camera_id" if camera_id else ""
    tparams = {"since": _sql_dt(since), "until": _sql_dt(until), "since_t": since_t, "bs": bucket_sec}
    if camera_id:
        tparams["camera_id"] = camera_id
    rows = db.execute(text(
        "SELECT CAST(((julianday(timestamp) - 2440587.5) * 86400.0 - :since_t) / :bs AS INTEGER) AS b, "
        "COUNT(*) AS n, SUM(CASE WHEN mode = 'TARGET' THEN 1 ELSE 0 END) AS target "
        f"FROM detections WHERE timestamp >= :since AND timestamp <= :until {clause} GROUP BY b"), tparams).fetchall()
    cells = {r.b: (r.n, r.target or 0) for r in rows}
    timeline = []
    for i in range(n_buckets):
        n, tgt = cells.get(i, (0, 0))
        start = since + timedelta(seconds=i * bucket_sec)
        timeline.append({"start": to_iso_utc(start), "observations": n, "target": tgt})

    return {"since": to_iso_utc(since), "until": to_iso_utc(until),
            "passages": sum(by_mode.values()), "observations": observations,
            "by_mode": by_mode,
            "enrolled_people": people if config.HISTORY_SHOW_IDENTITIES else None,
            "by_camera": sorted(by_camera.values(), key=lambda c: (-c["passages"], c["camera_id"])),
            "bucket_minutes": bucket, "timeline": timeline}


# ---- Trajets de la cible ---------------------------------------------------------------
def read_trajectories(db, since: datetime, until: datetime, limit: int = 50) -> Dict[str, Any]:
    moves = (db.query(TargetMove)
             .filter(TargetMove.timestamp >= since, TargetMove.timestamp <= until)
             .order_by(TargetMove.session, TargetMove.timestamp, TargetMove.id).all())
    sessions: Dict[int, List[TargetMove]] = {}
    for m in moves:
        sessions.setdefault(m.session, []).append(m)
    items = []
    for sid, ms in sessions.items():
        items.append({
            "session": sid,
            "label": next((m.label for m in ms if m.label), None),
            "started": to_iso_utc(ms[0].timestamp), "ended": to_iso_utc(ms[-1].timestamp),
            "cameras": len({m.camera_id for m in ms}),
            "moves": [{"camera_id": m.camera_id, "timestamp": to_iso_utc(m.timestamp),
                       "ts": (m.timestamp - datetime(1970, 1, 1)).total_seconds(),
                       "score": round(m.score, 3) if m.score is not None else None} for m in ms],
        })
    items.sort(key=lambda s: s["started"], reverse=True)
    return {"items": items[:limit], "total": len(items), "since": to_iso_utc(since), "until": to_iso_utc(until)}


# ---- Export CSV ------------------------------------------------------------------------
CSV_HEADER = ["debut_utc", "fin_utc", "duree_s", "camera", "piste", "type", "identite", "observations", "score_max",
              "id_personne", "correspondance"]
_STATUS_FR = {"probable": "probable", "a_verifier": "à vérifier"}
_MODE_FR = {"TARGET": "cible", "DB": "personne enrôlée", "UNK": "inconnu"}


def csv_safe(value: Any) -> Any:
    """Empêche l'interprétation d'une cellule comme une formule par Excel / LibreOffice."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def iter_csv(db, since, until, camera_id="", mode="", label="", participant_id=None) -> Iterator[str]:
    """Génère le CSV (séparateur « ; », UTF-8 avec BOM : s'ouvre correctement dans Excel en français)."""
    _check_sqlite()
    cte, params, where = _episode_query(since, until, camera_id, mode, label, participant_id)
    result = db.execute(text(cte + f"SELECT * FROM episodes{where} ORDER BY first_seen DESC, camera_id, track_id "
                                   "LIMIT :limit"), {**params, "limit": config.HISTORY_CSV_MAX_ROWS})

    def line(cells):
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="\r\n").writerow([csv_safe(c) for c in cells])
        return buf.getvalue()

    yield "\ufeff" + line(CSV_HEADER)
    for r in result:
        ep = _row_to_episode(r)
        yield line([ep["first_seen"], ep["last_seen"], ep["duration_sec"], ep["camera_id"], ep["track_id"],
                    _MODE_FR.get(ep["mode"], ep["mode"]), ep["label"] or "", ep["observations"],
                    "" if ep["best_score"] is None else ep["best_score"],
                    "" if ep["participant_id"] is None else ep["participant_id"],
                    _STATUS_FR.get(ep["match_status"], "")])
