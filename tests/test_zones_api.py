import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import analytics, events, main
from backend.app.db import SessionLocal, Event, Zone, ZoneStat, utcnow

SQUARE = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
LINE = [[0.5, 0.0], [0.5, 1.0]]


class ZonesApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(Zone).delete(); s.query(ZoneStat).delete(); s.commit(); s.close()
        main.app.state.analytics.reload()

    def zone(self, **kw):
        body = {"camera_id": "entre", "kind": "zone", "name": "Hall", "points": SQUARE, **kw}
        return self.client.post("/api/zones", json=body)

    def test_page_is_served(self):
        self.assertEqual(self.client.get("/zones").status_code, 200)

    def test_create_list_update_delete(self):
        r = self.zone(capacity=4, restricted=True)
        self.assertEqual(r.status_code, 201, r.text)
        z = r.json()
        self.assertEqual((z["kind"], z["capacity"], z["restricted"], z["enabled"]), ("zone", 4, True, True))
        line = self.client.post("/api/zones", json={"camera_id": "salle", "kind": "line", "name": "Porte", "points": LINE,
                                                    "label_in": "Entrée", "label_out": "Sortie"}).json()
        self.assertEqual([x["name"] for x in self.client.get("/api/zones").json()["zones"]], ["Hall", "Porte"])
        self.assertEqual([x["name"] for x in self.client.get("/api/zones?camera_id=salle").json()["zones"]], ["Porte"])
        r = self.client.put(f"/api/zones/{z['id']}", json={"name": "Hall A", "capacity": None, "restricted": False})
        self.assertEqual((r.json()["name"], r.json()["capacity"], r.json()["restricted"]), ("Hall A", None, False))
        self.assertEqual(self.client.delete(f"/api/zones/{line['id']}").json(), {"deleted": True})
        self.assertEqual(self.client.delete(f"/api/zones/{line['id']}").status_code, 404)
        self.assertEqual(self.client.put("/api/zones/999", json={"name": "x"}).status_code, 404)

    def test_engine_is_reloaded_after_changes(self):
        z = self.zone().json()
        self.assertIn(z["id"], main.app.state.analytics.snapshot()["zones"])
        self.client.put(f"/api/zones/{z['id']}", json={"enabled": False})
        self.assertNotIn(z["id"], main.app.state.analytics.snapshot()["zones"])
        self.client.put(f"/api/zones/{z['id']}", json={"enabled": True})
        self.client.delete(f"/api/zones/{z['id']}")
        self.assertEqual(main.app.state.analytics.snapshot()["zones"], {})

    def test_validation_errors_are_plain_messages(self):
        for body, code in (({"camera_id": "inconnue"}, 404), ({"kind": "line"}, 400), ({"name": ""}, 400),
                           ({"points": [[0, 0]]}, 400), ({"capacity": 0}, 400), ({"points": [[0, 0], [2, 1], [1, 1]]}, 400),
                           ({"extra": 1}, 422)):
            with self.subTest(body=body):
                r = self.zone(**body)
                self.assertEqual(r.status_code, code, r.text)
        self.assertIsInstance(self.zone(name="").json()["detail"], str)
        self.assertEqual(self.client.get("/api/zones").json()["zones"], [])

    def test_a_trace_cannot_change_camera_or_kind(self):
        z = self.zone().json()
        self.assertEqual(self.client.put(f"/api/zones/{z['id']}", json={"camera_id": "salle"}).status_code, 400)
        self.assertEqual(self.client.put(f"/api/zones/{z['id']}", json={"kind": "line"}).status_code, 400)

    def test_limit_per_camera(self):
        with mock.patch.object(analytics, "MAX_ZONES_PER_CAMERA", 2):
            self.zone(name="a"); self.zone(name="b")
            self.assertEqual(self.zone(name="c").status_code, 409)
            self.assertEqual(self.zone(name="d", camera_id="salle").status_code, 201)

    def test_live_snapshot(self):
        z = self.zone(capacity=3).json()
        live = self.client.get("/api/analytics/live").json()
        self.assertEqual(live["zones"][str(z["id"])]["occupancy"], 0)
        self.assertEqual(live["zones"][str(z["id"])]["capacity"], 3)
        self.assertIn("cameras", live)

    def test_series_and_overview(self):
        z = self.zone().json()
        s = SessionLocal()
        start = analytics.bucket_start(utcnow())
        s.add(ZoneStat(zone_id=z["id"], bucket=start - timedelta(minutes=10), entries=5, exits=4, samples=10,
                       occupancy_sum=30, occupancy_max=6))
        s.commit(); s.close()
        series = self.client.get(f"/api/analytics/zones/{z['id']}/series?minutes=60").json()
        self.assertEqual(series["bucket_sec"], 300)
        self.assertEqual([(p["entries"], p["occupancy_avg"], p["occupancy_max"]) for p in series["series"]], [(5, 3.0, 6)])
        ov = self.client.get("/api/analytics/overview?minutes=60").json()["items"]
        self.assertEqual((ov[0]["name"], ov[0]["entries"], ov[0]["exits"], ov[0]["occupancy_max"]), ("Hall", 5, 4, 6))
        self.assertEqual(self.client.get("/api/analytics/zones/999/series").status_code, 404)
        self.assertEqual(self.client.get("/api/analytics/overview?minutes=1").status_code, 422)

    def test_events_reach_the_feed_and_the_analytics_family_filter(self):
        z = self.zone(restricted=True).json()
        cam = main.app.state.cams["entre"]
        eng = main.app.state.analytics
        import time
        t = time.time()
        for i in range(3):
            cam.latest_events = [{"track_id": 1, "fx": .5, "fy": .5, "mode": "UNK"}]
            cam.frame_seq += 1
            eng.step(t + i * 0.2)
        got = self.client.get("/api/events?family=analytics").json()
        types = [e["type"] for e in got["events"]]
        self.assertIn(events.ZONE_INTRUSION, types)
        self.assertEqual(self.client.get("/api/events?family=autre").status_code, 422)

    def test_deleting_a_camera_does_not_leave_the_engine_broken(self):
        self.zone()
        self.client.delete("/api/admin/cameras/entre")
        main.app.state.analytics.step()
        self.assertEqual(self.client.get("/api/analytics/live").status_code, 200)


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in ZonesApiTests.__dict__]:
    setattr(ZonesApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
