import json
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
from .db import (Authority, Dispatch, init_db, SessionLocal, Participant, EmbeddingRef, Detection,
                 utcnow, to_iso_utc, delete_participant, save_target_move, Event)
from .people import PersonError
from . import alerts, camera_probe, dispatch, events, inbox, floors, history, people, supervision, target_profile, target_sessions
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
    width, height = cfg.get("capture_width"), cfg.get("capture_height")
    capture_size = (width, height) if width and height else config.DEFAULT_CAPTURE_SIZE
    return PersonPipeline(
        source=cfg["source"], camera_id=cfg["id"], reid=app.state.reid,
        capture_size=capture_size, out_size=config.STREAM_SIZE,
        tracker_yaml="bytetrack.yaml", imgsz=480, yolo_conf=0.35,
        head_ratio=0.50, recog_every_sec=0.60, lock_hold_sec=config.IDENTITY_HOLD_SEC, jpeg_quality=70,
        process_fps=cfg.get("process_fps"),
    )


# ---------- Cycle de vie ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
    imported = app.state.camera_manager.seed_if_needed(config.load_cameras())
    if imported:
        print(f"[MAIN] {imported} caméra(s) importée(s) depuis {config.CAMERAS_FILE.name} (premier démarrage)")
    app.state.camera_manager.load_and_start()

    app.state.target_worker = TargetWorker(app.state.targets, lambda: app.state.cams)
    app.state.target_worker.start()
    app.state.logger = DBLogger(lambda: app.state.cams)
    app.state.logger.start()
    app.state.monitor = SupervisionMonitor(lambda: app.state.cams, app.state.targets.items,
                                           app.state.feed, lambda: app.state.camera_meta)
    app.state.monitor.start()
    app.state.alerts = alerts.AlertCenter(app.state.feed, lambda: app.state.camera_meta)
    app.state.alerts.start()
    app.state.dispatcher = dispatch.Dispatcher(lambda: app.state.camera_meta, app.state.targets.get,
                                                 lambda cid: getattr(app.state.cams.get(cid), "latest_jpeg", None),
                                                 lambda cid, tid: (lambda c: c.target_crop(tid, max_age_sec=3.0) if c is not None else None)(app.state.cams.get(cid)))
    app.state.dispatcher.start(app.state.feed)
    _db = SessionLocal()
    try:
        dispatch.ensure_tokens(_db)
        inbox.set_lan(dispatch.lan_enabled(_db))
    finally:
        _db.close()
    app.state.inbox = inbox.InboxServer(lambda: app.state.camera_meta)
    if app.state.inbox.start():
        print(f"[MAIN] Boîte de réception des autorités : {inbox.public_base()}/a/<lien de l'autorité>")
    print(f"[MAIN] Caméras démarrées: {list(app.state.cams.keys())}")
    try:
        yield
    finally:
        app.state.inbox.stop()
        app.state.dispatcher.stop()
        app.state.alerts.stop()
        app.state.monitor.stop()
        app.state.logger.stop()
        app.state.target_worker.stop()
        app.state.camera_manager.stop_all()


app = FastAPI(title="NiifYéén — Cheminement + Historique + DB names", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def _revalidate_pages(request, call_next):
    """Pages et scripts : le navigateur revérifie à chaque fois (sinon une ancienne interface peut rester affichée après une mise à jour)."""
    response = await call_next(request)
    path = request.url.path
    if (path.startswith("/static/") or response.headers.get("content-type", "").startswith("text/html")) \
            and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
def page_home():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/cameras", response_class=HTMLResponse)
def page_cameras():
    return (BASE_DIR / "static" / "cameras.html").read_text(encoding="utf-8")


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


@app.get("/authorities", response_class=HTMLResponse)
def page_authorities():
    return (BASE_DIR / "static" / "authorities.html").read_text(encoding="utf-8")


@app.get("/alerts", response_class=HTMLResponse)
def page_alerts():
    return (BASE_DIR / "static" / "alerts.html").read_text(encoding="utf-8")


@app.get("/map", response_class=HTMLResponse)
def page_map():
    return (BASE_DIR / "static" / "map.html").read_text(encoding="utf-8")


@app.get("/history", response_class=HTMLResponse)
def page_history():
    return (BASE_DIR / "static" / "history.html").read_text(encoding="utf-8")


# ---------- API ----------
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
                before_id: int = Query(None, ge=1), family: str = Query("", pattern="^(targets|people|cameras)?$"),
                camera_id: str = Query("", max_length=100),
                severity: str = Query("", pattern="^(info|warning|critical)?$")):
    """Événements, du plus récent au plus ancien. ``after_id`` ne renvoie que les nouveautés."""
    items = app.state.feed.recent(limit=limit, after_id=after_id, before_id=before_id,
                                  family=family or None, camera_id=camera_id or None, severity=severity or None)
    return {"events": items, "last_id": app.state.feed.last_id}


@app.get("/api/cameras")
def api_cameras():
    try:
        app.state.camera_manager.refresh_meta()      # noms/positions modifiés directement dans la base
    except Exception:
        pass
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


class AlertScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets_only: bool


@app.get("/api/alerts/scope")
def api_alert_scope():
    db = SessionLocal()
    try:
        return {"targets_only": alerts.targets_only(db)}
    finally:
        db.close()


@app.put("/api/alerts/scope")
def api_alert_scope_save(body: AlertScopeIn):
    db = SessionLocal()
    try:
        value = alerts.set_targets_only(db, body.targets_only)
    finally:
        db.close()
    app.state.alerts.invalidate()
    return {"targets_only": value}


class AuthorityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    service: Optional[str] = None
    phone: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    cameras: Optional[List[str]] = None
    x: Optional[float] = None
    y: Optional[float] = None
    enabled: Optional[bool] = None


class DispatchScopeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _authority_out(a: Authority) -> dict:
    """Autorité telle que l'affiche la page d'administration locale : le lien secret de sa boîte de réception, jamais le jeton seul."""
    view = dispatch.authority_view(a)
    token = view.pop("inbox_token", None)
    view["inbox_url"] = f"{inbox.public_base()}/a/{token}" if token else None
    return view


def _dispatch_error(exc: "dispatch.DispatchError"):
    return HTTPException(exc.status, {"message": exc.message, "field": exc.field})


@app.get("/api/authorities")
def api_authorities():
    db = SessionLocal()
    try:
        dispatch.ensure_tokens(db)
        return {"authorities": [_authority_out(a) for a in db.query(Authority).order_by(Authority.id).all()],
                "enabled": dispatch.is_enabled(db), "telegram_configured": dispatch.telegram_ready(),
                "inbox": {"enabled": config.INBOX_ENABLED, "port": config.INBOX_PORT,
                          "lan_forced": config.INBOX_HOST not in ("127.0.0.1", "localhost", "::1"),
                          "lan_open": inbox.lan_open(), "host": inbox.current_host(),
                          "lan_ip": (inbox.lan_addresses() or [None])[0], "lan_ips": inbox.lan_addresses()},
                "cooldown_sec": dispatch.COOLDOWN_SEC,
                "cameras": [{"id": k, "label": v.get("label") or k} for k, v in getattr(app.state, "camera_meta", {}).items()]}
    finally:
        db.close()


@app.put("/api/inbox-lan")
def api_inbox_lan(body: DispatchScopeIn):
    """Ouvre (ou ferme) la boîte de réception aux téléphones du réseau local, sans variable d'environnement."""
    db = SessionLocal()
    try:
        dispatch.set_lan_enabled(db, body.enabled)
    finally:
        db.close()
    inbox.set_lan(body.enabled)
    srv = getattr(app.state, "inbox", None)
    restarted = srv.restart() if srv is not None else False
    return {"lan_open": inbox.lan_open(), "restarted": restarted, "base": inbox.public_base()}


@app.put("/api/authorities-scope")
def api_authorities_scope(body: DispatchScopeIn):
    db = SessionLocal()
    try:
        return {"enabled": dispatch.set_enabled(db, body.enabled)}
    finally:
        db.close()


@app.post("/api/authorities", status_code=201)
def api_authority_create(body: AuthorityIn):
    db = SessionLocal()
    try:
        try:
            data = dispatch.clean_authority(body.model_dump(exclude_unset=True))
        except dispatch.DispatchError as exc:
            raise _dispatch_error(exc)
        cams = data.pop("cameras", [])
        row = Authority(cameras=json.dumps(cams), inbox_token=dispatch.new_token(), **data)
        db.add(row)
        db.commit()
        return _authority_out(row)
    finally:
        db.close()


@app.put("/api/authorities/{authority_id}")
def api_authority_update(authority_id: int, body: AuthorityIn):
    db = SessionLocal()
    try:
        row = db.get(Authority, authority_id)
        if row is None:
            raise HTTPException(404, {"message": "Autorité introuvable.", "field": None})
        try:
            data = dispatch.clean_authority(body.model_dump(exclude_unset=True), partial=True)
        except dispatch.DispatchError as exc:
            raise _dispatch_error(exc)
        if "cameras" in data:
            row.cameras = json.dumps(data.pop("cameras"))
        for key, value in data.items():
            setattr(row, key, value)
        db.commit()
        return _authority_out(row)
    finally:
        db.close()


@app.delete("/api/authorities/{authority_id}")
def api_authority_delete(authority_id: int):
    db = SessionLocal()
    try:
        row = db.get(Authority, authority_id)
        if row is None:
            raise HTTPException(404, {"message": "Autorité introuvable.", "field": None})
        db.delete(row)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/authorities/{authority_id}/inbox-token")
def api_authority_new_link(authority_id: int):
    """Régénère le lien secret : l'ancien cesse de fonctionner immédiatement (lien perdu ou partagé par erreur)."""
    db = SessionLocal()
    try:
        row = db.get(Authority, authority_id)
        if row is None:
            raise HTTPException(404, {"message": "Autorité introuvable.", "field": None})
        row.inbox_token = dispatch.new_token()
        db.commit()
        return _authority_out(row)
    finally:
        db.close()


@app.post("/api/authorities/{authority_id}/test")
def api_authority_test(authority_id: int):
    """Message d'essai (sans donnée sur une personne) : réel si Telegram est configuré, sinon simulé et journalisé."""
    db = SessionLocal()
    try:
        row = db.get(Authority, authority_id)
        if row is None:
            raise HTTPException(404, {"message": "Autorité introuvable.", "field": None})
        view = dispatch.authority_view(row)
        text = "[NiifYéén] Message d'essai : aucune cible, aucune donnée personnelle."
        channel, status = dispatch.send_to_authority(view, text)
        return dispatch.log_dispatch(db, view, channel, status, text, None, "essai")
    finally:
        db.close()


@app.get("/api/dispatches")
def api_dispatches(limit: int = Query(50, ge=1, le=200)):
    db = SessionLocal()
    try:
        rows = db.query(Dispatch).order_by(Dispatch.id.desc()).limit(limit).all()
        return {"dispatches": [dispatch.dispatch_view(d) for d in rows]}
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
                "default_plan": "/static/blank-plan.svg", "default_size": list(floors.SVG_DEFAULT)}
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
