import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

import tests  # noqa: F401
from backend.app import config
from backend.app.reid import GlobalReId
from backend.app.vision import cosine_similarity


def unit(*values):
    v = np.array(values, dtype=np.float32)
    return v / np.linalg.norm(v)


class ReIdTests(unittest.TestCase):
    def test_similar_embeddings_share_an_id(self):
        r = GlobalReId(sim_threshold=0.45)
        a = r.assign(unit(1, 0, 0))
        self.assertEqual(r.assign(unit(1, 0.1, 0)), a)

    def test_different_embeddings_get_different_ids(self):
        r = GlobalReId(sim_threshold=0.45)
        self.assertNotEqual(r.assign(unit(1, 0, 0)), r.assign(unit(0, 1, 0)))

    def test_expired_ids_are_forgotten(self):
        r = GlobalReId(sim_threshold=0.45, ttl_sec=-1.0)
        self.assertNotEqual(r.assign(unit(1, 0, 0)), r.assign(unit(1, 0, 0)))

    def test_cosine_similarity(self):
        self.assertAlmostEqual(cosine_similarity(unit(1, 0), unit(1, 0)), 1.0, places=5)
        self.assertAlmostEqual(cosine_similarity(unit(1, 0), unit(0, 1)), 0.0, places=5)


class ConfigTests(unittest.TestCase):
    def write(self, content):
        d = tempfile.mkdtemp()
        p = Path(d) / "cameras.json"
        p.write_text(json.dumps(content), encoding="utf-8")
        return p

    def test_missing_file_falls_back_to_default_webcam(self):
        cams = config.load_cameras(Path(tempfile.mkdtemp()) / "absent.json")
        self.assertEqual([c["id"] for c in cams], ["entre"])

    def test_valid_file_is_loaded(self):
        p = self.write([{"id": "a", "source": 0}, {"id": "b", "source": "http://x/video"}])
        self.assertEqual([c["id"] for c in config.load_cameras(p)], ["a", "b"])

    def test_invalid_files_are_rejected(self):
        bad = [
            {"id": "a", "source": 0},                                  # pas une liste
            [{"id": "a b", "source": 0}],                              # id invalide
            [{"id": "<img>", "source": 0}],                            # id dangereux
            [{"id": "a", "source": 0}, {"id": "a", "source": 1}],      # doublon
            [{"id": "a"}],                                             # pas de source
            [{"id": "a", "source": ""}],
            [{"id": "a", "source": 0, "x": 1.5}],                      # hors du plan
            [{"id": "a", "source": 0, "y": -0.1}],
            [{"id": "a", "source": 0, "x": "0.5"}],
            [{"id": "a", "source": 0, "label": ""}],
            [{"id": "a", "source": 0, "label": "x" * 41}],
            [{"id": "a", "source": 0, "location": 5}],
            [{"id": "a", "source": 0, "location": "x" * 81}],
        ]
        for content in bad:
            with self.subTest(content=content), self.assertRaises(ValueError):
                config.load_cameras(self.write(content))

    def test_camera_meta_uses_the_same_ids_as_cameras(self):
        cams = [{"id": "a", "source": 0, "label": "Entrée"}, {"id": "b", "source": 1}]
        meta = config.camera_meta(cams)
        self.assertEqual(set(meta), {"a", "b"})
        self.assertEqual(meta["a"]["label"], "Entrée")
        self.assertEqual(meta["b"]["label"], "b")

    def test_camera_positions_default_to_plan_slots_and_can_be_overridden(self):
        meta = config.camera_meta([{"id": "a", "source": 0}, {"id": "b", "source": 1, "x": 0.5, "y": 0.25}])
        self.assertEqual((meta["a"]["x"], meta["a"]["y"]), config._PLAN_SLOTS[0])
        self.assertEqual((meta["b"]["x"], meta["b"]["y"]), (0.5, 0.25))

    def test_many_cameras_all_get_a_position_inside_the_plan(self):
        meta = config.camera_meta([{"id": f"c{i}", "source": i} for i in range(12)])
        for m in meta.values():
            self.assertTrue(0 <= m["x"] <= 1 and 0 <= m["y"] <= 1)

    def test_location_is_kept_in_the_camera_metadata(self):
        meta = config.camera_meta([{"id": "a", "source": 0, "location": "Hall d'accueil"}, {"id": "b", "source": 1}])
        self.assertEqual((meta["a"]["location"], meta["b"]["location"]), ("Hall d'accueil", ""))

    def test_missing_values_from_the_database_fall_back_to_defaults(self):
        meta = config.camera_meta([{"id": "a", "label": None, "location": None, "x": None, "y": None}])
        self.assertEqual(meta["a"], {"label": "a", "location": "", "x": config._PLAN_SLOTS[0][0], "y": config._PLAN_SLOTS[0][1],
                                    "placed": False, "floor_id": None, "heading": None, "fov": None, "view_range": None})

    def test_stream_size_defaults(self):
        self.assertEqual(config.STREAM_SIZE, (640, 360))

    def test_example_file_is_valid(self):
        cams = config.load_cameras(config.PROJECT_ROOT / "cameras.example.json")
        self.assertEqual([c["id"] for c in cams], ["entre", "salle", "sortie"])

    def test_paths_are_absolute(self):
        self.assertTrue(config.PROJECT_ROOT.is_absolute())
        self.assertTrue(config.MODELS_DIR.is_absolute())


if __name__ == "__main__":
    unittest.main()


class IdentityHoldConfigTest(unittest.TestCase):
    def test_default_and_bounds(self):
        import importlib, os
        from backend.app import config
        try:
            os.environ["NIIFYEEN_IDENTITY_HOLD_SEC"] = "9999"
            importlib.reload(config)
            self.assertEqual(config.IDENTITY_HOLD_SEC, 300.0)
            os.environ["NIIFYEEN_IDENTITY_HOLD_SEC"] = "0"
            importlib.reload(config)
            self.assertEqual(config.IDENTITY_HOLD_SEC, 1.0)
        finally:
            os.environ.pop("NIIFYEEN_IDENTITY_HOLD_SEC", None)
            importlib.reload(config)
        self.assertEqual(config.IDENTITY_HOLD_SEC, 10.0)
        self.assertEqual((config.FACE_MIN_SIZE_PX, config.FACE_MIN_DET_SCORE, config.IDENTITY_CONTRADICTIONS), (32, 0.5, 2))
