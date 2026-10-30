import unittest
from datetime import timedelta
from types import SimpleNamespace

import tests  # noqa: F401
from backend.app.db import SessionLocal, Detection, init_db, utcnow
from backend.app.db_logger import DBLogger


def event(track=1, mode="UNK", cam="entre", **extra):
    return {"camera_id": cam, "track_id": track, "mode": mode, **extra}


class DBLoggerTests(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal()
        s.query(Detection).delete()
        s.commit()
        s.close()
        self.cam = SimpleNamespace(latest_events=[])
        self.logger = DBLogger(lambda: {"entre": self.cam}, log_every_sec=0.8, retention_days=30)

    def count(self):
        s = SessionLocal()
        try:
            return s.query(Detection).count()
        finally:
            s.close()

    def test_events_are_throttled_per_track(self):
        self.cam.latest_events = [event(track=1), event(track=2)]
        self.assertEqual(self.logger.log_once(now=100.0), 2)
        self.assertEqual(self.logger.log_once(now=100.3), 0)   # trop tôt
        self.assertEqual(self.logger.log_once(now=101.0), 2)
        self.assertEqual(self.count(), 4)

    def test_fields_are_stored(self):
        self.cam.latest_events = [event(mode="DB", label="Awa", db_score=0.61, x=0.2, y=0.4, global_id=7)]
        self.logger.log_once(now=100.0)
        s = SessionLocal()
        d = s.query(Detection).one()
        self.assertEqual((d.mode, d.label, d.global_id), ("DB", "Awa", 7))
        self.assertAlmostEqual(d.db_score, 0.61)
        s.close()

    def test_old_keys_are_forgotten(self):
        self.logger.last = {("c", i, "UNK"): 0.0 for i in range(600)}
        self.cam.latest_events = [event()]
        self.logger.log_once(now=1000.0)
        self.assertLess(len(self.logger.last), 10)

    def test_retention_purge_runs_at_most_once_per_hour(self):
        s = SessionLocal()
        s.add(Detection(camera_id="a", mode="UNK", timestamp=utcnow() - timedelta(days=90)))
        s.commit()
        s.close()
        self.assertEqual(self.logger.purge_if_due(now=10_000.0), 1)
        self.assertEqual(self.logger.purge_if_due(now=10_100.0), 0)

    def test_retention_zero_keeps_everything(self):
        keep = DBLogger(lambda: {}, retention_days=0)
        self.assertEqual(keep.purge_if_due(now=10_000.0), 0)


if __name__ == "__main__":
    unittest.main()
