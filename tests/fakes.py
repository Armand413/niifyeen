"""Fausse caméra partagée par les tests : mêmes méthodes publiques que PersonPipeline, sans aucun modèle."""
import time


class FakeCamera:
    def __init__(self, **kw):
        self.camera_id = kw["camera_id"]
        self.source = kw.get("source")
        self.latest_events = []
        self.latest_jpeg = b"\xff\xd8annot\xff\xd9"
        self.latest_jpeg_raw = None
        self.frame_seq = 0
        self.raw_seq = 0
        self.last_frame_ts = time.time()
        self.running = False
        self.known = None
        self.targets = []
        self.forced_status = "online"       # les tests peuvent le changer
        self.raw_viewers = 0
        self.reconnects = 0

    # --- cycle de vie
    def set_known(self, known): self.known = known
    def start(self): self.running = True
    def stop(self): self.running = False
    def set_targets(self, targets): self.targets = list(targets)
    def clear_targets(self): self.targets = []

    # --- image
    def add_raw_viewer(self): self.raw_viewers += 1
    def remove_raw_viewer(self): self.raw_viewers = max(0, self.raw_viewers - 1)

    def publish(self, annotated=None, raw=None):
        """Simule l'arrivée d'une nouvelle image."""
        self.frame_seq += 1
        if annotated is not None:
            self.latest_jpeg = annotated
        if raw is not None and self.raw_viewers > 0:
            self.latest_jpeg_raw = raw
            self.raw_seq = self.frame_seq
        self.last_frame_ts = time.time()

    # --- état
    def status(self, now=None):
        return self.forced_status

    def stats(self, now=None):
        online = self.forced_status == "online"
        return {"status": self.forced_status, "fps": 12.5 if online else 0.0,
                "latency_ms": 40 if online else None, "persons": len(self.latest_events) if online else 0,
                "width": 1280, "height": 720,
                "age_sec": 0.1 if self.last_frame_ts else None, "reconnects": self.reconnects}
