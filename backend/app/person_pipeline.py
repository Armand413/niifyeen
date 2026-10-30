import time
import threading
from typing import Optional, Dict, Any, List, Tuple, Union

import cv2
import numpy as np
from ultralytics import YOLO

from . import config
from .vision import detect_faces_safe, cosine_similarity
from .reid import GlobalReId


class PersonPipeline:
    def __init__(
        self,
        source: Union[int, str],
        camera_id: str,
        reid: Optional[GlobalReId] = None,

        capture_size: Tuple[int, int] = (1280, 720),
        out_size: Tuple[int, int] = (640, 360),

        jpeg_quality: int = 70,
        imgsz: int = 640,
        yolo_conf: float = 0.35,
        tracker_yaml: str = "bytetrack.yaml",

        head_ratio: float = 0.50,
        recog_every_sec: float = 0.35,
        lock_hold_sec: float = 3.0,
        reconnect_after_sec: float = 3.0,
    ):
        self.source = source
        self.camera_id = camera_id
        self.reid = reid

        self.cap_w, self.cap_h = capture_size
        self.out_w, self.out_h = out_size

        self.jpeg_quality = int(jpeg_quality)
        self.imgsz = int(imgsz)
        self.yolo_conf = float(yolo_conf)
        self.tracker_yaml = tracker_yaml

        self.head_ratio = float(head_ratio)
        self.recog_every_sec = float(recog_every_sec)
        self.lock_hold_sec = float(lock_hold_sec)
        self.reconnect_after_sec = float(reconnect_after_sec)
        self._stop_evt = threading.Event()

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.cap: Optional[cv2.VideoCapture] = None

        self.latest_jpeg: Optional[bytes] = None
        self.latest_events: List[Dict[str, Any]] = []
        self.last_frame_ts: float = 0.0

        self.yolo = YOLO(config.YOLO_WEIGHTS)

        # DB enrôlée
        self.known = []
        self.db_threshold = config.DB_THRESHOLD

        # CIBLE
        self._tlock = threading.Lock()
        self._target_emb: Optional[np.ndarray] = None
        self._target_label: str = "CIBLE"
        self._target_threshold: float = config.TARGET_THRESHOLD

        # état par track
        self._track_state: Dict[int, Dict[str, Any]] = {}
        self._state_ttl_sec = 8.0

    def set_known(self, known_list):
        self.known = known_list

    def set_target(self, emb: np.ndarray, label: str = "CIBLE", threshold: Optional[float] = None):
        with self._tlock:
            self._target_emb = emb.astype(np.float32)
            self._target_label = label
            self._target_threshold = float(config.TARGET_THRESHOLD if threshold is None else threshold)

    def clear_target(self):
        with self._tlock:
            self._target_emb = None

    def _get_target(self):
        with self._tlock:
            return self._target_emb, self._target_label, self._target_threshold

    def start(self):
        if self.running:
            return
        self._stop_evt.clear()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Arrête le thread *puis* libère la capture (jamais l'inverse : le thread lit dessus)."""
        self.running = False
        self._stop_evt.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=5.0)
        self._release_cap()
        self.latest_events = []

    def _release_cap(self):
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _placeholder(self, text: str):
        img = np.zeros((self.out_h, self.out_w, 3), dtype=np.uint8)
        img[:] = (245, 245, 245)
        cv2.putText(img, text, (18, self.out_h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 80, 80), 2)
        ok, jpg = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if ok:
            self.latest_jpeg = jpg.tobytes()

    def _try_open(self) -> bool:
        # important: URL MJPEG/RTSP => pas de set(width/height)
        backends = [cv2.CAP_FFMPEG, cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]

        for be in backends:
            cap = cv2.VideoCapture(self.source, be)

            if not cap.isOpened():
                cap.release()
                continue

            if not isinstance(self.source, str):
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cap_w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cap_h)
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                except Exception:
                    pass

            ok, frame = cap.read()
            if ok and frame is not None:
                got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                print(f"[{self.camera_id}] OK backend={be} obtenu={got_w}x{got_h} source={self.source}")
                self.cap = cap
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

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    def _crop_head_from_full(self, frame_full: np.ndarray, box_full_xyxy: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
        H, W = frame_full.shape[:2]
        x1, y1, x2, y2 = box_full_xyxy

        x1 = self._clamp(x1, 0, W - 1)
        x2 = self._clamp(x2, 0, W - 1)
        y1 = self._clamp(y1, 0, H - 1)
        y2 = self._clamp(y2, 0, H - 1)
        if x2 <= x1 or y2 <= y1:
            return None

        bw = x2 - x1
        bh = y2 - y1
        head_h = max(40, int(bh * self.head_ratio))

        pad_x = int(bw * 0.10)
        pad_y = int(head_h * 0.10)

        hx1 = self._clamp(x1 - pad_x, 0, W - 1)
        hx2 = self._clamp(x2 + pad_x, 0, W - 1)
        hy1 = self._clamp(y1 - pad_y, 0, H - 1)
        hy2 = self._clamp(y1 + head_h + pad_y, 0, H - 1)

        if hx2 <= hx1 or hy2 <= hy1:
            return None

        return frame_full[hy1:hy2, hx1:hx2]

    def _recognize_from_crop(self, head_crop_bgr: np.ndarray):
        """
        Retourne:
          mode ∈ {TARGET, DB, UNK}
          label
          target_score
          db_score
          global_id (si embedding visage dispo)
        """
        target_emb, target_label, target_thr = self._get_target()

        faces = detect_faces_safe(head_crop_bgr)
        if not faces:
            return "UNK", None, None, None, None

        faces = sorted(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
            reverse=True
        )
        face_emb = faces[0].normed_embedding.astype(np.float32)

        gid = self.reid.assign(face_emb) if self.reid is not None else None

        # TARGET
        if target_emb is not None:
            tscore = cosine_similarity(face_emb, target_emb)
            if tscore >= target_thr:
                return "TARGET", target_label, float(tscore), None, gid
        else:
            tscore = None

        # DB
        best, dbsim = self._best_db_match(face_emb)
        if best is not None:
            return "DB", best["nom"], None if tscore is None else float(tscore), float(dbsim), gid

        return "UNK", None, None if tscore is None else float(tscore), float(dbsim), gid

    def _loop(self):
        self._placeholder(f"{self.camera_id}: ouverture...")
        last_ok = time.time()
        while self.running:
            if self.cap is None:
                if not self._try_open():
                    self.latest_events = []
                    self._placeholder(f"{self.camera_id}: PAS DE SIGNAL")
                    self._stop_evt.wait(2.0)
                    continue
                last_ok = time.time()

            ok, frame_full = self.cap.read()
            if not ok or frame_full is None:
                # Plus d'image : ne pas laisser d'anciens événements affichés.
                self.latest_events = []
                self._placeholder(f"{self.camera_id}: frame introuvable")
                if time.time() - last_ok > self.reconnect_after_sec:
                    print(f"[{self.camera_id}] flux interrompu, tentative de reconnexion")
                    self._release_cap()
                else:
                    self._stop_evt.wait(0.1)
                continue

            last_ok = time.time()
            self._process_frame(frame_full)

    def _process_frame(self, frame_full):
        now = time.time()
        self.last_frame_ts = now
        self._purge_state(now)

        frame_small = cv2.resize(frame_full, (self.out_w, self.out_h), interpolation=cv2.INTER_AREA)
        frame_draw = frame_small.copy()

        full_h, full_w = frame_full.shape[:2]
        sx = full_w / float(self.out_w)
        sy = full_h / float(self.out_h)

        results = self.yolo.track(
            frame_small,
            persist=True,
            verbose=False,
            classes=[0],
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
            return

        xyxy_small = boxes.xyxy.cpu().numpy()
        tids = boxes.id.cpu().numpy().astype(int)

        for (pb, tid) in zip(xyxy_small, tids):
            x1s, y1s, x2s, y2s = map(float, pb)
            tid = int(tid)

            st = self._track_state.setdefault(tid, {})
            st["last_seen"] = now

            # lock si valable
            mode = "UNK"
            label = None
            target_score = st.get("last_target_score")
            db_score = st.get("last_db_score")
            global_id = st.get("global_id")

            if st.get("locked_until", 0.0) > now:
                mode = st.get("locked_mode", "UNK")
                label = st.get("locked_label", None)

            # reconnaissance throttled
            last_recog = st.get("last_recog", 0.0)
            if (now - last_recog) >= self.recog_every_sec:
                st["last_recog"] = now

                x1f = int(x1s * sx)
                y1f = int(y1s * sy)
                x2f = int(x2s * sx)
                y2f = int(y2s * sy)

                head_crop = self._crop_head_from_full(frame_full, (x1f, y1f, x2f, y2f))
                if head_crop is not None and head_crop.size > 0:
                    rm, rl, rts, rdb, gid = self._recognize_from_crop(head_crop)

                    if gid is not None:
                        global_id = gid
                        st["global_id"] = gid

                    if rm in ("TARGET", "DB"):
                        mode = rm
                        label = rl
                        st["locked_mode"] = mode
                        st["locked_label"] = label
                        st["locked_until"] = now + self.lock_hold_sec

                    if rts is not None:
                        target_score = rts
                        st["last_target_score"] = rts
                    if rdb is not None:
                        db_score = rdb
                        st["last_db_score"] = rdb

            # overlay
            if mode == "TARGET":
                color = (0, 0, 255)
                txt = f"{self.camera_id} | CIBLE {'' if target_score is None else f'{target_score:.2f}'} | gid={global_id or '-'}"
            elif mode == "DB":
                color = (0, 180, 0)
                txt = f"{self.camera_id} | {label} {'' if db_score is None else f'{db_score:.2f}'} | gid={global_id or '-'}"
            else:
                color = (120, 120, 120)
                txt = f"{self.camera_id} | track {tid} | gid={global_id or '-'}"

            cv2.rectangle(frame_draw, (int(x1s), int(y1s)), (int(x2s), int(y2s)), color, 2)
            cv2.putText(frame_draw, txt, (int(x1s), max(18, int(y1s) - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

            cx = float((x1s + x2s) / 2.0) / float(self.out_w)
            cy = float((y1s + y2s) / 2.0) / float(self.out_h)

            events.append({
                "camera_id": self.camera_id,
                "track_id": tid,
                "global_id": global_id,
                "mode": mode,
                "label": label,
                "target_score": target_score,
                "db_score": db_score,
                "x": cx, "y": cy,
                "bbox": [x1s, y1s, x2s, y2s],
                "ts": now
            })

        self.latest_events = events

        ok2, jpg = cv2.imencode(".jpg", frame_draw, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        if ok2:
            self.latest_jpeg = jpg.tobytes()

        time.sleep(0.005)