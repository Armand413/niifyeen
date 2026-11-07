"""Centre d'alertes (Module 7).

Une alerte est un événement de supervision qui demande l'attention de l'opérateur. Chaque type d'événement
a une **règle** (activée ou non, priorité, délai de regroupement, son, notification externe). Les événements
identiques rapprochés dans le temps sont regroupés en une seule alerte (``count``) pour éviter la répétition.

États : ``new`` -> ``seen`` (consultée) -> ``acknowledged`` (acquittée) -> ``closed`` (clôturée). Une alerte
clôturée peut être rouverte (retour à ``seen``). Tout changement est conservé dans l'historique de l'alerte.

Notifications externes (e-mail SMTP, Telegram) : facultatives, configurées **uniquement** par variables
d'environnement (aucun secret en base ni dans l'API). Elles partent dans un thread à part, ne contiennent
par défaut aucune identité, et sont limitées en nombre par minute.
"""
import json
import logging
import os
import queue
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import or_

from .db import Alert, AlertAction, Setting, SessionLocal, to_iso_utc, utcnow

log = logging.getLogger(__name__)

PRIORITIES = ("low", "medium", "high", "critical")
PRIORITY_LABELS = {"low": "Faible", "medium": "Moyenne", "high": "Haute", "critical": "Critique"}
STATUSES = ("new", "seen", "acknowledged", "closed")
STATUS_LABELS = {"new": "Nouvelle", "seen": "Consultée", "acknowledged": "Acquittée", "closed": "Clôturée"}
CATEGORIES = {"target": "Cibles", "people": "Personnes", "camera": "Caméras", "analytics": "Lignes et zones"}

# type d'événement -> (libellé, catégorie)
TYPES = {
    "target_seen": ("Cible repérée", "target"),
    "target_moved": ("Cible passée d'une caméra à une autre", "target"),
    "target_lost": ("Cible perdue de vue", "target"),
    "person_recognized": ("Personne reconnue", "people"),
    "person_inactive_seen": ("Personne désactivée reconnue", "people"),
    "camera_offline": ("Caméra hors ligne", "camera"),
    "camera_obstructed": ("Caméra masquée ou image uniforme", "camera"),
    "camera_frozen": ("Flux figé", "camera"),
    "line_crossed": ("Franchissement de ligne", "analytics"),
    "zone_overcrowded": ("Seuil de fréquentation dépassé", "analytics"),
    "zone_intrusion": ("Intrusion en zone interdite", "analytics"),
    "zone_loitering": ("Présence prolongée en zone", "analytics"),
}

# Règles par défaut. Les franchissements de ligne et les reconnaissances simples sont désactivés : ils sont
# très fréquents et ont leur propre page (compteurs, historique) ; l'opérateur peut les activer.
DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    "target_seen": dict(enabled=True, priority="medium", dedup_sec=60, sound=True, notify=False),
    "target_moved": dict(enabled=True, priority="medium", dedup_sec=30, sound=True, notify=False),
    "target_lost": dict(enabled=True, priority="low", dedup_sec=60, sound=False, notify=False),
    "person_recognized": dict(enabled=False, priority="low", dedup_sec=120, sound=False, notify=False),
    "person_inactive_seen": dict(enabled=True, priority="high", dedup_sec=300, sound=True, notify=True),
    "camera_offline": dict(enabled=True, priority="high", dedup_sec=120, sound=True, notify=True),
    "camera_obstructed": dict(enabled=True, priority="high", dedup_sec=300, sound=True, notify=True),
    "camera_frozen": dict(enabled=True, priority="high", dedup_sec=300, sound=True, notify=True),
    "line_crossed": dict(enabled=False, priority="low", dedup_sec=10, sound=False, notify=False),
    "zone_overcrowded": dict(enabled=True, priority="high", dedup_sec=300, sound=True, notify=True),
    "zone_intrusion": dict(enabled=True, priority="critical", dedup_sec=20, sound=True, notify=True),
    "zone_loitering": dict(enabled=True, priority="medium", dedup_sec=120, sound=True, notify=False),
}
RULES_KEY = "alert_rules"
MAX_DEDUP_SEC = 86400
MAX_NOTE = 500
MAX_BULK = 200
# Clés du contenu d'un événement reprises dans l'identité (regroupement) d'une alerte.
_KEY_FIELDS = ("zone_id", "target_id", "participant_id")


class AlertError(Exception):
    def __init__(self, message: str, status: int = 422, field: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.field = field


# ---- règles ---------------------------------------------------------------------------------------------------------
def validate_rule(type_: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise AlertError("Règle invalide.", field=type_)
    unknown = set(data) - {"enabled", "priority", "dedup_sec", "sound", "notify"}
    if unknown:
        raise AlertError(f"Réglage inconnu : {sorted(unknown)[0]}", field=type_)
    out: Dict[str, Any] = {}
    for key in ("enabled", "sound", "notify"):
        if key in data:
            if not isinstance(data[key], bool):
                raise AlertError(f"« {key} » doit être vrai ou faux.", field=type_)
            out[key] = data[key]
    if "priority" in data:
        if data["priority"] not in PRIORITIES:
            raise AlertError("Priorité inconnue (low, medium, high, critical).", field=type_)
        out["priority"] = data["priority"]
    if "dedup_sec" in data:
        v = data["dedup_sec"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_DEDUP_SEC:
            raise AlertError(f"Le délai de regroupement doit être un entier de 0 à {MAX_DEDUP_SEC} secondes.", field=type_)
        out["dedup_sec"] = v
    return out


def load_rules(db) -> Dict[str, Dict[str, Any]]:
    rules = {k: dict(v) for k, v in DEFAULT_RULES.items()}
    row = db.get(Setting, RULES_KEY)
    if row and row.value:
        try:
            saved = json.loads(row.value)
        except ValueError:
            saved = {}
        for type_, data in (saved.items() if isinstance(saved, dict) else []):
            if type_ in rules:
                try:
                    rules[type_].update(validate_rule(type_, data))
                except AlertError:
                    continue                                  # réglage enregistré devenu invalide : valeur par défaut
    return rules


def save_rules(db, changes: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(changes, dict) or not changes:
        raise AlertError("Aucune règle à enregistrer.", field="rules")
    clean = {}
    for type_, data in changes.items():
        if type_ not in TYPES:
            raise AlertError(f"Type d'alerte inconnu : {type_}", field=str(type_)[:40])
        clean[type_] = validate_rule(type_, data)
    rules = load_rules(db)
    for type_, data in clean.items():
        rules[type_].update(data)
    value = json.dumps({k: v for k, v in rules.items()})
    row = db.get(Setting, RULES_KEY)
    if row is None:
        db.add(Setting(key=RULES_KEY, value=value))
    else:
        row.value = value
    db.commit()
    return rules


def rules_view(rules: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"type": t, "label": TYPES[t][0], "category": TYPES[t][1], **rules[t]} for t in TYPES]


# ---- lecture ---------------------------------------------------------------------------------------------------------
def view(a: Alert) -> Dict[str, Any]:
    try:
        payload = json.loads(a.payload) if a.payload else {}
    except ValueError:
        payload = {}
    try:
        notified = json.loads(a.notified) if a.notified else {}
    except ValueError:
        notified = {}
    return {"id": a.id, "event_id": a.event_id, "created_at": to_iso_utc(a.created_at), "last_at": to_iso_utc(a.last_at),
            "type": a.type, "type_label": TYPES.get(a.type, (a.type, ""))[0], "category": a.category,
            "category_label": CATEGORIES.get(a.category, a.category), "priority": a.priority,
            "status": a.status, "status_at": to_iso_utc(a.status_at), "camera_id": a.camera_id, "zone_id": a.zone_id,
            "title": a.title, "description": a.description, "payload": payload, "count": a.count, "notified": notified}


def _parse_time(value: Optional[str], field: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AlertError("Date invalide (format ISO 8601 attendu).", field=field)
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def counts(db) -> Dict[str, int]:
    from sqlalchemy import func
    out = {s: 0 for s in STATUSES}
    for status, n in db.query(Alert.status, func.count(Alert.id)).group_by(Alert.status).all():
        if status in out:
            out[status] = n
    return out


def list_alerts(db, status: Optional[str] = None, priority: Optional[str] = None, category: Optional[str] = None,
                camera_id: Optional[str] = None, q: Optional[str] = None, since: Optional[str] = None,
                until: Optional[str] = None, before_id: Optional[int] = None, after_id: Optional[int] = None,
                limit: int = 50) -> List[Dict[str, Any]]:
    """``status`` peut être un état, ou ``open`` (non clôturées). Du plus récent au plus ancien."""
    query = db.query(Alert)
    if status == "open":
        query = query.filter(Alert.status != "closed")
    elif status:
        if status not in STATUSES:
            raise AlertError("État inconnu.", field="status")
        query = query.filter(Alert.status == status)
    if priority:
        if priority not in PRIORITIES:
            raise AlertError("Priorité inconnue.", field="priority")
        query = query.filter(Alert.priority == priority)
    if category:
        if category not in CATEGORIES:
            raise AlertError("Catégorie inconnue.", field="category")
        query = query.filter(Alert.category == category)
    if camera_id:
        query = query.filter(Alert.camera_id == camera_id)
    if q and q.strip():
        like = _like(q.strip()[:100])
        query = query.filter(or_(Alert.title.like(like, escape="\\"), Alert.description.like(like, escape="\\"),
                                 Alert.camera_id.like(like, escape="\\"), Alert.type.like(like, escape="\\")))
    t0, t1 = _parse_time(since, "since"), _parse_time(until, "until")
    if t0:
        query = query.filter(Alert.last_at >= t0)
    if t1:
        query = query.filter(Alert.created_at <= t1)
    if before_id is not None:
        query = query.filter(Alert.id < before_id)
    if after_id is not None:
        query = query.filter(Alert.id > after_id)
    return [view(a) for a in query.order_by(Alert.id.desc()).limit(max(1, min(limit, 200))).all()]


def history(db, alert_id: int) -> List[Dict[str, Any]]:
    rows = db.query(AlertAction).filter(AlertAction.alert_id == alert_id).order_by(AlertAction.id).all()
    return [{"id": r.id, "timestamp": to_iso_utc(r.timestamp), "action": r.action, "note": r.note} for r in rows]


def get_alert(db, alert_id: int) -> Dict[str, Any]:
    a = db.get(Alert, alert_id)
    if a is None:
        raise AlertError("Alerte introuvable.", 404)
    out = view(a)
    out["history"] = history(db, alert_id)
    return out


# ---- changements d'état ------------------------------------------------------------------------------------------------
_ALLOWED = {  # état actuel -> états atteignables
    "new": {"seen", "acknowledged", "closed"},
    "seen": {"acknowledged", "closed"},
    "acknowledged": {"closed"},
    "closed": {"seen"},                                       # réouverture
}


def _clean_note(note: Optional[str]) -> Optional[str]:
    if note is None:
        return None
    if not isinstance(note, str):
        raise AlertError("Note invalide.", field="note")
    note = note.strip()
    if len(note) > MAX_NOTE:
        raise AlertError(f"Note trop longue ({MAX_NOTE} caractères au plus).", field="note")
    return note or None


def set_status(db, alert_id: int, status: str, note: Optional[str] = None) -> Dict[str, Any]:
    if status not in STATUSES or status == "new":
        raise AlertError("État demandé invalide (seen, acknowledged, closed).", field="status")
    note = _clean_note(note)
    a = db.get(Alert, alert_id)
    if a is None:
        raise AlertError("Alerte introuvable.", 404)
    if a.status == status:
        if note:
            db.add(AlertAction(alert_id=a.id, action="note", note=note))
            db.commit()
        return view(a)
    if status not in _ALLOWED.get(a.status, set()):
        raise AlertError(f"Passage de « {STATUS_LABELS[a.status]} » à « {STATUS_LABELS[status]} » impossible.", 409, "status")
    reopened = a.status == "closed"
    a.status, a.status_at = status, utcnow()
    db.add(AlertAction(alert_id=a.id, action="reopened" if reopened else status, note=note))
    db.commit()
    return view(a)


def bulk_status(db, ids: List[int], status: str) -> Dict[str, Any]:
    if not isinstance(ids, list) or not ids or len(ids) > MAX_BULK:
        raise AlertError(f"Choisir de 1 à {MAX_BULK} alertes.", field="ids")
    changed, skipped = 0, 0
    for alert_id in sorted(set(int(i) for i in ids)):
        try:
            a = db.get(Alert, alert_id)
            if a is None or a.status == status:
                skipped += 1
                continue
            set_status(db, alert_id, status)
            changed += 1
        except AlertError:
            db.rollback()
            skipped += 1
    return {"changed": changed, "skipped": skipped}


def add_note(db, alert_id: int, note: str) -> Dict[str, Any]:
    note = _clean_note(note)
    if not note:
        raise AlertError("La note est vide.", field="note")
    a = db.get(Alert, alert_id)
    if a is None:
        raise AlertError("Alerte introuvable.", 404)
    db.add(AlertAction(alert_id=a.id, action="note", note=note))
    db.commit()
    return get_alert(db, alert_id)


def summary(db, after_id: Optional[int] = None) -> Dict[str, Any]:
    """Pour le badge et les notifications dans l'application : compteurs, dernier identifiant, nouveautés."""
    from sqlalchemy import func
    c = counts(db)
    last = db.query(func.max(Alert.id)).scalar() or 0
    fresh: List[Dict[str, Any]] = []
    if after_id is not None:
        rules = load_rules(db)
        rows = db.query(Alert).filter(Alert.id > after_id).order_by(Alert.id.desc()).limit(5).all()
        fresh = [{"id": a.id, "title": a.title, "priority": a.priority, "type_label": TYPES.get(a.type, (a.type,))[0],
                  "sound": bool(rules.get(a.type, {}).get("sound"))} for a in reversed(rows)]
    critical = db.query(func.count(Alert.id)).filter(Alert.status == "new", Alert.priority.in_(("high", "critical"))).scalar() or 0
    return {"counts": c, "new": c["new"], "new_urgent": critical, "last_id": last, "fresh": fresh}


# ---- notifications externes ---------------------------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def notify_settings() -> Dict[str, Any]:
    """Configuration lue dans l'environnement (jamais exposée par l'API, hors indicateurs « configuré »)."""
    e = os.environ.get
    to = [x.strip() for x in (e("NIIFYEEN_SMTP_TO") or "").split(",") if x.strip()]
    return {
        "smtp_host": (e("NIIFYEEN_SMTP_HOST") or "").strip(), "smtp_port": _env_int("NIIFYEEN_SMTP_PORT", 587),
        "smtp_user": e("NIIFYEEN_SMTP_USER") or "", "smtp_password": e("NIIFYEEN_SMTP_PASSWORD") or "",
        "smtp_from": (e("NIIFYEEN_SMTP_FROM") or e("NIIFYEEN_SMTP_USER") or "").strip(), "smtp_to": to,
        "smtp_security": (e("NIIFYEEN_SMTP_SECURITY") or "starttls").strip().lower(),
        "telegram_token": (e("NIIFYEEN_TELEGRAM_TOKEN") or "").strip(),
        "telegram_chat": (e("NIIFYEEN_TELEGRAM_CHAT_ID") or "").strip(),
        "min_priority": (e("NIIFYEEN_NOTIFY_MIN_PRIORITY") or "high").strip().lower(),
        "identities": (e("NIIFYEEN_NOTIFY_IDENTITIES") or "0").strip().lower() in ("1", "true", "oui", "yes"),
        "per_minute": max(1, _env_int("NIIFYEEN_NOTIFY_PER_MINUTE", 10)),
    }


def channels_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """État des canaux, sans aucun secret."""
    return {
        "email": {"configured": bool(cfg["smtp_host"] and cfg["smtp_from"] and cfg["smtp_to"]), "recipients": len(cfg["smtp_to"])},
        "telegram": {"configured": bool(cfg["telegram_token"] and cfg["telegram_chat"])},
        "min_priority": cfg["min_priority"] if cfg["min_priority"] in PRIORITIES else "high",
        "identities": bool(cfg["identities"]),
        "per_minute": cfg["per_minute"],
    }


def send_email(cfg: Dict[str, Any], subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["smtp_from"], ", ".join(cfg["smtp_to"])
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if cfg["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=10, context=ctx)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10)
    try:
        if cfg["smtp_security"] == "starttls":
            server.starttls(context=ctx)
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_telegram(cfg: Dict[str, Any], text: str) -> None:
    data = json.dumps({"chat_id": cfg["telegram_chat"], "text": text[:3500], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as res:
        if res.status != 200:
            raise RuntimeError(f"HTTP {res.status}")


def _safe_error(exc: Exception) -> str:
    """Message sans secret (le jeton Telegram est dans l'URL, le mot de passe SMTP dans la session)."""
    return f"échec ({type(exc).__name__})"


class Notifier:
    """Envoi des notifications externes dans un thread à part, avec plafond par minute."""

    def __init__(self, get_meta: Callable[[], Dict[str, Any]], on_result: Callable[[int, str, str], None],
                 settings: Optional[Callable[[], Dict[str, Any]]] = None):
        self._get_meta = get_meta
        self._on_result = on_result
        self._settings = settings or notify_settings
        self._queue: "queue.Queue" = queue.Queue(maxsize=100)
        self._sent: deque = deque()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="alert-notifier", daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def wanted(self, alert: Dict[str, Any], rule: Dict[str, Any]) -> bool:
        cfg = self._settings()
        st = channels_status(cfg)
        if not (st["email"]["configured"] or st["telegram"]["configured"]):
            return False
        return bool(rule.get("notify")) and PRIORITIES.index(alert["priority"]) >= PRIORITIES.index(st["min_priority"])

    def submit(self, alert: Dict[str, Any]):
        try:
            self._queue.put_nowait(alert)
        except queue.Full:
            self._on_result(alert["id"], "all", "file pleine")

    def message(self, alert: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or self._settings()
        meta = (self._get_meta() or {}).get(alert.get("camera_id") or "", {}) or {}
        camera = meta.get("label") or alert.get("camera_id") or "—"
        subject = f"[NiifYéén] {PRIORITY_LABELS.get(alert['priority'], alert['priority'])} — {alert['type_label']}"
        lines = [subject, f"Caméra : {camera}", f"Heure (UTC) : {alert['last_at']}"]
        if alert.get("count", 1) > 1:
            lines.append(f"Répétée {alert['count']} fois")
        if cfg["identities"] and alert.get("title"):
            lines.append(f"Détail : {alert['title']}")
        return subject, "\n".join(lines)

    def _within_limit(self, cfg: Dict[str, Any]) -> bool:
        now = time.time()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= cfg["per_minute"]:
            return False
        self._sent.append(now)
        return True

    def deliver(self, alert: Dict[str, Any]):
        cfg = self._settings()
        st = channels_status(cfg)
        if not self._within_limit(cfg):
            self._on_result(alert["id"], "all", "limité (trop de notifications par minute)")
            return
        subject, body = self.message(alert, cfg)
        for name, ready, send in (("email", st["email"]["configured"], lambda: send_email(cfg, subject, body)),
                                  ("telegram", st["telegram"]["configured"], lambda: send_telegram(cfg, body))):
            if not ready:
                continue
            try:
                send()
                self._on_result(alert["id"], name, "envoyée")
            except Exception as exc:
                log.warning("Notification %s en échec : %s", name, type(exc).__name__)
                self._on_result(alert["id"], name, _safe_error(exc))

    def test(self, channel: str) -> str:
        """Envoie un message d'essai (synchrone). Retourne « envoyée » ou la raison de l'échec, sans secret."""
        cfg = self._settings()
        st = channels_status(cfg)
        if channel not in ("email", "telegram") or not st[channel]["configured"]:
            return "canal non configuré"
        text = "[NiifYéén] Message d'essai : les notifications fonctionnent."
        try:
            if channel == "email":
                send_email(cfg, "[NiifYéén] Essai", text)
            else:
                send_telegram(cfg, text)
            return "envoyée"
        except Exception as exc:
            log.warning("Essai %s en échec : %s", channel, type(exc).__name__)
            return _safe_error(exc)

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self.deliver(item)
            except Exception:
                log.exception("Notification externe")


# ---- centre d'alertes ----------------------------------------------------------------------------------------------------
class AlertCenter:
    """Transforme les événements en alertes (règles, regroupement, historique) et déclenche les notifications."""

    def __init__(self, feed, get_meta: Callable[[], Dict[str, Any]], notifier: Optional[Notifier] = None):
        self.feed = feed
        self._get_meta = get_meta
        self._lock = threading.Lock()
        self.notifier = notifier or Notifier(get_meta, self.record_notification)
        self._rules: Optional[Dict[str, Dict[str, Any]]] = None
        self._event_lock = threading.Lock()                # évite deux alertes pour deux événements simultanés

    def start(self):
        self.feed.add_listener(self.on_event)
        self.notifier.start()

    def stop(self):
        self.feed.remove_listener(self.on_event)
        self.notifier.stop()

    def rules(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if self._rules is None:
                db = SessionLocal()
                try:
                    self._rules = load_rules(db)
                finally:
                    db.close()
            return {k: dict(v) for k, v in self._rules.items()}

    def invalidate(self):
        with self._lock:
            self._rules = None

    @staticmethod
    def dedup_key(ev: Dict[str, Any]) -> str:
        payload = ev.get("payload") or {}
        parts = [ev.get("type") or "", ev.get("camera_id") or ""] + [str(payload.get(k, "")) for k in _KEY_FIELDS]
        return "|".join(parts)

    def on_event(self, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Crée ou met à jour l'alerte correspondant à un événement. Ne lève jamais."""
        try:
            with self._event_lock:
                return self._on_event(ev)
        except Exception:
            log.exception("Alerte non enregistrée pour l'événement %s", (ev or {}).get("id"))
            return None

    def _on_event(self, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        type_ = ev.get("type")
        rule = self.rules().get(type_)
        if rule is None or not rule["enabled"]:
            return None
        payload = ev.get("payload") or {}
        key = self.dedup_key(ev)
        now = utcnow()
        db = SessionLocal()
        created = None
        try:
            last = db.query(Alert).filter(Alert.dedup_key == key).order_by(Alert.id.desc()).first()
            if (last is not None and last.status != "closed" and rule["dedup_sec"] > 0
                    and (now - (last.last_at or last.created_at)).total_seconds() <= rule["dedup_sec"]):
                last.count = (last.count or 1) + 1
                last.last_at = now
                last.title = ev.get("title") or last.title
                db.commit()
                return view(last)
            zone_id = payload.get("zone_id")
            pid = payload.get("participant_id")
            a = Alert(event_id=ev.get("id"), type=type_, category=TYPES[type_][1], priority=rule["priority"],
                      status="new", camera_id=ev.get("camera_id"),
                      zone_id=zone_id if isinstance(zone_id, int) else None,
                      participant_id=pid if isinstance(pid, int) else None,
                      title=(ev.get("title") or TYPES[type_][0])[:300], description=TYPES[type_][0],
                      payload=json.dumps(payload, ensure_ascii=False, default=str) if payload else None,
                      dedup_key=key, created_at=now, last_at=now, status_at=now)
            db.add(a)
            db.flush()
            db.add(AlertAction(alert_id=a.id, action="created", note=None))
            db.commit()
            created = view(a)
        finally:
            db.close()
        if created and self.notifier.wanted(created, rule):
            self.notifier.submit(created)
        return created

    def record_notification(self, alert_id: int, channel: str, result: str):
        db = SessionLocal()
        try:
            a = db.get(Alert, alert_id)
            if a is None:
                return
            try:
                current = json.loads(a.notified) if a.notified else {}
            except ValueError:
                current = {}
            current[channel] = result
            a.notified = json.dumps(current, ensure_ascii=False)
            db.add(AlertAction(alert_id=alert_id, action="notified", note=f"{channel} : {result}"))
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Résultat de notification non enregistré")
        finally:
            db.close()
