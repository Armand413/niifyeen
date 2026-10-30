import unittest
from types import SimpleNamespace

import tests  # noqa: F401  (prépare l'environnement)
from backend.app.target_path import TargetTracker, TargetWorker


def best(cam, score=0.8):
    return {"camera_id": cam, "track_id": 1, "global_id": 1, "score": score}


class PickBestTests(unittest.TestCase):
    def test_ignores_non_target_events(self):
        events = [{"mode": "DB", "target_score": 0.99, "camera_id": "a"},
                  {"mode": "UNK", "camera_id": "b"}]
        self.assertIsNone(TargetTracker.pick_best(events))

    def test_keeps_highest_score(self):
        events = [{"mode": "TARGET", "target_score": 0.5, "camera_id": "a", "track_id": 1},
                  {"mode": "TARGET", "target_score": 0.9, "camera_id": "b", "track_id": 2},
                  {"mode": "TARGET", "target_score": None, "camera_id": "c", "track_id": 3}]
        self.assertEqual(TargetTracker.pick_best(events)["camera_id"], "b")


class TargetTrackerTests(unittest.TestCase):
    def setUp(self):
        self.t = TargetTracker(switch_confirm_sec=0.8, timeout_sec=3.0, path_max=5)

    def path_cams(self):
        return [p["camera_id"] for p in self.t.snapshot()["target_path"]]

    def test_first_sighting_starts_the_path(self):
        self.t.update(best("entre"), 100.0)
        snap = self.t.snapshot()
        self.assertTrue(snap["target"]["active"])
        self.assertEqual(self.path_cams(), ["entre"])

    def test_same_camera_does_not_grow_the_path(self):
        for i in range(5):
            self.t.update(best("entre"), 100.0 + i * 0.1)
        self.assertEqual(self.path_cams(), ["entre"])

    def test_camera_change_needs_confirmation(self):
        self.t.update(best("entre"), 100.0)
        self.t.update(best("salle"), 101.0)   # démarre la confirmation
        self.assertEqual(self.path_cams(), ["entre"])
        self.t.update(best("salle"), 101.5)   # trop tôt (0.5 s < 0.8 s)
        self.assertEqual(self.path_cams(), ["entre"])
        self.t.update(best("salle"), 101.9)   # confirmé
        self.assertEqual(self.path_cams(), ["entre", "salle"])

    def test_flicker_between_cameras_is_not_recorded(self):
        self.t.update(best("entre"), 100.0)
        self.t.update(best("salle"), 101.0)
        self.t.update(best("sortie"), 101.4)  # la candidate change : on repart de zéro
        self.t.update(best("salle"), 101.6)
        self.t.update(best("salle"), 102.0)   # 0.4 s seulement depuis le dernier changement
        self.assertEqual(self.path_cams(), ["entre"])

    def test_target_becomes_inactive_after_timeout(self):
        self.t.update(best("entre"), 100.0)
        self.t.update(None, 102.0)
        self.assertTrue(self.t.snapshot()["target"]["active"])
        self.t.update(None, 103.5)
        target = self.t.snapshot()["target"]
        self.assertFalse(target["active"])
        self.assertIsNone(target["camera_id"])

    def test_path_is_capped(self):
        t = 100.0
        for i in range(12):
            cam = "a" if i % 2 == 0 else "b"
            self.t.update(best(cam), t)
            self.t.update(best(cam), t + 1.0)
            t += 2.0
        self.assertLessEqual(len(self.t.snapshot()["target_path"]), 5)

    def test_reset_clears_everything(self):
        self.t.update(best("entre"), 100.0)
        self.t.reset()
        snap = self.t.snapshot()
        self.assertFalse(snap["target"]["active"])
        self.assertEqual(snap["target_path"], [])

    def test_snapshot_is_a_copy(self):
        self.t.update(best("entre"), 100.0)
        snap = self.t.snapshot()
        snap["target_path"].clear()
        self.assertEqual(self.path_cams(), ["entre"])


class TargetWorkerTests(unittest.TestCase):
    def test_path_is_tracked_without_any_websocket_client(self):
        cam = SimpleNamespace(latest_events=[
            {"mode": "TARGET", "target_score": 0.7, "camera_id": "entre", "track_id": 3}])
        tracker = TargetTracker()
        worker = TargetWorker(tracker, lambda: {"entre": cam})
        worker.step(50.0)
        self.assertEqual(tracker.snapshot()["target_path"][0]["camera_id"], "entre")


if __name__ == "__main__":
    unittest.main()
