import numpy as np
import cv2
import threading
from insightface.app import FaceAnalysis

_app = None
_lock = threading.Lock()

def get_face_app():
    global _app
    if _app is None:
        print("[VISION] Chargement InsightFace ...")
        _app = FaceAnalysis(name="buffalo_l", root="./models_insightface")
        _app.prepare(ctx_id=-1, det_size=(320, 320))  # CPU
        print("[VISION] InsightFace prêt.")
    return _app

def detect_faces_safe(frame):
    """Thread-safe wrapper autour de InsightFace (important en multi-cam/threads)."""
    app = get_face_app()
    with _lock:
        return app.get(frame)

def read_image_bytes(file_bytes: bytes):
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
    return float(np.dot(a, b) / denom)