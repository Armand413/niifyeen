import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import numpy as np

import tests  # noqa: F401
from backend.app import config, demo
from backend.app.db import (Alert, Detection, Event, Participant, SessionLocal, TargetMove, Zone, ZoneStat, init_db, utcnow)

ROOT = Path(__file__).resolve().parents[1]


class IsolationTests(unittest.TestCase):
    def run_config(self, **env):
        code = "from backend.app import config; print(config.DEMO, config.DB_PATH.name, config.RECORDINGS_DIR.name)"
        full = {k: v for k, v in os.environ.items() if not k.startswith("NIIFYEEN_")}
        full.update(env)
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), env=full, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.split()

    def test_demo_ignores_real_database_variable(self):
        got = self.run_config(NIIFYEEN_DEMO="1", NIIFYEEN_DB="/tmp/reel.db")
        self.assertEqual(got, ["True", "niifyeen-demo.db", "recordings-demo"])

    def test_normal_mode_unchanged(self):
        self.assertEqual(self.run_config(NIIFYEEN_DB="/tmp/reel.db"), ["False", "reel.db", "recordings"])
        self.assertEqual(self.run_config()[1], "niifyeen.db")

    def test_assert_isolated(self):
        with self.assertRaises(RuntimeError):
            demo.assert_isolated()                                   # démo non activée dans ce processus
        old = (config.DEMO, config.DB_PATH)
        self.addCleanup(lambda: (setattr(config, "DEMO", old[0]), setattr(config, "DB_PATH", old[1])))
        config.DEMO, config.DB_PATH = True, Path("niifyeen.db")
        with self.assertRaises(RuntimeError):
            demo.assert_isolated()
        config.DB_PATH = Path("niifyeen-demo.db")
        demo.assert_isolated()

    def test_seed_and_reset_refuse_outside_demo(self):
        db = SessionLocal()
        self.addCleanup(db.close)
        for fn in (demo.seed, demo.backfill, demo.reset):
            with self.subTest(fn=fn.__name__), self.assertRaises(RuntimeError):
                fn(db)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.sc = demo.Scenario(speed=1.0, clock=lambda: self.t[0])
        self.sc.target_id = 1
        self.sc.people_ids = [11, 12, 13]

    def at(self, sec, loop=0):
        self.t[0] = 1000.0 + loop * demo.PERIOD_SEC + sec

    def test_position_loops(self):
        self.at(30)
        self.assertEqual(self.sc.position(), (30.0, 0))
        self.at(30, loop=2)
        self.assertEqual(self.sc.position(), (30.0, 2))

    def test_speed(self):
        sc = demo.Scenario(speed=4.0, clock=lambda: self.t[0])
        self.t[0] = 1000.0 + 10
        self.assertEqual(sc.position()[0], 40.0)
        self.assertEqual(demo.Scenario(speed=0.0).speed, 0.25)

    def test_events_have_pipeline_fields(self):
        self.at(10)                                                  # visiteur + cible à l'entrée
        evs = self.sc.events("entre", 10.0, 0, 1010.0)
        self.assertEqual(sorted(e["mode"] for e in evs), ["TARGET", "UNK"])
        keys = {"camera_id", "track_id", "mode", "label", "target_id", "participant_id", "match_status", "target_score", "db_score",
                "x", "y", "fx", "fy", "bbox", "ts"}
        for e in evs:
            self.assertTrue(keys <= set(e), keys - set(e))
            self.assertTrue(0 <= e["fx"] <= 1 and 0 <= e["fy"] <= 1)
            x1, y1, x2, y2 = e["bbox"]
            self.assertTrue(x1 < x2 and y1 < y2)
        target = next(e for e in evs if e["mode"] == "TARGET")
        self.assertEqual((target["target_id"], target["label"]), (1, "Cible 1"))

    def test_enrolled_person_carries_participant_id(self):
        evs = self.sc.events("entre", 28.0, 0, 0)
        p = [e for e in evs if e["mode"] == "DB"]
        self.assertEqual(len(p), 1)
        self.assertEqual((p[0]["participant_id"], p[0]["match_status"], p[0]["label"]), (11, "probable", "Alice (démo)"))

    def test_target_becomes_unknown_without_registry_target(self):
        self.sc.target_id = None
        self.assertNotIn("TARGET", [e["mode"] for e in self.sc.events("entre", 10.0, 0, 0)])

    def test_track_ids_change_each_loop(self):
        a = {e["track_id"] for e in self.sc.events("entre", 10.0, 0, 0)}
        b = {e["track_id"] for e in self.sc.events("entre", 10.0, 1, 0)}
        self.assertFalse(a & b)

    def test_scripted_moments(self):
        crowd = self.sc.events("salle", 90.0, 0, 0)
        self.assertEqual(len(crowd), 4)                              # dépasse la capacité de 3
        self.assertEqual(len(self.sc.events("couloir", 103.0, 0, 0)), 1)
        self.assertTrue(self.sc.outage("couloir", 110.0))
        self.assertFalse(self.sc.outage("couloir", 50.0))
        self.assertFalse(self.sc.outage("salle", 110.0))
        self.assertEqual(self.sc.events("entre", 119.0, 0, 0), [])

    def test_line_is_really_crossed(self):
        """Le visiteur passe de la gauche à la droite de la ligne x = 0,5 : le moteur d'analyse comptera un passage."""
        xs = [self.sc.events("entre", 4.0 + k, 0, 0)[0]["fx"] for k in range(12) if self.sc.events("entre", 4.0 + k, 0, 0)]
        self.assertLess(min(xs), 0.5)
        self.assertGreater(max(xs), 0.5)

    def test_restricted_zone_is_entered(self):
        fxs = [e["fx"] for k in range(10) for e in self.sc.events("couloir", 98.0 + k, 0, 0)]
        self.assertGreater(max(fxs), 0.65)

    def test_script_view(self):
        rows = demo.script_view()
        self.assertEqual(rows, sorted(rows, key=lambda r: r["t"]))
        self.assertTrue(all(r["text"] for r in rows))


class SimCameraTests(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.sc = demo.Scenario(clock=lambda: self.t[0])
        self.sc.target_id = 1
        self.cam = demo.SimCamera("entre", self.sc, (96, 84, 70), "Entrée")

    def test_publishes_frames_and_events(self):
        self.t[0] = 1010.0
        self.assertTrue(self.cam.step(1010.0))
        self.assertTrue(self.cam.latest_jpeg.startswith(b"\xff\xd8"))
        self.assertEqual(self.cam.frame_seq, 1)
        self.assertIsNone(self.cam.latest_jpeg_raw)                  # brute seulement à la demande
        self.assertEqual(len(self.cam.latest_events), 2)
        self.assertEqual(set(self.cam.frame_stats), {"mean", "std", "crc", "ts"})

    def test_raw_frame_on_demand_and_watermark(self):
        import cv2
        self.cam.add_raw_viewer()
        self.assertEqual(self.cam.raw_viewers, 1)
        self.t[0] = 1010.0
        self.cam.step(1010.0)
        self.assertEqual(self.cam.raw_seq, self.cam.frame_seq)
        raw = cv2.imdecode(np.frombuffer(self.cam.latest_jpeg_raw, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(raw.shape[:2], (config.STREAM_SIZE[1], config.STREAM_SIZE[0]))
        self.cam.remove_raw_viewer(); self.cam.remove_raw_viewer()
        self.assertEqual(self.cam.raw_viewers, 0)

    def test_raw_frame_has_no_label_text_but_annotated_does(self):
        import cv2
        self.cam.add_raw_viewer()
        self.t[0] = 1010.0
        self.cam.step(1010.0)
        raw = cv2.imdecode(np.frombuffer(self.cam.latest_jpeg_raw, np.uint8), cv2.IMREAD_COLOR).astype(int)
        ann = cv2.imdecode(np.frombuffer(self.cam.latest_jpeg, np.uint8), cv2.IMREAD_COLOR).astype(int)
        self.assertGreater(np.abs(raw - ann).sum(), 1000)            # cadres et noms seulement sur l'image annotée

    def test_outage_stops_publishing_and_goes_offline(self):
        cam = demo.SimCamera("couloir", self.sc, (84, 78, 96))
        cam.start(); self.addCleanup(cam.stop)
        cam.stop()                                                   # on pilote les images à la main
        cam.running = True
        self.assertTrue(cam.step(1050.0))
        self.assertEqual(cam.last_frame_ts, 1050.0)
        before = cam.frame_seq
        self.assertFalse(cam.step(1110.0))                           # 110 s : panne simulée
        self.assertEqual(cam.frame_seq, before)
        self.assertEqual(cam.latest_events, [])
        self.assertEqual(cam.last_frame_ts, 1050.0)
        self.assertEqual(cam.status(1050.0 + 1), "online")
        self.assertEqual(cam.status(1050.0 + 20), "offline")

    def test_empty_scene_images_differ(self):
        """Deux images d'une scène vide ne doivent pas avoir la même empreinte (sinon : fausse alerte « flux figé »)."""
        crcs = set()
        for k in range(5):
            self.t[0] = 1000.0 + 119.0
            self.cam.step(1119.0 + k)
            self.assertEqual(self.cam.latest_events, [])
            crcs.add(self.cam.frame_stats["crc"])
        self.assertEqual(len(crcs), 5)

    def test_stats_shape_matches_pipeline(self):
        cam = demo.SimCamera("entre", self.sc)
        cam.start(); self.addCleanup(cam.stop)
        cam.step(5000.0)
        s = cam.stats(5000.5)
        self.assertEqual(s["status"], "online")
        self.assertEqual({"status", "fps", "latency_ms", "persons", "width", "height", "age_sec", "reconnects"}, set(s))
        self.assertEqual(demo.SimCamera("x", self.sc).status(), "stopped")


class NullNotifierTests(unittest.TestCase):
    def test_never_wants_to_notify(self):
        n = demo.NullNotifier()
        n.start(); n.stop()
        self.assertFalse(n.wanted({"priority": "critical"}, {"notify": True}))


class DataTests(unittest.TestCase):
    """Données de départ et réinitialisation, avec la base de test (la démonstration est simulée comme activée)."""

    def setUp(self):
        init_db()
        self.old = (config.DEMO, config.DB_PATH)
        config.DEMO, config.DB_PATH = True, Path("niifyeen-demo.db")
        self.addCleanup(lambda: (setattr(config, "DEMO", self.old[0]), setattr(config, "DB_PATH", self.old[1])))
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        for m in (ZoneStat, Zone, TargetMove, Event, Alert, Detection, Participant):
            self.db.query(m).delete()
        self.db.commit()

    def test_seed_is_idempotent(self):
        ids = demo.seed(self.db)["people_ids"]
        again = demo.seed(self.db)["people_ids"]
        self.assertEqual(ids, again)
        self.assertEqual(self.db.query(Participant).count(), 3)
        self.assertEqual(self.db.query(Zone).count(), 3)
        p = self.db.get(Participant, ids[0])
        self.assertEqual((p.display_name, p.matricule), ("Alice (démo)", "DEMO-001"))
        self.assertEqual(self.db.query(Zone).filter(Zone.restricted.is_(True)).count(), 1)

    def test_backfill_creates_history_once(self):
        demo.seed(self.db)
        now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
        first = demo.backfill(self.db, now=now)
        self.assertGreater(first["detections"], 200)
        self.assertGreater(first["passages"], 20)
        self.assertEqual(demo.backfill(self.db, now=now), {"detections": 0, "passages": 0})
        latest = max(d.timestamp for d in self.db.query(Detection).all())
        self.assertLessEqual(latest, now + timedelta(seconds=30))
        self.assertGreater(self.db.query(TargetMove).count(), 0)
        self.assertGreater(self.db.query(Event).count(), 0)
        self.assertGreater(self.db.query(ZoneStat).count(), 0)

    def test_backfill_is_deterministic(self):
        demo.seed(self.db)
        now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
        a = demo.backfill(self.db, now=now)
        self.db.query(Detection).delete(); self.db.query(TargetMove).delete(); self.db.query(Event).delete(); self.db.query(ZoneStat).delete(); self.db.commit()
        self.assertEqual(demo.backfill(self.db, now=now), a)

    def test_backfill_is_consistent_with_statistics(self):
        from backend.app import stats
        demo.seed(self.db)
        now = utcnow()
        demo.backfill(self.db, now=now)
        o = stats.overview(self.db, now - timedelta(hours=25), now + timedelta(hours=1))
        self.assertGreater(o["passages"], 20)
        self.assertGreaterEqual(o["unique_enrolled"], 1)
        self.assertLessEqual(o["unique_enrolled"], 3)

    def test_reset_clears_produced_data_but_keeps_fixtures(self):
        demo.seed(self.db)
        demo.backfill(self.db)
        self.db.add(Event(type="zone_intrusion", severity="warning", title="x"))
        self.db.commit()
        self.db.query(Detection).filter(Detection.id > 0).update({"label": "AVANT"})
        self.db.commit()
        demo.reset(self.db)
        self.assertEqual(self.db.query(Detection).filter(Detection.label == "AVANT").count(), 0)
        self.assertEqual(self.db.query(Participant).count(), 3)
        self.assertEqual(self.db.query(Zone).count(), 3)
        self.assertGreater(self.db.query(Detection).count(), 0)


if __name__ == "__main__":
    unittest.main()
