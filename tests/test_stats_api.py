import csv
import io
import unittest
from datetime import timedelta

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import config
from backend.app.db import Detection, Participant, SessionLocal, TargetMove, utcnow


class StatsApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        for m in (TargetMove, Detection):
            s.query(m).delete()
        now = utcnow()
        for tr, cam, ago in ((1, "entre", 30), (2, "salle", 20)):
            for k in range(3):
                s.add(Detection(camera_id=cam, track_id=tr, mode="UNK", x=100.0, y=100.0, timestamp=now - timedelta(minutes=ago, seconds=-k)))
        s.commit(); s.close()

    def test_page_is_served(self):
        self.assertEqual(self.client.get("/statistics").status_code, 200)

    def test_overview(self):
        r = self.client.get("/api/stats/overview?minutes=120&tz_offset_min=60")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["observations"], d["passages"]), (6, 2))
        self.assertEqual(len(d["by_hour"]), 24)
        self.assertEqual(self.client.get("/api/stats/overview?camera_id=entre").json()["passages"], 1)

    def test_overview_validation(self):
        for q in ("minutes=0", "tz_offset_min=9999", "start=hier", "minutes=999999"):
            with self.subTest(q=q):
                self.assertEqual(self.client.get("/api/stats/overview?" + q).status_code, 422)

    def test_heatmap(self):
        self.assertEqual(self.client.get("/api/stats/heatmap").status_code, 422)
        d = self.client.get("/api/stats/heatmap?camera_id=entre&cols=8&rows=4&minutes=120").json()
        self.assertEqual((d["total"], d["max"]), (3, 3))
        self.assertEqual(self.client.get("/api/stats/heatmap?camera_id=entre&cols=500").status_code, 422)

    def test_compare_defaults_to_previous_period(self):
        d = self.client.get("/api/stats/compare?minutes=60").json()
        self.assertEqual(d["a"]["passages"], 2)
        self.assertEqual(d["b"]["passages"], 0)
        self.assertIsNone(d["delta"]["passages"]["pct"])
        self.assertEqual(self.client.get("/api/stats/compare?minutes=60&ref_start=n.importe").status_code, 422)

    def test_transitions(self):
        d = self.client.get("/api/stats/transitions?minutes=120").json()
        self.assertEqual(d["edges"], [])
        self.assertEqual(self.client.get("/api/stats/transitions?max_gap_sec=1").status_code, 422)

    def test_csv(self):
        r = self.client.get("/api/stats/overview.csv?minutes=120")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        rows = list(csv.reader(io.StringIO(r.text), delimiter=";"))
        self.assertIn(["total", "passages", "2"], rows)

    def test_identities_hidden_never_leak(self):
        s = SessionLocal()
        s.add(Detection(camera_id="entre", track_id=5, mode="DB", label="Jean Secret", timestamp=utcnow() - timedelta(minutes=5)))
        s.commit(); s.close()
        for path in ("overview", "transitions", "overview.csv"):
            self.assertNotIn("Jean Secret", self.client.get("/api/stats/" + path + "?minutes=120").text)


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in StatsApiTests.__dict__]:
    setattr(StatsApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
