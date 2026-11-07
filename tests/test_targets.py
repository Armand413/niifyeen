import unittest

import cv2
import numpy as np

import tests  # noqa: F401
from backend.app import config
from backend.app.targets import (TargetRegistry, TargetLimitError, DuplicateTargetError,
                                 make_thumbnail, hex_to_bgr)


def vec(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def distinct(n):
    """n empreintes très différentes (une par axe)."""
    return [vec(*[1.0 if i == k else 0.0 for i in range(max(n, 3))]) for k in range(n)]


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.r = TargetRegistry(max_targets=3)

    def test_targets_are_added_without_removing_the_previous_ones(self):
        a, b = distinct(2)
        t1 = self.r.add(a)
        t2 = self.r.add(b)
        self.assertEqual([t.id for t in self.r.items()], [t1.id, t2.id])
        self.assertEqual((t1.label, t2.label), ("Cible 1", "Cible 2"))
        self.assertEqual(len(self.r), 2)

    def test_each_target_has_its_own_color_and_session_and_tracker(self):
        a, b, c = distinct(3)
        ts = [self.r.add(x) for x in (a, b, c)]
        self.assertEqual(len({t.color for t in ts}), 3)
        self.assertEqual(len({t.session for t in ts}), 3)
        self.assertEqual(len({id(t.tracker) for t in ts}), 3)

    def test_version_changes_on_every_add_remove_and_clear(self):
        a, b = distinct(2)
        versions = [self.r.version]
        t = self.r.add(a); versions.append(self.r.version)
        self.r.add(b); versions.append(self.r.version)
        self.r.remove(t.id); versions.append(self.r.version)
        self.r.clear(); versions.append(self.r.version)
        self.assertEqual(versions, sorted(set(versions)))

    def test_limit(self):
        for x in distinct(3):
            self.r.add(x)
        with self.assertRaises(TargetLimitError):
            self.r.add(vec(1, 1, 1))

    def test_same_person_cannot_be_added_twice(self):
        a, = distinct(1)
        t1 = self.r.add(a)
        with self.assertRaises(DuplicateTargetError) as ctx:
            self.r.add(a)
        self.assertEqual(ctx.exception.existing.id, t1.id)
        self.assertGreaterEqual(ctx.exception.score, config.TARGET_DUPLICATE_THRESHOLD)
        self.assertEqual(len(self.r), 1)

    def test_remove_one_keeps_the_others(self):
        a, b = distinct(2)
        t1, t2 = self.r.add(a), self.r.add(b)
        self.assertTrue(self.r.remove(t1.id))
        self.assertFalse(self.r.remove(t1.id))
        self.assertEqual([t.id for t in self.r.items()], [t2.id])
        self.assertIsNone(self.r.get(t1.id))

    def test_ids_are_not_reused_after_a_removal_but_restart_after_clear(self):
        a, b, c = distinct(3)
        t1 = self.r.add(a)
        self.r.remove(t1.id)
        self.assertEqual(self.r.add(b).label, "Cible 2")
        self.assertEqual(self.r.clear(), 1)
        self.assertEqual(self.r.add(c).label, "Cible 1")

    def test_freed_colour_is_reused(self):
        a, b, c = distinct(3)
        t1, t2 = self.r.add(a), self.r.add(b)
        freed = t1.color
        self.r.remove(t1.id)
        self.assertEqual(self.r.add(c).color, freed)

    def test_for_cameras_payload(self):
        a, = distinct(1)
        self.r.add(a)
        payload = self.r.for_cameras()
        self.assertEqual(len(payload), 1)
        self.assertEqual(set(payload[0]), {"id", "label", "embedding", "threshold", "color_bgr"})
        self.assertEqual(payload[0]["threshold"], config.TARGET_THRESHOLD)

    def test_snapshot_reports_each_target_state(self):
        a, b = distinct(2)
        t1, t2 = self.r.add(a), self.r.add(b)
        t1.tracker.update({"camera_id": "entre", "track_id": 1, "global_id": 1, "score": .7}, 100.0)
        snap = {s["id"]: s for s in self.r.snapshot()}
        self.assertTrue(snap[t1.id]["active"])
        self.assertEqual(snap[t1.id]["camera_id"], "entre")
        self.assertEqual(snap[t1.id]["path"][0]["camera_id"], "entre")
        self.assertFalse(snap[t2.id]["active"])
        self.assertEqual(snap[t2.id]["path"], [])
        self.assertEqual(snap[t1.id]["color"], t1.color)

    def test_moves_are_reported_with_their_own_target(self):
        moves = []
        r = TargetRegistry(on_move=lambda target, move: moves.append((target.label, target.session, move["camera_id"])))
        a, b = distinct(2)
        t1, t2 = r.add(a), r.add(b)
        t1.tracker.update({"camera_id": "entre", "track_id": 1, "global_id": 1, "score": .7}, 100.0)
        t2.tracker.update({"camera_id": "salle", "track_id": 2, "global_id": 2, "score": .6}, 100.0)
        self.assertEqual(moves, [("Cible 1", t1.session, "entre"), ("Cible 2", t2.session, "salle")])

    def test_embedding_is_stored_as_float32(self):
        t = self.r.add(np.array([1.0, 0.0, 0.0]))     # float64 en entrée
        self.assertEqual(t.embedding.dtype, np.float32)


class ThumbnailTests(unittest.TestCase):
    def decode(self, data):
        return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)

    def test_large_image_is_reduced(self):
        img = np.full((1200, 800, 3), 128, dtype=np.uint8)
        out = self.decode(make_thumbnail(img))
        self.assertLessEqual(max(out.shape[:2]), config.TARGET_THUMBNAIL_PX)
        self.assertAlmostEqual(out.shape[0] / out.shape[1], 1.5, delta=0.05)     # proportions conservées

    def test_small_image_is_not_enlarged(self):
        out = self.decode(make_thumbnail(np.zeros((40, 30, 3), dtype=np.uint8)))
        self.assertEqual(out.shape[:2], (40, 30))

    def test_hex_to_bgr(self):
        self.assertEqual(hex_to_bgr("#d92d20"), (0x20, 0x2d, 0xd9))
        self.assertEqual(hex_to_bgr("2e5bff"), (0xff, 0x5b, 0x2e))


if __name__ == "__main__":
    unittest.main()
