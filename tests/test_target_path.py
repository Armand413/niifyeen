import unittest
from types import SimpleNamespace

import numpy as np

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


class OnMoveTests(unittest.TestCase):
    def test_callback_receives_first_sighting_then_confirmed_switches_only(self):
        moves = []
        t = TargetTracker(switch_confirm_sec=0.8, on_move=moves.append)
        t.update(best("entre"), 100.0)
        t.update(best("entre"), 100.5)               # même caméra : rien
        t.update(best("salle"), 101.0)               # confirmation en cours : rien
        self.assertEqual([m["camera_id"] for m in moves], ["entre"])
        t.update(best("salle"), 101.9)
        self.assertEqual([m["camera_id"] for m in moves], ["entre", "salle"])
        self.assertEqual(moves[1]["ts"], 101.9)

    def test_callback_runs_outside_the_lock(self):
        seen = []
        holder = {}
        holder["t"] = TargetTracker(on_move=lambda m: seen.append(holder["t"].snapshot()))   # bloquerait si le verrou était pris
        holder["t"].update(best("entre"), 100.0)
        self.assertEqual(len(seen), 1)

    def test_a_failing_callback_does_not_break_tracking(self):
        def boom(move):
            raise RuntimeError("base indisponible")
        t = TargetTracker(on_move=boom)
        with self.assertLogs(level="ERROR"):
            t.update(best("entre"), 100.0)
        self.assertTrue(t.snapshot()["target"]["active"])


class PickBestPerTargetTests(unittest.TestCase):
    def test_only_the_requested_target_is_considered(self):
        events = [{"mode": "TARGET", "target_id": 1, "target_score": 0.5, "camera_id": "a", "track_id": 1},
                  {"mode": "TARGET", "target_id": 2, "target_score": 0.9, "camera_id": "b", "track_id": 2}]
        self.assertEqual(TargetTracker.pick_best(events, 1)["camera_id"], "a")
        self.assertEqual(TargetTracker.pick_best(events, 2)["camera_id"], "b")
        self.assertIsNone(TargetTracker.pick_best(events, 3))
        self.assertEqual(TargetTracker.pick_best(events)["camera_id"], "b")      # sans filtre : la meilleure


class TargetWorkerTests(unittest.TestCase):
    def test_each_target_gets_its_own_path_without_any_websocket_client(self):
        from backend.app.targets import TargetRegistry
        registry = TargetRegistry()
        t1 = registry.add(np.array([1, 0, 0], dtype=np.float32), duplicate_threshold=2)
        t2 = registry.add(np.array([0, 1, 0], dtype=np.float32), duplicate_threshold=2)
        cam_a = SimpleNamespace(latest_events=[
            {"mode": "TARGET", "target_id": t1.id, "target_score": 0.7, "camera_id": "entre", "track_id": 3}])
        cam_b = SimpleNamespace(latest_events=[
            {"mode": "TARGET", "target_id": t2.id, "target_score": 0.6, "camera_id": "salle", "track_id": 9}])
        worker = TargetWorker(registry, lambda: {"entre": cam_a, "salle": cam_b})
        worker.step(50.0)
        self.assertEqual(t1.tracker.snapshot()["target_path"][0]["camera_id"], "entre")
        self.assertEqual(t2.tracker.snapshot()["target_path"][0]["camera_id"], "salle")

    def test_target_not_seen_stays_inactive(self):
        from backend.app.targets import TargetRegistry
        registry = TargetRegistry()
        t1 = registry.add(np.array([1, 0, 0], dtype=np.float32))
        worker = TargetWorker(registry, lambda: {"entre": SimpleNamespace(latest_events=[])})
        worker.step(50.0)
        self.assertFalse(t1.tracker.snapshot()["target"]["active"])


if __name__ == "__main__":
    unittest.main()
