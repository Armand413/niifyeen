import csv
import io
import unittest
from datetime import datetime, timedelta
from unittest import mock

import tests  # noqa: F401
from backend.app import config, history
from backend.app.db import (SessionLocal, Detection, TargetMove, init_db, utcnow,
                            purge_old_detections, save_target_move)


class HistoryBase(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        self.db.query(Detection).delete()
        self.db.query(TargetMove).delete()
        self.db.commit()
        self.now = utcnow()
        self.addCleanup(self.db.close)

    def add(self, seconds_ago, cam="entre", track=1, mode="UNK", label=None, ts=None, ds=None, gid=None):
        self.db.add(Detection(camera_id=cam, track_id=track, mode=mode, label=label, global_id=gid,
                              target_score=ts, db_score=ds, timestamp=self.now - timedelta(seconds=seconds_ago)))

    def commit(self):
        self.db.commit()

    @property
    def window(self):
        return self.now - timedelta(hours=1), self.now + timedelta(seconds=1)

    def episodes(self, **kw):
        return history.read_episodes(self.db, *self.window, **kw)


class RangeTests(unittest.TestCase):
    NOW = datetime(2026, 10, 30, 12, 0, 0)

    def test_relative_range(self):
        since, until = history.resolve_range(minutes=30, now=self.NOW)
        self.assertEqual((since, until), (self.NOW - timedelta(minutes=30), self.NOW))

    def test_absolute_range_with_timezone_is_converted_to_utc(self):
        since, until = history.resolve_range(start="2026-10-30T10:00:00+02:00", end="2026-10-30T11:00:00Z")
        self.assertEqual(since, datetime(2026, 10, 30, 8, 0))
        self.assertEqual(until, datetime(2026, 10, 30, 11, 0))

    def test_missing_end_means_now_and_missing_start_uses_minutes(self):
        since, until = history.resolve_range(start="2026-10-30T11:00:00Z", now=self.NOW)
        self.assertEqual(until, self.NOW)
        since, until = history.resolve_range(minutes=15, end="2026-10-30T11:00:00Z", now=self.NOW)
        self.assertEqual(since, datetime(2026, 10, 30, 10, 45))

    def test_invalid_ranges(self):
        for kw in ({"start": "abc"}, {"start": "2026-10-30T12:00:00Z", "end": "2026-10-30T11:00:00Z"},
                   {"start": "2020-01-01T00:00:00Z", "end": "2026-10-30T00:00:00Z"}):
            with self.subTest(kw=kw), self.assertRaises(history.HistoryError):
                history.resolve_range(now=self.NOW, **kw)


class EpisodeTests(HistoryBase):
    def test_observations_of_one_track_become_one_passage(self):
        for i in range(10):
            self.add(100 - i * 0.8)
        self.commit()
        r = self.episodes()
        self.assertEqual(r["total"], 1)
        ep = r["items"][0]
        self.assertEqual(ep["observations"], 10)
        self.assertAlmostEqual(ep["duration_sec"], 7.2, places=1)
        self.assertTrue(ep["first_seen"].endswith("Z"))

    def test_a_pause_longer_than_the_gap_starts_a_new_passage(self):
        self.add(100); self.add(99); self.add(50); self.add(49)
        self.commit()
        self.assertEqual(self.episodes()["total"], 2)
        with mock.patch.object(config, "HISTORY_GAP_SEC", 60.0):
            self.assertEqual(self.episodes()["total"], 1)

    def test_strongest_mode_wins_inside_a_passage(self):
        self.add(100, mode="UNK"); self.add(99, mode="UNK")
        self.add(98, mode="DB", label="Awa", ds=0.6)
        self.add(97, mode="DB", label="Awa", ds=0.7)
        self.commit()
        ep = self.episodes()["items"][0]
        self.assertEqual((ep["mode"], ep["label"], ep["observations"], ep["best_score"]), ("DB", "Awa", 4, 0.7))
        self.add(96, mode="TARGET", label="CIBLE", ts=0.5)
        self.commit()
        self.assertEqual(self.episodes()["items"][0]["mode"], "TARGET")

    def test_tracks_and_cameras_are_separate(self):
        self.add(100, track=1); self.add(100, track=2); self.add(100, cam="salle", track=1)
        self.commit()
        self.assertEqual(self.episodes()["total"], 3)

    def test_filters(self):
        self.add(100, cam="entre", track=1, mode="DB", label="Awa", ds=.6)
        self.add(100, cam="salle", track=2, mode="TARGET", label="CIBLE", ts=.7)
        self.add(100, cam="salle", track=3, mode="UNK")
        self.commit()
        self.assertEqual(self.episodes(camera_id="salle")["total"], 2)
        self.assertEqual(self.episodes(mode="TARGET")["total"], 1)
        self.assertEqual(self.episodes(camera_id="salle", mode="UNK")["total"], 1)
        self.assertEqual([e["label"] for e in self.episodes(label="AW")["items"]], ["Awa"])
        with self.assertRaises(history.HistoryError):
            self.episodes(mode="BAD")

    def test_label_search_treats_wildcards_literally(self):
        self.add(100, mode="DB", label="Awa", ds=.6)
        self.commit()
        self.assertEqual(self.episodes(label="%")["total"], 0)
        self.assertEqual(self.episodes(label="_wa")["total"], 0)

    def test_pagination_and_order(self):
        for track in range(7):
            self.add(100 - track * 10, track=track)
        self.commit()
        p1 = self.episodes(page=1, page_size=3)
        p3 = self.episodes(page=3, page_size=3)
        self.assertEqual((p1["total"], len(p1["items"]), len(p3["items"])), (7, 3, 1))
        self.assertEqual([e["track_id"] for e in p1["items"]], [6, 5, 4])    # plus récent d'abord

    def test_period_is_respected(self):
        self.add(100); self.add(7200, track=2)
        self.commit()
        self.assertEqual(self.episodes()["total"], 1)

    def test_identities_can_be_hidden(self):
        self.add(100, mode="DB", label="Awa", ds=.6)
        self.add(100, track=2, mode="TARGET", label="CIBLE", ts=.5)
        self.commit()
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            r = self.episodes()
            labels = sorted(e["label"] for e in r["items"])
            self.assertEqual(labels, ["Cible", "Personne enrôlée"])
            self.assertFalse(r["identities_visible"])
            self.assertEqual(self.episodes(label="Awa")["total"], 2)     # la recherche par nom est ignorée
            self.assertNotIn("Awa", "".join(history.iter_csv(self.db, *self.window)))
            self.assertEqual(history.public_label("UNK", None), None)


class SummaryTests(HistoryBase):
    def test_counts_and_timeline(self):
        for i in range(20):
            self.add(1800 - i)                                   # 20 obs inconnus, 1 passage
        for i in range(10):
            self.add(600 - i, cam="salle", track=2, mode="TARGET", label="CIBLE", ts=.6)
        self.add(300, cam="salle", track=3, mode="DB", label="Awa", ds=.5)
        self.commit()
        s = history.read_summary(self.db, *self.window)
        self.assertEqual((s["passages"], s["observations"]), (3, 31))
        self.assertEqual(s["by_mode"], {"TARGET": 1, "DB": 1, "UNK": 1})
        self.assertEqual(s["enrolled_people"], 1)
        self.assertEqual(s["by_camera"][0]["camera_id"], "salle")
        self.assertEqual(sum(b["observations"] for b in s["timeline"]), 31)
        self.assertEqual(sum(b["target"] for b in s["timeline"]), 10)
        self.assertLessEqual(len(s["timeline"]), history.MAX_BUCKETS + 1)
        only = history.read_summary(self.db, *self.window, camera_id="entre")
        self.assertEqual((only["passages"], only["observations"]), (1, 20))

    def test_bucket_size_grows_with_the_period(self):
        now = utcnow()
        sizes = [history.pick_bucket_minutes(now - timedelta(minutes=m), now) for m in (10, 60, 1440, 43200)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(sizes[0], 1)
        self.assertGreater(sizes[-1], 60)

    def test_empty_period(self):
        s = history.read_summary(self.db, *self.window)
        self.assertEqual((s["passages"], s["observations"], s["by_camera"]), (0, 0, []))


class CsvTests(HistoryBase):
    def rows(self, **kw):
        text = "".join(history.iter_csv(self.db, *self.window, **kw))
        self.assertTrue(text.startswith("\ufeff"))
        return list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))

    def test_header_and_values(self):
        self.add(100, mode="DB", label="Awa", ds=.6); self.add(99, mode="DB", label="Awa", ds=.7)
        self.commit()
        rows = self.rows()
        self.assertEqual(rows[0], history.CSV_HEADER)
        self.assertEqual(rows[1][2:], ["1.0", "entre", "1", "personne enrôlée", "Awa", "2", "0.7", "", ""])

    def test_formula_injection_is_neutralised(self):
        self.add(100, mode="DB", label="=HYPERLINK(\"http://x\")", ds=.6)
        self.commit()
        self.assertTrue(self.rows()[1][6].startswith("'="))
        self.assertEqual(history.csv_safe("@x"), "'@x")
        self.assertEqual(history.csv_safe("Awa"), "Awa")
        self.assertEqual(history.csv_safe(3.5), 3.5)


class TrajectoryTests(HistoryBase):
    def test_sessions_are_grouped_and_ordered(self):
        t0 = (self.now - timedelta(minutes=30) - datetime(1970, 1, 1)).total_seconds()
        t1 = (self.now - timedelta(minutes=10) - datetime(1970, 1, 1)).total_seconds()
        save_target_move(1, "entre", t0, .6); save_target_move(1, "salle", t0 + 8, .7)
        save_target_move(2, "sortie", t1, .5)
        r = history.read_trajectories(self.db, *self.window)
        self.assertEqual(r["total"], 2)
        self.assertEqual([s["session"] for s in r["items"]], [2, 1])        # le plus récent d'abord
        self.assertEqual([m["camera_id"] for m in r["items"][1]["moves"]], ["entre", "salle"])
        self.assertEqual(r["items"][1]["cameras"], 2)

    def test_retention_purges_old_moves_too(self):
        old = (self.now - timedelta(days=40) - datetime(1970, 1, 1)).total_seconds()
        save_target_move(1, "entre", old); save_target_move(2, "entre", (self.now - datetime(1970, 1, 1)).total_seconds())
        purge_old_detections(30)
        self.assertEqual(self.db.query(TargetMove).count(), 1)


if __name__ == "__main__":
    unittest.main()
