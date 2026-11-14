"""Envoi des alertes de cible à l'autorité la plus proche de la caméra.

Canal par défaut : la **boîte de réception interne** (voir inbox.py). Le message est enregistré, puis l'autorité le lit sur son
téléphone ou son ordinateur, sur le réseau local, avec un lien secret qui lui est propre. Rien ne passe par un service externe.

Quand une cible est repérée (ou passe d'une caméra à une autre), le message est adressé à l'autorité rattachée à la caméra
(sinon à la plus proche sur la carte). Il contient ce que le système sait de la cible : fiche de la personne enrôlée si elle
est reconnue, caméra, heure, derniers passages. Les vecteurs d'empreinte faciale ne sont jamais transmis.

Canaux, affichés tels quels dans le journal :
  * « interne » (par défaut) : le message est déposé dans la boîte de réception de l'autorité ;
  * « telegram » (facultatif, service externe) : en plus, si NIIFYEEN_TELEGRAM_TOKEN et l'identifiant Telegram de l'autorité existent.
Chaque envoi est inscrit dans le journal ; le message reste lisible dans la boîte de l'autorité dans tous les cas.
"""
import json
import logging
import math
import os
import queue
import secrets
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from . import config, events
from .db import Authority, Dispatch, Participant, SessionLocal, Setting, to_iso_utc, utcnow

log = logging.getLogger("niifyeen.dispatch")

TRIGGER_TYPES = (events.TARGET_SEEN, events.TARGET_MOVED)
ENABLED_KEY = "authority_dispatch"
COOLDOWN_SEC = max(5.0, float(os.getenv("NIIFYEEN_DISPATCH_COOLDOWN_SEC", "120") or 120))
SEND_PHOTO = (os.getenv("NIIFYEEN_DISPATCH_PHOTO", "1").strip().lower() not in ("0", "false", "non", "no"))
MAX_NAME = 80


class DispatchError(Exception):
    def __init__(self, message: str, status: int = 422, field: Optional[str] = None):
        super().__init__(message)
        self.message, self.status, self.field = message, status, field


# ---- réglage général -------------------------------------------------------------------------------------------------
def is_enabled(db) -> bool:
    row = db.get(Setting, ENABLED_KEY)
    return True if row is None else (row.value or "1") == "1"


LAN_KEY = "inbox_lan"


def lan_enabled(db) -> bool:
    row = db.get(Setting, LAN_KEY)
    return row is not None and (row.value or "0") == "1"


def set_lan_enabled(db, value: bool) -> bool:
    row = db.get(Setting, LAN_KEY)
    if row is None:
        db.add(Setting(key=LAN_KEY, value="1" if value else "0"))
    else:
        row.value = "1" if value else "0"
    db.commit()
    return bool(value)


def set_enabled(db, value: bool) -> bool:
    row = db.get(Setting, ENABLED_KEY)
    if row is None:
        db.add(Setting(key=ENABLED_KEY, value="1" if value else "0"))
    else:
        row.value = "1" if value else "0"
    db.commit()
    return bool(value)


def telegram_ready() -> bool:
    return bool((os.getenv("NIIFYEEN_TELEGRAM_TOKEN") or "").strip())


# ---- autorités -------------------------------------------------------------------------------------------------------
def _clean_text(value: Any, field: str, maxlen: int, required: bool = False) -> Optional[str]:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DispatchError("Valeur invalide.", field=field)
    value = value.strip()
    if required and not value:
        raise DispatchError("Ce champ est obligatoire.", field=field)
    if len(value) > maxlen:
        raise DispatchError(f"Limité à {maxlen} caractères.", field=field)
    return value or None


def _clean_coord(value: Any, field: str) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise DispatchError("Nombre attendu.", field=field)
    if not 0.0 <= f <= 1.0:
        raise DispatchError("Valeur comprise entre 0 et 1 attendue.", field=field)
    return f


def new_token() -> str:
    return secrets.token_urlsafe(24)


def ensure_tokens(db) -> int:
    """Donne un lien secret aux autorités qui n'en ont pas encore (anciennes lignes). Retourne le nombre créé."""
    count = 0
    for a in db.query(Authority).filter(Authority.inbox_token.is_(None)).all():
        a.inbox_token = new_token()
        count += 1
    if count:
        db.commit()
    return count


def clean_authority(data: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "name" in data or not partial:
        out["name"] = _clean_text(data.get("name"), "name", MAX_NAME, required=True)
    for key, maxlen in (("service", 80), ("phone", 30), ("telegram_chat_id", 40)):
        if key in data:
            out[key] = _clean_text(data.get(key), key, maxlen)
    if out.get("phone") and not all(c in "+0123456789 .-()" for c in out["phone"]):
        raise DispatchError("Numéro de téléphone invalide.", field="phone")
    if out.get("telegram_chat_id") and not all(c in "-0123456789" for c in out["telegram_chat_id"]):
        raise DispatchError("Identifiant Telegram invalide (nombre).", field="telegram_chat_id")
    for key in ("x", "y"):
        if key in data:
            out[key] = _clean_coord(data.get(key), key)
    if "enabled" in data:
        out["enabled"] = bool(data.get("enabled"))
    if "cameras" in data:
        cams = data.get("cameras") or []
        if not isinstance(cams, list) or len(cams) > 50 or not all(isinstance(c, str) and 0 < len(c) <= 60 for c in cams):
            raise DispatchError("Liste de caméras invalide.", field="cameras")
        out["cameras"] = sorted(set(cams))
    return out


def authority_view(a: Authority) -> Dict[str, Any]:
    try:
        cams = json.loads(a.cameras) if a.cameras else []
    except ValueError:
        cams = []
    return {"id": a.id, "name": a.name, "service": a.service or "", "phone": a.phone or "",
            "telegram_chat_id": a.telegram_chat_id or "", "cameras": cams, "x": a.x, "y": a.y,
            "enabled": bool(a.enabled), "channel": "telegram" if (telegram_ready() and a.telegram_chat_id) else "interne",
            "inbox_token": a.inbox_token}


def nearest_authority(authorities: List[Dict[str, Any]], camera_id: str,
                      camera_meta: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Autorité rattachée à la caméra ; sinon la plus proche sur la carte (distance sur le plan) ; sinon aucune."""
    active = [a for a in authorities if a["enabled"]]
    for a in active:
        if camera_id in a["cameras"]:
            return a
    meta = (camera_meta or {}).get(camera_id) or {}
    cx, cy = meta.get("x"), meta.get("y")
    if cx is None or cy is None:
        return None
    placed = [a for a in active if a["x"] is not None and a["y"] is not None]
    if not placed:
        return None
    return min(placed, key=lambda a: math.hypot(a["x"] - cx, a["y"] - cy))


# ---- message ---------------------------------------------------------------------------------------------------------
def _clock(ts: Any) -> str:
    if isinstance(ts, (int, float)):
        return datetime.utcfromtimestamp(ts).strftime("%H:%M:%S UTC")
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime("%H:%M:%S UTC")
    except ValueError:
        return str(ts)


def compose_message(db, ev: Dict[str, Any], target, camera_meta: Dict[str, Dict[str, Any]]) -> str:
    """Texte envoyé à l'autorité. Jamais de vecteur d'empreinte ; l'identité n'apparaît que si la fiche est connue."""
    payload = ev.get("payload") or {}
    cam = ev.get("camera_id") or ""
    meta = (camera_meta or {}).get(cam) or {}
    place = meta.get("label") or cam or "—"
    where = f"{place} ({meta['location']})" if meta.get("location") else place
    label = payload.get("target_label") or "Cible"
    lines = [f"ALERTE — {label} détectée", f"Lieu : {where}", f"Heure : {_clock(ev.get('timestamp'))}"]
    if payload.get("score") is not None:
        try:
            lines.append(f"Confiance du système : {float(payload['score']):.0%} (estimation, à vérifier sur place)")
        except (TypeError, ValueError):
            pass
    if ev.get("type") == events.TARGET_MOVED and payload.get("from_camera"):
        prev = ((camera_meta or {}).get(payload["from_camera"]) or {}).get("label") or payload["from_camera"]
        lines.append(f"Vue juste avant : {prev}")

    match = getattr(target, "match", None) if target is not None else None
    if match and match.get("matched") and config.HISTORY_SHOW_IDENTITIES:
        person = db.get(Participant, match["participant_id"])
        if person is not None:
            lines.append("")
            lines.append("Personne reconnue dans la base :")
            lines.append(f"  Nom : {person.display_name}")
            if person.matricule:
                lines.append(f"  Référence : {person.matricule}")
            if person.infos:
                lines.append(f"  Informations : {person.infos[:400]}")
            lines.append(f"  Fiche active : {'oui' if person.actif else 'non'}")
            lines.append(f"  Correspondance : {float(match.get('score') or 0):.0%} (peut être fausse)")
    elif match is not None:
        lines.append("")
        lines.append("Personne non enregistrée dans la base : identité inconnue du système.")

    if target is not None:
        path = target.tracker.snapshot().get("target_path") or []
        if path:
            lines.append("")
            lines.append("Derniers passages :")
            for step in path[-5:]:
                step_cam = ((camera_meta or {}).get(step.get("camera_id")) or {}).get("label") or step.get("camera_id")
                lines.append(f"  {_clock(step.get('ts') or step.get('timestamp') or '')} — {step_cam}")
    lines.append("")
    lines.append("Photos jointes : gros plan de la cible et image de la caméra à cet instant (cible encadrée).")
    lines.append("Message automatique NiifYéén (prototype). Vérifier l'identité avant toute action.")
    return "\n".join(lines)[:3500]


# ---- envoi -----------------------------------------------------------------------------------------------------------
def _telegram_post(method: str, fields: Dict[str, str], photo: Optional[bytes] = None) -> None:
    token = (os.getenv("NIIFYEEN_TELEGRAM_TOKEN") or "").strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    if photo is None:
        req = urllib.request.Request(url, data=json.dumps(fields).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    else:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="cible.jpg"\r\n'
                 f'Content-Type: image/jpeg\r\n\r\n').encode() + photo + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=10) as res:
        if res.status != 200:
            raise RuntimeError(f"HTTP {res.status}")


def send_to_authority(authority: Dict[str, Any], text: str, photos: Optional[List[tuple]] = None) -> (str, str):
    """Retourne (canal, statut). Sans jeton ou sans identifiant Telegram : dépôt dans la boîte interne, rien ne sort du réseau.

    ``photos`` : liste de (légende, JPEG) ; la première porte le texte du message en légende, les suivantes sont envoyées à la suite.
    """
    if not (telegram_ready() and authority.get("telegram_chat_id")):
        return "interne", "déposé"
    photos = [p for p in (photos or []) if p and p[1]] if SEND_PHOTO else []
    try:
        chat = authority["telegram_chat_id"]
        if photos:
            _telegram_post("sendPhoto", {"chat_id": chat, "caption": text[:1000]}, photos[0][1])
            if len(text) > 1000:
                _telegram_post("sendMessage", {"chat_id": chat, "text": text[:3500]})
            for caption, extra in photos[1:3]:
                _telegram_post("sendPhoto", {"chat_id": chat, "caption": caption}, extra)
        else:
            _telegram_post("sendMessage", {"chat_id": chat, "text": text[:3500], "disable_web_page_preview": True})
        return "telegram", "envoyé"
    except Exception as exc:               # le jeton est dans l'URL : on ne journalise que le type d'erreur
        log.warning("Envoi à l'autorité en échec : %s", type(exc).__name__)
        return "telegram", f"échec ({type(exc).__name__})"


def log_dispatch(db, authority: Dict[str, Any], channel: str, status: str, message: str, ev: Optional[Dict[str, Any]] = None,
                 target_label: Optional[str] = None) -> Dict[str, Any]:
    row = Dispatch(event_id=(ev or {}).get("id"), target_label=target_label, camera_id=(ev or {}).get("camera_id"),
                   authority_id=authority["id"], authority_name=authority["name"], channel=channel, status=status,
                   message=message)
    db.add(row)
    db.commit()
    return dispatch_view(row)


class PhotoStore:
    """Photos des envois récents, gardées en mémoire seulement (jamais sur disque ni en base) et oubliées au bout d'un moment."""

    def __init__(self, capacity: int = 60, ttl_sec: float = 3600.0):
        self.capacity, self.ttl = capacity, ttl_sec
        self._items: Dict[int, tuple] = {}
        self._lock = threading.Lock()

    def put(self, dispatch_id: int, photos: List[tuple], now: Optional[float] = None) -> None:
        """``photos`` : liste de (légende, JPEG)."""
        now = time.time() if now is None else now
        with self._lock:
            self._items[dispatch_id] = (now, [p for p in photos if p and p[1]])
            for key in [k for k, (t, _) in self._items.items() if now - t > self.ttl]:
                del self._items[key]
            while len(self._items) > self.capacity:
                del self._items[min(self._items)]

    def forget(self, dispatch_ids) -> None:
        with self._lock:
            for k in dispatch_ids:
                self._items.pop(k, None)

    def labels(self, dispatch_id: int, now: Optional[float] = None) -> List[str]:
        return [caption for caption, _ in self._entry(dispatch_id, now)]

    def get_all(self, dispatch_id: int, now: Optional[float] = None) -> List[bytes]:
        return [data for _, data in self._entry(dispatch_id, now)]

    def _entry(self, dispatch_id: int, now: Optional[float]) -> List[tuple]:
        now = time.time() if now is None else now
        with self._lock:
            item = self._items.get(dispatch_id)
            return list(item[1]) if item and now - item[0] <= self.ttl else []


PHOTOS = PhotoStore()
ACK_STATUS = "pris en charge"


def inbox_messages(db, authority_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    rows = (db.query(Dispatch).filter(Dispatch.authority_id == authority_id)
            .filter((Dispatch.hidden.is_(None)) | (Dispatch.hidden == 0))
            .order_by(Dispatch.id.desc()).limit(limit).all())
    out = []
    for d in rows:
        v = dispatch_view(d)
        v["photos"] = PHOTOS.labels(d.id)          # légendes des photos encore disponibles (mémoire seulement)
        v["acknowledged"] = d.status == ACK_STATUS
        out.append(v)
    return out


def acknowledge(db, authority_id: int, dispatch_id: int) -> bool:
    row = db.get(Dispatch, dispatch_id)
    if row is None or row.authority_id != authority_id:
        return False
    row.status = ACK_STATUS
    db.commit()
    return True


def hide_message(db, authority_id: int, dispatch_id: int) -> bool:
    """Retire une alerte de la boîte de l'autorité (et ses photos en mémoire). Le journal de l'application la conserve."""
    row = db.get(Dispatch, dispatch_id)
    if row is None or row.authority_id != authority_id:
        return False
    row.hidden = 1
    db.commit()
    PHOTOS.forget([dispatch_id])
    return True


def hide_all(db, authority_id: int) -> int:
    rows = (db.query(Dispatch).filter(Dispatch.authority_id == authority_id)
            .filter((Dispatch.hidden.is_(None)) | (Dispatch.hidden == 0)).all())
    for r in rows:
        r.hidden = 1
    db.commit()
    PHOTOS.forget([r.id for r in rows])
    return len(rows)


def dispatch_view(d: Dispatch) -> Dict[str, Any]:
    return {"id": d.id, "created_at": to_iso_utc(d.created_at), "target_label": d.target_label or "",
            "camera_id": d.camera_id or "", "authority_id": d.authority_id, "authority_name": d.authority_name or "",
            "channel": d.channel, "status": d.status, "message": d.message or ""}


class Dispatcher:
    """Écoute les événements de cible et prévient l'autorité concernée (thread à part, délai minimal par cible et caméra)."""

    def __init__(self, get_meta: Callable[[], Dict[str, Any]], get_target: Callable[[int], Any],
                 get_snapshot: Optional[Callable[[str], Optional[bytes]]] = None,
                 get_crop: Optional[Callable[[str, int], Optional[bytes]]] = None):
        self._get_meta, self._get_target = get_meta, get_target
        self._get_snapshot = get_snapshot or (lambda camera_id: None)
        self._get_crop = get_crop or (lambda camera_id, target_id: None)
        self._queue: "queue.Queue" = queue.Queue(maxsize=100)
        self._last: Dict[Any, float] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self, feed):
        self._feed = feed
        feed.add_listener(self.on_event)
        self._thread = threading.Thread(target=self._run, name="authority-dispatch", daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._feed.remove_listener(self.on_event)
        except Exception:
            pass
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def on_event(self, ev: Dict[str, Any]):
        if (ev or {}).get("type") not in TRIGGER_TYPES:
            return
        try:
            self._queue.put_nowait(ev)
        except queue.Full:
            log.warning("File d'envoi aux autorités pleine")

    def _run(self):
        while True:
            ev = self._queue.get()
            if ev is None:
                return
            try:
                self.handle(ev)
            except Exception:
                log.exception("Envoi à l'autorité")

    def _collect_photos(self, ev: Dict[str, Any], target, target_id) -> List[tuple]:
        """Images prises par la caméra à l'instant de la détection : gros plan de la personne et vue d'ensemble (cible encadrée).
        La photo de référence envoyée pour définir la cible n'est jamais transmise."""
        photos: List[tuple] = []
        camera_id = ev.get("camera_id") or ""
        for caption, getter in (("Gros plan de la cible", lambda: self._get_crop(camera_id, target_id) if target_id is not None else None),
                                ("Image de la caméra", lambda: self._get_snapshot(camera_id))):
            try:
                data = getter()
            except Exception:
                data = None
            if data:
                photos.append((caption, data))
        return photos

    def handle(self, ev: Dict[str, Any], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        now = time.time() if now is None else now
        payload = ev.get("payload") or {}
        key = (payload.get("target_id"), ev.get("camera_id"))
        if key in self._last and now - self._last[key] < COOLDOWN_SEC:
            return None
        meta = self._get_meta() or {}
        db = SessionLocal()
        try:
            if not is_enabled(db):
                return None
            authorities = [authority_view(a) for a in db.query(Authority).order_by(Authority.id).all()]
            authority = nearest_authority(authorities, ev.get("camera_id") or "", meta)
            if authority is None:
                return None
            self._last[key] = now
            target = self._get_target(payload.get("target_id")) if payload.get("target_id") is not None else None
            text = compose_message(db, ev, target, meta)
            photos = self._collect_photos(ev, target, payload.get("target_id"))
            channel, status = send_to_authority(authority, text, photos)
            names = ", ".join(c.lower() for c, _ in photos)
            shown = text + (f"\n\n[{len(photos)} photo(s) jointe(s) : {names}]" if photos
                            else "\n\n[Aucune photo disponible pour cet envoi]")
            logged = log_dispatch(db, authority, channel, status, shown, ev, payload.get("target_label"))
            PHOTOS.put(logged["id"], photos)
            return logged
        finally:
            db.close()
