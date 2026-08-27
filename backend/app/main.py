import asyncio
import time
from pathlib import Path
from typing import List

import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import init_db, SessionLocal, Participant, EmbeddingRef
from .vision import read_image_bytes, detect_faces_safe, cosine_similarity
from .person_pipeline import PersonPipeline

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NiifYéén — YOLOv8 + Tracking (multi-cam ready)")
init_db()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

CAMERAS = [
    {"id": "cam0", "source": 0},
    # Ajoute plus tard:
    # {"id": "cam1", "source": 1},
]


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


@app.on_event("startup")
def startup():
    cams = {}
    known = load_known_from_db()

    for cfg in CAMERAS:
        cam_id = cfg["id"]
        cam = PersonPipeline(
            source=cfg["source"],
            camera_id=cam_id,
            out_size=(640, 360),
            tracker_yaml="bytetrack.yaml",
            imgsz=640,
            yolo_conf=0.35
        )
        cam.set_known(known)
        cam.start()
        cams[cam_id] = cam

    app.state.cams = cams
    print(f"[MAIN] Caméras démarrées: {list(cams.keys())}")


@app.on_event("shutdown")
def shutdown():
    for cam in app.state.cams.values():
        cam.stop()
    print("[MAIN] Caméras arrêtées.")


# ---------- PAGES ----------
@app.get("/", response_class=HTMLResponse)
def page_home():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/cameras", response_class=HTMLResponse)
def page_cameras():
    return (BASE_DIR / "static" / "cameras.html").read_text(encoding="utf-8")


@app.get("/api/cameras")
def api_cameras():
    return {"cameras": list(app.state.cams.keys())}


# ---------- API ----------
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
    """
    Accepte plusieurs photos. Pour chaque photo:
    - détecte le plus grand visage
    - récupère l'embedding
    Puis:
    - fusionne les embeddings (moyenne) + normalise
    - définit la cible sur toutes les caméras
    """
    embeddings = []
    per_photo = []

    for up in photos[:10]:  # limite de sécurité (optionnel)
        img = read_image_bytes(await up.read())
        if img is None:
            per_photo.append({"file": up.filename, "ok": False, "reason": "invalid_image"})
            continue

        faces = detect_faces_safe(img)
        if not faces:
            per_photo.append({"file": up.filename, "ok": False, "reason": "no_face"})
            continue

        # visage le plus grand
        faces = sorted(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
            reverse=True
        )
        emb = faces[0].normed_embedding.astype(np.float32)
        embeddings.append(emb)
        per_photo.append({"file": up.filename, "ok": True})

    if not embeddings:
        # aucune photo exploitable => on enlève la cible
        for cam in app.state.cams.values():
            cam.clear_target()
        return {"target_set": False, "reason": "no_valid_face_in_any_photo", "details": per_photo}

    # Fusion: moyenne + renormalisation (important)
    mean_emb = np.mean(np.stack(embeddings, axis=0), axis=0)
    mean_emb = mean_emb / (np.linalg.norm(mean_emb) + 1e-9)

    # Appliquer la cible sur toutes les caméras
    for cam in app.state.cams.values():
        cam.set_target(mean_emb, label="CIBLE", threshold=0.40)

    # (Optionnel) info DB: best match sur l'embedding fusionné
    known = load_known_from_db()
    best, best_sim = None, -1.0
    for k in known:
        sim = cosine_similarity(mean_emb, k["embedding"])
        if sim > best_sim:
            best_sim = sim
            best = k

    return {
        "target_set": True,
        "nb_photos_recues": len(photos),
        "nb_photos_utilisees": len(embeddings),
        "target_threshold": 0.40,
        "db_best_match": None if best is None else {
            "participant_id": best["participant_id"],
            "nom": best["nom"],
            "score": best_sim
        },
        "details": per_photo
    }

@app.post("/target/clear")
def clear_target():
    for cam in app.state.cams.values():
        cam.clear_target()
    return {"cleared": True}


def mjpeg_gen(cam: PersonPipeline):
    while True:
        jpg = cam.latest_jpeg
        if jpg is None:
            time.sleep(0.02)
            continue
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")


@app.get("/video/{cam_id}")
def video_cam(cam_id: str):
    cam = app.state.cams.get(cam_id)
    if not cam:
        return JSONResponse({"error": "cam_id inconnu"}, status_code=404)
    return StreamingResponse(mjpeg_gen(cam), media_type="multipart/x-mixed-replace; boundary=frame")


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            events = []
            for cam in app.state.cams.values():
                events.extend(cam.latest_events or [])
            await ws.send_json({"events": events, "cameras": list(app.state.cams.keys())})
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass