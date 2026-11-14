import time
import threading
from typing import Optional, Dict, Any, List, Tuple, Union

import cv2
import zlib
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
        process_fps: Optional[float] = None,
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
        self.process_fps = float(process_fps) if process_fps else None   # None = pas de limite
        self._stop_evt = threading.Event()

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.cap: Optional[cv2.VideoCapture] = None

        self.latest_jpeg: Optional[bytes] = None          # image annotée (cadres, noms)
        self.latest_jpeg_raw: Optional[bytes] = None      # image sans annotations (encodée seulement si demandée)
        # Gros plan (sans annotation) de chaque cible vue récemment : identifiant de cible -> (instant, JPEG).
        # Gardé en mémoire seulement ; sert à illustrer l'alerte envoyée à l'autorité.
        self.latest_target_crops: Dict[int, Tuple[float, bytes]] = {}
        self.frame_seq: int = 0                           # +1 à chaque nouvelle image publiée
        self.raw_seq: int = 0                             # = frame_seq quand l'image brute est à jour
        self.latest_events: List[Dict[str, Any]] = []
        self.last_frame_ts: float = 0.0

        # Statistiques de fonctionnement (supervision)
        self._started_at: float = 0.0
        self.frame_stats: Dict[str, Any] = {}        # luminosité, contraste et empreinte de l'image (détection d'une caméra masquée)
        self._fps: float = 0.0                 # images traitées par seconde (moyenne lissée)
        self._proc_ms: float = 0.0             # durée de traitement d'une image (moyenne lissée)
        self._last_publish: float = 0.0
        self.source_size: Tuple[int, int] = (0, 0)
        self.reconnects: int = 0
        self._raw_lock = threading.Lock()
        self._raw_viewers: int = 0

        self.yolo = YOLO(config.YOLO_WEIGHTS)

        # DB enrôlée
        self.known = []
        self.db_threshold = config.DB_THRESHOLD                # en dessous : « inconnu »
        self.probable_threshold = config.DB_PROBABLE_THRESHOLD  # au-dessus : « correspondance probable »
        self.ambiguity_margin = config.DB_AMBIGUITY_MARGIN      # deux personnes plus proches que cela : « à vérifier »

        # CIBLE
        self._tlock = threading.Lock()
        self._targets: List[Dict[str, Any]] = []   # [{id, label, embedding, threshold, color_bgr}]

        # état par track
        self._track_state: Dict[int, Dict[str, Any]] = {}
        self._state_ttl_sec = 8.0

    def set_known(self, known_list):
        self.known = known_list

    def set_targets(self, targets):
        """Remplace l'ensemble des cibles recherchées (liste de dicts : id, label, embedding, threshold, color_bgr)."""
        prepared = []
        for t in targets:
            item = dict(t)
            item["embedding"] = np.asarray(t["embedding"], dtype=np.float32)
            item.setdefault("threshold", config.TARGET_THRESHOLD)
            item.setdefault("color_bgr", (0, 0, 255))
            prepared.append(item)
        with self._tlock:
            self._targets = prepared

    def clear_targets(self):
        with self._tlock:
            self._targets = []

    def _get_targets(self):
        with self._tlock:
            return list(self._targets)

    def start(self):
        if self.running:
            return
        self._stop_evt.clear()
        self._started_at = time.time()
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

    # ---- vue sans annotations (encodée uniquement si au moins un client la demande) -------------
    def add_raw_viewer(self):
        with self._raw_lock:
            self._raw_viewers += 1

    def remove_raw_viewer(self):
        with self._raw_lock:
            self._raw_viewers = max(0, self._raw_viewers - 1)

    @property
    def raw_viewers(self) -> int:
        with self._raw_lock:
            return self._raw_viewers

    # ---- état et statistiques --------------------------------------------------------------
    ONLINE_WITHIN_SEC = 3.0      # une image plus récente que cela => en ligne
    OFFLINE_AFTER_SEC = 10.0     # aucune image depuis cela => hors ligne

    def status(self, now: Optional[float] = None) -> str:
        """stopped | connecting | online | reconnecting | offline"""
        now = time.time() if now is None else now
        if not self.running:
            return "stopped"
        if self.last_frame_ts and now - self.last_frame_ts < self.ONLINE_WITHIN_SEC:
            return "online"
        since = self.last_frame_ts or self._started_at
        if now - since >= self.OFFLINE_AFTER_SEC:
            return "offline"
        return "reconnecting" if self.last_frame_ts else "connecting"

    def stats(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        status = self.status(now)
        online = status == "online"
        return {
            "status": status,
            "fps": round(self._fps, 1) if online else 0.0,
            "latency_ms": round(self._proc_ms) if online else None,
            "persons": len(self.latest_events) if online else 0,
            "width": self.source_size[0], "height": self.source_size[1],
            "age_sec": round(now - self.last_frame_ts, 1) if self.last_frame_ts else None,
            "reconnects": self.reconnects,
        }

    def _publish(self, frame_small, frame_draw, started: float):
        """Encode et publie l'image (annotée, et brute si demandée), et met à jour les statistiques."""
        ok, jpg = cv2.imencode(".jpg", frame_draw, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        if self.raw_viewers > 0:
            ok_raw, jpg_raw = cv2.imencode(".jpg", frame_small, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            raw_bytes = jpg_raw.tobytes() if ok_raw else None
        else:
            raw_bytes = None
        if ok:
            self.latest_jpeg = jpg.tobytes()
        self.frame_seq += 1
        if raw_bytes is not None:
            self.latest_jpeg_raw = raw_bytes
            self.raw_seq = self.frame_seq

        now = time.time()
        self._proc_ms = self._smooth(self._proc_ms, (time.perf_counter() - started) * 1000.0)
        if self._last_publish:
            dt = now - self._last_publish
            if dt > 0:
                self._fps = self._smooth(self._fps, 1.0 / dt)
        self._last_publish = now

    @staticmethod
    def _measure(frame, now: float) -> Dict[str, Any]:
        """Mesures légères : luminosité moyenne et contraste (sur une miniature), somme de contrôle de l'image (flux figé)."""
        try:
            tiny = cv2.resize(frame, (16, 9), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(tiny, cv2.COLOR_BGR2GRAY) if tiny.ndim == 3 else tiny
            return {"mean": float(gray.mean()), "std": float(gray.std()),
                    "crc": zlib.crc32(frame.tobytes()) if frame.flags["C_CONTIGUOUS"] else zlib.crc32(frame.copy().tobytes()),
                    "ts": now}
        except Exception:
            return {}

    @staticmethod
    def _smooth(previous: float, value: float, alpha: float = 0.2) -> float:
        return value if previous <= 0 else previous + alpha * (value - previous)

    def _placeholder(self, text: str):
        img = np.zeros((self.out_h, self.out_w, 3), dtype=np.uint8)
        img[:] = (245, 245, 245)
        cv2.putText(img, text, (18, self.out_h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 80, 80), 2)
        ok, jpg = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if ok:
            self.latest_jpeg = self.latest_jpeg_raw = jpg.tobytes()
            self.frame_seq += 1
            self.raw_seq = self.frame_seq

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

    def _match_known(self, face_emb: np.ndarray):
        """Meilleure personne enrôlée pour ce visage : (correspondance ou None, similarité la plus élevée ou None).

        La similarité est calculée *par personne* (meilleure de ses références). Elle n'est jamais une identité
        certaine : au-dessus de ``probable_threshold`` la correspondance est « probable », entre ``db_threshold``
        et ce seuil elle est « à vérifier », et elle l'est aussi quand une autre personne est presque aussi proche.
        """
        per_person = {}
        for k in self.known:
            sim = cosine_similarity(face_emb, k["embedding"])
            key = k.get("participant_id", k.get("nom"))
            current = per_person.get(key)
            if current is None or sim > current[0]:
                per_person[key] = (sim, k)
        if not per_person:
            return None, None
        ranked = sorted(per_person.values(), key=lambda item: -item[0])
        best_sim, best = ranked[0]
        if best_sim < self.db_threshold:
            return None, float(best_sim)
        runner_up = ranked[1][0] if len(ranked) > 1 else None
        ambiguous = runner_up is not None and runner_up >= self.db_threshold and (best_sim - runner_up) < self.ambiguity_margin
        status = "probable" if best_sim >= self.probable_threshold and not ambiguous else "a_verifier"
        return ({"participant_id": best.get("participant_id"), "label": best["nom"], "score": float(best_sim),
                 "status": status, "active": bool(best.get("active", True)), "ambiguous": ambiguous},
                float(best_sim))

    def _remember_target_crop(self, target_id: int, frame_small, box, now: float) -> None:
        """Garde un gros plan net (sans cadre ni texte) de la cible, rafraîchi au plus une fois par seconde."""
        previous = self.latest_target_crops.get(target_id)
        if previous is not None and now - previous[0] < 1.0:
            return
        h, w = frame_small.shape[:2]
        x1, y1, x2, y2 = box
        pad_x, pad_y = 0.08 * (x2 - x1), 0.05 * (y2 - y1)
        x1, x2 = int(max(0, x1 - pad_x)), int(min(w, x2 + pad_x))
        y1, y2 = int(max(0, y1 - pad_y)), int(min(h, y2 + pad_y))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return
        crop = frame_small[y1:y2, x1:x2]
        if crop.shape[0] < 320:                       # agrandi pour être lisible sur un téléphone (aucune information ajoutée)
            scale = min(320.0 / crop.shape[0], 480.0 / crop.shape[1])
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        ok, jpg = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if ok:
            self.latest_target_crops[target_id] = (now, jpg.tobytes())
            for key in [k for k, (t, _) in self.latest_target_crops.items() if now - t > 120.0]:
                del self.latest_target_crops[key]

    def target_crop(self, target_id: int, max_age_sec: float = 10.0) -> Optional[bytes]:
        item = self.latest_target_crops.get(target_id)
        return item[1] if item is not None and time.time() - item[0] <= max_age_sec else None

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

    @staticmethod
    def _usable_face(face) -> bool:
        """Visage assez grand et assez net pour être comparé (sinon l'empreinte est trop bruitée)."""
        try:
            x1, y1, x2, y2 = (float(v) for v in face.bbox[:4])
        except (TypeError, ValueError, AttributeError):
            return False
        if min(x2 - x1, y2 - y1) < config.FACE_MIN_SIZE_PX:
            return False
        score = getattr(face, "det_score", None)
        return score is None or float(score) >= config.FACE_MIN_DET_SCORE

    def _recognize_from_crop(self, head_crop_bgr: np.ndarray) -> Dict[str, Any]:
        """
        Retourne un dict :
          mode        TARGET | DB | UNK
          label       libellé de la cible ou nom affiché de la personne
          target_score  meilleure similarité avec l'une des cibles
          db_score      meilleure similarité avec une personne enrôlée
          global_id     (si embedding visage dispo)
          target_id     cible reconnue, sinon None
          match         correspondance avec une personne enrôlée (mode DB) : identifiant, statut, score…
        """
        targets = self._get_targets()
        result = {"mode": "UNK", "label": None, "target_score": None, "db_score": None, "global_id": None,
                  "target_id": None, "match": None, "face_seen": False}

        faces = [f for f in (detect_faces_safe(head_crop_bgr) or []) if self._usable_face(f)]
        if not faces:
            return result
        result["face_seen"] = True

        faces = sorted(
            faces,
            key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
            reverse=True
        )
        face_emb = faces[0].normed_embedding.astype(np.float32)

        result["global_id"] = self.reid.assign(face_emb) if self.reid is not None else None

        # TARGET : on compare le visage à toutes les cibles ; la meilleure au-dessus de son seuil l'emporte.
        tscore = None
        matched, matched_score = None, None
        for t in targets:
            score = cosine_similarity(face_emb, t["embedding"])
            if tscore is None or score > tscore:
                tscore = score
            if score >= t["threshold"] and (matched is None or score > matched_score):
                matched, matched_score = t, score
        if matched is not None:
            result.update(mode="TARGET", label=matched["label"], target_score=float(matched_score),
                          target_id=matched["id"])
            return result
        result["target_score"] = None if tscore is None else float(tscore)

        # DB
        match, best_sim = self._match_known(face_emb)
        result["db_score"] = best_sim
        if match is not None:
            result.update(mode="DB", label=match["label"], match=match)
        return result

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
                    self.reconnects += 1
                    self._last_publish = 0.0
                    self._release_cap()
                else:
                    self._stop_evt.wait(0.1)
                continue

            last_ok = time.time()
            self._process_frame(frame_full)
            self._throttle(last_ok)

    def _throttle(self, started: float):
        """Limite la cadence de traitement. Pendant l'attente, les images de la caméra sont *lues et jetées* :
        sans cela, une caméra réseau accumulerait un retard croissant dans son tampon."""
        if not self.process_fps:
            return
        deadline = started + 1.0 / self.process_fps
        while self.running and time.time() < deadline:
            cap = self.cap
            grab = getattr(cap, "grab", None)
            if grab is None:
                self._stop_evt.wait(max(0.0, deadline - time.time()))
                return
            if not grab():
                return                        # flux interrompu : la boucle principale s'en occupe

    def _process_frame(self, frame_full):
        started = time.perf_counter()
        now = time.time()
        self.last_frame_ts = now
        self.source_size = (frame_full.shape[1], frame_full.shape[0])
        self._purge_state(now)

        frame_small = cv2.resize(frame_full, (self.out_w, self.out_h), interpolation=cv2.INTER_AREA)
        frame_draw = frame_small.copy()
        self.frame_stats = self._measure(frame_small, now)

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
            self._publish(frame_small, frame_draw, started)
            time.sleep(0.005)
            return

        xyxy_small = boxes.xyxy.cpu().numpy()
        tids = boxes.id.cpu().numpy().astype(int)

        targets = self._get_targets()
        target_colors = {t["id"]: t["color_bgr"] for t in targets}

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

            target_id = None
            match = None            # correspondance avec une personne enrôlée (mode DB)

            if st.get("locked_until", 0.0) > now:
                locked_mode = st.get("locked_mode", "UNK")
                # Une cible retirée entre-temps ne doit plus être reconnue grâce à l'ancien verrou.
                if locked_mode != "TARGET" or st.get("locked_target_id") in target_colors:
                    mode = locked_mode
                    label = st.get("locked_label", None)
                    target_id = st.get("locked_target_id") if mode == "TARGET" else None
                    if mode == "DB":
                        match = st.get("match")

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
                    rec = self._recognize_from_crop(head_crop)

                    if rec["global_id"] is not None:
                        global_id = rec["global_id"]
                        st["global_id"] = global_id

                    if rec["mode"] == "UNK" and rec["face_seen"] and st.get("locked_until", 0.0) > now:
                        # Un visage net est visible et ne correspond à personne : l'identité reportée est sans doute celle
                        # d'une autre personne (échange de piste, croisement). Retirée après quelques confirmations.
                        st["contradictions"] = st.get("contradictions", 0) + 1
                        if st["contradictions"] >= config.IDENTITY_CONTRADICTIONS:
                            for key in ("locked_until", "locked_mode", "locked_label", "locked_target_id", "match", "face_ts",
                                        "contradictions"):
                                st.pop(key, None)
                            mode, label, target_id, match = "UNK", None, None, None
                    elif rec["mode"] in ("TARGET", "DB"):
                        st["contradictions"] = 0
                        mode = rec["mode"]
                        label = rec["label"]
                        st["locked_mode"] = mode
                        st["locked_label"] = label
                        st["locked_target_id"] = rec["target_id"]
                        target_id = rec["target_id"] if mode == "TARGET" else None
                        st["locked_until"] = now + self.lock_hold_sec
                        st["face_ts"] = now                    # dernière fois que le visage a confirmé l'identité
                        if mode == "DB":
                            # Au sein d'une même piste, le meilleur cliché fait foi : une image floue ensuite ne
                            # dégrade pas une correspondance « probable » déjà obtenue pour la même personne.
                            new, previous = rec["match"], st.get("match")
                            if previous is None or previous["participant_id"] != new["participant_id"] \
                                    or new["score"] >= previous["score"]:
                                st["match"] = new
                            match = st["match"]
                            label = match["label"]
                            st["locked_label"] = label
                        else:
                            st["match"] = None

                    if rec["target_score"] is not None:
                        target_score = rec["target_score"]
                        st["last_target_score"] = target_score
                    if rec["db_score"] is not None:
                        db_score = rec["db_score"]
                        st["last_db_score"] = db_score

            # Identité conservée grâce au suivi de la silhouette (visage non visible depuis un moment) ?
            face_age = (now - st["face_ts"]) if mode in ("TARGET", "DB") and "face_ts" in st else None
            tracked_only = face_age is not None and face_age > max(2.0, 3 * self.recog_every_sec)

            # overlay
            if mode == "TARGET":
                color = target_colors.get(target_id, (0, 0, 255))
                txt = f"{self.camera_id} | {label} {'' if target_score is None else f'{target_score:.2f}'} | gid={global_id or '-'}"
            elif mode == "DB":
                status = (match or {}).get("status")
                shown = match["score"] if match else db_score
                if match is not None and not match.get("active", True):
                    color, mark = (180, 0, 180), " (inactif)"          # personne désactivée reconnue
                elif status == "probable":
                    color, mark = (0, 180, 0), ""                      # vert : correspondance probable
                else:
                    color, mark = (0, 165, 255), " ?"                  # orange : à vérifier
                txt = f"{self.camera_id} | {label}{mark} {'' if shown is None else f'{shown:.2f}'} | gid={global_id or '-'}"
            else:
                color = (120, 120, 120)
                txt = f"{self.camera_id} | track {tid} | gid={global_id or '-'}"

            if tracked_only:
                txt += " ~suivi"                       # identité reportée sans visage visible : moins sûre
            if mode == "TARGET" and target_id is not None:
                self._remember_target_crop(target_id, frame_small, (x1s, y1s, x2s, y2s), now)
            cv2.rectangle(frame_draw, (int(x1s), int(y1s)), (int(x2s), int(y2s)), color, 2)
            cv2.putText(frame_draw, txt, (int(x1s), max(18, int(y1s) - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

            cx = float((x1s + x2s) / 2.0) / float(self.out_w)
            cy = float((y1s + y2s) / 2.0) / float(self.out_h)

            if mode == "DB" and match is not None:
                db_score = match["score"]          # le score affiché est celui qui justifie le statut
            events.append({
                "camera_id": self.camera_id,
                "track_id": tid,
                "global_id": global_id,
                "mode": mode,
                "label": label,
                "target_id": target_id if mode == "TARGET" else None,
                # Personne enrôlée : identifiant, statut de correspondance (jamais une identité certaine), fiche active ?
                "participant_id": match["participant_id"] if mode == "DB" and match else None,
                "match_status": match["status"] if mode == "DB" and match else None,
                "participant_active": match["active"] if mode == "DB" and match else None,
                "match_ambiguous": bool(match["ambiguous"]) if mode == "DB" and match else False,
                "target_score": target_score,
                "db_score": db_score,
                "tracked_only": bool(tracked_only),
                "face_age": None if face_age is None else round(float(face_age), 1),
                "x": cx, "y": cy,
                # Point au sol (milieu du bas du cadre) : référence des lignes et zones du Module 6.
                "fx": float((x1s + x2s) / 2.0) / float(self.out_w), "fy": float(y2s) / float(self.out_h),
                "bbox": [x1s, y1s, x2s, y2s],
                "ts": now
            })

        self.latest_events = events
        self._publish(frame_small, frame_draw, started)

        time.sleep(0.005)