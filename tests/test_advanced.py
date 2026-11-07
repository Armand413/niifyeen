"""Module 14 : présence prolongée en zone et couverture des caméras."""
import unittest

import tests  # noqa: F401
from tests import test_api as _api
from tests import test_analytics as ta
from tests.test_floors import png
from backend.app import alerts, analytics, events, floors, main
from backend.app.analytics import ZoneError
from backend.app.db import Camera, Floor, SessionLocal, Zone, ZoneStat

SQUARE = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]


class LoiteringEngineTests(ta.EngineTests):
    def test_loitering_is_announced_once_per_stay(self):
        self.add(kind="zone", name="Hall", points=SQUARE, max_dwell_sec=10)
        for _ in range(40):                                  # 8 s : pas encore
            self.tick([ta.person(1, 0.5, 0.5)])
        self.assertEqual(self.feed.of(events.ZONE_LOITERING), [])
        for _ in range(20):                                  # > 10 s
            self.tick([ta.person(1, 0.5, 0.5)])
        got = self.feed.of(events.ZONE_LOITERING)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["severity"], got[0]["max_dwell_sec"]), ("warning", 10))
        self.assertGreaterEqual(got[0]["dwell_sec"], 10)
        for _ in range(100):                                 # toujours là : pas de répétition
            self.tick([ta.person(1, 0.5, 0.5)])
        self.assertEqual(len(self.feed.of(events.ZONE_LOITERING)), 1)

    def test_loitering_payload_is_anonymous(self):
        self.add(kind="zone", name="Hall", points=SQUARE, max_dwell_sec=5)
        for _ in range(40):
            self.tick([ta.person(1, 0.5, 0.5, mode="DB", participant_id=7, match_status="ok")])
        got = self.feed.of(events.ZONE_LOITERING)
        self.assertEqual(len(got), 1)
        self.assertNotIn("participant_id", got[0])

    def test_a_new_stay_can_be_announced_again(self):
        self.add(kind="zone", name="Hall", points=SQUARE, max_dwell_sec=5)
        for _ in range(40):
            self.tick([ta.person(1, 0.5, 0.5)])
        self.tick([], dt=30)                                  # part (perdu de vue)
        self.tick([], dt=1)
        for _ in range(40):
            self.tick([ta.person(1, 0.5, 0.5)])
        self.assertEqual(len(self.feed.of(events.ZONE_LOITERING)), 2)

    def test_no_threshold_no_event_and_passers_by_are_ignored(self):
        self.add(kind="zone", name="Libre", points=SQUARE)
        for _ in range(100):
            self.tick([ta.person(1, 0.5, 0.5)])
        self.assertEqual(self.feed.of(events.ZONE_LOITERING), [])


class LoiteringValidationTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(analytics.clean_zone({"max_dwell_sec": 60}, False, "zone"), {"max_dwell_sec": 60})
        self.assertEqual(analytics.clean_zone({"max_dwell_sec": None}, False, "zone"), {"max_dwell_sec": None})
        for bad in (4, 86401, "10", 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ZoneError):
                analytics.clean_zone({"max_dwell_sec": bad}, False, "zone")
        with self.assertRaises(ZoneError):
            analytics.clean_zone({"max_dwell_sec": 30}, False, "line")

    def test_registered_everywhere(self):
        self.assertIn("zone_loitering", alerts.TYPES)
        self.assertIn("zone_loitering", alerts.DEFAULT_RULES)
        self.assertIn(events.ZONE_LOITERING, events.FAMILIES["analytics"])


class LoiteringApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(Zone).delete(); s.query(ZoneStat).delete(); s.commit(); s.close()
        main.app.state.analytics.reload()

    def test_create_update_and_reject(self):
        body = {"camera_id": "entre", "kind": "zone", "name": "Hall", "points": SQUARE, "max_dwell_sec": 120}
        r = self.client.post("/api/zones", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        z = r.json()
        self.assertEqual(z["max_dwell_sec"], 120)
        self.assertIn(z["id"], main.app.state.analytics.snapshot()["zones"])
        r = self.client.put(f"/api/zones/{z['id']}", json={"max_dwell_sec": None})
        self.assertIsNone(r.json()["max_dwell_sec"])
        self.assertEqual(self.client.put(f"/api/zones/{z['id']}", json={"max_dwell_sec": 1}).status_code, 400)
        line = self.client.post("/api/zones", json={"camera_id": "entre", "kind": "line", "name": "L",
                                                    "points": [[0.5, 0], [0.5, 1]], "max_dwell_sec": 30})
        self.assertEqual(line.status_code, 400)


class CoverageTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(Floor).delete()
        s.query(Camera).filter(Camera.floor_id.isnot(None)).update({Camera.floor_id: None})
        s.commit(); s.close()
        r = self.client.post("/api/floors", data={"name": "RDC", "level": "0"},
                             files={"file": ("p.png", __import__("io").BytesIO(png()), "application/octet-stream")})
        self.fid = r.json()["id"]

    def place(self, cam, **kw):
        s = SessionLocal()
        row = s.get(Camera, cam)
        row.floor_id = self.fid
        for k, v in kw.items():
            setattr(row, k, v)
        s.commit(); s.close()

    def cov(self):
        r = self.client.get(f"/api/floors/{self.fid}/coverage")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_unknown_floor(self):
        self.assertEqual(self.client.get("/api/floors/9999/coverage").status_code, 404)

    def test_no_camera_means_everything_blind(self):
        c = self.cov()
        self.assertEqual((c["cameras_counted"], c["covered_pct"], c["blind_pct"]), (0, 0.0, 100.0))
        self.assertTrue(c["walls_ignored"])

    def test_full_circle_covers_the_known_area(self):
        # plan 200 x 100 ; portée 0,25 x largeur = 50 px ; cercle de rayon 50 px -> aire pi*2500 / 20000 = 39,3 %
        self.place("entre", x=0.5, y=0.5, heading=0.0, fov=360.0, view_range=0.25)
        c = self.cov()
        self.assertEqual(c["cameras_counted"], 1)
        self.assertAlmostEqual(c["covered_pct"], 39.3, delta=3.0)
        self.assertAlmostEqual(c["covered_pct"] + c["blind_pct"], 100.0, places=1)
        self.assertEqual(c["overlap_pct"], 0.0)

    def test_direction_matters(self):
        self.place("entre", x=0.5, y=0.5, heading=90.0, fov=60.0, view_range=0.3)     # regarde à droite
        c = self.cov()
        rows = [r for r in c["grid"]]
        cols = c["cols"]
        left = sum(r[: cols // 2].count("1") for r in rows)
        right = sum(r[cols // 2:].count("1") for r in rows)
        self.assertLessEqual(left, 4)             # seulement la cellule de la caméra elle-même
        self.assertGreater(right, 20)

    def test_overlap_and_unique_share(self):
        self.place("entre", x=0.4, y=0.5, heading=0.0, fov=360.0, view_range=0.25)
        self.place("salle", x=0.6, y=0.5, heading=0.0, fov=360.0, view_range=0.25)
        c = self.cov()
        self.assertGreater(c["overlap_pct"], 5.0)
        a, b = c["cameras"][0], c["cameras"][1]
        self.assertLess(a["only_pct"], a["covered_pct"])
        self.assertGreater(c["covered_pct"], max(a["covered_pct"], b["covered_pct"]))

    def test_disabled_and_unplaced_cameras_are_ignored(self):
        self.place("entre", x=0.5, y=0.5, fov=360.0, view_range=0.25, enabled=False)
        self.place("salle", x=None, y=None)
        c = self.cov()
        self.assertEqual(c["cameras_counted"], 0)
        self.assertEqual(c["cameras_disabled"], 1)
        s = SessionLocal(); s.get(Camera, "entre").enabled = True; s.commit(); s.close()

    def test_defaults_are_used_when_fields_are_missing(self):
        self.place("entre", x=0.5, y=0.5, heading=None, fov=None, view_range=None)
        self.assertGreater(self.cov()["covered_pct"], 0)


for _cls in (LoiteringApiTests, CoverageTests):
    for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in _cls.__dict__]:
        setattr(_cls, _name, None)

for _name in [n for n in dir(ta.EngineTests) if n.startswith("test_") and n not in LoiteringEngineTests.__dict__]:
    setattr(LoiteringEngineTests, _name, None)

if __name__ == "__main__":
    unittest.main()
