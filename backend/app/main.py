import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import (FastAPI, UploadFile, File, Form, HTTPException, WebSocket,
                     WebSocketDisconnect, Query)
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .activity_log import read_activity
from .db import (init_db, SessionLocal, Participant, EmbeddingRef, Detection,
                 utcnow, to_iso_utc, delete_participant)
from .db_logger import DBLogger
from .person_pipeline import PersonPipeline
from .reid import GlobalReId
from .target_path import TargetTracker, TargetWorker
from .video_stream import mjpeg_frames
from .vision import read_image_bytes, detect_faces_safe

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


# ---------- Embeddings ----------
def embedding_to_bytes(emb: np.ndarray) -> bytes:
    return emb.astype(np.float32).tobytes()


def bytes_to_embedding(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def load_known_from_db():
    db = SessionLocal()
    try:
        rows = (
            db.query(EmbeddingRef, Participant)
            .join(Participant, Participant.id == EmbeddingRef.participant_id)
            .all()
        )
        return [{"participant_id": p.id, "nom": p.nom,
                 "embedding": bytes_to_embedding(e.embedding)} for e, p in rows]
    finally:
        db.close()


def refresh_known():
    known = load_known_from_db()
    for cam in app.state.cams.values():
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
    """Retourne [(embedding, nom_de_fichier)] pour les photos contenant un visage."""
    results = []
    for up in photos:
        img = read_image_bytes(await _read_upload(up))
        if img is None:
            continue
        emb = await asyncio.to_thread(_face_embedding, img)
        if emb is not None:
            results.append((emb, up.filename))
    return results


# ---------- Cycle de vie ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cameras = config.load_cameras()
    app.state.camera_meta = config.camera_meta(cameras)
    app.state.reid = GlobalReId(sim_threshold=config.REID_THRESHOLD, ttl_sec=20.0)
    app.state.cams = {}
    app.state.tracker = TargetTracker()
    known = load_known_from_db()

    for cfg in cameras:
        cam = PersonPipeline(
            source=cfg["source"], camera_id=cfg["id"], reid=app.state.reid,
            capture_size=(1280, 720), out_size=(640, 360),
            tracker_yaml="bytetrack.yaml", imgsz=480, yolo_conf=0.35,
            head_ratio=0.50, recog_every_sec=0.60, lock_hold_sec=3.0, jpeg_quality=70,
        )
        cam.set_known(known)
        cam.start()
        app.state.cams[cfg["id"]] = cam

    app.state.target_worker = TargetWorker(app.state.tracker, lambda: app.state.cams)
    app.state.target_worker.start()
    app.state.logger = DBLogger(lambda: app.state.cams)
    app.state.logger.start()
    print(f"[MAIN] Caméras démarrées: {list(app.state.cams.keys())}")
    try:
        yield
    finally:
        app.state.logger.stop()
        app.state.target_worker.stop()
        for cam in app.state.cams.values():
            cam.stop()


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
                "label": r.label,
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
@app.get("/participants")
def list_participants():
    db = SessionLocal()
    try:
        rows = db.query(Participant).order_by(Participant.id).all()
        return [{"id": p.id, "nom": p.nom,
                 "date_consentement": to_iso_utc(p.date_consentement) if p.date_consentement else None}
                for p in rows]
    finally:
        db.close()


@app.post("/participants/enroll")
async def enroll(
    nom: str = Form(..., max_length=config.MAX_NAME_LENGTH),
    infos: str = Form("", max_length=config.MAX_INFOS_LENGTH),
    consentement: bool = Form(False),
    photos: List[UploadFile] = File(...),
):
    nom = nom.strip()
    if not nom:
        raise HTTPException(422, "Le nom est obligatoire.")
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
        p = Participant(nom=nom, infos=infos.strip(), date_consentement=utcnow())
        db.add(p)
        db.flush()
        for emb, filename in found:
            db.add(EmbeddingRef(participant_id=p.id, embedding=embedding_to_bytes(emb), source=filename))
        db.commit()
        participant_id = p.id
    finally:
        db.close()

    refresh_known()
    return {"participant_id": participant_id, "saved_embeddings": len(found),
            "ignored_photos": len(photos) - len(found)}


@app.delete("/participants/{participant_id}")
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


# ---------- Cible ----------
@app.post("/search/by-image")
async def search_by_image(photos: List[UploadFile] = File(...)):
    found = await _embeddings_from_uploads(photos[:config.MAX_SEARCH_PHOTOS])

    if not found:
        for cam in app.state.cams.values():
            cam.clear_target()
        app.state.tracker.reset()
        return {"target_set": False, "reason": "no_valid_face"}

    mean_emb = np.mean(np.stack([e for e, _ in found], axis=0), axis=0)
    mean_emb = mean_emb / (np.linalg.norm(mean_emb) + 1e-9)

    for cam in app.state.cams.values():
        cam.set_target(mean_emb, label="CIBLE", threshold=config.TARGET_THRESHOLD)
    app.state.tracker.reset()

    return {"target_set": True, "target_threshold": config.TARGET_THRESHOLD,
            "nb_photos_utilisees": len(found)}


@app.post("/target/clear")
def clear_target():
    for cam in app.state.cams.values():
        cam.clear_target()
    app.state.tracker.reset()
    return {"cleared": True}


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
                **app.state.tracker.snapshot(),
            })
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, RuntimeError):
        pass
