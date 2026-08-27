import time
import threading
from typing import Optional, Dict, Any, List, Tuple, Union

import cv2
import numpy as np
from ultralytics import YOLO

from .vision import detect_faces_safe, cosine_similarity


def face_center_in_person(face_xyxy, person_xyxy):
    fx1, fy1, fx2, fy2 = face_xyxy
    px1, py1, px2, py2 = person_xyxy
    cx = (fx1 + fx2) / 2.0
    cy = (fy1 + fy2) / 2.0
    return (px1 <= cx <= px2) and (py1 <= cy <= py2)


class PersonPipeline:
    """
    - Détection+tracking PERSONNE (YOLOv8 + ByteTrack/BoT-SORT)
    - Visage (InsightFace) seulement pour accrocher:
        * TARGET (cible) via similarité embedding
        * DB (enrôlés) via similarité embedding
    - Lock: si visage caché (casquette, main), on garde l’étiquette quelques secondes.
    """
    def __init__(
        self,
        source: Union[int, str],
        camera_id: str,
        out_size: Tuple[int, int] = (640, 360),
        jpeg_quality: int = 60,
        imgsz: int = 640,
        yolo_conf: float = 0.35,
        tracker_yaml: str = "bytetrack.yaml",  # plus léger que botsort.yaml
        lock_hold_sec: float = 3.0,
    ):
        self.source = source
        self.camera_id = camera_id
        self.out_w, self.out_h = out_size
        self.jpeg_quality = jpeg_quality

        self.imgsz = imgsz
        self.yolo_conf = yolo_conf
        self.tracker_yaml = tracker_yaml

        self.lock_hold_sec = lock_hold_sec

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.cap: Optional[cv2.VideoCapture] = None

        self.latest_jpeg: Optional[bytes] = None
        self.latest_events: List[Dict[str, Any]] = []
        self.last_frame_ts: float = 0.0

        # Charge YOLO (téléchargement au premier run)
        self.yolo = YOLO("yolov8n.pt")

        # DB enrôlée
        self.known = []
        self.db_threshold = 0.35

        # Cible
        self._lock = threading.Lock()
        self._target_emb: Optional[np.ndarray] = None
        self._target_label: str = "CIBLE"
        self._target_threshold: float = 0.40

        # état par track_id (lock)
        self._track_state: Dict[int, Dict[str, Any]] = {}
        self._state_ttl_sec = 6.0

    def set_known(self, known_list):
        self.known = known_list

    def set_target(self, emb: np.ndarray, label: str = "CIBLE", threshold: float = 0.40):
        with self._lock:
            self._target_emb = emb.astype(np.float32)
            self._target_label = label
            self._target_threshold = float(threshold)

    def clear_target(self):
        with self._lock:
            self._target_emb = None

    def _get_target(self):
        with self._lock:
            return self._target_emb, self._target_label, self._target_threshold

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

    def _placeholder(self, text: str):
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
            ok, frame = cap.read()
            if ok and frame is not None:
                self.cap = cap
                print(f"[{self.camera_id}] OK backend={be} frame={frame.shape[1]}x{frame.shape[0]}")
                return True
            cap.release()
        print(f"[{self.camera_id}] ÉCHEC ouverture caméra source={self.source}")
        return False

    def _purge_state(self, now: float):
        for tid in list(self._track_state.keys()):
            if now - self._track_state[tid].get("last_seen", 0.0) > self._state_ttl_sec:
                del self._track_state[tid]

    def _best_db_match(self, face_emb: np.ndarray):
        best = None
        best_sim = -1.0
        for k in self.known:
            sim = cosine_similarity(face_emb, k["embedding"])
            if sim > best_sim:
                best_sim = sim
                best = k
        if best is None or best_sim < self.db_threshold:
            return None, best_sim
        return best, best_sim

    def _loop(self):
        self._placeholder(f"{self.camera_id}: ouverture...")
        while self.running and not self._try_open():
            self._placeholder(f"{self.camera_id}: PAS DE SIGNAL")
            time.sleep(2.0)
        if not self.running:
            return

        while self.running:
            ok, frame = self.cap.read() if self.cap else (False, None)
            if not ok or frame is None:
                self._placeholder(f"{self.camera_id}: frame introuvable")
                time.sleep(0.1)
                continue

            now = time.time()
            self.last_frame_ts = now
            self._purge_state(now)

            frame_small = cv2.resize(frame, (self.out_w, self.out_h), interpolation=cv2.INTER_AREA)
            frame_draw = frame_small.copy()

            # 1) Visages (1 passe)
            faces = detect_faces_safe(frame_small)
            face_items = []
            for f in faces:
                fb = tuple(map(float, f.bbox))
                emb = getattr(f, "normed_embedding", None)
                if emb is not None:
                    face_items.append((fb, emb))

            # 2) Personnes + tracking (YOLO.track)
            results = self.yolo.track(
                frame_small,
                persist=True,
                verbose=False,
                classes=[0],              # person
                conf=self.yolo_conf,
                imgsz=self.imgsz,
                tracker=self.tracker_yaml
            )
            r = results[0]
            boxes = r.boxes

            events: List[Dict[str, Any]] = []

            if boxes is None or boxes.xyxy is None or len(boxes.xyxy) == 0 or boxes.id is None:
                self.latest_events = []
                ok2, jpg = cv2.imencode(".jpg", frame_draw, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
                if ok2:
                    self.latest_jpeg = jpg.tobytes()
                time.sleep(0.005)
                continue

            xyxy = boxes.xyxy.cpu().numpy()
            tids = boxes.id.cpu().numpy().astype(int)

            target_emb, target_label, target_thr = self._get_target()
            h, w = frame_small.shape[:2]

            for (pb, tid) in zip(xyxy, tids):
                x1, y1, x2, y2 = map(float, pb)
                person_bbox = (x1, y1, x2, y2)

                st = self._track_state.setdefault(tid, {})
                st["last_seen"] = now

                mode = "UNK"
                label = None
                target_score = None
                db_score = None

                # associer visage à la personne
                face_emb = None
                for (fb, emb) in face_items:
                    if face_center_in_person(fb, person_bbox):
                        face_emb = emb
                        break

                if face_emb is not None:
                    # TARGET prioritaire
                    if target_emb is not None:
                        target_score = cosine_similarity(face_emb, target_emb)
                        if target_score >= target_thr:
                            mode = "TARGET"
                            label = target_label
                            st["locked_mode"] = mode
                            st["locked_label"] = label
                            st["locked_until"] = now + self.lock_hold_sec

                    # DB si pas target
                    best, sim_db = self._best_db_match(face_emb)
                    db_score = sim_db
                    if mode != "TARGET" and best is not None:
                        mode = "DB"
                        label = best["nom"]
                        st["locked_mode"] = mode
                        st["locked_label"] = label
                        st["locked_until"] = now + self.lock_hold_sec

                # lock si visage caché
                if face_emb is None and st.get("locked_until", 0) > now:
                    mode = st.get("locked_mode", "UNK")
                    label = st.get("locked_label", None)

                # dessin
                if mode == "TARGET":
                    color = (0, 0, 255)
                    text = f"{self.camera_id} | {label} {'' if target_score is None else f'{target_score:.2f}'}"
                elif mode == "DB":
                    color = (0, 180, 0)
                    text = f"{self.camera_id} | {label} {'' if db_score is None else f'{db_score:.2f}'}"
                else:
                    color = (120, 120, 120)
                    text = f"{self.camera_id} | track {tid}"

                cv2.rectangle(frame_draw, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
                cv2.putText(frame_draw, text, (int(x1), max(18, int(y1) - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

                cx = float((x1 + x2) / 2.0) / float(w)
                cy = float((y1 + y2) / 2.0) / float(h)

                events.append({
                    "camera_id": self.camera_id,
                    "track_id": int(tid),
                    "mode": mode,
                    "label": label,
                    "target_score": None if target_score is None else float(target_score),
                    "db_score": None if db_score is None else float(db_score),
                    "x": cx, "y": cy,
                    "bbox": [x1, y1, x2, y2],
                    "ts": now
                })

            self.latest_events = events

            ok2, jpg = cv2.imencode(".jpg", frame_draw, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if ok2:
                self.latest_jpeg = jpg.tobytes()

            time.sleep(0.005)