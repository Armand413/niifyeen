from sqlalchemy import create_engine, Column, Integer, String, LargeBinary, DateTime, Float, Text, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

DB_URL = "sqlite:///./niifyeen.db"

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Participant(Base):
    __tablename__ = "participants"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    infos = Column(Text, nullable=True)
    date_consentement = Column(DateTime, default=datetime.utcnow)


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
    timestamp = Column(DateTime, default=datetime.utcnow)

    track_id = Column(Integer, nullable=True)
    global_id = Column(Integer, nullable=True)

    mode = Column(String, nullable=False)  # TARGET / DB / UNK
    label = Column(String, nullable=True)

    target_score = Column(Float, nullable=True)
    db_score = Column(Float, nullable=True)

    x = Column(Float, nullable=True)
    y = Column(Float, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    # Older SQLite databases predate these nullable observation fields.
    # Preserve their rows and original columns while updating compatibility.
    if engine.dialect.name == "sqlite":
        columns = {column["name"] for column in inspect(engine).get_columns("detections")}
        additions = {
            "track_id": "INTEGER", "global_id": "INTEGER",
            "mode": "VARCHAR", "label": "VARCHAR",
            "target_score": "FLOAT", "db_score": "FLOAT",
        }
        with engine.begin() as connection:
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE detections ADD COLUMN {name} {sql_type}"))
