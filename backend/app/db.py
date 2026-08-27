from sqlalchemy import create_engine, Column, Integer, String, LargeBinary, DateTime
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
    infos = Column(String, nullable=True)
    date_consentement = Column(DateTime, default=datetime.utcnow)


class EmbeddingRef(Base):
    __tablename__ = "embeddings_reference"
    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer, nullable=False)
    embedding = Column(LargeBinary, nullable=False)  # float32 bytes
    source = Column(String, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)