import asyncio
import base64
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Union

import numpy as np
from fastapi import (FastAPI, UploadFile, File, Form, HTTPException, WebSocket,
                     WebSocketDisconnect, Query)
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .activity_log import read_activity
from .db import (init_db, SessionLocal, Participant, EmbeddingRef, Detection,
                 utcnow, to_iso_utc, delete_participant, save_target_move, Event)
from .people import PersonError
from . import alerts, analytics, demo, investigation, pdf_report, recordings, reports, stats, camera_probe, events, floors, history, people, supervision, target_profile, target_sessions
from .camera_manager import CameraManager, CameraError, normalize_source, source_kind
from .db_logger import DBLogger
from .supervision import SupervisionMonitor
from .person_pipeline import PersonPipeline
from .reid import GlobalReId
from .target_path import TargetWorker
from .targets import (TargetRegistry, TargetLimitError, DuplicateTargetError, make_thumbnail)
from .video_stream import mjpeg_frames
from .vision import read_image_bytes, detect_faces_safe

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


# ---------- Embeddings ----------
embedding_to_bytes = people.embedding_to_bytes          # alias conservés (utilisés par les tests et d'anciens modules)
bytes_to_embedding = people.bytes_to_embedding


def load_known_from_db():
    """Références de toutes les personnes enrôlées (actives ou non : une fiche désactivée reste reconnue)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(EmbeddingRef, Participant)
            .join(Participant, Participant.id == EmbeddingRef.participant_id)
            .all()
        )
        return [{"participant_id": p.id, "nom": p.display_name, "active": bool(p.actif),
                 "embedding": bytes_to_embedding(e.embedding)} for e, p in rows]
    finally:
        db.close()


def refresh_known():
    known = load_known_from_db()
    for cam in list(app.state.cams.values()):
        cam.set_known(known)


def _face_embedding(img) -> Optional[np.ndarray]:
    """Embedding du plus grand visage de l'image, ou None. Bloquant : à lancer dans un thread."""
    faces = detect_faces_safe(img)
    if not faces:
        return None
    biggest = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
    return np.asarray(biggest.normed_embedding, dtype=np.float32)


async def _read_upload(up: UploadFile) -> bytes:
    """Lit un fichier envoyé sans jamais dépasser la limite de taille en mémoire."""
    data = await up.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(413, f"« {up.filename} » dépasse la taille maximale ({mb} Mo).")
    return data


async def _embeddings_from_uploads(photos: List[UploadFile]):
    """Retourne [(embedding, nom_de_fichier, image)] pour les photos contenant un visage."""
    results = []
    for up in photos:
        img = read_image_bytes(await _read_upload(up))
        if img is None:
            continue
        emb = await asyncio.to_thread(_face_embedding, img)
        if emb is not None:
            results.append((emb, up.filename, img))
    return results


def _save_move(target, move):
    """Enregistre chaque étape du trajet d'une cible (appelé par son suivi, hors verrou)."""
    save_target_move(target.session, move["camera_id"], move["ts"], move.get("score"), label=target.label)
    monitor = getattr(app.state, "monitor", None)
    if monitor is not None:
        monitor.on_target_move(target, move)


def _apply_targets():
    """Transmet aux caméras la liste courante des cibles à reconnaître."""
    targets = app.state.targets.for_cameras()
    for cam in list(app.state.cams.values()):
        cam.set_targets(targets)


def _make_pipeline(cfg: dict):
    """Crée le traitement d'une caméra à partir de sa configuration enregistrée."""
    if config.DEMO:                                    # caméra virtuelle : aucun matériel, aucun modèle de vision
        style = next((c for c in demo.CAMERAS if c["id"] == cfg["id"]), {})
        return demo.SimCamera(cfg["id"], app.state.demo, background=style.get("bg", (80, 80, 80)), label=cfg.get("label") or cfg["id"])
    width, height = cfg.get("capture_width"), cfg.get("capture_height")
    capture_size = (width, height) if width and height else config.DEFAULT_CAPTURE_SIZE
    return PersonPipeline(
        source=cfg["source"], camera_id=cfg["id"], reid=app.state.reid,
        capture_size=capture_size, out_size=config.STREAM_SIZE,
        tracker_yaml="bytetrack.yaml", imgsz=480, yolo_conf=0.35,
        head_ratio=0.50, recog_every_sec=0.60, lock_hold_sec=3.0, jpeg_quality=70,
        process_fps=cfg.get("process_fps"),
    )


# ---------- Cycle de vie ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.demo = None
    if config.DEMO:
        demo.assert_isolated()
        app.state.demo = demo.Scenario(speed=config.DEMO_SPEED)
        _ddb = SessionLocal()
        try:
            app.state.demo.people_ids = demo.seed(_ddb)["people_ids"]
            demo.backfill(_ddb)
        finally:
            _ddb.close()
        print("[MAIN] MODE DÉMONSTRATION : données et caméras simulées, base séparée", config.DB_PATH.name)
    app.state.camera_meta = {}
    app.state.camera_sources = {}
    app.state.feed = events.EventFeed()
    app.state.reid = GlobalReId(sim_threshold=config.REID_THRESHOLD, ttl_sec=20.0)
    app.state.cams = {}
    app.state.targets = TargetRegistry(on_move=_save_move)
    _db = SessionLocal()
    try:
        _cap = target_sessions.load_capacity(_db)
    finally:
        _db.close()
    if _cap:
        app.state.targets.max_targets = _cap

    # Les caméras viennent de la base ; cameras.json n'est importé qu'au tout premier démarrage.
    app.state.probe_slots = asyncio.Semaphore(2)       # au plus 2 tests de connexion simultanés
    app.state.camera_manager = CameraManager(
        cams=app.state.cams, meta=app.state.camera_meta, sources=app.state.camera_sources,
        make_pipeline=_make_pipeline, known=load_known_from_db,
        targets=app.state.targets.for_cameras, feed=app.state.feed)
    imported = app.state.camera_manager.seed_if_needed(demo.CAMERAS if config.DEMO else config.load_cameras())
    if imported:
        print(f"[MAIN] {imported} caméra(s) importée(s) depuis {config.CAMERAS_FILE.name} (premier démarrage)")
    if config.DEMO and len(app.state.targets) == 0:           # cible fictive (aucune photo, empreinte aléatoire)
        _t = app.state.targets.add(np.random.RandomState(7).randn(512), photo=None)
        app.state.demo.target_id = _t.id
    elif config.DEMO:
        app.state.demo.target_id = app.state.targets.items()[0].id
    app.state.camera_manager.load_and_start()

    app.state.target_worker = TargetWorker(app.state.targets, lambda: app.state.cams)
    app.state.target_worker.start()
    app.state.logger = DBLogger(lambda: app.state.cams)
    app.state.logger.start()
    app.state.monitor = SupervisionMonitor(lambda: app.state.cams, app.state.targets.items,
                                           app.state.feed, lambda: app.state.camera_meta)
    app.state.monitor.start()
    app.state.alerts = alerts.AlertCenter(app.state.feed, lambda: app.state.camera_meta,
                                          notifier=demo.NullNotifier() if config.DEMO else None)
    app.state.alerts.start()
    app.state.recorder = recordings.Recorder(lambda: app.state.cams, app.state.feed)
    app.state.recorder.start()
    app.state.analytics = analytics.AnalyticsEngine(lambda: app.state.cams, app.state.feed,
                                                    lambda: app.state.camera_meta)
    app.state.analytics.start()
    print(f"[MAIN] Caméras démarrées: {list(app.state.cams.keys())}")
    try:
        yield
    finally:
        app.state.analytics.stop()
        app.state.alerts.stop()
        app.state.recorder.stop()
        app.state.monitor.stop()
        app.state.logger.stop()
        app.state.target_worker.stop()
        app.state.camera_manager.stop_all()


app = FastAPI(title="NiifYéén — Cheminement + Historique + DB names", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
def page_home():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/cameras", response_class=HTMLResponse)
def page_cameras():
    return (BASE_DIR / "static" / "cameras.html").read_text(encoding="utf-8")


@app.get("/logs", response_class=HTMLResponse)
def page_logs():
    return (BASE_DIR / "static" / "logs.html").read_text(encoding="utf-8")


@app.get("/supervision", response_class=HTMLResponse)
def page_supervision():
    return (BASE_DIR / "static" / "supervision.html").read_text(encoding="utf-8")


@app.get("/people", response_class=HTMLResponse)
@app.get("/people/{participant_id}", response_class=HTMLResponse)
def page_people(participant_id: Optional[int] = None):
    return (BASE_DIR / "static" / "people.html").read_text(encoding="utf-8")


@app.get("/admin/cameras", response_class=HTMLResponse)
def page_cameras_admin():
    return (BASE_DIR / "static" / "cameras-admin.html").read_text(encoding="utf-8")


@app.get("/recordings", response_class=HTMLResponse)
def page_recordings():
    return (BASE_DIR / "static" / "recordings.html").read_text(encoding="utf-8")


@app.get("/reports", response_class=HTMLResponse)
def page_reports():
    return (BASE_DIR / "static" / "reports.html").read_text(encoding="utf-8")


@app.get("/demo", response_class=HTMLResponse)
def page_demo():
    return (BASE_DIR / "static" / "demo.html").read_text(encoding="utf-8")


@app.get("/statistics", response_class=HTMLResponse)
def page_statistics():
    return (BASE_DIR / "static" / "statistics.html").read_text(encoding="utf-8")


@app.get("/investigation", response_class=HTMLResponse)
def page_investigation():
    return (BASE_DIR / "static" / "investigation.html").read_text(encoding="utf-8")


@app.get("/alerts", response_class=HTMLResponse)
def page_alerts():
    return (BASE_DIR / "static" / "alerts.html").read_text(encoding="utf-8")


@app.get("/zones", response_class=HTMLResponse)
def page_zones():
    return (BASE_DIR / "static" / "zones.html").read_text(encoding="utf-8")


@app.get("/map", response_class=HTMLResponse)
def page_map():
    return (BASE_DIR / "static" / "map.html").read_text(encoding="utf-8")


@app.get("/history", response_class=HTMLResponse)
def page_history():
    return (BASE_DIR / "static" / "history.html").read_text(encoding="utf-8")


# ---------- API ----------
@app.get("/api/logs/activity")
def activity_logs(minutes: int = Query(60, ge=1, le=10080),
                  camera_id: str = Query("", max_length=100),
                  page: int = Query(1, ge=1),
                  page_size: int = Query(50, ge=1, le=100)):
    db = SessionLocal()
    try:
        return read_activity(db, minutes, camera_id, page, page_size)
    finally:
        db.close()


# ---------- Historique des détections ----------
def _history_range(minutes: int, start: str, end: str):
    try:
        return history.resolve_range(minutes, start or None, end or None)
    except history.HistoryError as exc:
        raise HTTPException(422, str(exc))


def _history_call(fn, *args, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, *args, **kwargs)
    except history.HistoryError as exc:
        raise HTTPException(422, str(exc))
    finally:
        db.close()


_MODE_PATTERN = "^(TARGET|DB|UNK)?$"


@app.get("/api/history/episodes")
def history_episodes(minutes: int = Query(60, ge=1, le=60 * 24 * 90),
                     start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                     camera_id: str = Query("", max_length=100),
                     mode: str = Query("", pattern=_MODE_PATTERN),
                     label: str = Query("", max_length=100),
                     participant_id: int = Query(None, ge=1),
                     page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)):
    since, until = _history_range(minutes, start, end)
    return _history_call(history.read_episodes, since, until, camera_id, mode, label, page, page_size, participant_id)


@app.get("/api/history/summary")
def history_summary(minutes: int = Query(60, ge=1, le=60 * 24 * 90),
                    start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                    camera_id: str = Query("", max_length=100)):
    since, until = _history_range(minutes, start, end)
    return _history_call(history.read_summary, since, until, camera_id)


@app.get("/api/history/trajectories")
def history_trajectories(minutes: int = Query(60, ge=1, le=60 * 24 * 90),
                         start: str = Query("", max_length=40), end: str = Query("", max_length=40)):
    since, until = _history_range(minutes, start, end)
    return _history_call(history.read_trajectories, since, until)


@app.get("/api/history/episodes.csv")
def history_csv(minutes: int = Query(60, ge=1, le=60 * 24 * 90),
                start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                camera_id: str = Query("", max_length=100),
                mode: str = Query("", pattern=_MODE_PATTERN),
                label: str = Query("", max_length=100),
                participant_id: int = Query(None, ge=1)):
    since, until = _history_range(minutes, start, end)

    def generate():
        db = SessionLocal()
        try:
            yield from history.iter_csv(db, since, until, camera_id, mode, label, participant_id)
        finally:
            db.close()

    name = f"historique-niifyeen-{utcnow():%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(generate(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Cache-Control": "no-store"})


# ---------- Rapports (Module 12) ----------
def _report_build(minutes, start, end, title, camera_id, sections, tz_offset_min):
    since, until = _history_range(minutes, start, end)
    try:
        params = reports.parse_params(title, camera_id, sections, tz_offset_min)
    except reports.ReportError as exc:
        raise HTTPException(422, str(exc))
    db = SessionLocal()
    try:
        return reports.build(db, since, until, params, _camera_labels())
    except history.HistoryError as exc:
        raise HTTPException(422, str(exc))
    finally:
        db.close()


_RPT = dict(minutes=Query(1440, ge=1, le=60 * 24 * 90), start=Query("", max_length=40), end=Query("", max_length=40),
            title=Query("", max_length=120), camera_id=Query("", max_length=100), sections=Query(None, max_length=200),
            tz_offset_min=Query(0, ge=-840, le=840))


@app.get("/api/reports/options")
def reports_options():
    db = SessionLocal()
    try:
        return {"sections": [{"id": k, "label": v} for k, v in reports.SECTIONS.items()], "templates": reports.list_templates(db),
                "max_templates": reports.MAX_TEMPLATES}
    finally:
        db.close()


@app.get("/api/reports/preview")
def reports_preview(minutes: int = _RPT["minutes"], start: str = _RPT["start"], end: str = _RPT["end"], title: str = _RPT["title"],
                    camera_id: str = _RPT["camera_id"], sections: str = _RPT["sections"], tz_offset_min: int = _RPT["tz_offset_min"]):
    return _report_build(minutes, start, end, title, camera_id, sections, tz_offset_min)


@app.get("/api/reports/report.pdf")
def reports_pdf(minutes: int = _RPT["minutes"], start: str = _RPT["start"], end: str = _RPT["end"], title: str = _RPT["title"],
                camera_id: str = _RPT["camera_id"], sections: str = _RPT["sections"], tz_offset_min: int = _RPT["tz_offset_min"]):
    report = _report_build(minutes, start, end, title, camera_id, sections, tz_offset_min)
    return _pdf_response(lambda: reports.render_pdf(report), f"rapport-activite-niifyeen-{utcnow():%Y%m%d-%H%M%S}.pdf")


class ReportTemplateIn(BaseModel):
    title: str = Field("", max_length=120)
    camera_id: str = Field("", max_length=100)
    sections: List[str] = Field(default_factory=list, max_length=len(reports.SECTIONS))
    minutes: int = Field(1440, ge=1, le=60 * 24 * 90)


@app.put("/api/reports/templates/{name}")
def reports_template_save(name: str, body: ReportTemplateIn):
    db = SessionLocal()
    try:
        params = reports.parse_params(body.title, body.camera_id, ",".join(body.sections) if body.sections else None)
        return {"templates": reports.save_template(db, name, body.minutes, params)}
    except reports.ReportError as exc:
        raise HTTPException(422, str(exc))
    finally:
        db.close()


@app.delete("/api/reports/templates/{name}")
def reports_template_delete(name: str):
    db = SessionLocal()
    try:
        if not reports.delete_template(db, name):
            raise HTTPException(404, "Modèle introuvable.")
        return {"templates": reports.list_templates(db)}
    finally:
        db.close()


# ---------- Mode démonstration (Module 11) ----------
@app.get("/api/demo")
def demo_status():
    if not config.DEMO:
        return {"demo": False}
    st, loop = app.state.demo.position()
    return {"demo": True, "period_sec": demo.PERIOD_SEC, "speed": app.state.demo.speed, "scenario_sec": round(st, 1), "loop": loop,
            "script": demo.script_view(), "cameras": [{"id": c["id"], "label": c["label"]} for c in demo.CAMERAS],
            "people": app.state.demo.people_names, "database": config.DB_PATH.name,
            "simulated": ["Caméras et images (silhouettes dessinées, sans visage)", "Reconnaissance de personnes et de la cible",
                          "Personnes enrôlées (fictives, sans photo ni empreinte)", "Historique des dernières 24 h"],
            "real": ["Lignes et zones, comptages, affluence, intrusions", "Alertes, états, historique", "Suivi de la cible et trajets",
                     "Statistiques, investigation, enregistrement vidéo, rapports"]}


def _demo_only():
    if not config.DEMO:
        raise HTTPException(403, "Disponible uniquement en mode démonstration (lancez tools/run_demo.py).")


@app.post("/api/demo/restart")
def demo_restart():
    _demo_only()
    app.state.demo.restart()
    return {"ok": True}


@app.post("/api/demo/reset")
def demo_reset():
    """Efface les données de démonstration produites jusqu'ici, recrée l'historique fictif et relance le scénario."""
    _demo_only()
    db = SessionLocal()
    try:
        demo.reset(db)
    finally:
        db.close()
    app.state.demo.restart()
    return {"ok": True}


# ---------- Statistiques anonymes (Module 10) ----------
_TZ = Query(0, ge=-840, le=840, description="Décalage du fuseau du navigateur, en minutes par rapport à UTC")


@app.get("/api/stats/overview")
def stats_overview(minutes: int = Query(1440, ge=1, le=60 * 24 * 90),
                   start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                   camera_id: str = Query("", max_length=100), tz_offset_min: int = _TZ):
    since, until = _history_range(minutes, start, end)
    return _history_call(stats.overview, since, until, camera_id, tz_offset_min)


@app.get("/api/stats/heatmap")
def stats_heatmap(camera_id: str = Query("", max_length=100), minutes: int = Query(1440, ge=1, le=60 * 24 * 90),
                  start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                  cols: int = Query(32, ge=2, le=stats.MAX_GRID), rows: int = Query(18, ge=2, le=stats.MAX_GRID)):
    since, until = _history_range(minutes, start, end)
    return _history_call(stats.heatmap, since, until, camera_id, cols, rows)


@app.get("/api/stats/compare")
def stats_compare(minutes: int = Query(1440, ge=1, le=60 * 24 * 45), camera_id: str = Query("", max_length=100),
                  start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                  ref_start: str = Query("", max_length=40), ref_end: str = Query("", max_length=40)):
    """Période courante contre une référence : par défaut la période de même durée qui la précède."""
    a_since, a_until = _history_range(minutes, start, end)
    if ref_start or ref_end:
        b_since, b_until = _history_range(minutes, ref_start, ref_end)
    else:
        b_since, b_until = a_since - (a_until - a_since), a_since
    return _history_call(stats.compare, a_since, a_until, b_since, b_until, camera_id)


@app.get("/api/stats/transitions")
def stats_transitions(minutes: int = Query(1440, ge=1, le=60 * 24 * 90),
                      start: str = Query("", max_length=40), end: str = Query("", max_length=40),
                      max_gap_sec: int = Query(600, ge=30, le=3600)):
    since, until = _history_range(minutes, start, end)
    return _history_call(stats.transitions, since, until, max_gap_sec)


@app.get("/api/stats/overview.csv")
def stats_csv(minutes: int = Query(1440, ge=1, le=60 * 24 * 90),
              start: str = Query("", max_length=40), end: str = Query("", max_length=40),
              camera_id: str = Query("", max_length=100), tz_offset_min: int = _TZ):
    since, until = _history_range(minutes, start, end)

    def generate():
        db = SessionLocal()
        try:
            yield from stats.iter_csv(db, since, until, camera_id, tz_offset_min)
        finally:
            db.close()

    name = f"statistiques-niifyeen-{utcnow():%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(generate(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ---------- Gestion des caméras ----------
class CameraIn(BaseModel):
    """Création ou modification. Seuls les champs envoyés sont pris en compte ; ``null`` efface un réglage facultatif."""
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = None
    label: Optional[str] = None
    location: Optional[str] = None
    source: Optional[Union[str, int]] = None
    enabled: Optional[bool] = None
    x: Optional[float] = None
    y: Optional[float] = None
    capture_width: Optional[int] = None
    capture_height: Optional[int] = None
    process_fps: Optional[float] = None
    floor_id: Optional[int] = None
    heading: Optional[float] = None
    fov: Optional[float] = None
    view_range: Optional[float] = None


class ProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Optional[Union[str, int]] = None       # à tester ; vide = source enregistrée de ``camera_id``
    camera_id: Optional[str] = None
    capture_width: Optional[int] = None
    capture_height: Optional[int] = None


def _camera_error(exc: CameraError) -> HTTPException:
    return HTTPException(exc.status, {"message": exc.message, "field": exc.field})


def _body_fields(body: CameraIn) -> dict:
    data = {k: getattr(body, k) for k in body.model_fields_set}
    if data.get("source") in (None, ""):
        data.pop("source", None)                    # « source vide » = ne pas la changer
    return data


@app.get("/api/admin/cameras")
def admin_list_cameras():
    return {"cameras": app.state.camera_manager.list_admin(), "max": config.MAX_CAMERAS,
            "version": app.state.camera_manager.version}


@app.post("/api/admin/cameras", status_code=201)
def admin_create_camera(body: CameraIn):
    try:
        return app.state.camera_manager.create(_body_fields(body))
    except CameraError as exc:
        raise _camera_error(exc)


@app.put("/api/admin/cameras/{camera_id}")
def admin_update_camera(camera_id: str, body: CameraIn):
    try:
        return app.state.camera_manager.update(camera_id, _body_fields(body))
    except CameraError as exc:
        raise _camera_error(exc)


@app.delete("/api/admin/cameras/{camera_id}")
def admin_delete_camera(camera_id: str):
    try:
        app.state.camera_manager.delete(camera_id)
    except CameraError as exc:
        raise _camera_error(exc)
    return {"deleted": True}


@app.post("/api/admin/cameras/{camera_id}/restart")
def admin_restart_camera(camera_id: str):
    try:
        return app.state.camera_manager.restart(camera_id)
    except CameraError as exc:
        raise _camera_error(exc)


@app.post("/api/admin/cameras/test")
async def admin_test_camera(body: ProbeIn):
    """Teste une source avant de l'enregistrer : ouvre le flux, lit quelques images, renvoie un aperçu."""
    manager = app.state.camera_manager
    try:
        if body.source not in (None, ""):
            source = normalize_source(body.source)
        elif body.camera_id:
            source = manager.source_of(body.camera_id)
        else:
            raise CameraError("Indiquez la source à tester.", "source")
    except CameraError as exc:
        raise _camera_error(exc)
    kind = source_kind(source)

    running = manager.find_running(source)
    if running is not None:                              # ne pas rouvrir (et perturber) une source déjà en service
        camera_id, label, cam = running
        stats = cam.stats()
        jpeg = cam.latest_jpeg_raw or cam.latest_jpeg
        return {"ok": stats["status"] == "online", "already_running": True, "camera": label, "kind": kind,
                "width": stats["width"], "height": stats["height"], "fps": stats["fps"] or None,
                "first_frame_ms": None, "error": None if stats["status"] == "online" else "La caméra en service est hors ligne.",
                "preview": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii") if jpeg else None}

    size = (body.capture_width, body.capture_height) if body.capture_width and body.capture_height else None
    async with app.state.probe_slots:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(camera_probe.probe_source, source, kind, config.PROBE_TIMEOUT_SEC, 1.0, size),
                config.PROBE_TIMEOUT_SEC + 4)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Délai dépassé : la source ne répond pas."}


@app.get("/api/admin/cameras/webcams")
async def admin_list_webcams():
    """Webcams locales disponibles (celles déjà utilisées par NiifYéén ne sont pas rouvertes)."""
    in_use = sorted(s for s in list(app.state.camera_sources.values()) if isinstance(s, int))
    found = await asyncio.to_thread(camera_probe.list_webcams, in_use)
    return {"webcams": found, "in_use": in_use}


@app.post("/api/admin/cameras/discover")
async def admin_discover_onvif():
    """Cherche les caméras ONVIF du réseau local. Ne récupère pas l'adresse du flux : elle dépend de la marque."""
    devices = await asyncio.to_thread(camera_probe.discover_onvif)
    known = app.state.camera_manager.configured_hosts()
    for device in devices:
        device["configured"] = device["address"] in known
    return {"devices": devices,
            "note": "Découverte réseau uniquement (WS-Discovery) : l'adresse du flux (chemin RTSP, identifiants) dépend "
                    "de la marque et doit être saisie à la main."}


# ---------- Centre de supervision ----------
@app.get("/api/supervision/cameras")
def supervision_cameras():
    """Photographie complète pour l'affichage initial : caméras, cibles, personnes visibles."""
    targets = app.state.targets.snapshot()
    return {
        "cameras": supervision.describe_cameras(app.state.cams, app.state.camera_sources,
                                                app.state.camera_meta, targets),
        "targets": targets,
        "people": supervision.visible_people(app.state.cams),
        "feed_last_id": app.state.feed.last_id,
        "cameras_version": app.state.camera_manager.version,
        "identities_visible": config.HISTORY_SHOW_IDENTITIES,
    }


@app.get("/api/events")
def list_events(limit: int = Query(50, ge=1, le=200), after_id: int = Query(None, ge=0),
                before_id: int = Query(None, ge=1), family: str = Query("", pattern="^(targets|people|cameras|analytics)?$"),
                camera_id: str = Query("", max_length=100),
                severity: str = Query("", pattern="^(info|warning|critical)?$")):
    """Événements, du plus récent au plus ancien. ``after_id`` ne renvoie que les nouveautés."""
    items = app.state.feed.recent(limit=limit, after_id=after_id, before_id=before_id,
                                  family=family or None, camera_id=camera_id or None, severity=severity or None)
    return {"events": items, "last_id": app.state.feed.last_id}


@app.get("/api/cameras")
def api_cameras():
    return {"cameras": list(app.state.cams.keys()), "camera_meta": app.state.camera_meta}


@app.get("/detections/recent")
def detections_recent(minutes: int = Query(10, ge=1, le=240)):
    db = SessionLocal()
    try:
        since = utcnow() - timedelta(minutes=minutes)
        rows = (
            db.query(Detection)
            .filter(Detection.timestamp >= since)
            .order_by(Detection.timestamp.desc())
            .limit(800)
            .all()
        )
        return [
            {
                "camera_id": r.camera_id,
                "timestamp": to_iso_utc(r.timestamp),
                "track_id": r.track_id,
                "global_id": r.global_id,
                "mode": r.mode,
                "label": history.public_label(r.mode, r.label),
                "target_score": r.target_score,
                "db_score": r.db_score,
                "x": r.x,
                "y": r.y,
            }
            for r in rows
        ]
    finally:
        db.close()


# ---------- Personnes enrôlées ----------
def _person_error(exc: PersonError) -> HTTPException:
    return HTTPException(exc.status, {"message": exc.message, "field": exc.field, **exc.extra})


def _require_identities():
    """Les fiches contiennent des noms : elles ne sont pas consultables quand les identités sont masquées."""
    if not config.HISTORY_SHOW_IDENTITIES:
        raise HTTPException(403, {"message": "Identités masquées par la configuration du serveur "
                                             "(NIIFYEEN_HISTORY_IDENTITIES=0) : les fiches ne sont pas consultables.",
                                  "field": None})


@app.get("/participants")
def list_participants():
    """Liste simple (ancienne interface). La liste complète, filtrable, est ``/api/people``."""
    db = SessionLocal()
    try:
        rows = db.query(Participant).order_by(Participant.id).all()
        return [{"id": p.id, "nom": p.nom, "prenom": p.prenom or "", "display_name": p.display_name,
                 "actif": bool(p.actif),
                 "date_consentement": to_iso_utc(p.date_consentement) if p.date_consentement else None}
                for p in rows]
    finally:
        db.close()


@app.post("/participants/enroll")
async def enroll(
    nom: str = Form(..., max_length=config.MAX_NAME_LENGTH),
    prenom: str = Form("", max_length=config.MAX_PERSON_NAME),
    matricule: str = Form("", max_length=config.MAX_MATRICULE),
    infos: str = Form("", max_length=config.MAX_INFOS_LENGTH),
    actif: bool = Form(True),
    consentement: bool = Form(False),
    confirmer_doublon: bool = Form(False),
    photos: List[UploadFile] = File(...),
):
    try:
        fields = people.clean_person_input({"nom": nom, "prenom": prenom, "matricule": matricule, "infos": infos,
                                            "actif": actif}, creating=True)
    except PersonError as exc:
        raise _person_error(exc)
    if not consentement:
        raise HTTPException(400, "Le consentement de la personne est obligatoire pour l'enrôler.")
    if not 1 <= len(photos) <= config.MAX_ENROLL_PHOTOS:
        raise HTTPException(400, f"Envoyer entre 1 et {config.MAX_ENROLL_PHOTOS} photos.")

    # On calcule les empreintes *avant* de créer la personne : aucune fiche vide si aucun visage.
    found = await _embeddings_from_uploads(photos)
    if not found:
        raise HTTPException(422, "Aucun visage exploitable dans les photos envoyées.")

    db = SessionLocal()
    try:
        if not confirmer_doublon:
            duplicate = people.find_duplicate(db, [emb for emb, _f, _i in found])
            if duplicate is not None:
                raise _person_error(people.PersonConflict(
                    "Ce visage ressemble à une personne déjà enrôlée : vérifiez qu'il ne s'agit pas d'un doublon.",
                    existing_id=duplicate["participant_id"], needs_confirmation=True,
                    **({"existing_name": duplicate["nom"]} if config.HISTORY_SHOW_IDENTITIES else {})))
        person = people.create_person(db, fields, [(emb, filename) for emb, filename, _img in found])
        participant_id, shown = person.id, person.display_name
    except PersonError as exc:
        raise _person_error(exc)
    finally:
        db.close()

    refresh_known()
    return {"participant_id": participant_id, "display_name": shown, "saved_embeddings": len(found),
            "ignored_photos": len(photos) - len(found)}


@app.delete("/participants/{participant_id}")
@app.delete("/api/people/{participant_id}")
def remove_participant(participant_id: int):
    db = SessionLocal()
    try:
        result = delete_participant(db, participant_id)
    finally:
        db.close()
    if result is None:
        raise HTTPException(404, "Personne introuvable.")
    refresh_known()
    return {"deleted": True, **result}


class PersonIn(BaseModel):
    """Modification d'une fiche : seuls les champs envoyés sont pris en compte."""
    model_config = ConfigDict(extra="forbid")
    nom: Optional[str] = None
    prenom: Optional[str] = None
    matricule: Optional[str] = None
    infos: Optional[str] = None
    actif: Optional[bool] = None


@app.get("/api/people")
def api_list_people(q: str = Query("", max_length=100), statut: str = Query("", pattern="^(actif|inactif)?$"),
                    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    _require_identities()
    db = SessionLocal()
    try:
        return {**people.list_people(db, q, statut, page, page_size),
                "recognition": {"probable": config.DB_PROBABLE_THRESHOLD, "to_verify": config.DB_THRESHOLD}}
    finally:
        db.close()


@app.get("/api/people/{participant_id}")
def api_person(participant_id: int):
    _require_identities()
    db = SessionLocal()
    try:
        return people.person_detail(db, participant_id, app.state.camera_meta)
    except PersonError as exc:
        raise _person_error(exc)
    finally:
        db.close()


@app.put("/api/people/{participant_id}")
def api_update_person(participant_id: int, body: PersonIn):
    db = SessionLocal()
    try:
        fields = people.clean_person_input({k: getattr(body, k) for k in body.model_fields_set}, creating=False)
        person = people.update_person(db, participant_id, fields)
        view = {"id": person.id, "display_name": person.display_name, "actif": bool(person.actif)}
    except PersonError as exc:
        raise _person_error(exc)
    finally:
        db.close()
    refresh_known()                       # le nom affiché et le statut actif/inactif sont lus par les caméras
    return view


@app.post("/api/people/{participant_id}/references")
async def api_add_references(participant_id: int, photos: List[UploadFile] = File(...)):
    if not 1 <= len(photos) <= config.MAX_ENROLL_PHOTOS:
        raise HTTPException(400, f"Envoyer entre 1 et {config.MAX_ENROLL_PHOTOS} photos.")
    found = await _embeddings_from_uploads(photos)
    db = SessionLocal()
    try:
        added = people.add_references(db, participant_id, [(emb, filename) for emb, filename, _img in found])
    except PersonError as exc:
        raise _person_error(exc)
    finally:
        db.close()
    refresh_known()
    return {"added": added, "ignored_photos": len(photos) - len(found)}


@app.delete("/api/people/{participant_id}/references/{reference_id}")
def api_delete_reference(participant_id: int, reference_id: int):
    db = SessionLocal()
    try:
        people.delete_reference(db, participant_id, reference_id)
    except PersonError as exc:
        raise _person_error(exc)
    finally:
        db.close()
    refresh_known()
    return {"deleted": True}


# ---------- Enregistrement vidéo (Module 9) ----------
class RecordingStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str
    minutes: int = 30


class RecordingStopIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str


class RecordingSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    fps: Optional[int] = None
    pre_roll_sec: Optional[int] = None
    post_roll_sec: Optional[int] = None
    retention_days: Optional[int] = None
    max_storage_mb: Optional[int] = None
    allow_export: Optional[bool] = None
    event_types: Optional[list] = None
    cameras: Optional[list] = None
    schedules: Optional[list] = None


def _rec_call(fn, *args, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, *args, **kwargs)
    except recordings.RecordingError as exc:
        raise HTTPException(exc.status, str(exc))
    finally:
        db.close()


def _rec_range(minutes, start, end):
    try:
        return history.resolve_range(minutes, start or None, end or None)
    except history.HistoryError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/recordings/settings")
def api_rec_settings():
    s = _rec_call(lambda db: recordings.load_settings(db))
    return {"settings": s, "hard_disabled": app.state.recorder.hard_disabled,
            "event_types": list(recordings.EVENT_TYPES), "limits": {"manual_minutes": recordings.MAX_MANUAL_MINUTES,
                                                                    "export_minutes": recordings.MAX_EXPORT_MINUTES}}


@app.put("/api/recordings/settings")
def api_rec_settings_save(body: RecordingSettingsIn):
    data = {k: getattr(body, k) for k in body.model_fields_set}
    s = _rec_call(lambda db: recordings.save_settings(db, data))
    app.state.recorder.reload()
    return {"settings": s}


@app.get("/api/recordings/status")
def api_rec_status():
    return app.state.recorder.status()


@app.post("/api/recordings/start", status_code=201)
def api_rec_start(body: RecordingStartIn):
    try:
        return app.state.recorder.start_manual(body.camera_id, body.minutes)
    except recordings.RecordingError as exc:
        raise HTTPException(exc.status, str(exc))


@app.post("/api/recordings/stop")
def api_rec_stop(body: RecordingStopIn):
    return {"stopped": app.state.recorder.stop_manual(body.camera_id)}


@app.get("/api/recordings/export.avi")
def api_rec_export(camera_id: str = Query(..., max_length=100), start: str = Query(..., max_length=40), end: str = Query(..., max_length=40)):
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    if not _rec_call(lambda db: recordings.load_settings(db))["allow_export"]:
        raise HTTPException(403, "L'export est désactivé dans les réglages d'enregistrement.")
    try:
        since, until = history.parse_datetime(start), history.parse_datetime(end)
    except history.HistoryError as exc:
        raise HTTPException(422, str(exc))
    path = _rec_call(recordings.export_avi, camera_id, since, until, app.state.recorder.directory)
    name = f"video-niifyeen-{recordings.safe_name(camera_id)}-{utcnow():%Y%m%d-%H%M%S}.avi"
    return FileResponse(path, media_type="video/x-msvideo", filename=name,
                        headers={"Cache-Control": "no-store"}, background=BackgroundTask(lambda: os.path.exists(path) and os.unlink(path)))


@app.get("/api/recordings/for-event/{event_id}")
def api_rec_for_event(event_id: int):
    def find(db):
        ev = db.get(Event, event_id)
        if ev is None:
            raise recordings.RecordingError("Événement introuvable.", 404)
        if not ev.camera_id:
            return {"available": False}
        hit = recordings.find_for_time(db, ev.camera_id, ev.timestamp, app.state.recorder.directory)
        return {"available": bool(hit), **(hit or {})}
    return _rec_call(find)


@app.get("/api/recordings")
def api_rec_list(camera_id: str = Query("", max_length=100), minutes: int = Query(1440, ge=1, le=60 * 24 * 90),
                 start: str = Query("", max_length=40), end: str = Query("", max_length=40), limit: int = Query(200, ge=1, le=500)):
    since, until = _rec_range(minutes, start, end)
    return {"recordings": _rec_call(recordings.list_recordings, camera_id, since, until, limit),
            "status": app.state.recorder.status()}


@app.get("/api/recordings/{rec_id}")
def api_rec_get(rec_id: int):
    return _rec_call(recordings.detail, rec_id, app.state.recorder.directory)


@app.get("/api/recordings/{rec_id}/frame/{n}")
def api_rec_frame(rec_id: int, n: int):
    data = _rec_call(recordings.frame_bytes, rec_id, n, app.state.recorder.directory)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.delete("/api/recordings/{rec_id}")
def api_rec_delete(rec_id: int):
    _rec_call(recordings.delete_recording, rec_id, app.state.recorder.directory, set(app.state.recorder.active().values()))
    return {"deleted": True}


# ---------- Historique et investigation (Module 8) ----------
class DossierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = None
    note: Optional[str] = None


class DossierItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    ref_id: int
    note: Optional[str] = None


class DossierItemNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = None


def _inv_call(fn, *args, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, *args, **kwargs)
    except investigation.InvestigationError as exc:
        raise HTTPException(exc.status, str(exc))
    finally:
        db.close()


def _camera_labels() -> dict:
    return {cid: (m or {}).get("label") or cid for cid, m in (app.state.camera_meta or {}).items()}


def _inv_run(minutes, start, end, camera_id, types, participant_id, target_id, session, q, only_alerts, limit=investigation.MAX_ITEMS):
    """Exécute une recherche de chronologie et retourne (résultat, description des filtres)."""
    db = SessionLocal()
    try:
        since, until = history.resolve_range(minutes, start or None, end or None)
        result = investigation.timeline(db, since, until, camera_id, investigation.parse_types(types), participant_id,
                                        target_id, session, q, only_alerts, limit)
    except history.HistoryError as exc:
        raise HTTPException(422, str(exc))
    except investigation.InvestigationError as exc:
        raise HTTPException(exc.status, str(exc))
    finally:
        db.close()
    labels = _camera_labels()
    parts = []
    if camera_id:
        parts.append("caméra : " + labels.get(camera_id, camera_id))
    if types:
        parts.append("types : " + types)
    if participant_id is not None:
        parts.append(f"personne n°{participant_id}")
    if target_id is not None:
        parts.append(f"cible n°{target_id}")
    if session is not None:
        parts.append(f"session de cible n°{session}")
    if q:
        parts.append(f"texte : « {q} »")
    if only_alerts:
        parts.append("événements ayant donné une alerte")
    return result, ("Filtres : " + " ; ".join(parts) + ".") if parts else "Aucun filtre particulier."


_INV_FILTERS = dict(minutes=Query(60, ge=1, le=60 * 24 * 90), start=Query("", max_length=40), end=Query("", max_length=40),
                    camera_id=Query("", max_length=100), types=Query("", max_length=400),
                    participant_id=Query(None, ge=1), target_id=Query(None, ge=1), session=Query(None, ge=0),
                    q=Query("", max_length=100), only_alerts=Query(False))


@app.get("/api/investigation/timeline")
def api_inv_timeline(minutes: int = _INV_FILTERS["minutes"], start: str = _INV_FILTERS["start"], end: str = _INV_FILTERS["end"],
                     camera_id: str = _INV_FILTERS["camera_id"], types: str = _INV_FILTERS["types"],
                     participant_id: int = _INV_FILTERS["participant_id"], target_id: int = _INV_FILTERS["target_id"],
                     session: int = _INV_FILTERS["session"], q: str = _INV_FILTERS["q"],
                     only_alerts: bool = _INV_FILTERS["only_alerts"], limit: int = Query(500, ge=1, le=500)):
    return _inv_run(minutes, start, end, camera_id, types, participant_id, target_id, session, q, only_alerts, limit)[0]


@app.get("/api/investigation/timeline.csv")
def api_inv_csv(minutes: int = _INV_FILTERS["minutes"], start: str = _INV_FILTERS["start"], end: str = _INV_FILTERS["end"],
                camera_id: str = _INV_FILTERS["camera_id"], types: str = _INV_FILTERS["types"],
                participant_id: int = _INV_FILTERS["participant_id"], target_id: int = _INV_FILTERS["target_id"],
                session: int = _INV_FILTERS["session"], q: str = _INV_FILTERS["q"],
                only_alerts: bool = _INV_FILTERS["only_alerts"]):
    result, _ = _inv_run(minutes, start, end, camera_id, types, participant_id, target_id, session, q, only_alerts)
    name = f"investigation-niifyeen-{utcnow():%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(investigation.iter_csv(result), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _pdf_response(make, name: str):
    try:
        data = make()
    except pdf_report.PdfUnavailable as exc:
        raise HTTPException(501, str(exc))
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/investigation/report.pdf")
def api_inv_report(minutes: int = _INV_FILTERS["minutes"], start: str = _INV_FILTERS["start"], end: str = _INV_FILTERS["end"],
                   camera_id: str = _INV_FILTERS["camera_id"], types: str = _INV_FILTERS["types"],
                   participant_id: int = _INV_FILTERS["participant_id"], target_id: int = _INV_FILTERS["target_id"],
                   session: int = _INV_FILTERS["session"], q: str = _INV_FILTERS["q"],
                   only_alerts: bool = _INV_FILTERS["only_alerts"]):
    """Rapport PDF des résultats ; avec ``session``, c'est le rapport de session de cible."""
    result, description = _inv_run(minutes, start, end, camera_id, types, participant_id, target_id, session, q, only_alerts)
    title = f"Rapport de session de cible n°{session}" if session is not None else "Rapport d'investigation"
    return _pdf_response(lambda: investigation.timeline_pdf(result, _camera_labels(), description, title),
                         f"rapport-niifyeen-{utcnow():%Y%m%d-%H%M%S}.pdf")


@app.get("/api/investigation/events/{event_id}")
def api_inv_event(event_id: int):
    return _inv_call(investigation.event_detail, event_id, 60, app.state.recorder.directory)


@app.get("/api/dossiers")
def api_dossiers():
    return {"dossiers": _inv_call(investigation.list_dossiers), "max": investigation.MAX_DOSSIERS}


@app.post("/api/dossiers", status_code=201)
def api_dossier_create(body: DossierIn):
    return _inv_call(investigation.create_dossier, body.title, body.note)


@app.get("/api/dossiers/{dossier_id}")
def api_dossier_get(dossier_id: int):
    return _inv_call(investigation.get_dossier, dossier_id)


@app.put("/api/dossiers/{dossier_id}")
def api_dossier_update(dossier_id: int, body: DossierIn):
    return _inv_call(investigation.update_dossier, dossier_id, {k: getattr(body, k) for k in body.model_fields_set})


@app.delete("/api/dossiers/{dossier_id}")
def api_dossier_delete(dossier_id: int):
    _inv_call(investigation.delete_dossier, dossier_id)
    return {"deleted": True}


@app.post("/api/dossiers/{dossier_id}/items", status_code=201)
def api_dossier_add(dossier_id: int, body: DossierItemIn):
    return _inv_call(investigation.add_item, dossier_id, body.kind, body.ref_id, body.note)


@app.put("/api/dossiers/{dossier_id}/items/{item_id}")
def api_dossier_item_note(dossier_id: int, item_id: int, body: DossierItemNoteIn):
    _inv_call(investigation.update_item, dossier_id, item_id, body.note)
    return {"updated": True}


@app.delete("/api/dossiers/{dossier_id}/items/{item_id}")
def api_dossier_item_remove(dossier_id: int, item_id: int):
    _inv_call(investigation.remove_item, dossier_id, item_id)
    return {"deleted": True}


@app.get("/api/dossiers/{dossier_id}/report.pdf")
def api_dossier_report(dossier_id: int):
    dossier = _inv_call(investigation.get_dossier, dossier_id)
    return _pdf_response(lambda: investigation.dossier_pdf(dossier, _camera_labels()),
                         f"dossier-{dossier_id}-niifyeen-{utcnow():%Y%m%d}.pdf")


# ---------- Centre d'alertes (Module 7) ----------
class AlertStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    note: Optional[str] = None


class AlertNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str


class AlertBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: List[int]
    status: str


class AlertRulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: dict


class ChannelTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str


def _alert_http(exc: alerts.AlertError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@app.get("/api/alerts")
def api_list_alerts(status: str = Query("", max_length=20), priority: str = Query("", max_length=20),
                    category: str = Query("", max_length=20), camera_id: str = Query("", max_length=100),
                    q: str = Query("", max_length=100), since: str = Query("", max_length=40),
                    until: str = Query("", max_length=40), before_id: int = Query(None, ge=1),
                    after_id: int = Query(None, ge=0), limit: int = Query(50, ge=1, le=200)):
    db = SessionLocal()
    try:
        items = alerts.list_alerts(db, status or None, priority or None, category or None, camera_id or None,
                                   q or None, since or None, until or None, before_id, after_id, limit)
        return {"alerts": items, "counts": alerts.counts(db)}
    except alerts.AlertError as exc:
        raise _alert_http(exc)
    finally:
        db.close()


@app.get("/api/alerts/summary")
def api_alerts_summary(after_id: int = Query(None, ge=0)):
    db = SessionLocal()
    try:
        return alerts.summary(db, after_id)
    finally:
        db.close()


@app.get("/api/alerts/rules")
def api_alert_rules():
    db = SessionLocal()
    try:
        return {"rules": alerts.rules_view(alerts.load_rules(db)), "priorities": list(alerts.PRIORITIES)}
    finally:
        db.close()


@app.put("/api/alerts/rules")
def api_alert_rules_save(body: AlertRulesIn):
    db = SessionLocal()
    try:
        rules = alerts.save_rules(db, body.rules)
    except alerts.AlertError as exc:
        raise _alert_http(exc)
    finally:
        db.close()
    app.state.alerts.invalidate()
    return {"rules": alerts.rules_view(rules)}


@app.get("/api/alerts/channels")
def api_alert_channels():
    return alerts.channels_status(alerts.notify_settings())


@app.post("/api/alerts/channels/test")
def api_alert_channel_test(body: ChannelTestIn):
    if body.channel not in ("email", "telegram"):
        raise HTTPException(422, "Canal inconnu (email ou telegram).")
    result = app.state.alerts.notifier.test(body.channel)
    return {"channel": body.channel, "ok": result == "envoyée", "result": result}


@app.post("/api/alerts/bulk")
def api_alerts_bulk(body: AlertBulkIn):
    db = SessionLocal()
    try:
        return alerts.bulk_status(db, body.ids, body.status)
    except alerts.AlertError as exc:
        raise _alert_http(exc)
    finally:
        db.close()


@app.get("/api/alerts/{alert_id}")
def api_get_alert(alert_id: int):
    db = SessionLocal()
    try:
        return alerts.get_alert(db, alert_id)
    except alerts.AlertError as exc:
        raise _alert_http(exc)
    finally:
        db.close()


@app.post("/api/alerts/{alert_id}/status")
def api_alert_status(alert_id: int, body: AlertStatusIn):
    db = SessionLocal()
    try:
        alerts.set_status(db, alert_id, body.status, body.note)
        return alerts.get_alert(db, alert_id)
    except alerts.AlertError as exc:
        raise _alert_http(exc)
    finally:
        db.close()


@app.post("/api/alerts/{alert_id}/note")
def api_alert_note(alert_id: int, body: AlertNoteIn):
    db = SessionLocal()
    try:
        return alerts.add_note(db, alert_id, body.note)
    except alerts.AlertError as exc:
        raise _alert_http(exc)
    finally:
        db.close()


# ---------- Lignes, zones et affluence (Module 6) ----------
class ZoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: Optional[str] = None
    kind: Optional[str] = None
    name: Optional[str] = None
    points: Optional[list] = None
    label_in: Optional[str] = None
    label_out: Optional[str] = None
    capacity: Optional[int] = None
    restricted: Optional[bool] = None
    max_dwell_sec: Optional[int] = None
    enabled: Optional[bool] = None


def _zone_http(exc: analytics.ZoneError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@app.get("/api/zones")
def api_list_zones(camera_id: str = Query("", max_length=100)):
    db = SessionLocal()
    try:
        return {"zones": analytics.list_zones(db, camera_id or None), "max_per_camera": analytics.MAX_ZONES_PER_CAMERA}
    finally:
        db.close()


@app.post("/api/zones", status_code=201)
def api_create_zone(body: ZoneIn):
    data = {k: getattr(body, k) for k in body.model_fields_set}
    db = SessionLocal()
    try:
        row = analytics.create_zone(db, data, list(app.state.cams.keys()))
        out = analytics.view(row)
    except analytics.ZoneError as exc:
        raise _zone_http(exc)
    finally:
        db.close()
    app.state.analytics.reload()
    return out


@app.put("/api/zones/{zone_id}")
def api_update_zone(zone_id: int, body: ZoneIn):
    data = {k: getattr(body, k) for k in body.model_fields_set}
    db = SessionLocal()
    try:
        out = analytics.view(analytics.update_zone(db, zone_id, data))
    except analytics.ZoneError as exc:
        raise _zone_http(exc)
    finally:
        db.close()
    app.state.analytics.reload()
    return out


@app.delete("/api/zones/{zone_id}")
def api_delete_zone(zone_id: int):
    db = SessionLocal()
    try:
        analytics.delete_zone(db, zone_id)
    except analytics.ZoneError as exc:
        raise _zone_http(exc)
    finally:
        db.close()
    app.state.analytics.reload()
    return {"deleted": True}


@app.get("/api/analytics/live")
def api_analytics_live():
    """Occupation et compteurs depuis le démarrage du moteur (estimations), état des caméras (masquée / figée)."""
    return app.state.analytics.snapshot()


@app.get("/api/analytics/zones/{zone_id}/series")
def api_zone_series(zone_id: int, minutes: int = Query(180, ge=5, le=60 * 24 * 30)):
    app.state.analytics.flush()
    until = utcnow()
    db = SessionLocal()
    try:
        analytics.get_zone(db, zone_id)
        return {"zone_id": zone_id, "bucket_sec": analytics.BUCKET_SEC,
                "series": analytics.series(db, zone_id, analytics.bucket_start(until - timedelta(minutes=minutes)), until)}
    except analytics.ZoneError as exc:
        raise _zone_http(exc)
    finally:
        db.close()


@app.get("/api/analytics/overview")
def api_analytics_overview(minutes: int = Query(60, ge=5, le=60 * 24 * 30)):
    app.state.analytics.flush()
    until = utcnow()
    db = SessionLocal()
    try:
        return {"minutes": minutes, "items": analytics.overview(db, analytics.bucket_start(until - timedelta(minutes=minutes)), until)}
    finally:
        db.close()


# ---------- Étages et plans ----------
async def _read_floor_upload(upload: Optional[UploadFile]) -> Optional[bytes]:
    if upload is None or not upload.filename:
        return None
    data = await upload.read(floors.MAX_UPLOAD_BYTES + 1)
    return data


def _floor_http(exc: floors.FloorError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@app.get("/api/floors")
def api_floors():
    db = SessionLocal()
    try:
        return {"floors": floors.list_floors(db), "max": floors.MAX_FLOORS,
                "default_plan": "/static/site-plan.svg", "default_size": list(floors.SVG_DEFAULT)}
    finally:
        db.close()


@app.post("/api/floors", status_code=201)
async def api_create_floor(name: str = Form(""), level: str = Form("0"), file: UploadFile = File(...)):
    data = await _read_floor_upload(file)
    db = SessionLocal()
    try:
        floor = floors.create(db, name, level, data or b"")
        return floors.view(floor)
    except floors.FloorError as exc:
        raise _floor_http(exc)
    finally:
        db.close()


@app.put("/api/floors/{floor_id}")
async def api_update_floor(floor_id: int, name: Optional[str] = Form(None), level: Optional[str] = Form(None),
                           file: Optional[UploadFile] = File(None)):
    data = await _read_floor_upload(file)
    fields = {k: v for k, v in (("name", name), ("level", level)) if v is not None}
    db = SessionLocal()
    try:
        floor, ratio_changed = floors.update(db, floor_id, fields, data)
        out = floors.view(floor)
        out["ratio_changed"] = ratio_changed
        return out
    except floors.FloorError as exc:
        raise _floor_http(exc)
    finally:
        db.close()


@app.delete("/api/floors/{floor_id}")
def api_delete_floor(floor_id: int):
    db = SessionLocal()
    try:
        released = floors.delete(db, floor_id)
    except floors.FloorError as exc:
        raise _floor_http(exc)
    finally:
        db.close()
    app.state.camera_manager.refresh_meta()
    return {"deleted": True, "cameras_unplaced": released}


@app.get("/api/floors/{floor_id}/coverage")
def api_floor_coverage(floor_id: int):
    db = SessionLocal()
    try:
        return floors.coverage(db, floor_id)
    except floors.FloorError as exc:
        raise _floor_http(exc)
    finally:
        db.close()


@app.get("/api/floors/{floor_id}/image")
def api_floor_image(floor_id: int):
    db = SessionLocal()
    try:
        floor = floors.get(db, floor_id)
        data, mime = floor.image, floor.mime
    except floors.FloorError as exc:
        raise _floor_http(exc)
    finally:
        db.close()
    return Response(data, media_type=mime, headers={
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


# ---------- Cible ----------
def _target_json(target, db) -> dict:
    return {
        "id": target.id, "label": target.label, "color": target.color,
        "created_at": target.created_at, "source": target.source,
        "photo_url": f"/targets/{target.id}/photo?v={int(target.created_at * 1000)}" if target.photo else None,
        "profile": target_profile.render_profile(db, target.match, app.state.camera_meta),
    }


@app.post("/search/by-image")
async def search_by_image(photos: List[UploadFile] = File(...)):
    """Ajoute une cible à partir de 1 à 10 photos. Les cibles déjà définies sont conservées."""
    registry = app.state.targets
    found = await _embeddings_from_uploads(photos[:config.MAX_SEARCH_PHOTOS])
    if not found:
        return {"target_set": False, "reason": "no_valid_face", "targets_count": len(registry)}

    mean_emb = np.mean(np.stack([e for e, _, _ in found], axis=0), axis=0)
    mean_emb = mean_emb / (np.linalg.norm(mean_emb) + 1e-9)
    thumbnail = make_thumbnail(found[0][2])

    # Cette personne est-elle dans la base ? Si oui, on renvoie sa fiche.
    db = SessionLocal()
    try:
        match = target_profile.find_match(db, mean_emb)
        try:
            target = registry.add(mean_emb, photo=thumbnail, match=match)
        except TargetLimitError as exc:
            raise HTTPException(409, str(exc))
        except DuplicateTargetError as exc:
            raise HTTPException(409, f"Cette personne est déjà suivie ({exc.existing.label}).")
        _apply_targets()
        body = _target_json(target, db)
    finally:
        db.close()

    return {"target_set": True, "target_threshold": config.TARGET_THRESHOLD,
            "nb_photos_utilisees": len(found), "targets_count": len(registry),
            "target": body, "profile": body["profile"]}


class TargetFromPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participant_id: int = Field(ge=1)


class TargetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max: int = Field(ge=1, le=config.TARGETS_HARD_LIMIT)


def _add_person_target(db, participant_id: int):
    """Ajoute comme cible une personne enrôlée, à partir de ses empreintes de référence (aucune photo requise)."""
    registry = app.state.targets
    if participant_id in registry.participant_ids():
        raise HTTPException(409, "Cette personne est déjà suivie.")
    try:
        embedding = target_sessions.person_embedding(db, participant_id)
    except people.PersonNotFound as exc:
        raise HTTPException(404, str(exc))
    match = target_sessions.person_match(db, participant_id, embedding)
    try:
        return registry.add(embedding, photo=None, match=match, source="person")
    except TargetLimitError as exc:
        raise HTTPException(409, str(exc))
    except DuplicateTargetError as exc:
        raise HTTPException(409, f"Cette personne est déjà suivie ({exc.existing.label}).")


@app.post("/api/targets/from-person")
def target_from_person(body: TargetFromPerson):
    """Choisit une personne de la base comme cible (sans renvoyer de photo)."""
    if not config.HISTORY_SHOW_IDENTITIES:
        raise HTTPException(403, "Identités masquées : choix d'une personne indisponible.")
    db = SessionLocal()
    try:
        target = _add_person_target(db, body.participant_id)
        _apply_targets()
        payload = _target_json(target, db)
    finally:
        db.close()
    return {"target_set": True, "targets_count": len(app.state.targets), "target": payload}


@app.get("/api/targets/settings")
def targets_settings():
    reg = app.state.targets
    return {"max": reg.max_targets, "count": len(reg), "hard_limit": config.TARGETS_HARD_LIMIT,
            "default": config.MAX_TARGETS}


@app.put("/api/targets/settings")
def update_targets_settings(body: TargetSettings):
    try:
        value = app.state.targets.set_capacity(body.max)
    except TargetLimitError as exc:
        raise HTTPException(409, str(exc))
    db = SessionLocal()
    try:
        target_sessions.save_capacity(db, value)
    finally:
        db.close()
    return {"max": value, "count": len(app.state.targets)}


def _session_view(db, data):
    if data is None:
        return {"saved": False}
    out = {"saved": True, "saved_at": data["saved_at"], "count": len(data["participant_ids"]),
           "unenrolled_skipped": data["unenrolled_skipped"]}
    if config.HISTORY_SHOW_IDENTITIES:
        people_rows = []
        for pid in data["participant_ids"]:
            p = db.get(Participant, pid)
            people_rows.append({"participant_id": pid, "nom": p.display_name if p else None, "exists": p is not None})
        out["people"] = people_rows
    return out


@app.get("/api/targets/session")
def get_target_session():
    db = SessionLocal()
    try:
        return _session_view(db, target_sessions.load_session(db))
    finally:
        db.close()


@app.post("/api/targets/session/save")
def save_target_session():
    """Mémorise les personnes enrôlées actuellement suivies (pas les cibles non enrôlées)."""
    reg = app.state.targets
    ids = reg.participant_ids()
    unenrolled = len(reg) - len(ids)
    db = SessionLocal()
    try:
        data = target_sessions.save_session(db, ids, unenrolled)
        return _session_view(db, data)
    finally:
        db.close()


@app.post("/api/targets/session/resume")
def resume_target_session():
    """Remet comme cibles les personnes de la session sauvegardée (nouvelles trajectoires)."""
    if not config.HISTORY_SHOW_IDENTITIES:
        raise HTTPException(403, "Identités masquées : reprise indisponible.")
    db = SessionLocal()
    try:
        data = target_sessions.load_session(db)
        if data is None:
            raise HTTPException(404, "Aucune session sauvegardée.")
        added, already, missing = 0, 0, 0
        for pid in data["participant_ids"]:
            try:
                _add_person_target(db, pid)
                added += 1
            except HTTPException as exc:
                if exc.status_code == 404:
                    missing += 1
                elif "déjà suivie" in str(exc.detail):
                    already += 1
                else:
                    break                       # capacité atteinte : inutile d'insister
        _apply_targets()
        return {"added": added, "already_tracked": already, "missing": missing,
                "not_added_capacity": len(data["participant_ids"]) - added - already - missing,
                "targets_count": len(app.state.targets)}
    finally:
        db.close()


@app.delete("/api/targets/session")
def forget_target_session():
    db = SessionLocal()
    try:
        return {"forgotten": target_sessions.forget_session(db)}
    finally:
        db.close()


@app.get("/targets")
def list_targets():
    """Cibles en cours, avec leur fiche (reconstruite à chaque appel : l'activité évolue)."""
    registry = app.state.targets
    db = SessionLocal()
    try:
        items = [_target_json(t, db) for t in registry.items()]
    finally:
        db.close()
    return {"targets": items, "max": registry.max_targets, "version": registry.version}


@app.delete("/targets/{target_id}")
def remove_target(target_id: int):
    if not app.state.targets.remove(target_id):
        raise HTTPException(404, "Cible introuvable.")
    _apply_targets()
    return {"removed": True, "targets_count": len(app.state.targets)}


@app.get("/targets/{target_id}/photo")
def target_photo(target_id: int):
    target = app.state.targets.get(target_id)
    if target is None or not target.photo:
        raise HTTPException(404, "Photo introuvable.")
    return Response(target.photo, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/target/clear")
def clear_targets():
    """Retire toutes les cibles."""
    removed = app.state.targets.clear()
    _apply_targets()
    return {"cleared": True, "removed": removed}


# ---------- Vidéo & temps réel ----------
@app.get("/video/{cam_id}")
def video_cam(cam_id: str):
    cam = app.state.cams.get(cam_id)
    if not cam:
        return JSONResponse({"error": "cam_id inconnu"}, status_code=404)
    return StreamingResponse(
        mjpeg_frames(cam), media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            events = [ev for cam in list(app.state.cams.values()) for ev in list(cam.latest_events or [])]
            await ws.send_json({
                "events": events,
                "cameras": list(app.state.cams.keys()),
                "camera_meta": app.state.camera_meta,
                "targets": app.state.targets.snapshot(),
                "targets_version": app.state.targets.version,
                "cameras_status": supervision.camera_stats(app.state.cams),
                "cameras_version": app.state.camera_manager.version,
                "people": supervision.visible_people(app.state.cams),
                "feed_last_id": app.state.feed.last_id,
            })
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError):
        pass


# ---------- Flux vidéo multiplexé ----------
MAX_VIDEO_SUBSCRIPTIONS = 16


@app.websocket("/ws/video")
async def ws_video(ws: WebSocket):
    """Toutes les caméras demandées sur UNE seule connexion (un navigateur n'ouvre qu'environ 6
    connexions HTTP par serveur : une grille de 16 flux MJPEG se bloquerait).

    Le client envoie des messages JSON :
        {"subscribe": ["entre", "salle"], "annotations": true, "fps": 10}
    Le serveur répond par des messages binaires :  1 octet = longueur de l'identifiant caméra,
    l'identifiant en UTF-8, puis l'image JPEG.
    """
    await ws.accept()
    cams = app.state.cams
    sub = {"cams": [], "annotations": True, "fps": 10.0}
    last_sent: dict = {}
    raw_registered: set = set()

    async def receiver():
        try:
            while True:
                try:
                    msg = await ws.receive_json()
                except ValueError:
                    continue                                    # message illisible : ignoré
                if not isinstance(msg, dict):
                    continue
                if isinstance(msg.get("subscribe"), list):
                    wanted = [c for c in msg["subscribe"] if isinstance(c, str) and c in cams]
                    sub["cams"] = list(dict.fromkeys(wanted))[:MAX_VIDEO_SUBSCRIPTIONS]
                if isinstance(msg.get("annotations"), bool):
                    sub["annotations"] = msg["annotations"]
                if isinstance(msg.get("fps"), (int, float)) and not isinstance(msg["fps"], bool):
                    sub["fps"] = min(25.0, max(1.0, float(msg["fps"])))
        except (WebSocketDisconnect, RuntimeError):
            return

    task = asyncio.create_task(receiver())
    try:
        while not task.done():
            started = time.monotonic()
            use_raw = not sub["annotations"]

            # Les images sans annotations ne sont encodées que pour les caméras qui en ont un spectateur.
            desired = {c for c in sub["cams"] if c in cams} if use_raw else set()
            for cam_id in desired - raw_registered:
                cams[cam_id].add_raw_viewer()
            for cam_id in raw_registered - desired:
                other = cams.get(cam_id)
                if other is not None:
                    other.remove_raw_viewer()
            raw_registered = desired

            for cam_id in list(sub["cams"]):
                cam = cams.get(cam_id)
                if cam is None:                                 # caméra retirée ou désactivée entre-temps
                    continue
                if use_raw:
                    seq, jpeg = cam.raw_seq, cam.latest_jpeg_raw
                    if cam.raw_seq != cam.frame_seq:            # image brute périmée : on attend la suivante
                        continue
                else:
                    seq, jpeg = cam.frame_seq, cam.latest_jpeg
                if jpeg is None or last_sent.get((cam_id, use_raw)) == seq:
                    continue
                last_sent[(cam_id, use_raw)] = seq
                ident = cam_id.encode("utf-8")
                await ws.send_bytes(bytes([len(ident)]) + ident + jpeg)

            await asyncio.sleep(max(0.0, 1.0 / sub["fps"] - (time.monotonic() - started)))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        task.cancel()
        for cam_id in raw_registered:
            other = cams.get(cam_id)
            if other is not None:
                other.remove_raw_viewer()
