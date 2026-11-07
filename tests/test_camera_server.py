import http.client
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

import tests  # noqa: F401
from backend.app import person_pipeline as pp
from tools.camera_server import FrameSource, build_server, local_addresses


class Served:
    """Un serveur de caméra en mode image de test, sur un port libre."""

    def __init__(self, token="", port=0):
        self.source = FrameSource(test_pattern=True, fps=30, width=320, height=240).start()
        self.server = build_server(self.source, "127.0.0.1", port, token)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        return conn, conn.getresponse()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.source.stop()


class CameraServerTests(unittest.TestCase):
    def serve(self, **kw):
        s = Served(**kw)
        self.addCleanup(s.close)
        time.sleep(0.2)
        return s

    def test_snapshot_is_a_valid_jpeg(self):
        s = self.serve()
        conn, r = s.get("/snapshot.jpg")
        data = r.read()
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "image/jpeg"))
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(img.shape, (240, 320, 3))
        conn.close()

    def test_video_is_a_multipart_stream_of_distinct_frames(self):
        s = self.serve()
        conn, r = s.get("/video")
        self.assertIn("multipart/x-mixed-replace", r.getheader("Content-Type"))
        buf = b""
        while buf.count(b"--frame") < 4:
            buf += r.read(2048)
        parts = [p for p in buf.split(b"--frame") if b"image/jpeg" in p]
        self.assertGreaterEqual(len(parts), 3)
        self.assertEqual(len({p for p in parts}), len(parts))       # les images changent (l'image bouge)
        conn.close()

    def test_token_is_required_when_configured(self):
        s = self.serve(token="s3cret")
        for path in ("/video", "/snapshot.jpg", "/", "/video?token=wrong", "/snapshot.jpg?token="):
            with self.subTest(path=path):
                conn, r = s.get(path); r.read()
                self.assertEqual(r.status, 401)
                conn.close()
        conn, r = s.get("/snapshot.jpg?token=s3cret"); r.read()
        self.assertEqual(r.status, 200)
        conn.close()

    def test_preview_page_and_unknown_path(self):
        s = self.serve(token="abc")
        conn, r = s.get("/?token=abc")
        self.assertIn(b"/video?token=abc", r.read())
        conn.close()
        conn, r = s.get("/nope?token=abc"); r.read()
        self.assertEqual(r.status, 404)
        conn.close()

    def test_a_client_leaving_does_not_break_the_server(self):
        s = self.serve()
        for _ in range(3):
            conn, r = s.get("/video")
            r.read(500)
            conn.close()                      # fermeture brutale en plein flux
        conn, r = s.get("/snapshot.jpg"); r.read()
        self.assertEqual(r.status, 200)
        conn.close()

    def test_several_clients_share_one_camera(self):
        s = self.serve()
        conns = [s.get("/video") for _ in range(3)]
        for conn, r in conns:
            self.assertGreater(len(r.read(4000)), 1000)
            conn.close()

    def test_local_addresses_are_not_loopback(self):
        self.assertTrue(all(not a.startswith("127.") for a in local_addresses()))


class AppReadsTheRemoteCameraTests(unittest.TestCase):
    def make_pipeline(self, url):
        class NoPeople:
            def __init__(self, *a, **k): pass
            def track(self, *a, **k): return [SimpleNamespace(boxes=None)]
        with mock.patch.object(pp, "YOLO", NoPeople):
            return pp.PersonPipeline(source=url, camera_id="salle", out_size=(64, 36), reconnect_after_sec=0.5)

    def wait_for(self, cond, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_pipeline_reads_frames_from_the_stream_and_survives_the_other_pc_restarting(self):
        s = Served()
        cam = self.make_pipeline(f"http://127.0.0.1:{s.port}/video")
        cam.start()
        try:
            self.assertTrue(self.wait_for(lambda: cam.cap is not None and cam.last_frame_ts > 0),
                            "l'application n'a pas réussi à lire le flux")
            first = cam.last_frame_ts

            s.close()                                                   # l'autre ordinateur s'éteint
            self.assertTrue(self.wait_for(lambda: cam.cap is None, 20), "la coupure n'a pas été détectée")

            s2 = Served(port=s.port)                                    # ... puis se rallume (même adresse)
            self.addCleanup(s2.close)
            self.assertTrue(self.wait_for(lambda: cam.cap is not None and cam.last_frame_ts > first + 0.5, 30),
                            "pas de reconnexion automatique")
        finally:
            cam.stop()
            try:
                s.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
