import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import tests  # noqa: F401
from backend.app import person_pipeline as pp


class FakeCap:
    def __init__(self, frames):
        self.frames = frames          # nombre d'images avant la coupure
        self.released = False

    def read(self):
        if self.released:
            raise AssertionError("lecture après release()")
        if self.frames > 0:
            self.frames -= 1
            time.sleep(0.01)
            return True, np.zeros((72, 128, 3), dtype=np.uint8)
        return False, None

    def release(self):
        self.released = True


class FakeYolo:
    def __init__(self, *a, **k): pass
    def track(self, *a, **k):
        return [SimpleNamespace(boxes=None)]   # aucune personne détectée


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class PipelineLifecycleTests(unittest.TestCase):
    def make(self, opens):
        with mock.patch.object(pp, "YOLO", FakeYolo):
            cam = pp.PersonPipeline(source=0, camera_id="entre", out_size=(64, 36),
                                    reconnect_after_sec=0.15)
        caps = []

        def fake_open():
            cap = FakeCap(frames=opens[min(len(caps), len(opens) - 1)])
            caps.append(cap)
            cam.cap = cap
            return True

        cam._try_open = fake_open
        return cam, caps

    def test_reconnects_after_stream_loss_and_clears_stale_events(self):
        cam, caps = self.make(opens=[3, 3])
        cam.latest_events = [{"mode": "TARGET"}]
        cam.start()
        try:
            self.assertTrue(wait_for(lambda: len(caps) >= 2), "pas de reconnexion")
            self.assertTrue(caps[0].released, "l'ancienne capture doit être libérée")
            self.assertEqual(cam.latest_events, [])
        finally:
            cam.stop()

    def test_stop_joins_thread_before_releasing_capture(self):
        cam, caps = self.make(opens=[10_000])
        cam.start()
        self.assertTrue(wait_for(lambda: cam.latest_jpeg is not None and caps))
        cam.stop()                       # lèverait AssertionError si le thread lisait après release
        self.assertFalse(cam.thread.is_alive())
        self.assertTrue(caps[0].released)
        self.assertIsNone(cam.cap)

    def test_stop_is_prompt_when_camera_never_opens(self):
        with mock.patch.object(pp, "YOLO", FakeYolo):
            cam = pp.PersonPipeline(source=0, camera_id="x", out_size=(64, 36))
        cam._try_open = lambda: False
        cam.start()
        time.sleep(0.1)
        t0 = time.time()
        cam.stop()
        self.assertLess(time.time() - t0, 1.0)   # n'attend pas les 2 s de pause entre tentatives
        self.assertFalse(cam.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
