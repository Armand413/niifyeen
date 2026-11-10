"""Couverture des caméras sur un plan, et outil de carte de ville."""
import unittest

import tests  # noqa: F401
from tests import test_api as _api
from tests.test_floors import png
from backend.app import floors, main
from backend.app.db import Camera, Floor, SessionLocal

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


class CityMapToolTests(unittest.TestCase):
    def test_projection(self):
        from tools import city_map as cm
        bbox = (-1.56, 12.33, -1.47, 12.40)
        self.assertEqual(cm.to_xy(12.40, -1.56, bbox), (0.0, 0.0))
        x, y = cm.to_xy(12.33, -1.47, bbox)
        self.assertAlmostEqual(x, 1.0); self.assertAlmostEqual(y, 1.0)
        x, y = cm.to_xy(12.365, -1.515, bbox)
        self.assertTrue(0.4 < x < 0.6 and 0.4 < y < 0.6)

    def test_default_landmarks_fall_inside_default_bbox(self):
        from tools import city_map as cm
        for label, _, lat, lon, _ in cm.SPOTS:
            x, y = cm.to_xy(lat, lon, cm.DEFAULT_BBOX)
            self.assertTrue(0 <= x <= 1 and 0 <= y <= 1, label)


for _cls in (CoverageTests,):
    for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in _cls.__dict__]:
        setattr(_cls, _name, None)

if __name__ == "__main__":
    unittest.main()
