import cv2
import threading
import time
import numpy as np
from typing import List, Dict, Any, Optional, Union, Tuple

from .vision import detect_faces_safe, cosine_similarity
from .tracker import SimpleTracker


class CameraPipeline:
    def __init__(
        self,
        source: Union[int, str],
        camera_id: str,
        out_size: Tuple[int, int] = (640, 360),
        jpeg_quality: int = 60,
        process_interval_sec: float = 0.12,
    ):
        self.source = source
        self.camera_id = camera_id
        self.out_w, self.out_h = out_size
        self.jpeg_quality = jpeg_quality
        self.process_interval_sec = process_interval_sec

        self.cap: Optional[cv2.VideoCapture] = None
        self.tracker = SimpleTracker()

        self.running = False
        self.thread: Optional[threading.Thread] = None

        self.latest_jpeg: Optional[bytes] = None
        self.latest_events: List[Dict[str, Any]] = []
        self.last_frame_ts: float = 0.0

        # Base “connue” (enrôlée) optionnelle
        self.known = []
        self.sim_threshold = 0.35

        # --- CIBLE (target) : permet de retrouver une personne même hors DB ---
        self._lock = threading.Lock()
        self._target_embedding: Optional[np.ndarray] = None
        self._target_label: str = "CIBLE"
        self._target_threshold: float = 0.40  # à calibrer

    def set_known(self, known_list):
        self.known = known_list

    def set_target(self, embedding: np.ndarray, label: str = "CIBLE", threshold: float = 0.40):
        with self._lock:
            self._target_embedding = embedding.astype(np.float32)
            self._target_label = label
            self._target_threshold = float(threshold)

    def clear_target(self):
        with self._lock:
            self._target_embedding = None

    def _get_target(self):
        with self._lock:
            return self._target_embedding, self._target_label, self._target_threshold

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

    def _make_placeholder(self, text: str):
        img = np.zeros((self.out_h, self.out_w, 3), dtype=np.uint8)
        img[:] = (245, 245, 245)
        cv2.putText(img, text, (18, self.out_h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 80, 80), 2)
        ok, jpg = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            self.latest_jpeg = jpg.tobytes()

    def _try_open(self) -> bool:
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        for be in backends:
            cap = cv2.VideoCapture(self.source, be) if isinstance(self.source, int) else cv2.VideoCapture(self.source)
            if not cap.isOpened():
                cap.release()
                continue

            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass

            ok, frame = cap.read()
            if ok and frame is not None:
                self.cap = cap
                print(f"[{self.camera_id}] OK backend={be} frame={frame.shape[1]}x{frame.shape[0]}")
                return True

            cap.release()

        print(f"[{self.camera_id}] ÉCHEC ouverture caméra")
        return False

    def _best_match_db(self, emb: np.ndarray):
        best = None
        best_sim = -1.0
        for k in self.known:
            sim = cosine_similarity(emb, k["embedding"])
            if sim > best_sim:
                best_sim = sim
                best = k
        if best is None or best_sim < self.sim_threshold:
            return None, best_sim
        return best, best_sim

    def _loop(self):
        self._make_placeholder(f"{self.camera_id}: ouverture...")
        while self.running and not self._try_open():
            self._make_placeholder(f"{self.camera_id}: PAS DE SIGNAL")
            time.sleep(2.0)
        if not self.running:
            return

        last_proc = 0.0
        last_tracks = []

        while self.running:
            ok, frame = self.cap.read() if self.cap else (False, None)
            if not ok or frame is None:
                self._make_placeholder(f"{self.camera_id}: frame introuvable")
                time.sleep(0.1)
                continue

            now = time.time()
            self.last_frame_ts = now

            frame_small = cv2.resize(frame, (self.out_w, self.out_h), interpolation=cv2.INTER_AREA)
            frame_draw = frame_small.copy()

            do_process = (now - last_proc) >= self.process_interval_sec

            if do_process:
                last_proc = now

                faces = detect_faces_safe(frame_small)  # détecte + embeddings
                dets = []
                face_by_bbox = {}

                for f in faces:
                    x1, y1, x2, y2 = map(float, f.bbox)
                    det = (x1, y1, x2, y2)
                    dets.append(det)
                    face_by_bbox[det] = f

                tracks = self.tracker.update(dets)

                h, w = frame_small.shape[:2]
                events = []

                target_emb, target_label, target_thr = self._get_target()

                for tr in tracks:
                    x1, y1, x2, y2 = tr.bbox
                    f = face_by_bbox.get((x1, y1, x2, y2), None)

                    name = None
                    score_db = None
                    is_target = False
                    target_score = None

                    if f is not None and hasattr(f, "normed_embedding"):
                        emb = f.normed_embedding

                        # 1) Match DB (si personne enrôlée)
                        best, sim_db = self._best_match_db(emb)
                        score_db = sim_db
                        if best is not None:
                            name = best["nom"]

                        # 2) Match CIBLE (même si pas dans DB)
                        if target_emb is not None:
                            target_score = cosine_similarity(emb, target_emb)
                            if target_score >= target_thr:
                                is_target = True

                    # Label final
                    if is_target:
                        label = f"{target_label} {target_score:.2f}"
                        color = (0, 0, 255)  # rouge cible
                    elif name:
                        label = f"{name} {score_db:.2f}"
                        color = (0, 180, 0)  # vert identifié
                    else:
                        label = f"unknown({tr.track_id})"
                        color = (120, 120, 120)

                    cv2.rectangle(frame_draw, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                    cv2.putText(frame_draw, f"{self.camera_id} | {label}",
                                (int(x1), max(18, int(y1) - 8)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

                    cx = float((x1 + x2) / 2.0) / float(w)
                    cy = float((y1 + y2) / 2.0) / float(h)

                    events.append({
                        "camera_id": self.camera_id,
                        "track_id": tr.track_id,
                        "name": name,
                        "score_db": score_db,
                        "is_target": is_target,
                        "target_label": target_label if target_emb is not None else None,
                        "target_score": target_score,
                        "x": cx, "y": cy,
                        "bbox": [x1, y1, x2, y2],
                        "ts": now
                    })

                self.latest_events = events
                last_tracks = tracks
            else:
                # draw old boxes only
                for tr in last_tracks:
                    x1, y1, x2, y2 = tr.bbox
                    cv2.rectangle(frame_draw, (int(x1), int(y1)), (int(x2), int(y2)), (120, 120, 120), 1)

            ok2, jpg = cv2.imencode(".jpg", frame_draw, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if ok2:
                self.latest_jpeg = jpg.tobytes()

            time.sleep(0.005)