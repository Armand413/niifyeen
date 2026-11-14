import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import tests  # noqa: F401
from backend.app import config, person_pipeline as pp


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


class FakeTensor:
    def __init__(self, arr): self.arr = np.asarray(arr)
    def __len__(self): return len(self.arr)
    def cpu(self): return self
    def numpy(self): return self.arr


class TwoPeopleYolo:
    """Détecte deux personnes (pistes 1 et 2) sur chaque image."""
    def __init__(self, *a, **k): pass
    def track(self, *a, **k):
        boxes = SimpleNamespace(xyxy=FakeTensor([[4, 2, 28, 34], [34, 2, 58, 34]]), id=FakeTensor([1, 2]))
        return [SimpleNamespace(boxes=boxes)]


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def target(tid, vec, threshold=0.5):
    return {"id": tid, "label": f"Cible {tid}", "embedding": unit(*vec), "threshold": threshold,
            "color_bgr": (10 * tid, 0, 0)}


class MultiTargetRecognitionTests(unittest.TestCase):
    def make(self, faces):
        """faces : une liste d'embeddings, renvoyés dans l'ordre des appels de reconnaissance."""
        with mock.patch.object(pp, "YOLO", TwoPeopleYolo):
            cam = pp.PersonPipeline(source=0, camera_id="entre", out_size=(64, 36), recog_every_sec=0.0)
        it = iter(faces)
        def fake_detect(frame):
            emb = next(it)
            return [SimpleNamespace(bbox=[0, 0, 64, 64], det_score=0.9, normed_embedding=unit(*emb))]
        self.patch = mock.patch.object(pp, "detect_faces_safe", fake_detect)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return cam

    FRAME = np.zeros((72, 128, 3), dtype=np.uint8)

    def test_each_person_is_matched_to_its_own_target(self):
        cam = self.make([(1, 0, 0), (0, 1, 0)])
        cam.set_targets([target(1, (1, 0, 0)), target(2, (0, 1, 0))])
        cam._process_frame(self.FRAME)
        by_track = {e["track_id"]: e for e in cam.latest_events}
        self.assertEqual((by_track[1]["mode"], by_track[1]["target_id"], by_track[1]["label"]), ("TARGET", 1, "Cible 1"))
        self.assertEqual((by_track[2]["mode"], by_track[2]["target_id"], by_track[2]["label"]), ("TARGET", 2, "Cible 2"))

    def test_best_target_wins_when_several_are_above_threshold(self):
        cam = self.make([(1, 0.2, 0), (5, 5, 5)])
        cam.set_targets([target(1, (1, 0, 0), 0.5), target(2, (1, 0.5, 0), 0.5)])
        cam._process_frame(self.FRAME)
        first = [e for e in cam.latest_events if e["track_id"] == 1][0]
        self.assertEqual(first["target_id"], 1)           # (1, .2, 0) est plus proche de la cible 1

    def test_unknown_person_is_not_a_target(self):
        cam = self.make([(0, 0, 1), (0, 0, 1)])
        cam.set_targets([target(1, (1, 0, 0)), target(2, (0, 1, 0))])
        cam._process_frame(self.FRAME)
        for e in cam.latest_events:
            self.assertEqual((e["mode"], e["target_id"]), ("UNK", None))
            self.assertAlmostEqual(e["target_score"], 0.0, places=3)

    def test_per_target_threshold(self):
        cam = self.make([(1, 1, 0), (1, 1, 0)])
        cam.set_targets([target(1, (1, 0, 0), 0.9), target(2, (0, 1, 0), 0.5)])
        cam._process_frame(self.FRAME)
        for e in cam.latest_events:     # 0.707 : sous le seuil de la cible 1, au-dessus de celui de la cible 2
            self.assertEqual(e["target_id"], 2)

    def test_removed_target_is_no_longer_reported_even_while_locked(self):
        cam = self.make([(1, 0, 0), (0, 1, 0)])
        cam.set_targets([target(1, (1, 0, 0)), target(2, (0, 1, 0))])
        cam._process_frame(self.FRAME)
        cam.set_targets([target(1, (1, 0, 0))])           # la cible 2 est retirée
        cam.recog_every_sec = 999.0                      # pas de nouvelle reconnaissance : seul le verrou joue
        cam._process_frame(self.FRAME)
        by_track = {e["track_id"]: e for e in cam.latest_events}
        self.assertEqual(by_track[1]["target_id"], 1)
        self.assertEqual((by_track[2]["mode"], by_track[2]["target_id"]), ("UNK", None))

    def test_close_up_of_each_target_is_kept_for_the_alert(self):
        cam = self.make([(1, 0, 0), (0, 1, 0)])
        cam.set_targets([target(1, (1, 0, 0)), target(2, (0, 1, 0))])
        cam._process_frame(self.FRAME)
        for tid in (1, 2):
            jpg = cam.target_crop(tid)
            self.assertTrue(jpg and jpg[:2] == b"\xff\xd8", tid)         # un vrai JPEG
        self.assertIsNone(cam.target_crop(99))
        cam.latest_target_crops[1] = (time.time() - 60, cam.latest_target_crops[1][1])
        self.assertIsNone(cam.target_crop(1))                           # trop ancien : pas d'image périmée

    def test_unknown_person_has_no_close_up(self):
        cam = self.make([(0, 0, 1), (0, 0, 1)])
        cam.set_targets([target(1, (1, 0, 0))])
        cam._process_frame(self.FRAME)
        self.assertEqual(cam.latest_target_crops, {})

    def test_visible_face_that_contradicts_the_identity_removes_it(self):
        """Échange de piste : l'identité reportée ne doit pas survivre à un visage net qui ne correspond à personne."""
        cam = self.make([(1, 0, 0), (0, 1, 0)] + [(0, 0, 1)] * 4)     # deux personnes par image
        cam.set_targets([target(1, (1, 0, 0))])
        cam._process_frame(self.FRAME)
        self.assertEqual([e["mode"] for e in cam.latest_events if e["track_id"] == 1], ["TARGET"])
        # images suivantes (piste 1) : un inconnu net — conservé après une contradiction, retiré à la seconde
        modes = []
        for _ in range(2):
            cam._process_frame(self.FRAME)
            modes.append([e["mode"] for e in cam.latest_events if e["track_id"] == 1][0])
        self.assertEqual(modes, ["TARGET", "UNK"])

    def test_tiny_or_blurry_faces_are_ignored(self):
        tiny = SimpleNamespace(bbox=[0, 0, 10, 10], det_score=0.9)
        blurry = SimpleNamespace(bbox=[0, 0, 80, 80], det_score=0.2)
        good = SimpleNamespace(bbox=[0, 0, 80, 80], det_score=0.9)
        self.assertFalse(pp.PersonPipeline._usable_face(tiny))
        self.assertFalse(pp.PersonPipeline._usable_face(blurry))
        self.assertTrue(pp.PersonPipeline._usable_face(good))

    def test_identity_survives_when_no_usable_face_is_visible(self):
        """Visage absent ou minuscule : l'identité suivie est conservée (ce n'est pas une contradiction)."""
        cam = self.make([(1, 0, 0), (0, 0, 1)])
        cam.set_targets([target(1, (1, 0, 0))])
        cam._process_frame(self.FRAME)
        pp.detect_faces_safe = lambda frame: [SimpleNamespace(bbox=[0, 0, 10, 10], det_score=0.9, normed_embedding=unit(0, 0, 1))]
        for _ in range(3):
            cam._process_frame(self.FRAME)
        self.assertEqual([e["mode"] for e in cam.latest_events if e["track_id"] == 1], ["TARGET"])

    def test_no_targets_means_no_target_events(self):
        cam = self.make([(1, 0, 0), (0, 1, 0)])
        cam._process_frame(self.FRAME)
        self.assertTrue(all(e["mode"] == "UNK" and e["target_id"] is None for e in cam.latest_events))

    def test_clear_targets(self):
        cam = self.make([(1, 0, 0)])
        cam.set_targets([target(1, (1, 0, 0))])
        cam.clear_targets()
        self.assertEqual(cam._get_targets(), [])


class PipelineStatsTests(unittest.TestCase):
    FRAME = np.zeros((72, 128, 3), dtype=np.uint8)

    def make(self, yolo=FakeYolo):
        with mock.patch.object(pp, "YOLO", yolo):
            return pp.PersonPipeline(source=0, camera_id="entre", out_size=(64, 36))

    def test_status_transitions(self):
        cam = self.make()
        self.assertEqual(cam.status(100.0), "stopped")
        cam.running, cam._started_at = True, 100.0
        self.assertEqual(cam.status(101.0), "connecting")           # démarrage, pas encore d'image
        self.assertEqual(cam.status(112.0), "offline")               # jamais d'image depuis 10 s
        cam.last_frame_ts = 120.0
        self.assertEqual(cam.status(121.0), "online")
        self.assertEqual(cam.status(124.0), "reconnecting")          # plus d'image depuis > 3 s
        self.assertEqual(cam.status(131.0), "offline")               # plus d'image depuis >= 10 s

    def test_stats_when_online(self):
        cam = self.make(TwoPeopleYolo)
        cam.running, cam._started_at = True, time.time()
        with mock.patch.object(pp, "detect_faces_safe", lambda f: []):
            for _ in range(5):
                cam._process_frame(self.FRAME)
                time.sleep(0.02)
        s = cam.stats()
        self.assertEqual(s["status"], "online")
        self.assertEqual((s["width"], s["height"]), (128, 72))
        self.assertEqual(s["persons"], 2)
        self.assertGreater(s["fps"], 1.0)
        self.assertIsNotNone(s["latency_ms"])
        self.assertLess(s["age_sec"], 2.0)
        self.assertEqual(cam.frame_seq, 5)

    def test_stats_are_neutral_when_the_camera_is_not_online(self):
        cam = self.make()
        cam.running, cam._started_at = True, time.time() - 60
        cam.latest_events = [{"mode": "UNK"}]                          # reste d'une ancienne image
        s = cam.stats()
        self.assertEqual((s["status"], s["persons"], s["fps"], s["latency_ms"]), ("offline", 0, 0.0, None))

    def test_raw_image_is_only_encoded_when_somebody_watches_it(self):
        cam = self.make()
        cam._process_frame(self.FRAME)
        self.assertIsNone(cam.latest_jpeg_raw)                         # personne ne l'a demandée : aucun coût
        cam.add_raw_viewer()
        cam._process_frame(self.FRAME)
        self.assertIsNotNone(cam.latest_jpeg_raw)
        self.assertEqual(cam.raw_seq, cam.frame_seq)
        before = cam.raw_seq
        cam.remove_raw_viewer(); cam.remove_raw_viewer()                # jamais négatif
        self.assertEqual(cam.raw_viewers, 0)
        cam._process_frame(self.FRAME)
        self.assertEqual(cam.raw_seq, before)                          # plus encodée
        self.assertNotEqual(cam.raw_seq, cam.frame_seq)                # donc détectée comme périmée

    def test_annotated_and_raw_images_differ_when_people_are_drawn(self):
        cam = self.make(TwoPeopleYolo)
        cam.add_raw_viewer()
        with mock.patch.object(pp, "detect_faces_safe", lambda f: []):
            cam._process_frame(self.FRAME)
        self.assertNotEqual(cam.latest_jpeg, cam.latest_jpeg_raw)       # les cadres sont dessinés sur l'une seulement

    def test_placeholder_updates_both_images_and_the_sequence(self):
        cam = self.make()
        seq = cam.frame_seq
        cam._placeholder("PAS DE SIGNAL")
        self.assertEqual(cam.frame_seq, seq + 1)
        self.assertEqual(cam.latest_jpeg, cam.latest_jpeg_raw)
        self.assertEqual(cam.raw_seq, cam.frame_seq)

    def test_reconnect_counter(self):
        cam = self.make()
        cam.reconnect_after_sec = 0.0
        cam.running = True
        class DeadCap:
            def read(self): return False, None
            def release(self): pass
        cam.cap = DeadCap()
        t = threading.Thread(target=cam._loop, daemon=True)
        cam._try_open = lambda: False
        t.start()
        end = time.time() + 3
        while time.time() < end and cam.reconnects == 0:
            time.sleep(0.01)
        cam.running = False
        cam._stop_evt.set()
        t.join(timeout=3)
        self.assertGreaterEqual(cam.reconnects, 1)


class GrabbingCap:
    """Caméra qui fournit une image toutes les 10 ms et sait en jeter (grab)."""
    def __init__(self): self.reads = self.grabs = 0; self.released = False
    def read(self):
        self.reads += 1; time.sleep(0.01)
        return True, np.zeros((72, 128, 3), dtype=np.uint8)
    def grab(self):
        self.grabs += 1; time.sleep(0.01)
        return True
    def release(self): self.released = True


class ProcessingRateTests(unittest.TestCase):
    def run_for(self, seconds, process_fps, cap_factory=GrabbingCap):
        with mock.patch.object(pp, "YOLO", FakeYolo):
            cam = pp.PersonPipeline(source=0, camera_id="entre", out_size=(64, 36), process_fps=process_fps)
        cap = cap_factory()
        cam._try_open = lambda: setattr(cam, "cap", cap) or True
        cam.start()
        time.sleep(seconds)
        cam.stop()
        return cam, cap

    def test_without_a_limit_every_image_is_processed(self):
        cam, cap = self.run_for(0.6, None)
        self.assertGreater(cam.frame_seq, 20)
        self.assertEqual(cap.grabs, 0)

    def test_the_limit_reduces_processing_and_drops_the_extra_images(self):
        cam, cap = self.run_for(1.2, 5)
        self.assertTrue(2 <= cam.frame_seq <= 9, cam.frame_seq)             # ~5 images traitées par seconde
        self.assertGreater(cap.grabs, cam.frame_seq * 4)                    # les autres sont lues puis jetées (pas de retard)

    def test_a_capture_without_grab_still_respects_the_limit(self):
        class NoGrab:
            reads = 0
            def read(self): NoGrab.reads += 1; time.sleep(0.01); return True, np.zeros((72, 128, 3), dtype=np.uint8)
            def release(self): pass
        cam, _ = self.run_for(1.2, 5, NoGrab)
        self.assertTrue(2 <= cam.frame_seq <= 9, cam.frame_seq)

    def test_the_setting_is_stored(self):
        with mock.patch.object(pp, "YOLO", FakeYolo):
            self.assertEqual(pp.PersonPipeline(source=0, camera_id="x", process_fps=7).process_fps, 7.0)
            self.assertIsNone(pp.PersonPipeline(source=0, camera_id="x").process_fps)
            self.assertIsNone(pp.PersonPipeline(source=0, camera_id="x", process_fps=0).process_fps)


def vec_cos(c, axis=0, other=2, dim=3):
    """Vecteur unitaire dont la similarité cosinus avec l'axe ``axis`` vaut ``c``."""
    v = np.zeros(dim, dtype=np.float32)
    v[axis] = c
    v[other] = float(np.sqrt(max(0.0, 1 - c * c)))
    return v


def person(pid, name, axis, active=True):
    e = np.zeros(3, dtype=np.float32)
    e[axis] = 1.0
    return {"participant_id": pid, "nom": name, "active": active, "embedding": e}


class RecognitionStatusTests(unittest.TestCase):
    """Une similarité n'est jamais une identité certaine : probable, à vérifier ou inconnu."""

    def make(self, known, faces=()):
        with mock.patch.object(pp, "YOLO", TwoPeopleYolo):
            cam = pp.PersonPipeline(source=0, camera_id="entre", out_size=(64, 36), recog_every_sec=0.0)
        cam.set_known(known)
        it = iter(faces)
        patch = mock.patch.object(pp, "detect_faces_safe", lambda f: [SimpleNamespace(
            bbox=[0, 0, 64, 64], det_score=0.9, normed_embedding=np.asarray(next(it), dtype=np.float32))])
        patch.start()
        self.addCleanup(patch.stop)
        return cam

    # ---- logique de correspondance
    def test_probable_to_verify_and_unknown(self):
        cam = self.make([person(7, "Awa Ba", 0)])
        probable, score = cam._match_known(vec_cos(0.80))
        self.assertEqual((probable["status"], probable["participant_id"], probable["label"]), ("probable", 7, "Awa Ba"))
        self.assertAlmostEqual(score, 0.80, places=3)
        weak, _ = cam._match_known(vec_cos(0.42))
        self.assertEqual(weak["status"], "a_verifier")
        none, best = cam._match_known(vec_cos(0.20))
        self.assertIsNone(none)
        self.assertAlmostEqual(best, 0.20, places=3)                       # la meilleure similarité reste connue

    def test_thresholds_are_the_configured_ones(self):
        cam = self.make([person(7, "Awa", 0)])
        self.assertEqual((cam.db_threshold, cam.probable_threshold), (config.DB_THRESHOLD, config.DB_PROBABLE_THRESHOLD))
        edge = vec_cos(config.DB_PROBABLE_THRESHOLD + 0.01)
        self.assertEqual(cam._match_known(edge)[0]["status"], "probable")
        self.assertEqual(cam._match_known(vec_cos(config.DB_PROBABLE_THRESHOLD - 0.01))[0]["status"], "a_verifier")
        self.assertIsNone(cam._match_known(vec_cos(config.DB_THRESHOLD - 0.01))[0])

    def test_two_close_people_make_the_match_ambiguous(self):
        cam = self.make([person(7, "Awa", 0), person(8, "Aïssa", 1)])
        face = np.array([0.60, 0.58, np.sqrt(1 - 0.60 ** 2 - 0.58 ** 2)], dtype=np.float32)
        match, _ = cam._match_known(face)
        self.assertEqual((match["participant_id"], match["status"], match["ambiguous"]), (7, "a_verifier", True))

    def test_a_clear_winner_stays_probable(self):
        cam = self.make([person(7, "Awa", 0), person(8, "Aïssa", 1)])
        face = np.array([0.80, 0.20, np.sqrt(1 - 0.80 ** 2 - 0.20 ** 2)], dtype=np.float32)
        match, _ = cam._match_known(face)
        self.assertEqual((match["status"], match["ambiguous"]), ("probable", False))

    def test_several_references_of_one_person_are_not_an_ambiguity(self):
        known = [person(7, "Awa", 0), dict(person(7, "Awa", 0), embedding=np.array([0.99, 0.1, 0.0], dtype=np.float32))]
        cam = self.make(known)
        match, _ = cam._match_known(vec_cos(0.8))
        self.assertEqual((match["status"], match["ambiguous"]), ("probable", False))

    def test_inactive_flag_is_carried(self):
        cam = self.make([person(7, "Awa", 0, active=False)])
        self.assertFalse(cam._match_known(vec_cos(0.8))[0]["active"])

    def test_old_style_known_entries_still_work(self):
        cam = self.make([{"nom": "Awa", "embedding": np.array([1, 0, 0], dtype=np.float32)}])
        match, _ = cam._match_known(vec_cos(0.8))
        self.assertEqual((match["label"], match["participant_id"], match["active"]), ("Awa", None, True))

    def test_nobody_enrolled(self):
        self.assertEqual(self.make([])._match_known(vec_cos(0.9)), (None, None))

    # ---- événements produits
    def test_events_carry_the_identifier_and_the_status(self):
        cam = self.make([person(7, "Awa Ba", 0), person(8, "Moussa Sy", 1)], [vec_cos(0.8), np.array([0, 0.45, np.sqrt(1 - 0.45 ** 2)])])
        cam._process_frame(np.zeros((72, 128, 3), dtype=np.uint8))
        by_track = {e["track_id"]: e for e in cam.latest_events}
        a, b = by_track[1], by_track[2]
        self.assertEqual((a["mode"], a["label"], a["participant_id"], a["match_status"], a["participant_active"]),
                         ("DB", "Awa Ba", 7, "probable", True))
        self.assertAlmostEqual(a["db_score"], 0.8, places=2)
        self.assertEqual((b["label"], b["participant_id"], b["match_status"]), ("Moussa Sy", 8, "a_verifier"))

    def test_unknown_and_target_events_have_no_person(self):
        cam = self.make([person(7, "Awa", 0)], [vec_cos(0.1), vec_cos(0.1)])
        cam._process_frame(np.zeros((72, 128, 3), dtype=np.uint8))
        for e in cam.latest_events:
            self.assertEqual((e["mode"], e["participant_id"], e["match_status"], e["match_ambiguous"]), ("UNK", None, None, False))

    def test_within_a_track_the_best_picture_decides_the_status(self):
        cam = self.make([person(7, "Awa", 0)], [vec_cos(0.80), vec_cos(0.90), vec_cos(0.42), vec_cos(0.90)])
        frame = np.zeros((72, 128, 3), dtype=np.uint8)
        cam._process_frame(frame)                                   # image nette : probable
        cam._process_frame(frame)                                   # image floue ensuite : le statut ne se dégrade pas
        e = {x["track_id"]: x for x in cam.latest_events}[1]
        self.assertEqual((e["match_status"], round(e["db_score"], 2)), ("probable", 0.80))   # score = celui qui justifie le statut

    def test_a_different_person_on_the_same_track_resets_the_match(self):
        known = [person(7, "Awa", 0), person(8, "Moussa", 1)]
        cam = self.make(known, [vec_cos(0.80, axis=0), vec_cos(0.9, axis=0), np.array([0, 0.6, 0.8]), np.array([0, 0.6, 0.8])])
        frame = np.zeros((72, 128, 3), dtype=np.uint8)
        cam._process_frame(frame)
        cam._process_frame(frame)
        e = {x["track_id"]: x for x in cam.latest_events}[1]
        self.assertEqual((e["participant_id"], e["label"]), (8, "Moussa"))

    def test_the_video_overlay_uses_a_colour_per_status(self):
        import cv2
        def drawn_colours(second_face):
            with mock.patch.object(pp, "YOLO", TwoPeopleYolo):
                cam = pp.PersonPipeline(source=0, camera_id="entre", out_size=(640, 360), recog_every_sec=0.0, jpeg_quality=100)
            cam.set_known([person(7, "Awa", 0)])
            faces = iter([vec_cos(0.8), second_face])
            with mock.patch.object(pp, "detect_faces_safe", lambda f: [SimpleNamespace(
                    bbox=[0, 0, 64, 64], det_score=0.9, normed_embedding=np.asarray(next(faces), dtype=np.float32))]):
                cam._process_frame(np.zeros((360, 640, 3), dtype=np.uint8))
            img = cv2.imdecode(np.frombuffer(cam.latest_jpeg, dtype=np.uint8), cv2.IMREAD_COLOR).astype(int)
            near = lambda bgr: int((np.abs(img - np.array(bgr)).sum(axis=2) < 70).sum())
            return near((0, 180, 0)), near((0, 165, 255))
        green, orange = drawn_colours(vec_cos(0.42))        # piste 1 : probable (vert), piste 2 : à vérifier (orange)
        self.assertGreater(green, 20)
        self.assertGreater(orange, 20)

    def test_a_deactivated_person_is_drawn_differently(self):
        cam = self.make([person(7, "Awa", 0, active=False)], [vec_cos(0.8), vec_cos(0.1)])
        cam._process_frame(np.zeros((72, 128, 3), dtype=np.uint8))
        e = {x["track_id"]: x for x in cam.latest_events}[1]
        self.assertEqual((e["participant_active"], e["match_status"]), (False, "probable"))


if __name__ == "__main__":
    unittest.main()


class AnalyticsInputsTests(unittest.TestCase):
    """Ce que le suivi fournit au moteur d'analyse (Module 6) : point au sol et mesures d'image."""

    def make(self):
        with mock.patch.object(pp, "YOLO", TwoPeopleYolo):
            cam = pp.PersonPipeline(source=0, camera_id="entre", out_size=(64, 36), recog_every_sec=0.0)
        patch = mock.patch.object(pp, "detect_faces_safe", lambda f: [])
        patch.start()
        self.addCleanup(patch.stop)
        return cam

    def test_each_person_has_a_foot_point_in_the_image(self):
        cam = self.make()
        cam._process_frame(np.zeros((72, 128, 3), dtype=np.uint8))
        by_track = {e["track_id"]: e for e in cam.latest_events}
        self.assertAlmostEqual(by_track[1]["fx"], 16 / 64, places=3)           # milieu du bas du cadre [4,2,28,34]
        self.assertAlmostEqual(by_track[1]["fy"], 34 / 36, places=3)
        self.assertAlmostEqual(by_track[2]["fx"], 46 / 64, places=3)
        for e in by_track.values():
            self.assertTrue(0.0 <= e["fx"] <= 1.0 and 0.0 <= e["fy"] <= 1.0)

    def test_frame_statistics(self):
        cam = self.make()
        black = np.zeros((72, 128, 3), dtype=np.uint8)
        cam._process_frame(black)
        first = dict(cam.frame_stats)
        self.assertEqual((first["mean"], first["std"]), (0.0, 0.0))
        cam._process_frame(black.copy())
        self.assertEqual(cam.frame_stats["crc"], first["crc"])                  # images identiques : même somme de contrôle
        scene = np.zeros((72, 128, 3), dtype=np.uint8)
        scene[:, 64:] = 255                                                     # moitié noire, moitié blanche
        cam._process_frame(scene)
        self.assertGreater(cam.frame_stats["std"], 100)
        self.assertNotEqual(cam.frame_stats["crc"], first["crc"])
