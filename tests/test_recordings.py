import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

import tests  # noqa: F401
from tests.fakes import FakeCamera
from backend.app import config, events, recordings as rec
from backend.app.db import Event, Recording, Setting, SessionLocal, epoch_to_utc, init_db, utcnow


def jpeg(color=(40, 60, 90), size=(64, 48), text=None):
    img = np.full((size[1], size[0], 3), color, np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def reset():
    init_db()
    s = SessionLocal()
    s.query(Recording).delete()
    s.query(Setting).filter(Setting.key == rec.SETTINGS_KEY).delete()
    s.query(Event).delete()
    s.commit(); s.close()


class Base(unittest.TestCase):
    T0 = 1_800_000_000.0                        # horloge simulée

    def setUp(self):
        reset()
        self.dir = Path(tempfile.mkdtemp(prefix="nyr-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.now = self.T0
        self.local = datetime(2026, 1, 5, 10, 0)           # un lundi
        self.cam = FakeCamera(camera_id="c1")
        self.cams = {"c1": self.cam}
        self.feed = events.EventFeed()
        self.r = rec.Recorder(lambda: self.cams, self.feed, directory=self.dir, clock=lambda: self.now,
                              now_local=lambda: self.local, min_free_bytes=0, hard_disabled=False)
        self.addCleanup(self.r.stop)

    def configure(self, **kw):
        db = SessionLocal()
        try:
            rec.save_settings(db, kw)
        finally:
            db.close()
        self.r.reload()

    def frame(self, dt=0.6, color=(40, 60, 90), annotated=b"ANNOTE-NOM-SECRET"):
        """Avance l'horloge, publie une image brute et fait tourner l'enregistreur."""
        self.now += dt
        self.cam.publish(annotated=annotated, raw=jpeg(color))
        self.cam.last_frame_ts = self.now                 # publish() met l'heure réelle : on impose l'heure simulée
        self.r.step(self.now)

    def rows(self):
        s = SessionLocal()
        try:
            return s.query(Recording).order_by(Recording.id).all()
        finally:
            s.close()


class SettingsTests(Base):
    def test_defaults_are_off_and_private(self):
        s = rec.load_settings(SessionLocal())
        self.assertFalse(s["enabled"])
        self.assertEqual((s["fps"], s["retention_days"]), (2, 7))

    def test_validation(self):
        bad = [{"fps": 0}, {"fps": 11}, {"fps": True}, {"fps": "2"}, {"pre_roll_sec": 31}, {"post_roll_sec": 4}, {"retention_days": 0},
               {"retention_days": 366}, {"max_storage_mb": 99}, {"enabled": "oui"}, {"event_types": ["nope"]}, {"event_types": "x"},
               {"cameras": [""]}, {"cameras": "c1"}, {"inconnu": 1}, "x",
               {"schedules": [{"camera_id": "*", "days": [], "start": "08:00", "end": "09:00"}]},
               {"schedules": [{"camera_id": "*", "days": [7], "start": "08:00", "end": "09:00"}]},
               {"schedules": [{"camera_id": "*", "days": [1, 1], "start": "08:00", "end": "09:00"}]},
               {"schedules": [{"camera_id": "*", "days": [1], "start": "8:00", "end": "09:00"}]},
               {"schedules": [{"camera_id": "*", "days": [1], "start": "08:00", "end": "08:00"}]},
               {"schedules": [{"camera_id": "*", "days": [1], "start": "24:00", "end": "09:00"}]},
               {"schedules": [{"camera_id": "*", "days": [1], "start": "08:00", "end": "09:00", "x": 1}]},
               {"schedules": [{"camera_id": "*", "days": [1], "start": "08:00", "end": "09:00"}] * 21}]
        for data in bad:
            with self.subTest(data=str(data)[:60]), self.assertRaises(rec.RecordingError):
                rec.validate_settings(data)

    def test_save_is_partial_and_persistent(self):
        self.configure(fps=5)
        self.configure(retention_days=3)
        s = rec.load_settings(SessionLocal())
        self.assertEqual((s["fps"], s["retention_days"], s["enabled"]), (5, 3, False))
        with self.assertRaises(rec.RecordingError):
            self.configure()

    def test_hard_disable_cannot_be_overridden(self):
        with mock.patch.object(config, "RECORDING_HARD_DISABLED", True):
            with self.assertRaises(rec.RecordingError) as ctx:
                self.configure(enabled=True)
            self.assertEqual(ctx.exception.status, 403)
            self.configure(fps=3)                              # le reste reste modifiable

    def test_corrupted_saved_settings_fall_back(self):
        s = SessionLocal(); s.add(Setting(key=rec.SETTINGS_KEY, value="{oups")); s.commit(); s.close()
        self.assertFalse(rec.load_settings(SessionLocal())["enabled"])

    def test_schedule_logic(self):
        sch = [{"camera_id": "c1", "days": [0], "start": "08:00", "end": "12:00"},
               {"camera_id": "*", "days": [4], "start": "22:00", "end": "06:00"}]
        at = lambda d, h, m: datetime(2026, 1, 5 + d, h, m)             # 5 janvier 2026 = lundi
        self.assertTrue(rec.schedule_active(sch, "c1", at(0, 8, 0)))
        self.assertTrue(rec.schedule_active(sch, "c1", at(0, 11, 59)))
        self.assertFalse(rec.schedule_active(sch, "c1", at(0, 12, 0)))      # fin exclue
        self.assertFalse(rec.schedule_active(sch, "c2", at(0, 9, 0)))       # autre caméra
        self.assertFalse(rec.schedule_active(sch, "c1", at(1, 9, 0)))       # autre jour
        self.assertTrue(rec.schedule_active(sch, "c9", at(4, 23, 0)))       # vendredi soir
        self.assertTrue(rec.schedule_active(sch, "c9", at(5, 5, 59)))       # samedi petit matin (plage de vendredi)
        self.assertFalse(rec.schedule_active(sch, "c9", at(5, 6, 0)))
        self.assertFalse(rec.schedule_active(sch, "c9", at(4, 5, 0)))       # vendredi matin : plage de jeudi, absente


class DisabledTests(Base):
    def test_nothing_happens_when_disabled(self):
        for _ in range(5):
            self.frame()
        self.assertEqual(self.cam.raw_viewers, 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(self.dir.iterdir()), [])
        with self.assertRaises(rec.RecordingError) as ctx:
            self.r.start_manual("c1", 5)
        self.assertEqual(ctx.exception.status, 409)

    def test_hard_disabled_recorder_refuses(self):
        self.r.hard_disabled = True
        with self.assertRaises(rec.RecordingError) as ctx:
            self.r.start_manual("c1", 5)
        self.assertEqual(ctx.exception.status, 403)
        self.assertFalse(self.r.enabled())


class ManualTests(Base):
    def setUp(self):
        super().setUp()
        self.configure(enabled=True, fps=2, event_types=[])

    def test_validation(self):
        for cam, minutes, status in (("zz", 5, 404), ("c1", 0, 422), ("c1", 241, 422), ("c1", True, 422), ("c1", "5", 422)):
            with self.subTest(cam=cam, minutes=minutes), self.assertRaises(rec.RecordingError) as ctx:
                self.r.start_manual(cam, minutes)
            self.assertEqual(ctx.exception.status, status)

    def test_records_raw_frames_only_at_the_chosen_rate(self):
        self.r.start_manual("c1", 5)
        self.frame(0.1)                       # active la prise d'images brutes
        for _ in range(20):
            self.frame(0.1)                   # 10 images/s publiées, 2/s enregistrées
        self.r.stop_manual("c1"); self.frame(0.1)
        (row,) = self.rows()
        self.assertEqual((row.status, row.trigger, row.camera_id), ("done", "manual", "c1"))
        self.assertTrue(3 <= row.frames <= 6, row.frames)
        data = (self.dir / row.path).read_bytes()
        self.assertTrue(data.startswith(rec.MAGIC))
        self.assertNotIn(b"ANNOTE-NOM-SECRET", data)            # jamais l'image annotée (noms incrustés)
        self.assertEqual(len(rec.read_index(self.dir / row.path)), row.frames)

    def test_expires_by_itself_and_releases_the_raw_stream(self):
        self.r.start_manual("c1", 1)
        for _ in range(4):
            self.frame(0.6)
        self.assertEqual(self.cam.raw_viewers, 1)
        for _ in range(4):
            self.frame(30)
        self.assertEqual(self.rows()[0].status, "done")
        self.assertIsNone(self.r.session_info("c1"))
        self.assertEqual(self.cam.raw_viewers, 0)

    def test_stop_returns_false_when_nothing_runs(self):
        self.assertFalse(self.r.stop_manual("c1"))

    def test_offline_camera_records_nothing(self):
        self.r.start_manual("c1", 5)
        self.frame(0.6)
        self.cam.forced_status = "offline"
        for _ in range(4):
            self.frame(0.6)
        self.r.step(self.now)
        self.assertLessEqual(sum(r.frames for r in self.rows()), 1)

    def test_disabling_closes_everything(self):
        self.r.start_manual("c1", 5)
        for _ in range(4):
            self.frame(0.6)
        self.configure(enabled=False)
        self.r.step(self.now + 1)
        self.assertEqual(self.rows()[0].status, "done")
        self.assertEqual(self.cam.raw_viewers, 0)
        self.assertEqual(self.r.status()["recording"], [])

    def test_removed_camera_closes_its_segment(self):
        self.r.start_manual("c1", 5)
        for _ in range(3):
            self.frame(0.6)
        self.cams.clear()
        self.r.step(self.now + 1)
        self.assertEqual(self.rows()[0].status, "done")

    def test_segments_split(self):
        self.r.segment_sec = 5
        self.r.start_manual("c1", 5)
        for _ in range(30):
            self.frame(0.6)
        rows = self.rows()
        self.assertGreaterEqual(len(rows), 3)
        self.assertTrue(all(r.trigger == "manual" for r in rows))

    def test_camera_allowlist(self):
        self.configure(cameras=["autre"])
        self.r.start_manual("c1", 5)
        for _ in range(4):
            self.frame(0.6)
        self.assertEqual(self.rows(), [])


class EventTests(Base):
    def setUp(self):
        super().setUp()
        self.configure(enabled=True, fps=2, pre_roll_sec=3, post_roll_sec=5, event_types=["zone_intrusion"])
        self.r.start(); self.addCleanup(self.r.stop)
        self.r._stop.set(); self.r._thread.join(1)             # on pilote les pas à la main
        self.r._stop.clear()

    def emit(self, type_="zone_intrusion", cam="c1"):
        return self.feed.emit(type_, "Intrusion : Hall", camera_id=cam, zone_id=1)

    def test_pre_roll_and_post_roll(self):
        for _ in range(8):
            self.frame(0.6)                                    # mémoire tampon seulement : rien d'écrit
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.cam.raw_viewers, 1)
        ev = self.emit()
        for _ in range(4):
            self.frame(0.6)
        (row,) = self.rows()
        self.assertEqual((row.trigger, row.event_id, row.status), ("event", ev["id"], "recording"))
        for _ in range(15):
            self.frame(0.6)                                    # au-delà de la post-roll (5 s)
        (row,) = self.rows()
        self.assertEqual(row.status, "done")
        span = (row.ended_at - row.started_at).total_seconds()
        self.assertGreater(span, 7)                            # ~3 s avant + ~5 s après
        index = rec.read_index(self.dir / row.path)
        self.assertGreaterEqual(index[0][0], self.T0)
        self.assertLess(index[0][0], ev["id"] and self.now)
        self.assertGreater(len([e for e in index if e[0] < index[0][0] + 3.1]), 3)   # des images d'avant l'événement

    def test_repeated_events_extend_one_recording(self):
        for _ in range(3):
            self.frame(0.6)
        self.emit()
        for _ in range(6):
            self.frame(0.6)
            self.emit()
        for _ in range(3):
            self.frame(0.6)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].status, "recording")

    def test_ignored_events(self):
        for _ in range(3):
            self.frame(0.6)
        self.emit("camera_offline")                            # type non choisi
        self.emit(cam=None)                                    # sans caméra
        self.feed.emit("zone_intrusion", "x")                  # sans caméra
        for _ in range(3):
            self.frame(0.6)
        self.assertEqual(self.rows(), [])

    def test_no_buffer_without_event_types_or_pre_roll(self):
        self.configure(event_types=[])
        self.frame(0.6); self.frame(0.6)
        self.assertEqual(self.cam.raw_viewers, 0)
        self.configure(event_types=["zone_intrusion"], pre_roll_sec=0)
        self.frame(0.6); self.frame(0.6)
        self.assertEqual(self.cam.raw_viewers, 0)


class ScheduleTests(Base):
    def test_scheduled_recording_starts_and_stops(self):
        self.configure(enabled=True, event_types=[], schedules=[{"camera_id": "c1", "days": [0], "start": "10:00", "end": "11:00"}])
        for _ in range(5):
            self.frame(0.6)
        self.assertEqual(self.rows()[0].trigger, "schedule")
        self.local = datetime(2026, 1, 5, 11, 0)
        self.frame(0.6); self.frame(0.6)
        self.assertEqual(self.rows()[0].status, "done")
        n = len(self.rows())
        for _ in range(5):
            self.frame(0.6)
        self.assertEqual(len(self.rows()), n)


class FailureTests(Base):
    def setUp(self):
        super().setUp()
        self.configure(enabled=True, fps=10, event_types=[])

    def test_low_disk_space_prevents_recording(self):
        self.r.min_free = 10 ** 18
        self.r.start_manual("c1", 5)
        for _ in range(5):
            self.frame(0.2)
        self.assertEqual(self.rows(), [])
        self.assertIn("disque", self.r.status()["last_error"])

    def test_write_error_is_contained(self):
        self.r.start_manual("c1", 5)
        for _ in range(3):
            self.frame(0.2)
        with mock.patch.object(rec.SegmentWriter, "write", side_effect=OSError("disque plein")):
            self.frame(0.2)
        self.assertEqual(self.rows()[0].status, "error")
        n = len(self.rows())
        self.frame(0.2); self.frame(0.2)
        self.assertEqual(len(self.rows()), n)                  # pause de 30 s avant de réessayer
        self.assertEqual(self.r.status()["last_error"], "OSError")

    def test_recover_after_crash(self):
        self.r.start_manual("c1", 5)
        for _ in range(6):
            self.frame(0.2)
        path = self.dir / self.rows()[0].path
        with open(path, "ab") as f:
            f.write(b"\x00\x01\x02")                           # écriture interrompue en fin de fichier
        self.r2 = rec.Recorder(lambda: {}, self.feed, directory=self.dir, hard_disabled=False)
        self.r2.recover()
        (row,) = self.rows()
        self.assertEqual(row.status, "done")
        self.assertEqual(row.frames, len(rec.read_index(path)))
        self.assertGreater(row.frames, 0)

    def test_recover_empty_file_is_an_error(self):
        db = SessionLocal()
        db.add(Recording(camera_id="c1", started_at=utcnow(), path="vide.nyr", status="recording")); db.commit(); db.close()
        (self.dir / "vide.nyr").write_bytes(b"")
        self.r.recover()
        self.assertEqual(self.rows()[0].status, "error")


class FilesTests(Base):
    def test_truncated_and_garbage_files(self):
        p = self.dir / "a.nyr"
        w = rec.SegmentWriter(p); w.write(1.0, b"x" * 10); w.write(2.0, b"y" * 10); w.close()
        with open(p, "ab") as f:
            f.write(rec._REC.pack(3.0, 500) + b"court")        # taille annoncée > reste du fichier
        self.assertEqual([e[0] for e in rec.read_index(p)], [1.0, 2.0])
        (self.dir / "b.nyr").write_bytes(b"AUTRE")
        self.assertEqual(rec.read_index(self.dir / "b.nyr"), [])
        self.assertEqual(rec.read_index(self.dir / "absent.nyr"), [])

    def test_frames_roundtrip(self):
        p = self.dir / "a.nyr"
        w = rec.SegmentWriter(p)
        for k in range(3):
            w.write(10.0 + k, bytes([k]) * (5 + k))
        w.close()
        idx = rec.read_index(p)
        self.assertEqual([rec.read_frame(p, e) for e in idx], [b"\x00" * 5, b"\x01" * 6, b"\x02" * 7])

    def test_path_traversal_is_refused(self):
        for bad in ("../x.nyr", "/etc/passwd", "a/../../x"):
            with self.subTest(bad=bad), self.assertRaises(rec.RecordingError):
                rec.resolve_path(self.dir, bad)
        self.assertEqual(rec.resolve_path(self.dir, "ok.nyr").parent, self.dir.resolve())

    def test_safe_name(self):
        self.assertEqual(rec.safe_name("cam 1/../é"), "cam_1_____")
        self.assertEqual(rec.safe_name(""), "cam")


class QueryTests(Base):
    def setUp(self):
        super().setUp()
        self.configure(enabled=True, fps=10, event_types=[])
        self.r.start_manual("c1", 5)
        for k in range(8):
            self.frame(0.2, color=(10 * k, 60, 90))
        self.r.stop_manual("c1"); self.frame(0.2)
        self.db = SessionLocal(); self.addCleanup(self.db.close)
        self.row = self.rows()[0]

    def test_list_and_detail(self):
        lst = rec.list_recordings(self.db, "c1")
        self.assertEqual(len(lst), 1)
        self.assertEqual(rec.list_recordings(self.db, "autre"), [])
        d = rec.detail(self.db, self.row.id, self.dir)
        self.assertEqual(d["frames"], len(d["t"]))
        self.assertEqual(d["t"][0], 0)
        self.assertEqual(d["t"], sorted(d["t"]))
        with self.assertRaises(rec.RecordingError):
            rec.detail(self.db, 999, self.dir)

    def test_frame_bounds(self):
        self.assertTrue(rec.frame_bytes(self.db, self.row.id, 0, self.dir).startswith(b"\xff\xd8"))
        for n in (-1, 10_000):
            with self.assertRaises(rec.RecordingError) as ctx:
                rec.frame_bytes(self.db, self.row.id, n, self.dir)
            self.assertEqual(ctx.exception.status, 404)

    def test_find_for_time(self):
        mid = self.row.started_at + (self.row.ended_at - self.row.started_at) / 2
        hit = rec.find_for_time(self.db, "c1", mid, self.dir)
        self.assertEqual(hit["recording_id"], self.row.id)
        self.assertGreater(hit["offset_sec"], 0)
        self.assertIsNone(rec.find_for_time(self.db, "c1", mid + timedelta(hours=1), self.dir))
        self.assertIsNone(rec.find_for_time(self.db, "autre", mid, self.dir))

    def test_delete(self):
        path = self.dir / self.row.path
        rec.delete_recording(self.db, self.row.id, self.dir)
        self.assertFalse(path.exists())
        self.assertEqual(rec.list_recordings(self.db), [])
        with self.assertRaises(rec.RecordingError):
            rec.delete_recording(self.db, self.row.id, self.dir)

    def test_cannot_delete_a_running_recording(self):
        self.r.start_manual("c1", 5)
        for _ in range(3):
            self.frame(0.2)
        running = self.rows()[-1]
        with self.assertRaises(rec.RecordingError) as ctx:
            rec.delete_recording(self.db, running.id, self.dir, set(self.r.active().values()))
        self.assertEqual(ctx.exception.status, 409)

    def test_usage(self):
        u = rec.disk_usage(self.db, self.dir)
        self.assertEqual((u["segments"], u["used_bytes"]), (1, self.row.bytes))

    def test_export_avi(self):
        path = rec.export_avi(self.db, "c1", self.row.started_at - timedelta(seconds=1), self.row.ended_at + timedelta(seconds=1), self.dir)
        self.addCleanup(os.unlink, path)
        cap = cv2.VideoCapture(path)
        n = 0
        while cap.read()[0]:
            n += 1
        cap.release()
        self.assertEqual(n, self.row.frames)

    def test_export_limits(self):
        s, e = self.row.started_at, self.row.ended_at
        with self.assertRaises(rec.RecordingError):
            rec.export_avi(self.db, "c1", e, s, self.dir)
        with self.assertRaises(rec.RecordingError):
            rec.export_avi(self.db, "c1", s, s + timedelta(minutes=16), self.dir)
        with self.assertRaises(rec.RecordingError) as ctx:
            rec.export_avi(self.db, "c1", s + timedelta(hours=2), s + timedelta(hours=2, minutes=1), self.dir)
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(rec.RecordingError):
            rec.export_avi(self.db, "autre", s, e, self.dir)


class RetentionTests(Base):
    def make(self, days_ago, size, name):
        p = self.dir / name
        p.write_bytes(b"x" * size)
        db = SessionLocal()
        t = epoch_to_utc(self.T0) - timedelta(days=days_ago)
        db.add(Recording(camera_id="c1", started_at=t, ended_at=t + timedelta(minutes=5), path=name, bytes=size, status="done"))
        db.commit(); db.close()
        return p

    def test_by_age(self):
        old = self.make(10, 100, "old.nyr"); new = self.make(1, 100, "new.nyr")
        self.configure(retention_days=7)
        self.assertEqual(self.r.apply_retention(self.T0), 1)
        self.assertFalse(old.exists()); self.assertTrue(new.exists())
        self.assertEqual([r.path for r in self.rows()], ["new.nyr"])

    def test_by_size_removes_oldest_first(self):
        a = self.make(3, 600_000, "a.nyr"); b = self.make(2, 600_000, "b.nyr"); c = self.make(1, 600_000, "c.nyr")
        self.configure(max_storage_mb=100)
        s = SessionLocal(); st = rec.load_settings(s); st["max_storage_mb"] = 1; s.close()
        self.r._settings = st                                   # 1 Mo : 1 048 576 octets
        removed = self.r.apply_retention(self.T0)
        self.assertEqual(removed, 2)
        self.assertEqual([r.path for r in self.rows()], ["c.nyr"])

    def test_running_segment_is_never_removed(self):
        self.configure(enabled=True, retention_days=1, event_types=[])
        self.r.start_manual("c1", 5)
        for _ in range(3):
            self.frame(0.6)
        self.r.apply_retention(self.T0 + 5 * 86400)
        self.assertEqual(len(self.rows()), 1)


class StatusTests(Base):
    def test_status_shape(self):
        st = self.r.status()
        self.assertEqual((st["enabled"], st["hard_disabled"], st["recording"], st["segments"]), (False, False, [], 0))
        self.assertIn("free_bytes", st)


if __name__ == "__main__":
    unittest.main()
