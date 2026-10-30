import time
import threading
import numpy as np
from .vision import cosine_similarity

class GlobalReId:
    """
    Ré-identification simple inter-caméra basée sur l'embedding visage (ArcFace).
    - Si un embedding ressemble (cosine >= threshold) à un global_id actif, on réutilise ce global_id.
    - Sinon on crée un nouveau global_id.
    - ttl_sec: durée de vie des IDs non vus.
    """
    def __init__(self, sim_threshold: float = 0.45, ttl_sec: float = 20.0):
        self.sim_threshold = float(sim_threshold)
        self.ttl_sec = float(ttl_sec)
        self._lock = threading.Lock()
        self._next_id = 1
        self._active = {}  # gid -> dict(emb, last_seen)

    def _purge(self, now: float):
        for gid in list(self._active.keys()):
            if now - self._active[gid]["last_seen"] > self.ttl_sec:
                del self._active[gid]

    def assign(self, emb: np.ndarray) -> int:
        now = time.time()
        emb = emb.astype(np.float32)

        with self._lock:
            self._purge(now)

            best_gid = None
            best_sim = -1.0
            for gid, it in self._active.items():
                sim = cosine_similarity(emb, it["emb"])
                if sim > best_sim:
                    best_sim = sim
                    best_gid = gid

            if best_gid is not None and best_sim >= self.sim_threshold:
                self._active[best_gid]["emb"] = emb
                self._active[best_gid]["last_seen"] = now
                return best_gid

            gid = self._next_id
            self._next_id += 1
            self._active[gid] = {"emb": emb, "last_seen": now}
            return gid