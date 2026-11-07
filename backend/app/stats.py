"""Statistiques anonymes (Module 10).

Vocabulaire — trois quantités qu'il ne faut pas confondre :

* **observation** (« détection ») : une ligne de la table ``detections`` (≈ une toutes les 0,8 s par personne visible).
  Elle mesure le temps de présence, pas le nombre de personnes ;
* **passage** : une personne (une piste) qui reste visible sur une caméra, sans interruption de plus de
  ``HISTORY_GAP_SEC`` secondes. Une même personne qui sort puis revient fait deux passages ;
* **personne unique** : uniquement pour les personnes *enrôlées* (identifiant de fiche). Les inconnus ne peuvent pas
  être dédoublonnés entre caméras ni entre passages : ils ne sont comptés qu'en passages.

Aucune sortie de ce module ne contient de nom, d'identifiant de fiche ni d'image : ce sont des agrégats.
"""
import csv
import io
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import text

from . import config, history
from .db import TargetMove, Zone, ZoneStat, to_iso_utc

HeatmapError = history.HistoryError
MAX_GRID = 64
TRANSITION_GAP_SEC = (30, 3600)
DEFINITIONS = {
    "observations": "Lignes de détection enregistrées (≈ une toutes les 0,8 s par personne visible). Mesure la présence, pas le nombre de personnes.",
    "passages": "Une personne visible sur une caméra sans interruption de plus de %d s. Sortir puis revenir fait deux passages." % int(config.HISTORY_GAP_SEC),
    "unique_enrolled": "Personnes enrôlées distinctes (par fiche). Les inconnus ne sont pas dédoublonnés : ils ne sont comptés qu'en passages.",
}


def _tz(offset_min: int) -> timedelta:
    return timedelta(minutes=max(-840, min(840, int(offset_min))))


def _episode_rows(db, since, until, camera_id):
    cte, params, _ = history._episode_query(since, until, camera_id, "", "")
    return db.execute(text(cte + "SELECT camera_id, mode, participant_id, first_seen, last_seen, observations, "
                                 "(last_t - first_t) AS dur FROM episodes"), params).fetchall()


def _count_observations(db, since, until, camera_id) -> int:
    sql = "SELECT COUNT(*) FROM detections WHERE timestamp >= :s AND timestamp <= :u"
    params: Dict[str, Any] = {"s": history._sql_dt(since), "u": history._sql_dt(until)}
    if camera_id:
        sql += " AND camera_id = :c"
        params["c"] = camera_id
    return int(db.execute(text(sql), params).scalar() or 0)


def _totals(rows, observations: int) -> Dict[str, Any]:
    by_mode = {m: 0 for m in history.MODES}
    durations: List[float] = []
    people = set()
    for r in rows:
        by_mode[r.mode] += 1
        durations.append(max(0.0, r.dur or 0.0))
        if r.mode == "DB" and r.participant_id is not None:
            people.add(r.participant_id)
    durations.sort()
    n = len(durations)
    return {"observations": observations, "passages": n, "unique_enrolled": len(people), "by_mode": by_mode,
            "unknown_passages": by_mode["UNK"],
            "avg_duration_sec": round(sum(durations) / n, 1) if n else None,
            "median_duration_sec": round(durations[n // 2], 1) if n else None}


def overview(db, since: datetime, until: datetime, camera_id: str = "", tz_offset_min: int = 0) -> Dict[str, Any]:
    history._check_sqlite()
    rows = _episode_rows(db, since, until, camera_id)
    out = {"since": to_iso_utc(since), "until": to_iso_utc(until), "camera_id": camera_id or None,
           "tz_offset_min": int(tz_offset_min), "definitions": DEFINITIONS, "anonymous": True}
    out.update(_totals(rows, _count_observations(db, since, until, camera_id)))

    per: Dict[str, Dict[str, Any]] = {}
    people: Dict[str, set] = {}
    obs_rows = db.execute(text("SELECT camera_id, COUNT(*) AS n FROM detections WHERE timestamp >= :s AND timestamp <= :u"
                               + (" AND camera_id = :c" if camera_id else "") + " GROUP BY camera_id"),
                          {"s": history._sql_dt(since), "u": history._sql_dt(until), **({"c": camera_id} if camera_id else {})}).fetchall()
    for r in obs_rows:
        per[r.camera_id] = {"camera_id": r.camera_id, "observations": r.n, "passages": 0, "unique_enrolled": 0, "_dur": 0.0}
    for r in rows:
        c = per.setdefault(r.camera_id, {"camera_id": r.camera_id, "observations": 0, "passages": 0, "unique_enrolled": 0, "_dur": 0.0})
        c["passages"] += 1
        c["_dur"] += max(0.0, r.dur or 0.0)
        if r.mode == "DB" and r.participant_id is not None:
            people.setdefault(r.camera_id, set()).add(r.participant_id)
    cams = []
    for c in per.values():
        c["unique_enrolled"] = len(people.get(c["camera_id"], ()))
        c["avg_duration_sec"] = round(c.pop("_dur") / c["passages"], 1) if c["passages"] else None
        cams.append(c)
    out["by_camera"] = sorted(cams, key=lambda c: (-c["passages"], c["camera_id"]))

    # Profil horaire et semaine type (heure locale du navigateur), en passages.
    off = _tz(tz_offset_min)
    hours = [0] * 24
    week = [[0] * 24 for _ in range(7)]
    for r in rows:
        local = datetime.fromisoformat(r.first_seen) + off
        hours[local.hour] += 1
        week[local.weekday()][local.hour] += 1
    out["by_hour"] = hours
    out["by_weekday_hour"] = week

    # Lignes et zones (Module 6)
    zq = db.query(Zone)
    if camera_id:
        zq = zq.filter(Zone.camera_id == camera_id)
    zones = {z.id: z for z in zq.all()}
    zrows = []
    if zones:
        agg = db.query(ZoneStat.zone_id, ZoneStat.entries, ZoneStat.exits, ZoneStat.occupancy_max).filter(
            ZoneStat.zone_id.in_(list(zones)), ZoneStat.bucket >= since, ZoneStat.bucket <= until).all()
        tot: Dict[int, List[int]] = {}
        for zid, e, x, m in agg:
            t = tot.setdefault(zid, [0, 0, 0])
            t[0] += e; t[1] += x; t[2] = max(t[2], m)
        for zid, z in zones.items():
            e, x, m = tot.get(zid, [0, 0, 0])
            zrows.append({"zone_id": zid, "name": z.name, "kind": z.kind, "camera_id": z.camera_id,
                          "entries": e, "exits": x, "occupancy_max": m})
        zrows.sort(key=lambda z: (-(z["entries"] + z["exits"]), z["name"]))
    out["zones"] = zrows
    return out


def heatmap(db, since: datetime, until: datetime, camera_id: str, cols: int = 32, rows: int = 18) -> Dict[str, Any]:
    """Densité d'observations sur l'image d'**une** caméra (les vues de caméras différentes ne se superposent pas)."""
    if not camera_id:
        raise HeatmapError("Choisissez une caméra : les images de caméras différentes ne se superposent pas.")
    cols, rows = max(2, min(MAX_GRID, int(cols))), max(2, min(MAX_GRID, int(rows)))
    w, h = config.STREAM_SIZE
    found = db.execute(text("SELECT x, y FROM detections WHERE timestamp >= :s AND timestamp <= :u AND camera_id = :c "
                            "AND x IS NOT NULL AND y IS NOT NULL"),
                       {"s": history._sql_dt(since), "u": history._sql_dt(until), "c": camera_id}).fetchall()
    grid: Counter = Counter()
    for x, y in found:
        cx = min(cols - 1, max(0, int(x / w * cols)))
        cy = min(rows - 1, max(0, int(y / h * rows)))
        grid[(cx, cy)] += 1
    cells = [[cx, cy, n] for (cx, cy), n in sorted(grid.items())]
    return {"camera_id": camera_id, "cols": cols, "rows": rows, "total": len(found), "max": max(grid.values()) if grid else 0,
            "cells": cells, "since": to_iso_utc(since), "until": to_iso_utc(until), "anonymous": True,
            "note": "Position du centre du cadre de la personne dans l'image du flux (%dx%d). Les données enregistrées avec une autre taille de flux sont décalées." % (w, h)}


def _delta(a: Optional[float], b: Optional[float]) -> Dict[str, Any]:
    pct = None if not b else round((a - b) / b * 100.0, 1)
    return {"a": a, "b": b, "diff": None if a is None or b is None else round(a - b, 1), "pct": pct}


def compare(db, a_since, a_until, b_since, b_until, camera_id: str = "") -> Dict[str, Any]:
    """Période A (courante) contre période B (référence). ``pct`` vaut None quand la référence est nulle."""
    history._check_sqlite()

    def one(s, u):
        return _totals(_episode_rows(db, s, u, camera_id), _count_observations(db, s, u, camera_id))
    a, b = one(a_since, a_until), one(b_since, b_until)
    keys = ["observations", "passages", "unique_enrolled", "unknown_passages", "avg_duration_sec"]
    return {"a": {"since": to_iso_utc(a_since), "until": to_iso_utc(a_until), **a},
            "b": {"since": to_iso_utc(b_since), "until": to_iso_utc(b_until), **b},
            "delta": {k: _delta(a[k], b[k]) for k in keys}, "camera_id": camera_id or None, "anonymous": True,
            "note": "Les périodes de durées différentes ne sont pas comparables en valeur absolue."}


def transitions(db, since: datetime, until: datetime, max_gap_sec: int = 600) -> Dict[str, Any]:
    """Graphe caméra → caméra, sans identité. Deux sources : trajets de cibles et personnes enrôlées
    (deux passages consécutifs d'une même fiche sur deux caméras, séparés d'au plus ``max_gap_sec``)."""
    history._check_sqlite()
    lo, hi = TRANSITION_GAP_SEC
    max_gap = max(lo, min(hi, int(max_gap_sec)))
    edges: Dict[tuple, Dict[str, Any]] = {}

    def add(src, dst, gap, kind):
        e = edges.setdefault((src, dst), {"from": src, "to": dst, "count": 0, "target": 0, "enrolled": 0, "_gaps": []})
        e["count"] += 1
        e[kind] += 1
        e["_gaps"].append(gap)

    moves = (db.query(TargetMove).filter(TargetMove.timestamp >= since, TargetMove.timestamp <= until)
             .order_by(TargetMove.session, TargetMove.timestamp, TargetMove.id).all())
    last: Dict[int, TargetMove] = {}
    for m in moves:
        p = last.get(m.session)
        if p is not None and p.camera_id != m.camera_id:
            add(p.camera_id, m.camera_id, (m.timestamp - p.timestamp).total_seconds(), "target")
        last[m.session] = m

    cte, params, _ = history._episode_query(since, until, "", "", "")
    eps = db.execute(text(cte + "SELECT participant_id, camera_id, first_seen, last_seen FROM episodes "
                                "WHERE mode = 'DB' AND participant_id IS NOT NULL ORDER BY participant_id, first_seen LIMIT 200000"),
                     params).fetchall()
    prev = None
    nodes: Counter = Counter()
    for r in eps:
        nodes[r.camera_id] += 1
        if prev is not None and prev.participant_id == r.participant_id and prev.camera_id != r.camera_id:
            gap = (datetime.fromisoformat(r.first_seen) - datetime.fromisoformat(prev.last_seen)).total_seconds()
            if 0 <= gap <= max_gap:
                add(prev.camera_id, r.camera_id, gap, "enrolled")
        prev = r
    for m in moves:
        nodes[m.camera_id] += 0
    out_edges = []
    for e in edges.values():
        g = sorted(e.pop("_gaps"))
        e["median_gap_sec"] = round(g[len(g) // 2], 1)
        out_edges.append(e)
    out_edges.sort(key=lambda e: (-e["count"], e["from"], e["to"]))
    return {"since": to_iso_utc(since), "until": to_iso_utc(until), "max_gap_sec": max_gap, "anonymous": True,
            "nodes": [{"camera_id": c, "enrolled_passages": n} for c, n in sorted(nodes.items())],
            "edges": out_edges,
            "note": "Seuls les trajets de cibles et des personnes enrôlées sont reliables entre caméras ; les inconnus n'y figurent pas."}


def iter_csv(db, since, until, camera_id: str = "", tz_offset_min: int = 0) -> Iterator[str]:
    """Export agrégé et anonyme (section, clé, valeur)."""
    data = overview(db, since, until, camera_id, tz_offset_min)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")

    def flush():
        v = buf.getvalue(); buf.seek(0); buf.truncate(0); return v
    w.writerow(["section", "cle", "valeur"]); yield flush()
    for k in ("observations", "passages", "unique_enrolled", "unknown_passages", "avg_duration_sec", "median_duration_sec"):
        w.writerow(["total", k, data[k]])
    for m, n in data["by_mode"].items():
        w.writerow(["passages_par_type", m, n])
    for c in data["by_camera"]:
        for k in ("observations", "passages", "unique_enrolled", "avg_duration_sec"):
            w.writerow(["camera:" + str(history.csv_safe(c["camera_id"])), k, c[k]])
    for hr, n in enumerate(data["by_hour"]):
        w.writerow(["heure", "%02d" % hr, n])
    for z in data["zones"]:
        for k in ("entries", "exits", "occupancy_max"):
            w.writerow(["zone:" + str(history.csv_safe(z["name"])), k, z[k]])
    yield flush()
