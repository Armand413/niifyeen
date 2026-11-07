import io
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as exc:   # client de test absent : httpx / httpx2
    raise unittest.SkipTest(f"client de test indisponible ({exc}); pip install -r requirements-dev.txt")

import tests  # noqa: F401
from tests.fakes import FakeCamera
from tests.test_camera_server import Served
from backend.app import config, main
from backend.app.db import (SessionLocal, Participant, EmbeddingRef, Detection, TargetMove, Event, Camera, Setting,
                            init_db, utcnow)


FakePipeline = FakeCamera


def fake_face_embedding(img):
    return np.ones(512, dtype=np.float32) / np.sqrt(512)


def png_bytes(size=8, value=0):
    import cv2
    ok, buf = cv2.imencode(".png", np.full((size, size, 3), value, dtype=np.uint8))
    return buf.tobytes()


def embedding_from_pixel(img):
    """Une « personne » = la valeur du premier pixel : deux valeurs différentes donnent deux visages différents."""
    v = np.zeros(512, dtype=np.float32)
    v[int(img[0, 0, 0])] = 1.0
    return v


class ApiTests(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal()
        for model in (Detection, TargetMove, Event, EmbeddingRef, Participant, Camera, Setting):
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
    # ---- journal -----------------------------------------------------------
    def test_recent_detections_have_utc_suffix(self):
        s = SessionLocal()
        s.add(Detection(camera_id="entre", mode="UNK"))
        s.commit()
        s.close()
        rows = self.client.get("/detections/recent").json()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["timestamp"].endswith("Z"))

    # ---- historique ----------------------------------------------------------
    def seed_history(self):
        s = SessionLocal()
        now = utcnow()
        from datetime import timedelta
        for i in range(5):
            s.add(Detection(camera_id="entre", track_id=1, mode="DB", label="Awa", db_score=.6,
                            timestamp=now - timedelta(seconds=60 - i)))
        s.add(Detection(camera_id="salle", track_id=2, mode="UNK", timestamp=now - timedelta(seconds=30)))
        s.commit()
        s.close()

    def test_history_page_and_episodes(self):
        self.assertEqual(self.client.get("/history").status_code, 200)
        self.seed_history()
        j = self.client.get("/api/history/episodes?minutes=10").json()
        self.assertEqual(j["total"], 2)
        awa = [e for e in j["items"] if e["label"] == "Awa"][0]
        self.assertEqual((awa["observations"], awa["mode"], awa["camera_id"]), (5, "DB", "entre"))
        self.assertEqual(self.client.get("/api/history/episodes?minutes=10&camera_id=salle").json()["total"], 1)
        self.assertEqual(self.client.get("/api/history/episodes?minutes=10&mode=DB&label=aw").json()["total"], 1)

    def test_history_summary_and_csv(self):
        self.seed_history()
        s = self.client.get("/api/history/summary?minutes=10").json()
        self.assertEqual((s["passages"], s["observations"]), (2, 6))
        r = self.client.get("/api/history/episodes.csv?minutes=10")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertEqual(r.content.decode("utf-8-sig").splitlines()[0].split(";")[0], "debut_utc")
        self.assertEqual(len(r.content.decode("utf-8-sig").splitlines()), 3)

    def test_history_validation_errors_are_422(self):
        for q in ("mode=XX", "start=abc", "page=0", "page_size=500",
                  "start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z"):
            with self.subTest(q=q):
                self.assertEqual(self.client.get(f"/api/history/episodes?{q}").status_code, 422)
        self.assertEqual(self.client.get("/api/history/episodes.csv?mode=XX").status_code, 422)

    # ---- fiche de la cible -------------------------------------------------------
    def search(self):
        files = [("photos", ("t.png", io.BytesIO(png_bytes()), "image/png"))]
        return self.client.post("/search/by-image", files=files)
    # ---- cibles multiples ------------------------------------------------------
    def search(self, value=0, count=1):
        files = [("photos", (f"t{i}.png", io.BytesIO(png_bytes(value=value)), "image/png")) for i in range(count)]
        return self.client.post("/search/by-image", files=files)

    def use_pixel_faces(self):
        p = mock.patch.object(main, "_face_embedding", embedding_from_pixel)
        p.start()
        self.addCleanup(p.stop)

    def test_adding_a_target_keeps_the_previous_ones(self):
        self.use_pixel_faces()
        a = self.search(value=1).json()
        b = self.search(value=2).json()
        self.assertEqual((a["target"]["label"], b["target"]["label"]), ("Cible 1", "Cible 2"))
        self.assertEqual(b["targets_count"], 2)
        listing = self.client.get("/targets").json()
        self.assertEqual([t["label"] for t in listing["targets"]], ["Cible 1", "Cible 2"])
        self.assertEqual(listing["max"], config.MAX_TARGETS)
        self.assertEqual(len({t["color"] for t in listing["targets"]}), 2)
        for cam in self.client.app.state.cams.values():           # les caméras reconnaissent les deux
            self.assertEqual([t["id"] for t in cam.targets], [1, 2])

    def test_each_target_keeps_its_photo(self):
        self.use_pixel_faces()
        self.search(value=1); self.search(value=2)
        for t in self.client.get("/targets").json()["targets"]:
            r = self.client.get(t["photo_url"])
            self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/jpeg"))
            self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/targets/99/photo").status_code, 404)

    def test_remove_one_target_only(self):
        self.use_pixel_faces()
        self.search(value=1); self.search(value=2)
        self.assertEqual(self.client.delete("/targets/1").json()["targets_count"], 1)
        self.assertEqual([t["label"] for t in self.client.get("/targets").json()["targets"]], ["Cible 2"])
        self.assertEqual([t["id"] for t in self.client.app.state.cams["entre"].targets], [2])
        self.assertEqual(self.client.delete("/targets/1").status_code, 404)
        self.assertEqual(self.client.get("/targets/1/photo").status_code, 404)

    def test_clear_removes_every_target(self):
        self.use_pixel_faces()
        self.search(value=1); self.search(value=2)
        self.assertEqual(self.client.post("/target/clear").json(), {"cleared": True, "removed": 2})
        self.assertEqual(self.client.get("/targets").json()["targets"], [])
        self.assertEqual(self.client.app.state.cams["entre"].targets, [])

    def test_same_person_twice_and_too_many_targets_are_refused(self):
        self.use_pixel_faces()
        self.search(value=1)
        dup = self.search(value=1)
        self.assertEqual(dup.status_code, 409)
        self.assertIn("Cible 1", dup.json()["detail"])
        for v in range(2, config.MAX_TARGETS + 1):
            self.assertEqual(self.search(value=v).status_code, 200)
        full = self.search(value=config.MAX_TARGETS + 1)
        self.assertEqual(full.status_code, 409)
        self.assertIn("maximal", full.json()["detail"])
        self.assertEqual(len(self.client.get("/targets").json()["targets"]), config.MAX_TARGETS)

    def test_a_photo_without_face_does_not_remove_existing_targets(self):
        self.use_pixel_faces()
        self.search(value=1)
        with mock.patch.object(main, "_face_embedding", lambda img: None):
            r = self.search(value=2).json()
        self.assertEqual((r["target_set"], r["reason"], r["targets_count"]), (False, "no_valid_face", 1))
        self.assertEqual(len(self.client.get("/targets").json()["targets"]), 1)

    def test_each_target_has_its_own_profile(self):
        self.use_pixel_faces()
        # Awa est enrôlée avec le « visage » 1 ; la cible 2 n'est pas dans la base.
        with mock.patch.object(main, "_face_embedding", embedding_from_pixel):
            self.enroll(nom="Awa Ba", data=png_bytes(value=1))
        first = self.search(value=1).json()
        second = self.search(value=2).json()
        self.assertTrue(first["profile"]["matched"])
        self.assertEqual(first["profile"]["participant"]["nom"], "Awa Ba")
        self.assertFalse(second["profile"]["matched"])
        listing = {t["label"]: t["profile"] for t in self.client.get("/targets").json()["targets"]}
        self.assertTrue(listing["Cible 1"]["matched"])
        self.assertEqual(listing["Cible 2"]["reason"], "not_enrolled")
        self.assertNotIn("Awa", str(listing["Cible 2"]))

    def test_profile_shows_the_full_enrolment_record_and_activity(self):
        self.use_pixel_faces()
        with mock.patch.object(main, "_face_embedding", embedding_from_pixel):
            pid = self.enroll(nom="Awa Ba", data=png_bytes(value=1)).json()["participant_id"]
        s = SessionLocal()
        s.query(Participant).filter(Participant.id == pid).update({"infos": "Équipe A"})
        from datetime import timedelta
        for i in range(4):
            s.add(Detection(camera_id="salle", track_id=7, mode="DB", label="Awa Ba", db_score=.6,
                            participant_id=pid, match_status="probable",
                            timestamp=utcnow() - timedelta(seconds=60 - i)))
        s.commit(); s.close()
        profile = self.search(value=1).json()["profile"]
        self.assertEqual((profile["participant"]["nom"], profile["participant"]["infos"]), ("Awa Ba", "Équipe A"))
        self.assertEqual(profile["enrolment"]["embeddings"], 1)
        self.assertEqual((profile["activity"]["passages"], profile["activity"]["last_camera"]), (1, "salle"))
        self.assertAlmostEqual(profile["score"], 1.0, places=2)

    def test_profile_reports_a_deleted_person(self):
        self.use_pixel_faces()
        with mock.patch.object(main, "_face_embedding", embedding_from_pixel):
            pid = self.enroll(nom="Awa Ba", data=png_bytes(value=1)).json()["participant_id"]
        self.search(value=1)
        self.client.delete(f"/participants/{pid}")
        self.assertEqual(self.client.get("/targets").json()["targets"][0]["profile"]["reason"], "deleted")

    def test_profile_hides_identities_when_configured(self):
        self.use_pixel_faces()
        with mock.patch.object(main, "_face_embedding", embedding_from_pixel):
            self.enroll(nom="Awa Ba", data=png_bytes(value=1))
        self.search(value=1)
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            profile = self.client.get("/targets").json()["targets"][0]["profile"]
        self.assertEqual((profile["matched"], profile["hidden"]), (True, True))
        self.assertNotIn("Awa", str(profile))

    def test_trajectories_are_saved_separately_for_each_target(self):
        self.use_pixel_faces()
        self.search(value=1); self.search(value=2)
        registry = self.client.app.state.targets
        t1, t2 = registry.get(1), registry.get(2)
        best = lambda cam: {"camera_id": cam, "track_id": 1, "global_id": 1, "score": .8}
        base = time.time() - 120          # dans le passé : la période demandée s'arrête à « maintenant »
        t1.tracker.update(best("entre"), base);       t1.tracker.update(best("salle"), base + 1.0)
        t1.tracker.update(best("salle"), base + 2.0)
        t2.tracker.update(best("sortie"), base + 3.0)
        j = self.client.get("/api/history/trajectories?minutes=10").json()
        self.assertEqual(j["total"], 2)
        by_label = {s["label"]: [m["camera_id"] for m in s["moves"]] for s in j["items"]}
        self.assertEqual(by_label, {"Cible 1": ["entre", "salle"], "Cible 2": ["sortie"]})

    def test_a_removed_target_stops_being_recorded_but_keeps_its_history(self):
        self.use_pixel_faces()
        self.search(value=1)
        t1 = self.client.app.state.targets.get(1)
        base = time.time() - 60
        t1.tracker.update({"camera_id": "entre", "track_id": 1, "global_id": 1, "score": .8}, base)
        self.client.delete("/targets/1")
        self.assertEqual(self.client.get("/api/history/trajectories?minutes=10").json()["total"], 1)

    # ---- centre de supervision ---------------------------------------------------
    def cam(self, cam_id):
        return self.client.app.state.cams[cam_id]

    def test_supervision_page_is_served(self):
        self.assertEqual(self.client.get("/supervision").status_code, 200)

    def test_supervision_snapshot(self):
        self.cam("entre").latest_events = [{"mode": "UNK"}, {"mode": "DB", "label": "Awa", "db_score": .6}]
        self.cam("salle").forced_status = "offline"
        j = self.client.get("/api/supervision/cameras").json()
        cards = {c["id"]: c for c in j["cameras"]}
        self.assertEqual((cards["entre"]["status"], cards["entre"]["persons"]), ("online", 2))
        self.assertEqual((cards["salle"]["status"], cards["salle"]["fps"]), ("offline", 0.0))
        self.assertEqual(j["people"], [{"camera_id": "entre", "participant_id": None, "label": "Awa", "score": 0.6,
                                        "status": None, "active": True, "ambiguous": False}])
        self.assertEqual((j["targets"], j["identities_visible"]), ([], True))
        self.assertIn("feed_last_id", j)

    def test_supervision_never_exposes_the_stream_token(self):
        cams = [{"id": "entre", "source": "http://192.168.1.31:8081/video?token=TOPSECRET"}]
        s = SessionLocal(); s.query(Camera).delete(); s.query(Setting).delete(); s.commit(); s.close()
        with mock.patch.object(config, "load_cameras", return_value=cams):
            with TestClient(main.app) as client:
                body = client.get("/api/supervision/cameras").text
                self.assertNotIn("TOPSECRET", body)
                self.assertIn("http://192.168.1.31:8081/video?…", body)
                self.assertNotIn("TOPSECRET", client.get("/api/cameras").text)

    def test_events_endpoint_and_filters(self):
        feed = self.client.app.state.feed
        a = feed.emit("target_seen", "Cible 1 repérée", camera_id="entre")
        feed.emit("camera_offline", "Caméra hors ligne : Salle", severity="warning", camera_id="salle")
        j = self.client.get("/api/events?limit=10").json()
        self.assertEqual([e["type"] for e in j["events"]][:2], ["camera_offline", "target_seen"])
        self.assertEqual(j["last_id"], feed.last_id)
        self.assertEqual(len(self.client.get(f"/api/events?after_id={a['id']}").json()["events"]), 1)
        self.assertEqual(len(self.client.get("/api/events?family=targets").json()["events"]), 1)
        self.assertEqual(len(self.client.get("/api/events?severity=warning").json()["events"]), 1)
        self.assertEqual(len(self.client.get("/api/events?camera_id=salle").json()["events"]), 1)
        for bad in ("family=nope", "severity=high", "limit=0", "limit=500", "after_id=-1"):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.get(f"/api/events?{bad}").status_code, 422)

    def test_live_websocket_carries_camera_status_people_and_feed_id(self):
        self.cam("salle").forced_status = "reconnecting"
        self.cam("entre").latest_events = [{"mode": "DB", "label": "Awa", "db_score": .6}]
        with self.client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
        self.assertEqual(msg["cameras_status"]["salle"]["status"], "reconnecting")
        self.assertEqual(msg["cameras_status"]["entre"]["persons"], 1)
        self.assertEqual(msg["people"][0]["label"], "Awa")
        self.assertEqual(msg["feed_last_id"], self.client.app.state.feed.last_id)

    def test_target_events_are_emitted_when_a_target_moves(self):
        self.use_pixel_faces()
        self.search(value=1)
        t1 = self.client.app.state.targets.get(1)
        base = time.time() - 60
        for cam, dt in (("entre", 0), ("salle", 1.0), ("salle", 2.0)):
            t1.tracker.update({"camera_id": cam, "track_id": 1, "global_id": 1, "score": .8}, base + dt)
        titles = [e["title"] for e in reversed(self.client.get("/api/events?family=targets").json()["events"])]
        self.assertEqual(titles, ["Cible 1 repérée : entre", "Cible 1 : entre → salle"])

    # ---- flux vidéo multiplexé ---------------------------------------------------
    def read_frame(self, ws):
        data = ws.receive_bytes()
        n = data[0]
        return data[1:1 + n].decode(), data[1 + n:]

    def test_video_socket_sends_framed_jpegs_for_subscribed_cameras_only(self):
        self.cam("entre").latest_jpeg = b"\xff\xd8ENTRE\xff\xd9"
        self.cam("salle").latest_jpeg = b"\xff\xd8SALLE\xff\xd9"
        with self.client.websocket_connect("/ws/video") as ws:
            ws.send_json({"subscribe": ["salle"], "fps": 25})
            cam_id, jpeg = self.read_frame(ws)
            self.assertEqual((cam_id, jpeg), ("salle", b"\xff\xd8SALLE\xff\xd9"))
            self.cam("salle").publish(annotated=b"\xff\xd8NEW\xff\xd9")
            self.assertEqual(self.read_frame(ws), ("salle", b"\xff\xd8NEW\xff\xd9"))

    def test_video_socket_ignores_unknown_cameras_and_bad_messages(self):
        with self.client.websocket_connect("/ws/video") as ws:
            ws.send_text("pas du json")
            ws.send_json([1, 2, 3])
            ws.send_json({"subscribe": ["inconnue", 42, "entre"], "fps": "vite"})
            self.assertEqual(self.read_frame(ws)[0], "entre")

    def test_video_socket_sends_each_image_only_once(self):
        with self.client.websocket_connect("/ws/video") as ws:
            ws.send_json({"subscribe": ["entre"], "fps": 25})
            self.read_frame(ws)
            self.cam("entre").publish(annotated=b"\xff\xd8A\xff\xd9")
            self.assertEqual(self.read_frame(ws)[1], b"\xff\xd8A\xff\xd9")
            self.cam("entre").publish(annotated=b"\xff\xd8B\xff\xd9")
            self.assertEqual(self.read_frame(ws)[1], b"\xff\xd8B\xff\xd9")        # B et non A une seconde fois

    def test_video_socket_raw_mode_registers_a_viewer_and_sends_fresh_raw_images(self):
        cam = self.cam("entre")
        with self.client.websocket_connect("/ws/video") as ws:
            ws.send_json({"subscribe": ["entre"], "annotations": False, "fps": 25})
            deadline = time.time() + 3
            while time.time() < deadline and cam.raw_viewers == 0:
                time.sleep(0.01)
            self.assertEqual(cam.raw_viewers, 1)
            cam.publish(annotated=b"\xff\xd8ANNOT\xff\xd9", raw=b"\xff\xd8RAW\xff\xd9")
            self.assertEqual(self.read_frame(ws), ("entre", b"\xff\xd8RAW\xff\xd9"))   # jamais l'image annotée
            ws.send_json({"annotations": True})
            cam.publish(annotated=b"\xff\xd8ANNOT2\xff\xd9")
            self.assertEqual(self.read_frame(ws), ("entre", b"\xff\xd8ANNOT2\xff\xd9"))
            deadline = time.time() + 3
            while time.time() < deadline and cam.raw_viewers != 0:
                time.sleep(0.01)
            self.assertEqual(cam.raw_viewers, 0)                   # le coût d'encodage disparaît avec le dernier spectateur

    def test_video_socket_releases_raw_viewers_on_disconnect(self):
        cam = self.cam("salle")
        with self.client.websocket_connect("/ws/video") as ws:
            ws.send_json({"subscribe": ["salle"], "annotations": False})
            deadline = time.time() + 3
            while time.time() < deadline and cam.raw_viewers == 0:
                time.sleep(0.01)
            self.assertEqual(cam.raw_viewers, 1)
        deadline = time.time() + 3
        while time.time() < deadline and cam.raw_viewers != 0:
            time.sleep(0.01)
        self.assertEqual(cam.raw_viewers, 0)

    def test_video_socket_limits_subscriptions(self):
        self.assertEqual(main.MAX_VIDEO_SUBSCRIPTIONS, 16)

    # ---- gestion des caméras -------------------------------------------------------
    def admin(self):
        return {c["id"]: c for c in self.client.get("/api/admin/cameras").json()["cameras"]}

    def create_cam(self, **extra):
        body = {"label": "Parking", "source": "rtsp://admin:MOTDEPASSE@192.168.1.9:554/s?cle=CLE"}
        body.update(extra)
        return self.client.post("/api/admin/cameras", json=body)

    def test_cameras_page_is_served(self):
        self.assertEqual(self.client.get("/admin/cameras").status_code, 200)

    def test_cameras_come_from_the_database_after_the_first_start(self):
        j = self.client.get("/api/admin/cameras").json()
        self.assertEqual([c["id"] for c in j["cameras"]], ["entre", "salle"])
        self.assertEqual((j["cameras"][0]["kind"], j["cameras"][0]["source_display"]), ("webcam", "Webcam 0"))
        self.assertEqual(j["max"], config.MAX_CAMERAS)

    def test_create_a_camera_without_restarting_the_server(self):
        v0 = self.client.get("/api/supervision/cameras").json()["cameras_version"]
        r = self.create_cam(location="Extérieur", process_fps=5, capture_width=1920, capture_height=1080, x=0.4, y=0.2)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["id"], r.json()["status"], r.json()["kind"]), ("parking", "online", "rtsp"))
        self.assertIn("parking", self.client.app.state.cams)
        sup = self.client.get("/api/supervision/cameras").json()
        self.assertEqual([c["id"] for c in sup["cameras"]], ["entre", "salle", "parking"])
        self.assertGreater(sup["cameras_version"], v0)
        self.assertEqual(self.client.get("/api/cameras").json()["camera_meta"]["parking"]["x"], 0.4)
        with self.client.websocket_connect("/ws/live") as ws:
            self.assertEqual(ws.receive_json()["cameras_version"], sup["cameras_version"])

    def test_the_secret_never_leaves_the_server(self):
        self.create_cam()
        bodies = [self.client.get(p).text for p in ("/api/admin/cameras", "/api/supervision/cameras", "/api/cameras",
                                                    "/api/events?limit=50")]
        bodies.append(self.create_cam(label="Autre", source="http://10.0.0.7/v?token=JETON").text)
        for body in bodies:
            for secret in ("MOTDEPASSE", "CLE", "JETON"):
                self.assertNotIn(secret, body)
        self.assertEqual(self.admin()["parking"]["source_display"], "rtsp://192.168.1.9:554/s?…")

    def test_validation_errors_name_the_faulty_field(self):
        for body, field in (({"source": 0}, "label"), ({"label": "A"}, "source"),
                            ({"label": "A", "source": "file:///etc/passwd"}, "source"),
                            ({"label": "A", "source": "/etc/passwd"}, "source"),
                            ({"label": "x" * 41, "source": 5}, "label"), ({"label": "A", "source": 5, "x": 3}, "x"),
                            ({"label": "A", "source": 5, "process_fps": 0.1}, "process_fps"),
                            ({"label": "A", "source": 5, "capture_width": 1280}, "capture_width")):
            with self.subTest(body=body):
                r = self.client.post("/api/admin/cameras", json=body)
                self.assertEqual(r.status_code, 422)
                self.assertEqual(r.json()["detail"]["field"], field)
                self.assertTrue(r.json()["detail"]["message"])
        self.assertEqual(self.client.post("/api/admin/cameras", json={"label": "A", "source": 5, "inconnu": 1}).status_code, 422)
        self.assertEqual(sorted(self.admin()), ["entre", "salle"])

    def test_conflicts(self):
        self.assertEqual(self.create_cam().status_code, 201)
        dup = self.create_cam(label="Copie")
        self.assertEqual(dup.status_code, 409)
        self.assertIn("Parking", dup.json()["detail"]["message"])
        self.assertEqual(self.client.post("/api/admin/cameras", json={"label": "W", "source": 0}).status_code, 409)
        with mock.patch.object(config, "MAX_CAMERAS", 3):
            self.assertEqual(self.client.post("/api/admin/cameras", json={"label": "Z", "source": 9}).status_code, 409)

    def test_update_keeps_the_source_when_none_is_sent(self):
        self.create_cam()
        before = self.client.app.state.cams["parking"]
        r = self.client.put("/api/admin/cameras/parking", json={"label": "Parking visiteurs", "source": "", "location": "Nord"})
        self.assertEqual((r.status_code, r.json()["label"], r.json()["location"]), (200, "Parking visiteurs", "Nord"))
        self.assertIs(self.client.app.state.cams["parking"], before)                  # pas de redémarrage du flux
        self.assertEqual(self.client.app.state.camera_sources["parking"], "rtsp://admin:MOTDEPASSE@192.168.1.9:554/s?cle=CLE")

    def test_update_clears_optional_settings_with_null(self):
        self.create_cam(x=0.4, y=0.2, process_fps=5)
        r = self.client.put("/api/admin/cameras/parking", json={"x": None, "y": None, "process_fps": None}).json()
        self.assertEqual((r["x"], r["y"], r["process_fps"]), (None, None, None))

    def test_changing_the_source_restarts_that_camera_only(self):
        self.create_cam()
        cams = self.client.app.state.cams
        parking, entre = cams["parking"], cams["entre"]
        self.client.put("/api/admin/cameras/parking", json={"source": "http://10.0.0.8:8081/video"})
        self.assertIsNot(cams["parking"], parking)
        self.assertFalse(parking.running)
        self.assertIs(cams["entre"], entre)

    def test_disable_enable_restart_and_delete(self):
        self.create_cam()
        cams = self.client.app.state.cams
        r = self.client.put("/api/admin/cameras/parking", json={"enabled": False}).json()
        self.assertEqual((r["enabled"], r["status"]), (False, "disabled"))
        self.assertNotIn("parking", cams)
        self.assertEqual(self.client.get("/video/parking").status_code, 404)
        self.assertEqual(self.client.post("/api/admin/cameras/parking/restart").status_code, 409)
        self.assertTrue(self.client.put("/api/admin/cameras/parking", json={"enabled": True}).json()["enabled"])
        self.assertIn("parking", cams)
        self.assertEqual(self.client.post("/api/admin/cameras/parking/restart").status_code, 200)
        self.assertEqual(self.client.delete("/api/admin/cameras/parking").json(), {"deleted": True})
        self.assertNotIn("parking", self.admin())
        self.assertEqual(self.client.delete("/api/admin/cameras/parking").status_code, 404)
        self.assertEqual(self.client.put("/api/admin/cameras/parking", json={"label": "x"}).status_code, 404)
        types = [e["type"] for e in self.client.get("/api/events?family=cameras").json()["events"]]
        for expected in ("camera_added", "camera_disabled", "camera_enabled", "camera_removed"):
            self.assertIn(expected, types)

    def test_cameras_survive_a_server_restart(self):
        self.create_cam()
        self.client.__exit__(None, None, None)
        try:
            with TestClient(main.app) as again:
                ids = [c["id"] for c in again.get("/api/admin/cameras").json()["cameras"]]
                self.assertEqual(ids, ["entre", "salle", "parking"])
                self.assertIn("parking", again.app.state.cams)
        finally:
            self.client.__enter__()

    def test_a_camera_removed_while_streaming_does_not_break_the_video_socket(self):
        with self.client.websocket_connect("/ws/video") as ws:
            ws.send_json({"subscribe": ["salle", "entre"], "fps": 25})
            seen = {self.read_frame(ws)[0], self.read_frame(ws)[0]}
            self.assertEqual(seen, {"salle", "entre"})
            self.client.delete("/api/admin/cameras/salle")
            self.cam("entre").publish(annotated=b"\xff\xd8NEXT\xff\xd9")
            self.assertEqual(self.read_frame(ws), ("entre", b"\xff\xd8NEXT\xff\xd9"))

    # ---- test de connexion --------------------------------------------------------
    def test_testing_a_source_already_in_service_reuses_its_image(self):
        r = self.client.post("/api/admin/cameras/test", json={"source": 0}).json()
        self.assertEqual((r["ok"], r["already_running"], r["camera"]), (True, True, "entre"))
        self.assertTrue(r["preview"].startswith("data:image/jpeg;base64,"))

    def test_testing_a_real_stream(self):
        served = Served()
        self.addCleanup(served.close)
        url = f"http://127.0.0.1:{served.port}/video"
        r = self.client.post("/api/admin/cameras/test", json={"source": url}).json()
        self.assertEqual((r["ok"], r["width"], r["height"], r["kind"]), (True, 320, 240, "http"))
        self.assertNotIn("already_running", r)
        self.assertTrue(r["preview"].startswith("data:image/jpeg;base64,"))
        # même test sur la source enregistrée d'une caméra, sans la renvoyer au navigateur
        self.client.post("/api/admin/cameras", json={"label": "Distante", "source": url})
        again = self.client.post("/api/admin/cameras/test", json={"camera_id": "distante"}).json()
        self.assertTrue(again["ok"])
        self.assertTrue(again["already_running"])

    def test_testing_a_dead_address(self):
        import socket as _socket
        s = _socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        r = self.client.post("/api/admin/cameras/test", json={"source": f"http://127.0.0.1:{port}/video"}).json()
        self.assertEqual(r["ok"], False)
        self.assertIn("pare-feu", r["error"])

    def test_testing_refuses_unsafe_or_missing_sources(self):
        for body in ({"source": "file:///etc/passwd"}, {"source": "/etc/passwd"}, {}, {"source": ""},
                     {"camera_id": "inconnue"}):
            with self.subTest(body=body):
                self.assertIn(self.client.post("/api/admin/cameras/test", json=body).status_code, (404, 422))

    def test_webcam_detection(self):
        with mock.patch.object(main.camera_probe, "list_webcams", return_value=[{"index": 2, "width": 640, "height": 480}]) as m:
            j = self.client.get("/api/admin/cameras/webcams").json()
        self.assertEqual(j, {"webcams": [{"index": 2, "width": 640, "height": 480}], "in_use": [0, 1]})
        self.assertEqual(m.call_args.args[0], [0, 1])                      # les webcams en service ne sont pas rouvertes

    def test_onvif_discovery_flags_cameras_already_configured(self):
        self.create_cam(label="Dome", source="rtsp://u:p@192.168.1.64:554/s", enabled=False)
        found = [{"address": "192.168.1.64", "xaddrs": [], "name": "Dome", "hardware": "", "suggested_source": "rtsp://192.168.1.64:554/"},
                 {"address": "192.168.1.70", "xaddrs": [], "name": "", "hardware": "", "suggested_source": "rtsp://192.168.1.70:554/"}]
        with mock.patch.object(main.camera_probe, "discover_onvif", return_value=found):
            j = self.client.post("/api/admin/cameras/discover").json()
        self.assertEqual({d["address"]: d["configured"] for d in j["devices"]}, {"192.168.1.64": True, "192.168.1.70": False})
        self.assertIn("WS-Discovery", j["note"])

    # ---- personnes (module 3) ---------------------------------------------------
    def post_person(self, value=1, nom="Ba", **form):
        self.use_pixel_faces_once()
        data = {"nom": nom, "consentement": "true", **form}
        files = [("photos", ("p.png", io.BytesIO(png_bytes(value=value)), "image/png"))]
        return self.client.post("/participants/enroll", data=data, files=files)

    def use_pixel_faces_once(self):
        if not getattr(self, "_pixel_faces", False):
            self._pixel_faces = True
            self.use_pixel_faces()

    def person_detections(self, pid, name, cam="entre", n=3, status="probable"):
        s = SessionLocal()
        from datetime import timedelta
        for i in range(n):
            s.add(Detection(camera_id=cam, track_id=5, mode="DB", label=name, participant_id=pid, match_status=status,
                            db_score=.6, timestamp=utcnow() - timedelta(seconds=60 - i)))
        s.commit(); s.close()

    def test_pages_for_people_are_served(self):
        self.assertEqual(self.client.get("/people").status_code, 200)
        self.assertEqual(self.client.get("/people/5").status_code, 200)

    def test_enrol_with_the_new_fields(self):
        r = self.post_person(1, "Ba", prenom="Awa", matricule="E-042", infos="Équipe A", actif="false")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["display_name"], "Awa Ba")
        card = self.client.get(f"/api/people/{r.json()['participant_id']}").json()
        self.assertEqual((card["nom"], card["prenom"], card["matricule"], card["actif"], card["infos"]),
                         ("Ba", "Awa", "E-042", False, "Équipe A"))
        self.assertTrue(card["date_enrolement"].endswith("Z") and card["date_consentement"].endswith("Z"))
        known = self.client.app.state.cams["entre"].known
        self.assertEqual([(k["participant_id"], k["nom"], k["active"]) for k in known], [(r.json()["participant_id"], "Awa Ba", False)])

    def test_the_old_enrolment_form_still_works(self):
        r = self.post_person(1, "Awa Ba")
        self.assertEqual(r.status_code, 200)
        listing = self.client.get("/participants").json()
        self.assertEqual((listing[0]["nom"], listing[0]["display_name"], listing[0]["actif"]), ("Awa Ba", "Awa Ba", True))

    def test_the_same_face_twice_asks_for_confirmation(self):
        first = self.post_person(1, "Ba", prenom="Awa").json()
        dup = self.post_person(1, "Bah", prenom="Awa")
        self.assertEqual(dup.status_code, 409)
        d = dup.json()["detail"]
        self.assertEqual((d["needs_confirmation"], d["existing_id"], d["existing_name"]), (True, first["participant_id"], "Awa Ba"))
        self.assertEqual(len(self.client.get("/api/people").json()["items"]), 1)
        forced = self.post_person(1, "Bah", prenom="Awa", confirmer_doublon="true")
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(len(self.client.get("/api/people").json()["items"]), 2)

    def test_a_duplicate_warning_never_names_the_person_when_identities_are_hidden(self):
        self.post_person(1, "Ba", prenom="Awa")
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            d = self.post_person(1, "Bah").json()["detail"]
        self.assertNotIn("existing_name", d)
        self.assertNotIn("Awa", str(d))

    def test_matricule_conflict(self):
        self.post_person(1, "Ba", matricule="E-1")
        r = self.post_person(2, "Sy", matricule="e-1")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["field"], "matricule")
        self.assertEqual(len(self.client.get("/api/people").json()["items"]), 1)

    def test_enrolment_validation(self):
        for form, field in (({"nom": "  "}, "nom"), ({"nom": "A", "matricule": "a\tb"}, "matricule")):
            with self.subTest(form=form):
                r = self.post_person(1, **form)
                self.assertEqual(r.status_code, 422)
                self.assertEqual(r.json()["detail"]["field"], field)
        self.assertEqual(self.client.get("/api/people").json()["items"], [])

    def test_search_filter_and_pagination(self):
        self.post_person(1, "Ba", prenom="Awa", matricule="E-1")
        self.post_person(2, "Sy", prenom="Moussa", matricule="E-2", actif="false")
        self.post_person(3, "Diallo")
        names = lambda **q: [i["display_name"] for i in self.client.get("/api/people", params=q).json()["items"]]
        self.assertEqual(names(), ["Awa Ba", "Diallo", "Moussa Sy"])
        self.assertEqual(names(q="moussa"), ["Moussa Sy"])
        self.assertEqual(names(q="e-1"), ["Awa Ba"])
        self.assertEqual(names(statut="inactif"), ["Moussa Sy"])
        self.assertEqual(names(statut="actif", page_size=1, page=2), ["Diallo"])
        for bad in ({"statut": "xx"}, {"page": 0}, {"page_size": 500}):
            self.assertEqual(self.client.get("/api/people", params=bad).status_code, 422)
        self.assertEqual(self.client.get("/api/people").json()["recognition"],
                         {"probable": config.DB_PROBABLE_THRESHOLD, "to_verify": config.DB_THRESHOLD})

    def test_person_card_with_activity_and_zones(self):
        pid = self.post_person(1, "Ba", prenom="Awa").json()["participant_id"]
        self.person_detections(pid, "Awa Ba", "entre", 5)
        self.person_detections(pid, "Awa Ba", "salle", 3, status="a_verifier")
        card = self.client.get(f"/api/people/{pid}").json()
        a = card["activity"]
        self.assertEqual((a["passages"], a["observations"], a["probable_passages"], a["to_verify_passages"]), (2, 8, 1, 1))
        self.assertEqual(sorted(z["zone"] for z in a["zones"]), ["entre", "salle"])
        self.assertEqual(len(card["timeline"]["items"]), 2)
        self.assertEqual(card["references"][0]["source"], "p.png")
        self.assertNotIn("embedding", str(card))
        self.assertEqual(self.client.get("/api/people/9999").status_code, 404)

    def test_update_is_applied_to_the_cameras_immediately(self):
        pid = self.post_person(1, "Ba", prenom="Awa").json()["participant_id"]
        r = self.client.put(f"/api/people/{pid}", json={"prenom": "Awa-Marie", "actif": False, "matricule": "E-9"})
        self.assertEqual((r.status_code, r.json()["display_name"], r.json()["actif"]), (200, "Awa-Marie Ba", False))
        known = self.client.app.state.cams["salle"].known
        self.assertEqual([(k["nom"], k["active"]) for k in known], [("Awa-Marie Ba", False)])
        self.assertEqual(self.client.put(f"/api/people/{pid}", json={}).status_code, 200)

    def test_update_errors(self):
        a = self.post_person(1, "Ba", matricule="E-1").json()["participant_id"]
        self.post_person(2, "Sy", matricule="E-2")
        self.assertEqual(self.client.put(f"/api/people/{a}", json={"matricule": "e-2"}).status_code, 409)
        self.assertEqual(self.client.put(f"/api/people/{a}", json={"nom": " "}).status_code, 422)
        self.assertEqual(self.client.put(f"/api/people/{a}", json={"inconnu": 1}).status_code, 422)
        self.assertEqual(self.client.put("/api/people/999", json={"infos": "x"}).status_code, 404)

    def test_references_add_and_remove(self):
        pid = self.post_person(1, "Ba").json()["participant_id"]
        files = [("photos", ("b.png", io.BytesIO(png_bytes(value=1)), "image/png"))]
        self.assertEqual(self.client.post(f"/api/people/{pid}/references", files=files).json()["added"], 1)
        refs = self.client.get(f"/api/people/{pid}").json()["references"]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(self.client.app.state.cams["entre"].known), 2)
        self.assertEqual(self.client.delete(f"/api/people/{pid}/references/{refs[1]['id']}").status_code, 200)
        last = self.client.delete(f"/api/people/{pid}/references/{refs[0]['id']}")
        self.assertEqual(last.status_code, 409)                                   # jamais sans référence
        self.assertEqual(self.client.delete(f"/api/people/{pid}/references/9999").status_code, 404)

    def test_a_reference_without_a_face_is_refused(self):
        pid = self.post_person(1, "Ba").json()["participant_id"]
        with mock.patch.object(main, "_face_embedding", lambda img: None):
            r = self.client.post(f"/api/people/{pid}/references", files=[("photos", ("b.png", io.BytesIO(png_bytes()), "image/png"))])
        self.assertEqual(r.status_code, 422)

    def test_delete_erases_the_person_everywhere(self):
        pid = self.post_person(1, "Ba", prenom="Awa").json()["participant_id"]
        self.person_detections(pid, "Awa Ba")
        feed = self.client.app.state.feed
        feed.emit("person_recognized", "Personne reconnue : Awa Ba — Entrée", camera_id="entre", person="Awa Ba", participant_id=pid)
        r = self.client.delete(f"/api/people/{pid}")
        self.assertEqual(r.json(), {"deleted": True, "embeddings_deleted": 1, "detections_anonymised": 3, "events_anonymised": 1})
        self.assertNotIn("Awa", self.client.get("/api/events").text)
        self.assertEqual(self.client.app.state.cams["entre"].known, [])
        self.assertEqual(self.client.delete(f"/participants/{pid}").status_code, 404)

    def test_hidden_identities_block_reading_but_not_administration(self):
        pid = self.post_person(1, "Ba", prenom="Awa").json()["participant_id"]
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            for path in ("/api/people", f"/api/people/{pid}"):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 403)
                self.assertNotIn("Awa", r.text)
            self.assertEqual(self.client.put(f"/api/people/{pid}", json={"actif": False}).status_code, 200)

    def test_history_can_be_filtered_by_person(self):
        a = self.post_person(1, "Ba").json()["participant_id"]
        b = self.post_person(2, "Ba").json()["participant_id"]               # homonyme
        self.person_detections(a, "Ba", "entre"); self.person_detections(b, "Ba", "salle")
        j = self.client.get(f"/api/history/episodes?minutes=10&participant_id={a}").json()
        self.assertEqual([i["camera_id"] for i in j["items"]], ["entre"])
        self.assertEqual(j["items"][0]["participant_id"], a)
        csv_text = self.client.get(f"/api/history/episodes.csv?minutes=10&participant_id={b}").content.decode("utf-8-sig")
        self.assertEqual(len(csv_text.strip().splitlines()), 2)
        self.assertEqual(self.client.get("/api/history/episodes?participant_id=0").status_code, 422)

    def test_live_people_carry_the_status(self):
        self.client.app.state.cams["entre"].latest_events = [
            {"mode": "DB", "label": "Awa Ba", "db_score": .6, "participant_id": 4, "match_status": "a_verifier",
             "participant_active": True, "match_ambiguous": True}]
        with self.client.websocket_connect("/ws/live") as ws:
            people = ws.receive_json()["people"]
        self.assertEqual((people[0]["participant_id"], people[0]["status"], people[0]["ambiguous"]), (4, "a_verifier", True))

    def test_websocket_sends_every_target_state(self):
        self.use_pixel_faces()
        with self.client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
        self.assertEqual((msg["targets"], msg["cameras"]), ([], ["entre", "salle"]))
        v0 = msg["targets_version"]
        self.search(value=1); self.search(value=2)
        with self.client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
        self.assertEqual([t["label"] for t in msg["targets"]], ["Cible 1", "Cible 2"])
        self.assertNotEqual(msg["targets_version"], v0)
        self.assertEqual(set(msg["targets"][0]), {"id", "label", "color", "active", "camera_id", "score", "last_seen_ts", "path"})


if __name__ == "__main__":
    unittest.main()
