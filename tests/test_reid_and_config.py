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

    def test_paths_are_absolute(self):
        self.assertTrue(config.PROJECT_ROOT.is_absolute())
        self.assertTrue(config.MODELS_DIR.is_absolute())


if __name__ == "__main__":
    unittest.main()
