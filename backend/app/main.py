import asyncio
import time
import threading
import logging
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime, timedelta

import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import init_db, SessionLocal, Participant, EmbeddingRef, Detection
from .vision import read_image_bytes, detect_faces_safe
from .person_pipeline import PersonPipeline
from .video_stream import mjpeg_frames
from .activity_log import read_activity
from .reid import GlobalReId  # optionnel si tu l'utilises

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NiifYéén — Cheminement + Historique + DB names")
init_db()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---- CONFIG CAMERAS ----
CAMERAS = [
    {"id": "entre", "source": 0},
    {"id": "salle", "source": "http://172.20.10.2:8081/video"},
    {"id": "sortie", "source": "http://172.20.10.8:8081/video"},
]



# Chaque caméra = un point sur la carte (0..1)
CAMERA_META = {
    "cam0": {"x": 0.2, "y": 0.80, "label": "Entrée"},
    "cam1": {"x": 0.50, "y": 0.50, "label": "Couloir"},
    "cam2": {"x": 0.8, "y": 0.20, "label": "Salle"},
}

CAM_SWITCH_CONFIRM_SEC = 0.8
TARGET_TIMEOUT_SEC = 3.0
TARGET_PATH_MAX = 200

# Logging DB : éviter trop d'inserts
LOG_EVERY_SEC = 0.8  # par (cam_id, track_id, mode)


def embedding_to_bytes(emb: np.ndarray) -> bytes:
    return emb.astype(np.float32).tobytes()

def bytes_to_embedding(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)

def load_known_from_db():
    db = SessionLocal()
    try:
        known = []
        q = (
            db.query(EmbeddingRef, Participant)
            .join(Participant, Participant.id == EmbeddingRef.participant_id)
            .all()
        )
        for emb_ref, p in q:
            known.append({
                "participant_id": p.id,
                "nom": p.nom,
                "embedding": bytes_to_embedding(emb_ref.embedding)
            })
        return known
    finally:
        db.close()

def refresh_known():
    known = load_known_from_db()
    for cam in app.state.cams.values():
        cam.set_known(known)


class DBLogger:
    def __init__(self):
        self.running = False
        self.thread = None
        self.last = {}  # (cam,track,mode) -> ts

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def loop(self):
        while self.running:
            try:
                now = time.time()
                cams = app.state.cams
                db = SessionLocal()
                try:
                    for cam in cams.values():
                        for ev in (cam.latest_events or []):
                            cam_id = ev.get("camera_id")
                            track_id = ev.get("track_id")
                            mode = ev.get("mode") or "UNK"

                            key = (cam_id, track_id, mode)
                            last_ts = self.last.get(key, 0.0)
                            if now - last_ts < LOG_EVERY_SEC:
                                continue
                            self.last[key] = now

                            det = Detection(
                                camera_id=str(cam_id),
                                track_id=int(track_id) if track_id is not None else None,
                                global_id=int(ev["global_id"]) if ev.get("global_id") is not None else None,
                                mode=str(mode),
                                label=ev.get("label"),
                                target_score=float(ev["target_score"]) if ev.get("target_score") is not None else None,
                                db_score=float(ev["db_score"]) if ev.get("db_score") is not None else None,
                                x=float(ev["x"]) if ev.get("x") is not None else None,
                                y=float(ev["y"]) if ev.get("y") is not None else None,
                                timestamp=datetime.utcnow(),
                            )
                            db.add(det)
                    db.commit()
                finally:
                    db.close()
            except Exception:
                logging.getLogger(__name__).exception("Impossible d'enregistrer les observations")

            time.sleep(0.25)


@app.on_event("startup")
def startup():
    # (Optionnel) ReID global
    app.state.reid = GlobalReId(sim_threshold=0.45, ttl_sec=20.0)

    cams = {}
    known = load_known_from_db()

    for cfg in CAMERAS:
        cam_id = cfg["id"]
        cam = PersonPipeline(
            source=cfg["source"],
            camera_id=cam_id,
            reid=app.state.reid,
            capture_size=(1280, 720),
            out_size=(640, 360),
            tracker_yaml="bytetrack.yaml",
            imgsz=480,
            yolo_conf=0.35,
            head_ratio=0.50,
            recog_every_sec=0.60,
            lock_hold_sec=3.0,
            jpeg_quality=70
        )
        cam.set_known(known)
        cam.start()
        cams[cam_id] = cam

    app.state.cams = cams
    app.state.camera_meta = CAMERA_META

    app.state.target_status = {
        "active": False,
        "camera_id": None,
        "track_id": None,
        "global_id": None,
        "score": None,
        "last_seen_ts": 0.0,
    }

    app.state.target_path = []
    app.state._pending_cam = None
    app.state._pending_since = 0.0
    app.state._was_active = False

    app.state.logger = DBLogger()
    app.state.logger.start()

    print(f"[MAIN] Caméras démarrées: {list(cams.keys())}")


@app.on_event("shutdown")
def shutdown():
    try:
        app.state.logger.stop()
    except Exception:
        pass
    for cam in app.state.cams.values():
        cam.stop()


# ---------- Pages ----------
@app.get("/", response_class=HTMLResponse)
def page_home():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/cameras", response_class=HTMLResponse)
def page_cameras():
    return (BASE_DIR / "static" / "cameras.html").read_text(encoding="utf-8")


# ---------- API ----------
@app.get("/logs", response_class=HTMLResponse)
def page_logs():
    return (BASE_DIR / "static" / "logs.html").read_text(encoding="utf-8")


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
        since = datetime.utcnow() - timedelta(minutes=minutes)
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
                "timestamp": r.timestamp.isoformat(),
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


@app.post("/participants/enroll")
async def enroll(
    nom: str = Form(...),
    infos: str = Form(""),
    photos: List[UploadFile] = File(...)
):
    db = SessionLocal()
    try:
        p = Participant(nom=nom, infos=infos)
        db.add(p)
        db.commit()
        db.refresh(p)

        saved = 0
        for up in photos:
            img = read_image_bytes(await up.read())
            if img is None:
                continue
            faces = detect_faces_safe(img)
            if not faces:
                continue
            faces = sorted(
                faces,
                key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
                reverse=True
            )
            emb = faces[0].normed_embedding
            db.add(EmbeddingRef(participant_id=p.id, embedding=embedding_to_bytes(emb), source=up.filename))
            saved += 1

        db.commit()
        refresh_known()
        return {"participant_id": p.id, "saved_embeddings": saved}
    finally:
        db.close()


@app.post("/search/by-image")
async def search_by_image(photos: List[UploadFile] = File(...)):
    embeddings = []
    for up in photos[:10]:
        img = read_image_bytes(await up.read())
        if img is None:
            continue
        faces = detect_faces_safe(img)
        if not faces:
            continue
        faces = sorted(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
            reverse=True
        )
        embeddings.append(faces[0].normed_embedding.astype(np.float32))

    if not embeddings:
        for cam in app.state.cams.values():
            cam.clear_target()
        app.state.target_path = []
        app.state.target_status["active"] = False
        return {"target_set": False, "reason": "no_valid_face"}

    mean_emb = np.mean(np.stack(embeddings, axis=0), axis=0)
    mean_emb = mean_emb / (np.linalg.norm(mean_emb) + 1e-9)

    for cam in app.state.cams.values():
        cam.set_target(mean_emb, label="CIBLE", threshold=0.40)

    # reset path
    app.state.target_path = []
    app.state.target_status["active"] = False
    app.state._pending_cam = None
    app.state._pending_since = 0.0
    app.state._was_active = False

    return {"target_set": True, "target_threshold": 0.40, "nb_photos_utilisees": len(embeddings)}


@app.post("/target/clear")
def clear_target():
    for cam in app.state.cams.values():
        cam.clear_target()
    app.state.target_status["active"] = False
    app.state.target_path = []
    app.state._pending_cam = None
    app.state._pending_since = 0.0
    app.state._was_active = False
    return {"cleared": True}


def mjpeg_gen(cam: PersonPipeline):
    return mjpeg_frames(cam)


@app.get("/video/{cam_id}")
def video_cam(cam_id: str):
    cam = app.state.cams.get(cam_id)
    if not cam:
        return JSONResponse({"error": "cam_id inconnu"}, status_code=404)
    return StreamingResponse(
        mjpeg_gen(cam), media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def update_target_and_path(best: Dict[str, Any] | None, now: float):
    status = app.state.target_status

    if best is None:
        ts = status.get("last_seen_ts", 0.0)
        if ts and (now - ts) > TARGET_TIMEOUT_SEC:
            status["active"] = False
            status["camera_id"] = None
            status["track_id"] = None
            status["global_id"] = None
            status["score"] = None
        return

    status.update({
        "active": True,
        "camera_id": best["camera_id"],
        "track_id": best.get("track_id"),
        "global_id": best.get("global_id"),
        "score": best.get("score"),
        "last_seen_ts": now
    })

    current_cam = best["camera_id"]

    if not app.state._was_active:
        app.state.target_path.append({"camera_id": current_cam, "ts": now, "score": best.get("score")})
        app.state.target_path = app.state.target_path[-TARGET_PATH_MAX:]
        app.state._was_active = True
        app.state._pending_cam = None
        app.state._pending_since = 0.0
        return

    last_cam = app.state.target_path[-1]["camera_id"] if app.state.target_path else None
    pending_cam = app.state._pending_cam

    if pending_cam is None:
        if current_cam != last_cam:
            app.state._pending_cam = current_cam
            app.state._pending_since = now
        return

    if current_cam != pending_cam:
        app.state._pending_cam = current_cam
        app.state._pending_since = now
        return

    if (now - app.state._pending_since) >= CAM_SWITCH_CONFIRM_SEC:
        app.state.target_path.append({"camera_id": pending_cam, "ts": now, "score": best.get("score")})
        app.state.target_path = app.state.target_path[-TARGET_PATH_MAX:]
        app.state._pending_cam = None
        app.state._pending_since = 0.0


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            now = time.time()

            events = []
            best = None
            for cam in app.state.cams.values():
                for ev in (cam.latest_events or []):
                    events.append(ev)
                    if ev.get("mode") == "TARGET":
                        score = ev.get("target_score")
                        score = float(score) if score is not None else 0.0
                        if best is None or score > best["score"]:
                            best = {
                                "camera_id": ev.get("camera_id"),
                                "track_id": ev.get("track_id"),
                                "global_id": ev.get("global_id"),
                                "score": score
                            }

            update_target_and_path(best, now)

            await ws.send_json({
                "events": events,
                "cameras": list(app.state.cams.keys()),
                "camera_meta": app.state.camera_meta,
                "target": app.state.target_status,
                "target_path": app.state.target_path
            })
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
