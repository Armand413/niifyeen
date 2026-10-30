import io
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

import tests  # noqa: F401
from backend.app import config, main
from backend.app.db import SessionLocal, Participant, EmbeddingRef, Detection, init_db


class FakePipeline:
    """Remplace PersonPipeline : aucun modèle, aucune caméra."""
    def __init__(self, **kw):
        self.camera_id = kw["camera_id"]
        self.latest_events = []
        self.latest_jpeg = None
        self.running = False
        self.known = None
        self.target = None

    def set_known(self, known): self.known = known
    def start(self): self.running = True
    def stop(self): self.running = False
    def set_target(self, emb, label="CIBLE", threshold=None): self.target = (emb, threshold)
    def clear_target(self): self.target = None


def fake_face_embedding(img):
    return np.ones(512, dtype=np.float32) / np.sqrt(512)


def png_bytes(size=8):
    import cv2
    ok, buf = cv2.imencode(".png", np.zeros((size, size, 3), dtype=np.uint8))
    return buf.tobytes()


class ApiTests(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal()
        for model in (Detection, EmbeddingRef, Participant):
            s.query(model).delete()
        s.commit()
        s.close()
        cams = [{"id": "entre", "source": 0}, {"id": "salle", "source": 1}]
        patches = [mock.patch.object(main, "PersonPipeline", FakePipeline),
                   mock.patch.object(config, "load_cameras", return_value=cams),
                   mock.patch.object(main, "_face_embedding", fake_face_embedding)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def enroll(self, nom="Awa", consent="true", photos=1, data=None):
        form = {"nom": nom, "infos": "", "consentement": consent}
        files = [("photos", (f"p{i}.png", io.BytesIO(data or png_bytes()), "image/png"))
                 for i in range(photos)]
        return self.client.post("/participants/enroll", data=form, files=files)

    # ---- caméras & pages ----------------------------------------------
    def test_cameras_and_meta_use_the_same_ids(self):
        j = self.client.get("/api/cameras").json()
        self.assertEqual(j["cameras"], ["entre", "salle"])
        self.assertEqual(set(j["camera_meta"]), set(j["cameras"]))

    def test_pages_are_served(self):
        for path in ("/", "/cameras", "/logs"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_unknown_camera_is_404(self):
        self.assertEqual(self.client.get("/video/nope").status_code, 404)

    # ---- enrôlement -----------------------------------------------------
    def test_enroll_requires_consent(self):
        r = self.enroll(consent="false")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/participants").json(), [])

    def test_enroll_ok_then_list_then_delete(self):
        r = self.enroll()
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["participant_id"]
        self.assertEqual(r.json()["saved_embeddings"], 1)
        self.assertEqual([p["nom"] for p in self.client.get("/participants").json()], ["Awa"])
        self.assertEqual(len(self.client.app.state.cams["entre"].known), 1)

        d = self.client.delete(f"/participants/{pid}")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(self.client.get("/participants").json(), [])
        self.assertEqual(self.client.app.state.cams["entre"].known, [])
        self.assertEqual(self.client.delete(f"/participants/{pid}").status_code, 404)

    def test_enroll_without_face_creates_no_record(self):
        with mock.patch.object(main, "_face_embedding", lambda img: None):
            r = self.enroll()
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.client.get("/participants").json(), [])

    def test_enroll_rejects_blank_name_and_too_many_photos(self):
        self.assertEqual(self.enroll(nom="   ").status_code, 422)
        self.assertEqual(self.enroll(photos=config.MAX_ENROLL_PHOTOS + 1).status_code, 400)

    def test_enroll_rejects_oversized_file(self):
        with mock.patch.object(config, "MAX_UPLOAD_BYTES", 100):
            r = self.enroll(data=b"0" * 500)
        self.assertEqual(r.status_code, 413)

    def test_enroll_ignores_unreadable_images(self):
        r = self.enroll(data=b"not an image")
        self.assertEqual(r.status_code, 422)

    def test_enroll_rejects_overlong_name(self):
        self.assertEqual(self.enroll(nom="x" * (config.MAX_NAME_LENGTH + 1)).status_code, 422)

    # ---- cible ------------------------------------------------------------
    def test_target_set_and_clear(self):
        files = [("photos", ("t.png", io.BytesIO(png_bytes()), "image/png"))]
        r = self.client.post("/search/by-image", files=files)
        self.assertEqual(r.json()["target_set"], True)
        cam = self.client.app.state.cams["entre"]
        self.assertIsNotNone(cam.target)
        self.assertEqual(cam.target[1], config.TARGET_THRESHOLD)
        self.assertEqual(self.client.post("/target/clear").json(), {"cleared": True})
        self.assertIsNone(cam.target)

    # ---- journal -----------------------------------------------------------
    def test_recent_detections_have_utc_suffix(self):
        s = SessionLocal()
        s.add(Detection(camera_id="entre", mode="UNK"))
        s.commit()
        s.close()
        rows = self.client.get("/detections/recent").json()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["timestamp"].endswith("Z"))

    def test_websocket_sends_target_state(self):
        with self.client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
        self.assertEqual(msg["cameras"], ["entre", "salle"])
        self.assertIn("target", msg)
        self.assertEqual(msg["target_path"], [])


if __name__ == "__main__":
    unittest.main()
