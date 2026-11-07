import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from sqlalchemy import create_engine, inspect, text

import tests  # noqa: F401
from backend.app import db as dbmod
from backend.app.db import (SessionLocal, Participant, EmbeddingRef, Detection, init_db,
                            purge_old_detections, delete_participant, utcnow, to_iso_utc)


def reset_tables():
    init_db()
    s = SessionLocal()
    for model in (Detection, EmbeddingRef, Participant):
        s.query(model).delete()
    s.commit()
    s.close()


class DbTests(unittest.TestCase):
    def setUp(self):
        reset_tables()

    def test_purge_removes_only_old_rows(self):
        s = SessionLocal()
        s.add_all([Detection(camera_id="a", mode="UNK", timestamp=utcnow() - timedelta(days=40)),
                   Detection(camera_id="a", mode="UNK", timestamp=utcnow() - timedelta(days=1))])
        s.commit()
        s.close()
        self.assertEqual(purge_old_detections(30), 1)
        s = SessionLocal()
        self.assertEqual(s.query(Detection).count(), 1)
        s.close()

    def test_purge_disabled_when_zero(self):
        s = SessionLocal()
        s.add(Detection(camera_id="a", mode="UNK", timestamp=utcnow() - timedelta(days=999)))
        s.commit()
        s.close()
        self.assertEqual(purge_old_detections(0), 0)

    def test_delete_participant_removes_embeddings_and_names_in_journal(self):
        s = SessionLocal()
        p = Participant(nom="Awa")
        other = Participant(nom="Moussa")
        s.add_all([p, other])
        s.flush()
        s.add_all([EmbeddingRef(participant_id=p.id, embedding=b"x"),
                   EmbeddingRef(participant_id=other.id, embedding=b"y"),
                   Detection(camera_id="a", mode="DB", label="Awa"),
                   Detection(camera_id="a", mode="DB", label="Moussa")])
        s.commit()
        result = delete_participant(s, p.id)
        self.assertEqual(result, {"embeddings_deleted": 1, "detections_anonymised": 1, "events_anonymised": 0})
        self.assertEqual(s.query(Participant).count(), 1)
        self.assertEqual(s.query(EmbeddingRef).count(), 1)
        labels = sorted((d.label or "-", d.mode) for d in s.query(Detection).all())
        self.assertEqual(labels, [("-", "UNK"), ("Moussa", "DB")])
        self.assertIsNone(delete_participant(s, 9999))
        s.close()

    def test_iso_suffix(self):
        self.assertTrue(to_iso_utc(utcnow()).endswith("Z"))

    def test_old_target_moves_table_gets_its_label_column(self):
        path = os.path.join(tempfile.mkdtemp(), "old2.db")
        old = create_engine(f"sqlite:///{path}")
        with old.begin() as c:   # version précédente : sans la colonne « label »
            c.execute(text("CREATE TABLE target_moves (id INTEGER PRIMARY KEY, session INTEGER NOT NULL, "
                           "camera_id VARCHAR NOT NULL, timestamp DATETIME, score FLOAT)"))
            c.execute(text("INSERT INTO target_moves (session, camera_id) VALUES (1, 'entre')"))
        with mock.patch.object(dbmod, "engine", old):
            init_db()
        with old.connect() as c:
            columns = {r["name"] for r in inspect(c).get_columns("target_moves")}
            self.assertIn("label", columns)
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM target_moves")).scalar(), 1)   # données conservées
        old.dispose()

    def test_init_db_upgrades_an_old_database(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = create_engine(f"sqlite:///{path}")
        with old.begin() as c:   # ancien schéma : sans track_id, mode, x, y, ...
            c.execute(text("CREATE TABLE detections (id INTEGER PRIMARY KEY, camera_id VARCHAR NOT NULL, timestamp DATETIME)"))
        with mock.patch.object(dbmod, "engine", old):
            init_db()
        columns = {c["name"] for c in inspect(old).get_columns("detections")}
        self.assertTrue({"track_id", "global_id", "mode", "label",
                         "target_score", "db_score", "x", "y"} <= columns)
        old.dispose()


if __name__ == "__main__":
    unittest.main()
