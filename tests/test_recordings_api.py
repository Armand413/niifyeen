import time
import unittest
from datetime import timedelta
from unittest import mock

import cv2
import numpy as np

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import config, events, main, recordings as rec
from backend.app.db import Event, Recording, Setting, SessionLocal, utcnow


def jpeg(k=0):
    ok, buf = cv2.imencode(".jpg", np.full((48, 64, 3), (10 * k % 255, 60, 90), np.uint8))
    return buf.tobytes()


class RecordingsApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(Recording).delete(); s.query(Event).delete()
        s.query(Setting).filter(Setting.key == rec.SETTINGS_KEY).delete()
        s.commit(); s.close()
        self.rec = main.app.state.recorder
        self.rec.reload()
        self.addCleanup(self.rec.stop_manual, "entre")

    def drive(self, n=8, step=0.3):
        """Fait avancer l'enregistreur avec de vraies heures et des images brutes."""
        cam = main.app.state.cams["entre"]
        for k in range(n):
            cam.publish(annotated=b"A", raw=jpeg(k))
            self.rec.step(time.time())
            time.sleep(step)

    def enable(self, **kw):
        r = self.client.put("/api/recordings/settings", json={"enabled": True, "fps": 10, "event_types": [], **kw})
        self.assertEqual(r.status_code, 200, r.text)

    def test_page_is_served(self):
        self.assertEqual(self.client.get("/recordings").status_code, 200)

    def test_settings_roundtrip_and_errors(self):
        r = self.client.get("/api/recordings/settings").json()
        self.assertFalse(r["settings"]["enabled"])
        self.assertIn("zone_intrusion", r["event_types"])
        self.assertEqual(self.client.put("/api/recordings/settings", json={"fps": 4}).json()["settings"]["fps"], 4)
        for bad in ({"fps": 99}, {"x": 1}, {}, {"schedules": [{"camera_id": "*", "days": [9], "start": "08:00", "end": "09:00"}]}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.put("/api/recordings/settings", json=bad).status_code, 422)

    def test_hard_disabled(self):
        self.rec.hard_disabled = True
        self.addCleanup(setattr, self.rec, "hard_disabled", False)
        with mock.patch.object(config, "RECORDING_HARD_DISABLED", True):
            self.assertEqual(self.client.put("/api/recordings/settings", json={"enabled": True}).status_code, 403)
        self.assertEqual(self.client.post("/api/recordings/start", json={"camera_id": "entre", "minutes": 5}).status_code, 403)
        self.assertTrue(self.client.get("/api/recordings/status").json()["hard_disabled"])

    def test_start_requires_enabling(self):
        r = self.client.post("/api/recordings/start", json={"camera_id": "entre", "minutes": 5})
        self.assertEqual(r.status_code, 409)

    def test_full_cycle_manual_record_play_export_delete(self):
        self.enable()
        r = self.client.post("/api/recordings/start", json={"camera_id": "entre", "minutes": 5})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.client.post("/api/recordings/start", json={"camera_id": "zz", "minutes": 5}).status_code, 404)
        self.assertEqual(self.client.post("/api/recordings/start", json={"camera_id": "entre", "minutes": 0}).status_code, 422)
        self.drive()
        self.assertEqual(self.client.get("/api/recordings/status").json()["recording"], ["entre"])
        self.assertEqual(self.client.post("/api/recordings/stop", json={"camera_id": "entre"}).json(), {"stopped": True})
        self.drive(2, 0.05)
        items = self.client.get("/api/recordings?camera_id=entre").json()["recordings"]
        self.assertEqual(len(items), 1)
        rid = items[0]["id"]
        self.assertEqual(items[0]["status"], "done")
        d = self.client.get(f"/api/recordings/{rid}").json()
        self.assertGreater(d["frames"], 3)
        self.assertEqual(len(d["t"]), d["frames"])
        f = self.client.get(f"/api/recordings/{rid}/frame/0")
        self.assertEqual((f.status_code, f.headers["content-type"]), (200, "image/jpeg"))
        self.assertEqual(self.client.get(f"/api/recordings/{rid}/frame/9999").status_code, 404)
        self.assertEqual(self.client.get("/api/recordings/9999").status_code, 404)
        start = (utcnow() - timedelta(minutes=2)).isoformat() + "Z"
        end = (utcnow() + timedelta(minutes=1)).isoformat() + "Z"
        v = self.client.get(f"/api/recordings/export.avi?camera_id=entre&start={start}&end={end}")
        self.assertEqual(v.status_code, 200, v.text[:200])
        self.assertEqual(v.headers["content-type"], "video/x-msvideo")
        self.assertTrue(v.content[:4] == b"RIFF")
        self.assertEqual(self.client.get(f"/api/recordings/export.avi?camera_id=entre&start={end}&end={start}").status_code, 422)
        self.assertEqual(self.client.get("/api/recordings/export.avi?camera_id=entre&start=hier&end=demain").status_code, 422)
        self.client.put("/api/recordings/settings", json={"allow_export": False})
        self.assertEqual(self.client.get(f"/api/recordings/export.avi?camera_id=entre&start={start}&end={end}").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/recordings/{rid}").json(), {"deleted": True})
        self.assertEqual(self.client.delete(f"/api/recordings/{rid}").status_code, 404)

    def test_cannot_delete_while_recording(self):
        self.enable()
        self.client.post("/api/recordings/start", json={"camera_id": "entre", "minutes": 5})
        self.drive(4, 0.05)
        rid = self.client.get("/api/recordings").json()["recordings"][0]["id"]
        self.assertEqual(self.client.delete(f"/api/recordings/{rid}").status_code, 409)

    def test_event_has_a_clip_only_if_recording_was_on(self):
        self.enable()
        before = main.app.state.feed.emit("camera_offline", "Avant", camera_id="entre", timestamp=utcnow() - timedelta(seconds=30))
        self.assertEqual(self.client.get(f"/api/investigation/events/{before['id']}").json()["video"]["available"], False)
        self.client.post("/api/recordings/start", json={"camera_id": "entre", "minutes": 5})
        self.drive(3, 0.1)
        during = main.app.state.feed.emit("camera_offline", "Pendant", camera_id="entre")
        self.drive(4, 0.1)
        self.client.post("/api/recordings/stop", json={"camera_id": "entre"}); self.drive(2, 0.05)
        v = self.client.get(f"/api/investigation/events/{during['id']}").json()["video"]
        self.assertTrue(v["available"])
        self.assertIn("recording_id", v)
        hit = self.client.get(f"/api/recordings/for-event/{during['id']}").json()
        self.assertTrue(hit["available"])
        self.assertEqual(self.client.get(f"/api/recordings/for-event/{before['id']}").json()["available"], False)
        self.assertEqual(self.client.get("/api/recordings/for-event/99999").status_code, 404)

    def test_list_validation(self):
        self.assertEqual(self.client.get("/api/recordings?minutes=0").status_code, 422)
        self.assertEqual(self.client.get("/api/recordings?start=hier").status_code, 422)


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in RecordingsApiTests.__dict__]:
    setattr(RecordingsApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
