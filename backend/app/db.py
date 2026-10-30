from datetime import datetime, timedelta, timezone

from sqlalchemy import (create_engine, Column, Integer, String, LargeBinary,
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


class Participant(Base):
    __tablename__ = "participants"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    infos = Column(Text, nullable=True)
    # Date à laquelle le consentement a été déclaré lors de l'enrôlement.
    date_consentement = Column(DateTime, default=utcnow)


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


# Colonnes ajoutées après la première version du schéma.
_DETECTION_COLUMNS = {
    "track_id": "INTEGER", "global_id": "INTEGER",
    "mode": "VARCHAR", "label": "VARCHAR",
    "target_score": "FLOAT", "db_score": "FLOAT",
    "x": "FLOAT", "y": "FLOAT",
}


def init_db():
    """Crée les tables manquantes et met à niveau une ancienne base SQLite.

    À appeler explicitement au démarrage de l'application (et non à l'import).
    """
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return
    columns = {c["name"] for c in inspect(engine).get_columns("detections")}
    with engine.begin() as connection:
        for name, sql_type in _DETECTION_COLUMNS.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE detections ADD COLUMN {name} {sql_type}"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_detections_timestamp ON detections (timestamp)"))


def purge_old_detections(days: int) -> int:
    """Supprime les observations plus anciennes que ``days`` jours. 0 = ne rien supprimer."""
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    db = SessionLocal()
    try:
        deleted = db.query(Detection).filter(Detection.timestamp < cutoff).delete(
            synchronize_session=False)
        db.commit()
        return deleted
    finally:
        db.close()


def delete_participant(db, participant_id: int):
    """Supprime une personne enrôlée, ses empreintes, et retire son nom du journal.

    Retourne ``None`` si elle n'existe pas, sinon un dict de compteurs.
    """
    p = db.query(Participant).filter(Participant.id == participant_id).first()
    if p is None:
        return None
    nom = p.nom
    embeddings = db.query(EmbeddingRef).filter(
        EmbeddingRef.participant_id == participant_id).delete(synchronize_session=False)
    anonymised = db.query(Detection).filter(
        Detection.mode == "DB", Detection.label == nom).update(
        {Detection.label: None, Detection.mode: "UNK"}, synchronize_session=False)
    db.delete(p)
    db.commit()
    return {"embeddings_deleted": embeddings, "detections_anonymised": anonymised}
