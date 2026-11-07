"""Mode démonstration (Module 11).

Lancement : ``python tools/run_demo.py`` (variable ``NIIFYEEN_DEMO=1``). Tout est **simulé** et isolé des données réelles :

* base de données séparée (``niifyeen-demo.db``) : ``NIIFYEEN_DB`` est ignorée en mode démo (voir ``config.py``) ;
* caméras virtuelles (aucun matériel, aucun modèle de vision) qui dessinent des silhouettes sans visage ;
* personnes enrôlées fictives (« Alice (démo) »…), sans photo ni empreinte biométrique ;
* aucune notification externe (e-mail, Telegram) ;
* les silhouettes suivent un scénario en boucle ; les lignes, zones, alertes, cibles, statistiques et enregistrements
  sont produits par les **vrais** modules à partir de ces silhouettes (seule la vision par ordinateur est simulée).
"""
import hashlib
import json
import random
import threading
import time
import zlib
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from sqlalchemy import text

from . import config
from .db import (Alert, AlertAction, Detection, Dossier, DossierItem, Event, Participant, Recording, SessionLocal,
                 TargetMove, Zone, ZoneStat, display_name, utcnow)

PERIOD_SEC = 120.0
W, H = config.STREAM_SIZE
BANNER = "SIMULATION"

CAMERAS = [
    {"id": "entre", "source": "0", "label": "Entrée (démo)", "location": "Hall d'accueil", "x": 0.11, "y": 0.72, "bg": (96, 84, 70)},
    {"id": "salle", "source": "1", "label": "Salle (démo)", "location": "Salle polyvalente", "x": 0.63, "y": 0.30, "bg": (74, 92, 78)},
    {"id": "couloir", "source": "2", "label": "Couloir (démo)", "location": "Couloir technique", "x": 0.89, "y": 0.72, "bg": (84, 78, 96)},
]
PEOPLE = [("Alice", "(démo)", "DEMO-001"), ("Bruno", "(démo)", "DEMO-002"), ("Chloé", "(démo)", "DEMO-003")]
ZONES = [
    {"camera_id": "entre", "kind": "line", "name": "Porte principale", "points": [[0.5, 0.12], [0.5, 0.98]], "label_in": "Entrée", "label_out": "Sortie"},
    {"camera_id": "salle", "kind": "zone", "name": "Salle (capacité 3)", "points": [[0.05, 0.35], [0.95, 0.35], [0.95, 0.98], [0.05, 0.98]], "capacity": 3},
    {"camera_id": "couloir", "kind": "zone", "name": "Local technique", "points": [[0.65, 0.2], [0.98, 0.2], [0.98, 0.98], [0.65, 0.98]], "restricted": True},
]

# Acteurs : (clé, caméra, début, fin en secondes de scénario, trajet en fractions de l'image, type, personne)
SCRIPT: List[Dict[str, Any]] = [
    {"key": "visiteur", "cam": "entre", "t0": 4, "t1": 16, "path": [(0.10, 0.80), (0.90, 0.80)], "kind": "unk",
     "text": "Un visiteur inconnu franchit la porte principale (comptage d'entrée)."},
    {"key": "cible_entre", "cam": "entre", "t0": 8, "t1": 20, "path": [(0.15, 0.92), (0.85, 0.92)], "kind": "target",
     "text": "La « Cible 1 » est repérée à l'entrée."},
    {"key": "alice_sortie", "cam": "entre", "t0": 24, "t1": 36, "path": [(0.90, 0.72), (0.10, 0.72)], "kind": "person", "who": 0,
     "text": "Alice (démo) traverse la porte dans l'autre sens (comptage de sortie)."},
    {"key": "cible_couloir", "cam": "couloir", "t0": 26, "t1": 38, "path": [(0.10, 0.60), (0.55, 0.60)], "kind": "target",
     "text": "La cible passe au couloir : le trajet change de caméra."},
    {"key": "bruno_entre", "cam": "entre", "t0": 40, "t1": 52, "path": [(0.10, 0.68), (0.90, 0.68)], "kind": "person", "who": 1,
     "text": "Bruno (démo) est reconnu à l'entrée, puis dans la salle."},
    {"key": "alice_salle", "cam": "salle", "t0": 44, "t1": 60, "path": [(0.10, 0.85), (0.50, 0.62), (0.90, 0.85)], "kind": "person", "who": 0,
     "text": "Alice (démo) est reconnue dans la salle."},
    {"key": "cible_salle", "cam": "salle", "t0": 46, "t1": 58, "path": [(0.10, 0.92), (0.90, 0.92)], "kind": "target",
     "text": "La cible atteint la salle : fin du trajet entrée → couloir → salle."},
    {"key": "bruno_salle", "cam": "salle", "t0": 58, "t1": 72, "path": [(0.85, 0.70), (0.20, 0.80)], "kind": "person", "who": 1, "text": ""},
    {"key": "foule1", "cam": "salle", "t0": 74, "t1": 96, "path": [(0.20, 0.55), (0.30, 0.65), (0.22, 0.60)], "kind": "unk",
     "text": "Quatre inconnus se rassemblent dans la salle : la capacité de 3 est dépassée (affluence)."},
    {"key": "foule2", "cam": "salle", "t0": 75, "t1": 96, "path": [(0.45, 0.60), (0.55, 0.70), (0.47, 0.64)], "kind": "unk", "text": ""},
    {"key": "foule3", "cam": "salle", "t0": 76, "t1": 96, "path": [(0.70, 0.58), (0.78, 0.68), (0.72, 0.62)], "kind": "unk", "text": ""},
    {"key": "foule4", "cam": "salle", "t0": 77, "t1": 96, "path": [(0.85, 0.80), (0.75, 0.88), (0.83, 0.84)], "kind": "unk", "text": ""},
    {"key": "intrus", "cam": "couloir", "t0": 98, "t1": 108, "path": [(0.25, 0.72), (0.85, 0.72)], "kind": "unk",
     "text": "Un inconnu entre dans le local technique (zone interdite : intrusion)."},
]
OUTAGES = [{"cam": "couloir", "t0": 108.0, "t1": 120.0,
            "text": "La caméra du couloir se coupe pendant 12 s (hors ligne, puis rétablie)."}]
KIND_COLOR = {"unk": (150, 150, 150), "person": (200, 120, 40), "target": (60, 60, 230)}      # BGR


def assert_isolated() -> None:
    """Refuse de démarrer si la base visée pourrait être la vraie."""
    if not config.DEMO:
        raise RuntimeError("Mode démonstration non activé.")
    if config.DB_PATH.name == "niifyeen.db":
        raise RuntimeError("Le mode démonstration refuse d'utiliser la base réelle (niifyeen.db).")


def script_view() -> List[Dict[str, Any]]:
    """Déroulé du scénario, pour la page de démonstration."""
    rows = [{"t": a["t0"], "camera_id": a["cam"], "text": a["text"]} for a in SCRIPT if a["text"]]
    rows += [{"t": o["t0"], "camera_id": o["cam"], "text": o["text"]} for o in OUTAGES]
    return sorted(rows, key=lambda r: r["t"])


class Scenario:
    """Horloge du scénario et position des silhouettes. Pas de hasard : le déroulé est identique à chaque boucle."""

    def __init__(self, speed: float = 1.0, clock=time.time):
        self.speed = max(0.25, float(speed))
        self._clock = clock
        self.epoch = clock()
        self.target_id: Optional[int] = None
        self.people_ids: List[Optional[int]] = [None] * len(PEOPLE)
        self.people_names: List[str] = [display_name(n, p) for p, n, _ in PEOPLE]

    def restart(self):
        self.epoch = self._clock()

    def position(self, now: Optional[float] = None) -> Tuple[float, int]:
        """(secondes dans le scénario, numéro de boucle)."""
        elapsed = ((self._clock() if now is None else now) - self.epoch) * self.speed
        return elapsed % PERIOD_SEC, int(elapsed // PERIOD_SEC)

    def outage(self, cam: str, st: float) -> bool:
        return any(o["cam"] == cam and o["t0"] <= st < o["t1"] for o in OUTAGES)

    @staticmethod
    def _point(path, p: float) -> Tuple[float, float]:
        if len(path) == 1:
            return path[0]
        seg = min(len(path) - 2, int(p * (len(path) - 1)))
        local = p * (len(path) - 1) - seg
        (x0, y0), (x1, y1) = path[seg], path[seg + 1]
        return x0 + (x1 - x0) * local, y0 + (y1 - y0) * local

    def figures(self, cam: str, st: float, loop: int) -> List[Dict[str, Any]]:
        out = []
        for idx, a in enumerate(SCRIPT):
            if a["cam"] != cam or not (a["t0"] <= st < a["t1"]):
                continue
            fx, fy = self._point(a["path"], (st - a["t0"]) / (a["t1"] - a["t0"]))
            kind = a["kind"]
            if kind == "target" and self.target_id is None:
                kind = "unk"
            out.append({"key": a["key"], "kind": kind, "who": a.get("who"), "fx": fx, "fy": fy, "track_id": 1000 * (loop % 1000) + idx + 1})
        return out

    def events(self, cam: str, st: float, loop: int, now: float) -> List[Dict[str, Any]]:
        evs = []
        for f in self.figures(cam, st, loop):
            h = 50 + 150 * f["fy"]
            w = h * 0.36
            cx, foot = f["fx"] * W, f["fy"] * H
            bbox = [int(cx - w / 2), int(foot - h), int(cx + w / 2), int(foot)]
            ev = {"camera_id": cam, "track_id": f["track_id"], "global_id": None, "mode": "UNK", "label": None, "target_id": None,
                  "participant_id": None, "match_status": None, "participant_active": None, "match_ambiguous": False,
                  "target_score": None, "db_score": None, "x": cx, "y": foot - h / 2, "fx": f["fx"], "fy": f["fy"], "bbox": bbox, "ts": now}
            if f["kind"] == "person":
                ev.update(mode="DB", label=self.people_names[f["who"]], participant_id=self.people_ids[f["who"]],
                          match_status="probable", participant_active=True, db_score=0.72)
            elif f["kind"] == "target":
                ev.update(mode="TARGET", label=f"Cible {self.target_id}", target_id=self.target_id, target_score=0.82)
            evs.append(ev)
        return evs


class SimCamera:
    """Remplace ``PersonPipeline`` : mêmes attributs et méthodes publics, mais dessine des silhouettes (aucun modèle)."""

    ONLINE_WITHIN_SEC = 3.0
    OFFLINE_AFTER_SEC = 10.0
    FPS = 8.0

    def __init__(self, camera_id: str, scenario: Scenario, background=(80, 80, 80), label: str = ""):
        self.camera_id = camera_id
        self.source = f"demo:{camera_id}"
        self.scenario = scenario
        self.background = background
        self.label = label or camera_id
        self.latest_events: List[Dict[str, Any]] = []
        self.latest_jpeg: Optional[bytes] = None
        self.latest_jpeg_raw: Optional[bytes] = None
        self.frame_seq = 0
        self.raw_seq = 0
        self.last_frame_ts = 0.0
        self.frame_stats: Dict[str, Any] = {}
        self.source_size = (W, H)
        self.reconnects = 0
        self.running = False
        self.known: List[Any] = []
        self.targets: List[Any] = []
        self._raw_viewers = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = 0.0
        self._base = self._background()

    # --- cycle de vie
    def set_known(self, known): self.known = list(known or [])
    def set_targets(self, targets): self.targets = list(targets or [])
    def clear_targets(self): self.targets = []

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, name=f"demo-{self.camera_id}", daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def raw_viewers(self) -> int:
        return self._raw_viewers

    def add_raw_viewer(self):
        with self._lock:
            self._raw_viewers += 1

    def remove_raw_viewer(self):
        with self._lock:
            self._raw_viewers = max(0, self._raw_viewers - 1)

    # --- état
    def status(self, now: Optional[float] = None) -> str:
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
        online = self.status(now) == "online"
        return {"status": self.status(now), "fps": self.FPS if online else 0.0, "latency_ms": 12 if online else None,
                "persons": len(self.latest_events) if online else 0, "width": W, "height": H,
                "age_sec": round(now - self.last_frame_ts, 1) if self.last_frame_ts else None, "reconnects": self.reconnects}

    # --- image
    def _background(self) -> np.ndarray:
        img = np.empty((H, W, 3), np.uint8)
        img[:] = self.background
        horizon = int(H * 0.30)
        img[:horizon] = (np.array(self.background) * 0.62).astype(np.uint8)           # mur
        for k in range(1, 9):                                                         # dalles du sol, en perspective
            y = horizon + int((H - horizon) * (k / 8) ** 1.6)
            cv2.line(img, (0, y), (W, y), tuple(int(c * 0.8) for c in self.background), 1)
        for k in range(-6, 15):
            cv2.line(img, (int(W / 2 + (k - 4) * 20), horizon), (int(W / 2 + (k - 4) * 90), H), tuple(int(c * 0.85) for c in self.background), 1)
        cv2.putText(img, self.label, (14, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (235, 235, 235), 1, cv2.LINE_AA)
        if self.camera_id == "entre":
            cv2.line(img, (W // 2, int(H * 0.12)), (W // 2, int(H * 0.98)), (90, 200, 240), 1, cv2.LINE_AA)
            cv2.putText(img, "PORTE", (W // 2 + 6, int(H * 0.12) + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 200, 240), 1, cv2.LINE_AA)
        elif self.camera_id == "couloir":
            cv2.rectangle(img, (int(W * 0.65), int(H * 0.2)), (int(W * 0.98), int(H * 0.98)), (60, 60, 200), 1)
            cv2.putText(img, "LOCAL TECHNIQUE", (int(W * 0.66), int(H * 0.2) + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 90, 230), 1, cv2.LINE_AA)
        return img

    def render(self, events: List[Dict[str, Any]], tick: int) -> Tuple[np.ndarray, np.ndarray]:
        """(image brute, image annotée). Silhouettes sans visage ; filigrane « SIMULATION » dans les deux."""
        # Bruit de capteur léger : sans lui, une scène vide donne des images identiques et la supervision signalerait un flux figé.
        noise = np.random.RandomState(tick & 0x7FFFFFFF).randint(-2, 3, (H, W, 1)).astype(np.int16)
        raw = np.clip(self._base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        for ev in events:
            x1, y1, x2, y2 = ev["bbox"]
            kind = "target" if ev["mode"] == "TARGET" else "person" if ev["mode"] == "DB" else "unk"
            color = KIND_COLOR[kind]
            cx, h = (x1 + x2) // 2, y2 - y1
            cv2.ellipse(raw, (cx, y2), (max(4, (x2 - x1) // 2), max(2, h // 14)), 0, 0, 360, (30, 30, 30), -1)         # ombre
            cv2.rectangle(raw, (x1 + 2, y1 + h // 4), (x2 - 2, y2 - h // 10), color, -1)                              # corps
            cv2.circle(raw, (cx, y1 + h // 9), max(4, h // 9), (190, 205, 225), -1)                                    # tête (sans visage)
        cv2.putText(raw, BANNER, (W - 118, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 200, 255), 2, cv2.LINE_AA)
        annotated = raw.copy()
        for ev in events:
            x1, y1, x2, y2 = ev["bbox"]
            color = {"TARGET": (0, 0, 255), "DB": (255, 150, 40), "UNK": (180, 180, 180)}[ev["mode"]]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            label = ev["label"] or "Inconnu"
            cv2.putText(annotated, label, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        return raw, annotated

    def step(self, now: Optional[float] = None) -> bool:
        """Produit une image. Retourne False pendant une panne simulée (rien n'est publié)."""
        now = time.time() if now is None else now
        st, loop = self.scenario.position(now)
        if self.scenario.outage(self.camera_id, st):
            self.latest_events = []
            return False
        events = self.scenario.events(self.camera_id, st, loop, now)
        raw, annotated = self.render(events, self.frame_seq)
        small = cv2.cvtColor(cv2.resize(raw, (32, 18)), cv2.COLOR_BGR2GRAY)
        self.frame_stats = {"mean": float(small.mean()), "std": float(small.std()) + (self.frame_seq % 3) * 0.1,
                            "crc": zlib.crc32(raw.tobytes()), "ts": now}
        ok, jpg = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        raw_bytes = None
        if self._raw_viewers > 0:
            ok_raw, jr = cv2.imencode(".jpg", raw, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            raw_bytes = jr.tobytes() if ok_raw else None
        self.latest_events = events
        if ok:
            self.latest_jpeg = jpg.tobytes()
        self.frame_seq += 1
        if raw_bytes is not None:
            self.latest_jpeg_raw = raw_bytes
            self.raw_seq = self.frame_seq
        self.last_frame_ts = now
        return True

    def _run(self):
        period = 1.0 / self.FPS
        while not self._stop.is_set():
            started = time.time()
            try:
                self.step(started)
            except Exception:                                # une erreur de dessin ne doit pas tuer la caméra
                import logging
                logging.getLogger(__name__).exception("Caméra de démonstration : erreur")
            self._stop.wait(max(0.0, period - (time.time() - started)))


class NullNotifier:
    """Aucune notification externe en démonstration."""
    def start(self): pass
    def stop(self): pass
    def wanted(self, *a, **k): return False
    def submit(self, *a, **k): pass


# ---- données de départ ------------------------------------------------------------------------------------------------------------
def seed(db) -> Dict[str, Any]:
    """Crée (une seule fois) les personnes fictives et les zones. Retourne les identifiants des personnes."""
    assert_isolated()
    ids = []
    for prenom, nom, matricule in PEOPLE:
        p = db.query(Participant).filter(Participant.matricule == matricule).first()
        if p is None:
            p = Participant(nom=nom, prenom=prenom, matricule=matricule, infos="Personne fictive du mode démonstration.", actif=True)
            db.add(p); db.flush()
        ids.append(p.id)
    if db.query(Zone).count() == 0:
        for z in ZONES:
            db.add(Zone(camera_id=z["camera_id"], kind=z["kind"], name=z["name"], points=json.dumps(z["points"]),
                        label_in=z.get("label_in"), label_out=z.get("label_out"), capacity=z.get("capacity"),
                        restricted=bool(z.get("restricted", False)), enabled=True))
    db.commit()
    return {"people_ids": ids}


_HOUR_WEIGHT = [0, 0, 0, 0, 0, 1, 2, 5, 9, 12, 11, 9, 6, 8, 10, 11, 9, 6, 3, 2, 1, 0, 0, 0]


def backfill(db, hours: int = 24, now=None, seed_value: int = 42) -> Dict[str, int]:
    """Historique fictif des dernières heures, pour que statistiques et investigation ne soient pas vides au démarrage.
    Ne fait rien si la base contient déjà des détections."""
    assert_isolated()
    if db.query(Detection).count():
        return {"detections": 0, "passages": 0}
    rnd = random.Random(seed_value)
    now = now or utcnow()
    people = [p.id for p in db.query(Participant).filter(Participant.matricule.like("DEMO-%")).order_by(Participant.id)]
    names = {p.id: display_name(p.nom, p.prenom) for p in db.query(Participant).filter(Participant.id.in_(people))}
    cams = [c["id"] for c in CAMERAS]
    passages = rows = 0
    start = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    track = 1
    for h in range(hours + 1):
        hour_start = start + timedelta(hours=h)
        weight = _HOUR_WEIGHT[hour_start.hour]
        for _ in range(weight * 3):
            cam = rnd.choices(cams, weights=[5, 3, 2])[0]
            t0 = hour_start + timedelta(seconds=rnd.uniform(0, 3500))
            if t0 > now:
                continue
            n = rnd.randint(4, 14)
            kind = rnd.random()
            pid = rnd.choice(people) if kind < 0.25 and people else None
            fx, fy = rnd.uniform(0.1, 0.9), rnd.uniform(0.55, 0.95)
            dx = rnd.choice([-1, 1]) * rnd.uniform(0.02, 0.06)
            for k in range(n):
                px = min(0.98, max(0.02, fx + dx * k))
                db.add(Detection(camera_id=cam, track_id=track, mode="DB" if pid else "UNK", label=names.get(pid), participant_id=pid,
                                 match_status="probable" if pid else None, db_score=0.7 if pid else None,
                                 x=px * W, y=(fy - 0.2) * H, timestamp=t0 + timedelta(seconds=k * 0.8)))
                rows += 1
            track += 1
            passages += 1
    # trajets de cible : quelques sessions entrée -> couloir -> salle
    for s in range(1, 4):
        base = now - timedelta(hours=rnd.randint(2, 20))
        for step, cam in enumerate(["entre", "couloir", "salle"]):
            db.add(TargetMove(session=900 + s, camera_id=cam, label=f"Cible {s}", score=0.8, timestamp=base + timedelta(seconds=45 * step)))
    # affluence : une tranche de 5 minutes par zone et par heure ouvrée
    zones = {z.name: z for z in db.query(Zone).all()}
    for name, z in zones.items():
        for h in range(hours + 1):
            hs = start + timedelta(hours=h)
            if hs > now:
                continue
            w = _HOUR_WEIGHT[hs.hour]
            if w:
                db.add(ZoneStat(zone_id=z.id, bucket=hs + timedelta(minutes=5 * rnd.randint(0, 10)), entries=rnd.randint(0, w), exits=rnd.randint(0, w),
                                samples=60, occupancy_sum=float(rnd.randint(0, w * 10)), occupancy_max=rnd.randint(0, max(1, w // 3))))
    # événements fictifs
    for kind, title, cam, ago in (("camera_offline", "Caméra Couloir (démo) hors ligne", "couloir", 410),
                                  ("camera_online", "Caméra Couloir (démo) rétablie", "couloir", 405),
                                  ("zone_overcrowded", "Salle (capacité 3) : 4 personnes", "salle", 220),
                                  ("zone_intrusion", "Intrusion : Local technique", "couloir", 95)):
        db.add(Event(type=kind, severity="warning", camera_id=cam, title=title, timestamp=now - timedelta(minutes=ago), payload=json.dumps({"demo": True})))
    db.commit()
    return {"detections": rows, "passages": passages}


def reset(db) -> None:
    """Efface les données produites (détections, événements, alertes, trajets, affluence, dossiers, index d'enregistrements)
    puis recrée l'historique fictif. Garde caméras, personnes fictives et zones. Uniquement en démonstration."""
    assert_isolated()
    for model in (DossierItem, Dossier, AlertAction, Alert, Event, TargetMove, ZoneStat, Detection, Recording):
        db.query(model).delete()
    db.commit()
    backfill(db)
