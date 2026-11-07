import json
import unittest
from datetime import datetime, timedelta

import tests  # noqa: F401
from tests.fakes import FakeCamera
from backend.app import analytics, events
from backend.app.analytics import AnalyticsEngine, ZoneError
from backend.app.db import SessionLocal, Zone, ZoneStat, init_db, utcnow
from backend.app.geometry import crossing, is_simple_polygon, point_in_polygon, polygon_area, side


class GeometryTests(unittest.TestCase):
    A, B = (0.5, 0.0), (0.5, 1.0)             # ligne verticale, tracée de haut en bas

    def test_crossing_both_directions(self):
        self.assertEqual(crossing(self.A, self.B, (0.4, 0.5), (0.6, 0.5)), -1)
        self.assertEqual(crossing(self.A, self.B, (0.6, 0.5), (0.4, 0.5)), 1)

    def test_no_crossing_cases(self):
        self.assertEqual(crossing(self.A, self.B, (0.4, 0.5), (0.45, 0.5)), 0)         # même côté
        self.assertEqual(crossing(self.A, self.B, (0.4, 2.0), (0.6, 2.0)), 0)          # au-delà du segment
        self.assertEqual(crossing(self.A, self.B, (0.4, -1.0), (0.6, -1.0)), 0)
        self.assertEqual(crossing(self.A, self.B, (0.4, 0.5), (0.5, 0.5)), 0)          # s'arrête pile sur la ligne

    def test_diagonal_move_around_the_end_does_not_count(self):
        # le déplacement passe de l'autre côté de la droite mais en dehors du segment fini
        self.assertEqual(crossing((0.4, 0.4), (0.6, 0.4), (0.0, 0.0), (1.0, 0.1)), 0)

    def test_side(self):
        self.assertEqual(side(self.A, self.B, (0.4, 0.5)), 1)
        self.assertEqual(side(self.A, self.B, (0.6, 0.5)), -1)
        self.assertEqual(side(self.A, self.B, (0.5, 0.3)), 0)

    def test_point_in_polygon(self):
        sq = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
        self.assertTrue(point_in_polygon((0.5, 0.5), sq))
        self.assertFalse(point_in_polygon((0.9, 0.5), sq))
        self.assertFalse(point_in_polygon((0.5, 0.1), sq))
        concave = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 0.4), (0.0, 1.0)]
        self.assertFalse(point_in_polygon((0.5, 0.9), concave))
        self.assertTrue(point_in_polygon((0.5, 0.2), concave))

    def test_area_and_simple_polygon(self):
        self.assertAlmostEqual(polygon_area([(0, 0), (1, 0), (1, 1), (0, 1)]), 1.0)
        self.assertTrue(is_simple_polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))
        self.assertFalse(is_simple_polygon([(0, 0), (1, 1), (1, 0), (0, 1)]))          # nœud papillon


class ValidationTests(unittest.TestCase):
    def test_valid_line_and_zone(self):
        line = analytics.clean_zone({"camera_id": "c", "kind": "line", "name": " Porte ", "points": [[0.1, 0.5], [0.9, 0.5]],
                                     "label_in": "Entrée", "label_out": "Sortie"}, creating=True)
        self.assertEqual((line["name"], line["label_in"]), ("Porte", "Entrée"))
        zone = analytics.clean_zone({"camera_id": "c", "kind": "zone", "name": "Hall", "capacity": 5, "restricted": True,
                                     "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]}, creating=True)
        self.assertEqual((zone["capacity"], zone["restricted"]), (5, True))

    def test_invalid_inputs(self):
        sq = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
        bad = [
            {"kind": "cercle", "name": "x", "points": sq},
            {"kind": "line", "name": "", "points": [[0, 0], [1, 1]]},
            {"kind": "line", "name": "x" * 41, "points": [[0, 0], [1, 1]]},
            {"kind": "line", "name": "x", "points": [[0, 0], [1, 1], [0.5, 0.5]]},
            {"kind": "line", "name": "x", "points": [[0.5, 0.5], [0.5, 0.505]]},
            {"kind": "line", "name": "x", "points": [[0, 0], [1.5, 1]]},
            {"kind": "line", "name": "x", "points": [[0, 0], [-0.1, 1]]},
            {"kind": "line", "name": "x", "points": [[0, 0], ["a", 1]]},
            {"kind": "line", "name": "x", "points": [[0, 0], [True, 1]]},
            {"kind": "line", "name": "x", "points": "abc"},
            {"kind": "zone", "name": "x", "points": [[0, 0], [1, 1]]},
            {"kind": "zone", "name": "x", "points": [[i / 20, (i % 2) / 20 + 0.1 * (i % 3)] for i in range(13)]},
            {"kind": "zone", "name": "x", "points": [[0, 0], [1, 1], [1, 0], [0, 1]]},            # papillon
            {"kind": "zone", "name": "x", "points": [[0, 0], [0.5, 0.0], [1, 0.0]]},               # aplatie
            {"kind": "zone", "name": "x", "points": sq, "capacity": 0},
            {"kind": "zone", "name": "x", "points": sq, "capacity": 501},
            {"kind": "zone", "name": "x", "points": sq, "capacity": 2.5},
            {"kind": "zone", "name": "x", "points": sq, "capacity": True},
            {"kind": "zone", "name": "x", "points": sq, "restricted": "oui"},
            {"kind": "zone", "name": "x", "points": sq, "label_in": "a"},
            {"kind": "line", "name": "x", "points": [[0, 0], [1, 1]], "capacity": 3},
            {"kind": "line", "name": "x", "points": [[0, 0], [1, 1]], "restricted": True},
            {"kind": "line", "name": "x", "points": [[0, 0], [1, 1]], "label_in": "y" * 31},
        ]
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ZoneError):
                    analytics.clean_zone({"camera_id": "c", **data}, creating=True)

    def test_update_cannot_change_the_kind(self):
        with self.assertRaises(ZoneError):
            analytics.clean_zone({"kind": "zone"}, creating=False, current_kind="line")


class FakeFeed:
    def __init__(self):
        self.items = []

    def emit(self, type, title, severity="info", camera_id=None, **payload):
        self.items.append({"type": type, "title": title, "severity": severity, "camera_id": camera_id, **payload})
        return self.items[-1]

    def of(self, type):
        return [e for e in self.items if e["type"] == type]


def person(tid, fx, fy, **extra):
    return {"track_id": tid, "fx": fx, "fy": fy, "x": fx, "y": fy, "mode": "UNK", **extra}


class EngineTests(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal()
        s.query(Zone).delete(); s.query(ZoneStat).delete(); s.commit(); s.close()
        self.cam = FakeCamera(camera_id="c1")
        self.feed = FakeFeed()
        self.engine = AnalyticsEngine(lambda: {"c1": self.cam}, self.feed, lambda: {"c1": {"label": "Entrée"}},
                                      flush_every_sec=0)
        self.t = 1000.0

    def add(self, **data):
        s = SessionLocal()
        try:
            z = analytics.create_zone(s, {"camera_id": "c1", **data}, ["c1"])
            zid = z.id
        finally:
            s.close()
        self.engine.reload()
        return zid

    def tick(self, persons, dt=0.2):
        self.t += dt
        self.cam.latest_events = persons
        self.cam.frame_seq += 1
        self.engine.step(self.t)

    # ---- lignes
    def test_line_counts_each_direction(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]], label_in="Gauche", label_out="Droite")
        self.tick([person(1, 0.3, 0.5)])
        self.tick([person(1, 0.7, 0.5)])                                  # vers la droite : sens 2
        self.tick([person(2, 0.8, 0.5)])
        self.tick([person(2, 0.2, 0.5)])                                  # vers la gauche : sens 1
        snap = self.engine.snapshot()["zones"][zid]
        self.assertEqual((snap["count_in"], snap["count_out"]), (1, 1))
        got = {e["direction"] for e in self.feed.of(events.LINE_CROSSED)}
        self.assertEqual(got, {"Gauche", "Droite"})

    def test_default_direction_names(self):
        self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.3, 0.5)]); self.tick([person(1, 0.7, 0.5)])
        self.assertEqual(self.feed.of(events.LINE_CROSSED)[0]["direction"], "Sens 2")

    def test_standing_still_or_staying_on_one_side_counts_nothing(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        for x in (0.3, 0.32, 0.35, 0.4, 0.3):
            self.tick([person(1, x, 0.5)])
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_in"], 0)
        self.assertEqual(self.feed.items, [])

    def test_stepping_exactly_on_the_line_is_counted_once(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.4, 0.5)])
        self.tick([person(1, 0.5, 0.5)])                                  # pile dessus
        self.tick([person(1, 0.6, 0.5)])
        snap = self.engine.snapshot()["zones"][zid]
        self.assertEqual(snap["count_in"] + snap["count_out"], 1)

    def test_back_and_forth_is_limited_by_the_cooldown(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        for x in (0.4, 0.6, 0.4, 0.6):                                    # 0,2 s entre images : sous le délai de 2 s
            self.tick([person(1, x, 0.5)])
        snap = self.engine.snapshot()["zones"][zid]
        self.assertEqual(snap["count_in"] + snap["count_out"], 1)
        self.tick([person(1, 0.4, 0.5)], dt=3)                            # plus tard : compté de nouveau
        snap = self.engine.snapshot()["zones"][zid]
        self.assertEqual(snap["count_in"] + snap["count_out"], 2)

    def test_two_people_are_counted_separately(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.3, 0.4), person(2, 0.3, 0.6)])
        self.tick([person(1, 0.7, 0.4), person(2, 0.7, 0.6)])
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_out"], 2)

    def test_same_image_is_not_processed_twice(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.3, 0.5)])
        self.cam.latest_events = [person(1, 0.7, 0.5)]
        self.t += 0.2; self.engine.step(self.t)                           # frame_seq inchangé
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_out"], 0)

    def test_offline_camera_is_ignored(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.3, 0.5)])
        self.cam.forced_status = "offline"
        self.tick([person(1, 0.7, 0.5)])
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_out"], 0)

    def test_events_without_track_or_foot_point_are_ignored(self):
        zid = self.add(kind="zone", name="Hall", points=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])
        for _ in range(3):
            self.tick([{"track_id": None, "fx": .5, "fy": .5}, {"track_id": 3}, {"fx": .5, "fy": .5}])
        self.assertEqual(self.engine.snapshot()["zones"][zid]["occupancy"], 0)

    # ---- zones
    def test_zone_occupancy_entries_exits_and_dwell(self):
        zid = self.add(kind="zone", name="Hall", points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        self.tick([person(1, 0.5, 0.5), person(2, 0.9, 0.9)])
        self.assertEqual(self.engine.snapshot()["zones"][zid]["occupancy"], 0)        # pas encore confirmé (1 image)
        self.tick([person(1, 0.5, 0.5), person(2, 0.9, 0.9)])
        self.tick([person(1, 0.5, 0.5), person(2, 0.5, 0.6)])
        self.tick([person(1, 0.5, 0.5), person(2, 0.5, 0.6)])
        snap = self.engine.snapshot()["zones"][zid]
        self.assertEqual((snap["occupancy"], snap["count_in"], snap["count_out"]), (2, 2, 0))
        for _ in range(15):                                                            # tout le monde sort
            self.tick([], dt=0.2)
        snap = self.engine.snapshot()["zones"][zid]
        self.assertEqual((snap["occupancy"], snap["count_in"], snap["count_out"]), (0, 2, 2))
        self.assertGreater(snap["avg_dwell_sec"], 0)

    def test_flicker_does_not_count_a_new_entry(self):
        zid = self.add(kind="zone", name="Hall", points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        self.tick([person(1, 0.5, 0.5)]); self.tick([person(1, 0.5, 0.5)])
        self.tick([]); self.tick([person(1, 0.5, 0.5)])                                # disparaît une image
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_in"], 1)

    def test_overcrowding_alert_and_recovery(self):
        zid = self.add(kind="zone", name="Hall", capacity=1, points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        two = [person(1, 0.4, 0.5), person(2, 0.6, 0.5)]
        for _ in range(8):
            self.tick(two)                                                              # 1,6 s : trop tôt
        self.assertEqual(self.feed.of(events.ZONE_OVERCROWDED), [])
        for _ in range(10):
            self.tick(two)
        alerts = self.feed.of(events.ZONE_OVERCROWDED)
        self.assertEqual(len(alerts), 1)                                                # une seule alerte, pas une par image
        self.assertEqual((alerts[0]["severity"], alerts[0]["occupancy"], alerts[0]["capacity"]), ("warning", 2, 1))
        self.assertTrue(self.engine.snapshot()["zones"][zid]["over"])
        for _ in range(25):
            self.tick([person(1, 0.4, 0.5)])
        self.assertEqual(len(self.feed.of(events.ZONE_CLEARED)), 1)
        self.assertFalse(self.engine.snapshot()["zones"][zid]["over"])

    def test_no_alert_without_capacity(self):
        self.add(kind="zone", name="Hall", points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        for _ in range(40):
            self.tick([person(i, 0.3 + i * 0.05, 0.5) for i in range(6)])
        self.assertEqual(self.feed.items, [])

    def test_restricted_zone_intrusion_with_cooldown(self):
        self.add(kind="zone", name="Local technique", restricted=True,
                 points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        self.tick([person(1, 0.5, 0.5)]); self.tick([person(1, 0.5, 0.5)])
        got = self.feed.of(events.ZONE_INTRUSION)
        self.assertEqual((len(got), got[0]["severity"]), (1, "warning"))
        self.assertNotIn("participant_id", got[0])
        self.tick([person(2, 0.6, 0.6)]); self.tick([person(2, 0.6, 0.6)])             # 2e personne, dans le délai
        self.assertEqual(len(self.feed.of(events.ZONE_INTRUSION)), 1)
        self.tick([person(3, 0.4, 0.4)], dt=30); self.tick([person(3, 0.4, 0.4)])
        self.assertEqual(len(self.feed.of(events.ZONE_INTRUSION)), 2)

    def test_intrusion_names_an_enrolled_person_by_id_only(self):
        self.add(kind="zone", name="Local", restricted=True, points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        p = person(1, 0.5, 0.5, mode="DB", participant_id=7, match_status="a_verifier", label="Awa Ba")
        self.tick([p]); self.tick([p])
        ev = self.feed.of(events.ZONE_INTRUSION)[0]
        self.assertEqual((ev["participant_id"], ev["match_status"]), (7, "a_verifier"))
        self.assertNotIn("Awa", json.dumps(ev))

    def test_unrestricted_zone_never_raises_an_intrusion(self):
        self.add(kind="zone", name="Hall", points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        self.tick([person(1, 0.5, 0.5)]); self.tick([person(1, 0.5, 0.5)])
        self.assertEqual(self.feed.of(events.ZONE_INTRUSION), [])

    def test_disabled_zones_are_not_loaded_and_edits_reset_counters(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.3, 0.5)]); self.tick([person(1, 0.7, 0.5)])
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_out"], 1)
        s = SessionLocal(); analytics.update_zone(s, zid, {"points": [[0.4, 0.0], [0.4, 1.0]]}); s.close()
        self.engine.reload()
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_out"], 0)
        s = SessionLocal(); analytics.update_zone(s, zid, {"enabled": False}); s.close()
        self.engine.reload()
        self.assertEqual(self.engine.snapshot()["zones"], {})

    def test_renaming_keeps_the_counters(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.tick([person(1, 0.3, 0.5)]); self.tick([person(1, 0.7, 0.5)])
        s = SessionLocal(); analytics.update_zone(s, zid, {"name": "Porte A"}); s.close()
        self.engine.reload()
        self.assertEqual(self.engine.snapshot()["zones"][zid]["count_out"], 1)

    # ---- affluence enregistrée
    def test_affluence_is_aggregated_in_five_minute_buckets(self):
        zid = self.add(kind="line", name="Porte", points=[[0.5, 0.0], [0.5, 1.0]])
        self.t = datetime(2026, 11, 7, 10, 2, 0).timestamp() - 3600 * 0  # heure locale du test sans importance
        self.tick([person(1, 0.3, 0.5)]); self.tick([person(1, 0.7, 0.5)])
        self.engine.flush()
        s = SessionLocal()
        rows = s.query(ZoneStat).filter(ZoneStat.zone_id == zid).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].entries, rows[0].exits), (0, 1))
        self.assertEqual(rows[0].bucket.minute % 5, 0)
        s.close()

    def test_zone_series_and_overview(self):
        zid = self.add(kind="zone", name="Hall", points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        now = utcnow()
        s = SessionLocal()
        start = analytics.bucket_start(now)
        s.add(ZoneStat(zone_id=zid, bucket=start - timedelta(minutes=10), entries=3, exits=2, samples=10, occupancy_sum=25, occupancy_max=4))
        s.add(ZoneStat(zone_id=zid, bucket=start - timedelta(minutes=5), entries=1, exits=1, samples=5, occupancy_sum=5, occupancy_max=2))
        s.commit()
        out = analytics.series(s, zid, start - timedelta(minutes=30), now)
        self.assertEqual([(o["entries"], o["occupancy_avg"], o["occupancy_max"]) for o in out], [(3, 2.5, 4), (1, 1.0, 2)])
        ov = analytics.overview(s, start - timedelta(minutes=30), now)[0]
        self.assertEqual((ov["entries"], ov["exits"], ov["occupancy_max"]), (4, 3, 4))
        s.close()

    def test_deleting_a_zone_removes_its_statistics(self):
        zid = self.add(kind="zone", name="Hall", points=[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]])
        s = SessionLocal()
        s.add(ZoneStat(zone_id=zid, bucket=analytics.bucket_start(utcnow()), entries=1)); s.commit()
        analytics.delete_zone(s, zid)
        self.assertEqual(s.query(ZoneStat).filter(ZoneStat.zone_id == zid).count(), 0)
        s.close()

    # ---- caméra masquée / figée
    def stats(self, std, mean=120.0, crc=1, ts=None):
        self.cam.frame_stats = {"mean": mean, "std": std, "crc": crc, "ts": self.t if ts is None else ts}

    def test_obstructed_camera_is_reported_once_and_recovers(self):
        for i in range(20):                                             # 4 s : trop tôt
            self.t += 0.2; self.stats(1.0, mean=5, crc=i); self.engine.step(self.t)
        self.assertEqual(self.feed.of(events.CAMERA_OBSTRUCTED), [])
        for i in range(60):
            self.t += 0.2; self.stats(1.0, mean=5, crc=100 + i); self.engine.step(self.t)
        got = self.feed.of(events.CAMERA_OBSTRUCTED)
        self.assertEqual((len(got), got[0]["severity"]), (1, "warning"))
        self.assertTrue(self.engine.snapshot()["cameras"]["c1"]["obstructed"])
        for i in range(30):
            self.t += 0.2; self.stats(40.0, crc=500 + i); self.engine.step(self.t)
        self.assertEqual(len(self.feed.of(events.CAMERA_CLEARED)), 1)
        self.assertFalse(self.engine.snapshot()["cameras"]["c1"]["obstructed"])

    def test_normal_picture_never_raises_anything(self):
        for i in range(200):
            self.t += 0.2; self.stats(35.0, crc=i); self.engine.step(self.t)
        self.assertEqual(self.feed.items, [])

    def test_brief_dark_moment_is_tolerated(self):
        for i in range(10):
            self.t += 0.2; self.stats(1.0, crc=i); self.engine.step(self.t)
        for i in range(10, 50):
            self.t += 0.2; self.stats(30.0, crc=i); self.engine.step(self.t)
        self.assertEqual(self.feed.items, [])

    def test_frozen_stream_is_reported(self):
        for i in range(120):                                            # 24 s d'images strictement identiques
            self.t += 0.2; self.stats(30.0, crc=42); self.engine.step(self.t)
        self.assertEqual(len(self.feed.of(events.CAMERA_FROZEN)), 1)
        self.assertTrue(self.engine.snapshot()["cameras"]["c1"]["frozen"])
        self.t += 0.2; self.stats(30.0, crc=43); self.engine.step(self.t)
        self.assertEqual(len(self.feed.of(events.CAMERA_CLEARED)), 1)

    def test_stale_or_offline_stats_are_ignored(self):
        self.cam.forced_status = "offline"
        for i in range(100):
            self.t += 0.2; self.stats(0.5, crc=1); self.engine.step(self.t)
        self.assertEqual(self.feed.items, [])
        self.cam.forced_status = "online"
        for i in range(100):
            self.t += 0.2; self.stats(0.5, crc=i, ts=self.t - 10); self.engine.step(self.t)   # mesure ancienne
        self.assertEqual(self.feed.items, [])

    def test_removed_camera_is_forgotten(self):
        self.stats(30.0)
        self.engine.step(self.t)
        self.assertIn("c1", self.engine.snapshot()["cameras"])
        self.engine._get_cams = lambda: {}
        self.engine.step(self.t + 1)
        self.assertEqual(self.engine.snapshot()["cameras"], {})

    def test_family_filter_knows_the_new_events(self):
        self.assertIn(events.LINE_CROSSED, events.FAMILIES["analytics"])
        self.assertIn(events.CAMERA_OBSTRUCTED, events.FAMILIES["cameras"])


if __name__ == "__main__":
    unittest.main()
