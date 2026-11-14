import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (create_engine, Column, Integer, String, LargeBinary, Boolean,
                        DateTime, Float, Text, inspect, text)
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DB_URL

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def utcnow() -> datetime:
    """Heure UTC *naïve* (même convention que les données déjà en base).

    Remplace ``datetime.utcnow()``, déprécié depuis Python 3.12.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_iso_utc(dt: datetime) -> str:
    """Sérialise une date UTC naïve avec le suffixe « Z » (non ambigu pour le navigateur)."""
    return dt.isoformat() + "Z"


def display_name(nom: str, prenom: Optional[str] = None) -> str:
    """« Prénom Nom ». Les fiches créées avant NiifYéén 2.0 n'ont pas de prénom : le nom complet y est dans ``nom``."""
    return f"{(prenom or '').strip()} {(nom or '').strip()}".strip()


class Participant(Base):
    """Personne enrôlée. C'est son ``id`` (jamais son nom) qui relie détections et événements."""
    __tablename__ = "participants"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    prenom = Column(String, nullable=True)
    matricule = Column(String, nullable=True)            # référence facultative, unique (sans tenir compte de la casse)
    infos = Column(Text, nullable=True)
    actif = Column(Boolean, nullable=False, default=True)   # inactif = n'est plus autorisée : une détection fait réagir la supervision
    date_enrolement = Column(DateTime, default=utcnow)
    date_consentement = Column(DateTime, default=utcnow)     # date à laquelle le consentement a été déclaré
    updated_at = Column(DateTime, default=utcnow)

    @property
    def display_name(self) -> str:
        return display_name(self.nom, self.prenom)


class EmbeddingRef(Base):
    __tablename__ = "embeddings_reference"
    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer, nullable=False)
    embedding = Column(LargeBinary, nullable=False)  # float32 bytes
    source = Column(String, nullable=True)


class Detection(Base):
    __tablename__ = "detections"
    id = Column(Integer, primary_key=True)

    camera_id = Column(String, nullable=False)
    timestamp = Column(DateTime, default=utcnow)

    track_id = Column(Integer, nullable=True)
    global_id = Column(Integer, nullable=True)

    mode = Column(String, nullable=False)  # TARGET / DB / UNK
    label = Column(String, nullable=True)

    target_score = Column(Float, nullable=True)
    db_score = Column(Float, nullable=True)

    x = Column(Float, nullable=True)
    y = Column(Float, nullable=True)

    # Liaison par identifiant (NiifYéén 2.0) : mode « DB » seulement. Les anciennes lignes sont rattachées à la migration.
    participant_id = Column(Integer, nullable=True, index=True)
    match_status = Column(String, nullable=True)         # probable | a_verifier


class TargetMove(Base):
    """Une étape du trajet de la cible : première identification, puis chaque changement de caméra."""
    __tablename__ = "target_moves"
    id = Column(Integer, primary_key=True)
    session = Column(Integer, nullable=False, index=True)   # une session = une recherche de cible
    camera_id = Column(String, nullable=False)
    timestamp = Column(DateTime, default=utcnow, index=True)
    score = Column(Float, nullable=True)
    label = Column(String, nullable=True)                    # « Cible 2 »


class Event(Base):
    """Événement de supervision (cible repérée, caméra hors ligne, personne reconnue…).

    Fondation du centre d'alertes : un événement est un fait horodaté ; une alerte en sera un
    événement qui demande l'attention de l'opérateur.
    """
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    type = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False, default="info")     # info | warning | critical
    camera_id = Column(String, nullable=True, index=True)
    title = Column(String, nullable=False)
    payload = Column(Text, nullable=True)                         # JSON : détails propres au type


class Camera(Base):
    """Caméra configurée (source de vérité depuis NiifYéén 2.0 ; ``cameras.json`` ne sert qu'à l'import initial)."""
    __tablename__ = "cameras"
    id = Column(String, primary_key=True)                 # identifiant stable : référencé par détections, événements…
    site_id = Column(String, nullable=False, default="principal")   # préparation du multi-sites
    label = Column(String, nullable=False)
    location = Column(String, nullable=True)              # zone du bâtiment
    # « 0 » (webcam) ou une URL. Peut contenir un jeton ou des identifiants : l'API ne la renvoie jamais en clair.
    source = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    x = Column(Float, nullable=True)                      # position sur le plan (0-1) ; vide = emplacement par défaut
    y = Column(Float, nullable=True)
    capture_width = Column(Integer, nullable=True)        # résolution demandée aux webcams ; vide = 1280 x 720
    capture_height = Column(Integer, nullable=True)
    process_fps = Column(Float, nullable=True)            # cadence de traitement maximale ; vide = pas de limite
    floor_id = Column(Integer, nullable=True)             # étage du plan où la caméra est placée ; vide = non placée
    heading = Column(Float, nullable=True)                # direction du regard, en degrés (0 = haut du plan, sens horaire)
    fov = Column(Float, nullable=True)                    # ouverture du champ de vision, en degrés
    view_range = Column(Float, nullable=True)             # portée du champ de vision, fraction de la largeur du plan
    position = Column(Integer, nullable=False, default=0)   # ordre d'affichage
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class Floor(Base):
    """Étage du site : un plan (image PNG/JPEG ou SVG assaini) sur lequel on place les caméras."""
    __tablename__ = "floors"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    level = Column(Integer, nullable=False, default=0)    # ordre d'affichage (0 = rez-de-chaussée)
    mime = Column(String, nullable=False)
    image = Column(LargeBinary, nullable=False)
    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class Zone(Base):
    """Ligne virtuelle (2 points) ou zone (polygone) tracée sur l'image d'une caméra, en coordonnées 0-1."""
    __tablename__ = "zones"
    id = Column(Integer, primary_key=True)
    camera_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)                 # line | zone
    name = Column(String, nullable=False)
    points = Column(Text, nullable=False)                 # JSON [[x, y], ...]
    label_in = Column(String, nullable=True)              # ligne : nom du passage vers le côté « 1 » (ex. « Entrée »)
    label_out = Column(String, nullable=True)             # ligne : nom du passage inverse (ex. « Sortie »)
    capacity = Column(Integer, nullable=True)             # zone : seuil d'affluence (alerte au-delà)
    restricted = Column(Boolean, nullable=False, default=False)   # zone : toute entrée est une intrusion
    max_dwell_sec = Column(Integer, nullable=True)        # zone : présence prolongée au-delà de ce seuil (secondes)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow)


class ZoneStat(Base):
    """Affluence agrégée par tranche de 5 minutes (une ligne par zone ou ligne et par tranche)."""
    __tablename__ = "zone_stats"
    id = Column(Integer, primary_key=True)
    zone_id = Column(Integer, nullable=False, index=True)
    bucket = Column(DateTime, nullable=False, index=True)  # début de la tranche (UTC)
    entries = Column(Integer, nullable=False, default=0)   # ligne : passages sens 1 ; zone : entrées
    exits = Column(Integer, nullable=False, default=0)     # ligne : passages sens 2 ; zone : sorties
    samples = Column(Integer, nullable=False, default=0)
    occupancy_sum = Column(Float, nullable=False, default=0.0)
    occupancy_max = Column(Integer, nullable=False, default=0)


class Alert(Base):
    """Alerte : un événement qui demande l'attention de l'opérateur (Module 7).

    Plusieurs événements identiques rapprochés dans le temps se regroupent en une seule alerte (``count``).
    """
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, nullable=True, index=True)          # premier événement à l'origine de l'alerte
    created_at = Column(DateTime, default=utcnow, index=True)
    last_at = Column(DateTime, default=utcnow, index=True)         # dernier événement regroupé
    type = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False, index=True)          # target | people | camera | analytics
    priority = Column(String, nullable=False, default="medium", index=True)   # low | medium | high | critical
    status = Column(String, nullable=False, default="new", index=True)        # new | seen | acknowledged | closed
    status_at = Column(DateTime, default=utcnow)
    camera_id = Column(String, nullable=True, index=True)
    zone_id = Column(Integer, nullable=True)
    participant_id = Column(Integer, nullable=True, index=True)    # pour effacer l'identité si la fiche est supprimée
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    payload = Column(Text, nullable=True)
    count = Column(Integer, nullable=False, default=1)
    dedup_key = Column(String, nullable=False, index=True)
    notified = Column(Text, nullable=True)                         # JSON : résultat par canal externe


class AlertAction(Base):
    """Historique d'une alerte : création, changements d'état, notes, notifications."""
    __tablename__ = "alert_actions"
    id = Column(Integer, primary_key=True)
    alert_id = Column(Integer, nullable=False, index=True)
    timestamp = Column(DateTime, default=utcnow)
    action = Column(String, nullable=False)    # created | seen | acknowledged | closed | reopened | note | notified
    note = Column(Text, nullable=True)


class Dossier(Base):
    """Dossier d'événement : sélection d'événements ou d'alertes choisis par l'opérateur (Module 8)."""
    __tablename__ = "dossiers"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class DossierItem(Base):
    """Élément d'un dossier : une *référence* (jamais une copie) vers un événement ou une alerte.

    Ne pas copier le contenu évite qu'un nom survive à la suppression d'une personne ou à la purge.
    """
    __tablename__ = "dossier_items"
    id = Column(Integer, primary_key=True)
    dossier_id = Column(Integer, nullable=False, index=True)
    kind = Column(String, nullable=False)            # event | alert
    ref_id = Column(Integer, nullable=False)
    note = Column(Text, nullable=True)
    added_at = Column(DateTime, default=utcnow)


class Recording(Base):
    """Segment d'enregistrement vidéo d'une caméra (Module 9). Le fichier est dans le dossier des enregistrements."""
    __tablename__ = "recordings"
    id = Column(Integer, primary_key=True)
    camera_id = Column(String, nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, index=True)
    ended_at = Column(DateTime, nullable=True, index=True)
    trigger = Column(String, nullable=False, default="manual")      # manual | event | schedule
    event_id = Column(Integer, nullable=True, index=True)           # événement déclencheur (trigger = event)
    reason = Column(String, nullable=True)
    path = Column(String, nullable=False)                           # nom du fichier, relatif au dossier
    frames = Column(Integer, nullable=False, default=0)
    bytes = Column(Integer, nullable=False, default=0)
    fps = Column(Float, nullable=True)
    status = Column(String, nullable=False, default="recording")    # recording | done | error


class Authority(Base):
    """Autorité (poste de police, gendarmerie, etc.) qui reçoit sur son téléphone les alertes de cible des caméras proches."""
    __tablename__ = "authorities"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    service = Column(String, nullable=True)               # ex. « Commissariat central »
    phone = Column(String, nullable=True)                 # numéro à contacter (affiché, non utilisé pour un envoi automatique)
    telegram_chat_id = Column(String, nullable=True)      # destinataire de l'envoi réel sur téléphone (Telegram)
    cameras = Column(Text, nullable=True)                 # JSON : identifiants de caméras rattachées (prioritaire sur la distance)
    x = Column(Float, nullable=True)                      # position sur la carte (0-1) : sert au calcul de « la plus proche »
    y = Column(Float, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    inbox_token = Column(String, nullable=True, index=True)   # secret du lien d'accès à la boîte de réception interne
    created_at = Column(DateTime, default=utcnow)


class Dispatch(Base):
    """Journal des envois aux autorités : qui a reçu quoi, quand, et si l'envoi est réel ou simulé."""
    __tablename__ = "dispatches"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    event_id = Column(Integer, nullable=True)
    target_label = Column(String, nullable=True)
    camera_id = Column(String, nullable=True)
    authority_id = Column(Integer, nullable=True, index=True)
    authority_name = Column(String, nullable=True)
    channel = Column(String, nullable=False)              # telegram | simulation
    status = Column(String, nullable=False)               # envoyé | simulé | échec ...
    message = Column(Text, nullable=True)                 # texte exact transmis
    hidden = Column(Integer, default=0)                   # 1 = supprimé de la boîte de l'autorité (le journal le garde)


class Setting(Base):
    """Réglages persistants de l'application (clé / valeur)."""
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=True)


# Colonnes ajoutées après la première version du schéma : table -> {colonne: type SQL}.
_ADDED_COLUMNS = {
    "detections": {
        "track_id": "INTEGER", "global_id": "INTEGER",
        "mode": "VARCHAR", "label": "VARCHAR",
        "target_score": "FLOAT", "db_score": "FLOAT",
        "x": "FLOAT", "y": "FLOAT",
        "participant_id": "INTEGER", "match_status": "VARCHAR",
    },
    "participants": {
        "prenom": "VARCHAR", "matricule": "VARCHAR", "actif": "BOOLEAN NOT NULL DEFAULT 1",
        "date_enrolement": "DATETIME", "updated_at": "DATETIME",
    },
    "target_moves": {"label": "VARCHAR"},
    "zones": {"max_dwell_sec": "INTEGER"},
    "authorities": {"inbox_token": "VARCHAR"},
    "dispatches": {"hidden": "INTEGER DEFAULT 0"},
    "cameras": {"floor_id": "INTEGER", "heading": "FLOAT", "fov": "FLOAT", "view_range": "FLOAT"},
}


def init_db():
    """Crée les tables manquantes et met à niveau une ancienne base SQLite.

    À appeler explicitement au démarrage de l'application (et non à l'import).
    """
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        for table, wanted in _ADDED_COLUMNS.items():
            existing = {c["name"] for c in inspect(connection).get_columns(table)}
            for name, sql_type in wanted.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_detections_timestamp ON detections (timestamp)"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_detections_participant_id ON detections (participant_id)"))
        # Fiches existantes : date d'enrôlement = date de consentement (la seule connue).
        connection.execute(text("UPDATE participants SET date_enrolement = COALESCE(date_enrolement, date_consentement, CURRENT_TIMESTAMP)"))
        connection.execute(text("UPDATE participants SET updated_at = COALESCE(updated_at, date_enrolement)"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_participants_matricule ON participants (lower(matricule)) "
            "WHERE matricule IS NOT NULL"))
        _backfill_participant_ids(connection)


_BACKFILL_KEY = "detections_participant_backfill"


def _backfill_participant_ids(connection) -> int:
    """Rattache une seule fois les anciennes détections « DB » (reliées par le nom) à l'identifiant de la personne.

    Une ligne dont le nom correspond à plusieurs personnes (homonymes) n'est pas rattachée : on ne devine pas.
    """
    if connection.execute(text("SELECT 1 FROM settings WHERE key = :k"), {"k": _BACKFILL_KEY}).first() is not None:
        return 0
    result = connection.execute(text(
        "UPDATE detections SET participant_id = (SELECT p.id FROM participants p WHERE p.nom = detections.label) "
        "WHERE mode = 'DB' AND participant_id IS NULL AND label IS NOT NULL "
        "AND (SELECT COUNT(*) FROM participants p WHERE p.nom = detections.label) = 1"))
    connection.execute(text("INSERT INTO settings (key, value) VALUES (:k, '1')"), {"k": _BACKFILL_KEY})
    return result.rowcount or 0


def purge_old_detections(days: int) -> int:
    """Supprime observations, trajets et événements plus anciens que ``days`` jours (0 = ne rien supprimer).

    Retourne le nombre d'observations supprimées."""
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        deleted = db.query(Detection).filter(Detection.timestamp < cutoff).delete(
            synchronize_session=False)
        db.query(TargetMove).filter(TargetMove.timestamp < cutoff).delete(synchronize_session=False)
        db.query(Event).filter(Event.timestamp < cutoff).delete(synchronize_session=False)
        old_alerts = [a.id for a in db.query(Alert.id).filter(Alert.last_at < cutoff).all()]
        for i in range(0, len(old_alerts), 500):
            chunk = old_alerts[i:i + 500]
            db.query(AlertAction).filter(AlertAction.alert_id.in_(chunk)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(chunk)).delete(synchronize_session=False)
        db.commit()
        return deleted
    finally:
        db.close()


PERSON_EVENT_TYPES = ("person_recognized", "person_inactive_seen")


def anonymise_person_events(db, participant_id: int) -> int:
    """Efface le nom et l'identifiant d'une personne dans les événements de supervision. Retourne le nombre modifié."""
    needle_a, needle_b = f'%"participant_id": {int(participant_id)},%', f'%"participant_id": {int(participant_id)}}}%'
    rows = (db.query(Event).filter(Event.type.in_(PERSON_EVENT_TYPES))
            .filter((Event.payload.like(needle_a)) | (Event.payload.like(needle_b))).all())
    changed = 0
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except ValueError:
            continue
        if payload.get("participant_id") != participant_id:
            continue
        row.title = "Personne reconnue (fiche supprimée)"
        row.payload = json.dumps({"anonymised": True})
        changed += 1
    return changed


def delete_participant(db, participant_id: int):
    """Supprime une personne enrôlée, ses empreintes, et efface son identité du journal et des événements.

    Retourne ``None`` si elle n'existe pas, sinon un dict de compteurs.
    """
    p = db.get(Participant, participant_id)
    if p is None:
        return None
    names = {n for n in (p.nom, p.display_name) if n}
    others = {n for other in db.query(Participant).filter(Participant.id != participant_id).all()
              for n in (other.nom, other.display_name) if n}
    embeddings = db.query(EmbeddingRef).filter(
        EmbeddingRef.participant_id == participant_id).delete(synchronize_session=False)
    wipe = {Detection.label: None, Detection.mode: "UNK", Detection.participant_id: None, Detection.match_status: None}
    anonymised = db.query(Detection).filter(Detection.participant_id == participant_id).update(
        wipe, synchronize_session=False)
    # Anciennes lignes jamais rattachées (homonymes à la migration) : par le nom, sauf s'il appartient à quelqu'un d'autre.
    for name in names - others:
        anonymised += db.query(Detection).filter(
            Detection.mode == "DB", Detection.participant_id.is_(None), Detection.label == name).update(
            wipe, synchronize_session=False)
    events_anonymised = anonymise_person_events(db, participant_id)
    db.query(Alert).filter(Alert.participant_id == participant_id).update(
        {"title": "Personne reconnue (fiche supprimée)", "description": None, "payload": json.dumps({"anonymised": True}),
         "participant_id": None}, synchronize_session=False)
    db.delete(p)
    db.commit()
    return {"embeddings_deleted": embeddings, "detections_anonymised": anonymised,
            "events_anonymised": events_anonymised}


def epoch_to_utc(ts: float) -> datetime:
    """Secondes Unix -> datetime UTC naïf."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def save_target_move(session: int, camera_id: str, ts: float, score=None, label=None) -> None:
    db = SessionLocal()
    try:
        db.add(TargetMove(session=int(session), camera_id=str(camera_id),
                          timestamp=epoch_to_utc(ts),
                          score=float(score) if score is not None else None,
                          label=label))
        db.commit()
    finally:
        db.close()
