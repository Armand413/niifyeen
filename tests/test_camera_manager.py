import json
import unittest
from unittest import mock

import tests  # noqa: F401
from tests.fakes import FakeCamera
from backend.app import camera_manager as cm, config, events as ev
from backend.app.db import Camera, Event, Setting, SessionLocal, init_db


class SourceTests(unittest.TestCase):
    def test_webcam_indexes(self):
        for raw, expected in ((0, 0), ("0", 0), (" 2 ", 2), (31, 31), ("07", 7)):
            with self.subTest(raw=raw):
                self.assertEqual(cm.normalize_source(raw), expected)

    def test_valid_urls_are_kept_as_typed(self):
        for url in ("http://192.168.1.31:8081/video?token=abc", "https://cam.local/stream",
                    "rtsp://admin:pw@192.168.1.5:554/stream1", "RTSP://10.0.0.2/live"):
            with self.subTest(url=url):
                self.assertEqual(cm.normalize_source(url), url)

    def test_everything_else_is_refused(self):
        bad = [None, "", "   ", True, 32, -1, "abc", "video.mp4", r"C:\Users\Moi\video.mp4", "/etc/passwd",
               "file:///etc/passwd", "ftp://host/x", "javascript:alert(1)", "http://", "http:///x",
               "http://host:99999/x", "http://host:0/x", "http://ho st/x", "http://host/\nx", "http://h/" + "a" * 600, 3.5]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(cm.CameraError) as ctx:
                cm.normalize_source(raw)
            self.assertEqual(ctx.exception.field, "source")

    def test_local_files_are_never_accepted(self):
        """OpenCV lirait volontiers un fichier local : il ne doit pas être possible de le demander par l'interface."""
        for raw in ("/home/x/secret.mp4", "../../etc/passwd", "file:///C:/Windows/win.ini", "C:/Windows/win.ini"):
            with self.subTest(raw=raw), self.assertRaises(cm.CameraError):
                cm.normalize_source(raw)

    def test_kind_and_secrets(self):
        self.assertEqual(cm.source_kind(0), "webcam")
        self.assertEqual(cm.source_kind("http://h/v"), "http")
        self.assertEqual(cm.source_kind("rtsp://h/v"), "rtsp")
        self.assertEqual(cm.source_kind("rtsps://h/v"), "rtsp")
        self.assertTrue(cm.has_secret("http://h/v?token=x"))
        self.assertTrue(cm.has_secret("rtsp://u:p@h/v"))
        self.assertFalse(cm.has_secret("http://h/v"))
        self.assertFalse(cm.has_secret(0))

    def test_slugify(self):
        self.assertEqual(cm.slugify("Entrée principale"), "entree-principale")
        self.assertEqual(cm.slugify("  !!! "), "camera")
        self.assertLessEqual(len(cm.slugify("x" * 100)), 32)


class CleanInputTests(unittest.TestCase):
    def test_creation_requires_label_and_source(self):
        with self.assertRaises(cm.CameraError) as c:
            cm.clean_input({"source": 0}, creating=True)
        self.assertEqual(c.exception.field, "label")
        with self.assertRaises(cm.CameraError) as c:
            cm.clean_input({"label": "A"}, creating=True)
        self.assertEqual(c.exception.field, "source")

    def test_partial_update_returns_only_the_given_fields(self):
        self.assertEqual(cm.clean_input({"location": "  Hall "}, creating=False), {"location": "Hall"})
        self.assertEqual(cm.clean_input({}, creating=False), {})

    def test_limits(self):
        bad = [{"label": "x" * 41}, {"label": "  "}, {"location": "x" * 81}, {"location": 3}, {"enabled": "oui"},
               {"x": 1.5}, {"y": -0.1}, {"x": "0.5"}, {"capture_width": 100, "capture_height": 100},
               {"capture_width": 1280}, {"capture_height": 720}, {"capture_width": 1280, "capture_height": 99999},
               {"process_fps": 0.1}, {"process_fps": 61}, {"process_fps": True}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(cm.CameraError):
                cm.clean_input(data, creating=False)

    def test_valid_values(self):
        out = cm.clean_input({"label": "Salle", "source": "1", "x": 0.5, "y": 1, "capture_width": 1280,
                              "capture_height": 720, "process_fps": 5, "enabled": False}, creating=True)
        self.assertEqual((out["source"], out["x"], out["y"], out["process_fps"], out["enabled"]), (1, 0.5, 1.0, 5.0, False))

    def test_none_clears_optional_settings(self):
        out = cm.clean_input({"x": None, "capture_width": None, "capture_height": None, "process_fps": None}, creating=False)
        self.assertEqual(out, {"x": None, "capture_width": None, "capture_height": None, "process_fps": None})

    def test_ids(self):
        self.assertEqual(cm.clean_input({"label": "A", "source": 0, "id": "salle_2"}, creating=True)["id"], "salle_2")
        for bad in ("a b", "<x>", "é", "x" * 33, 5):
            with self.subTest(bad=bad), self.assertRaises(cm.CameraError):
                cm.clean_input({"label": "A", "source": 0, "id": bad}, creating=True)


class ManagerBase(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal()
        for model in (Camera, Setting, Event):
            s.query(model).delete()
        s.commit(); s.close()
        self.created = []
        self.cams, self.meta, self.sources = {}, {}, {}
        self.feed = ev.EventFeed()
        self.known, self.targets = [{"nom": "Awa"}], [{"id": 1}]
        self.manager = self.new_manager()

    def factory(self, cfg):
        cam = FakeCamera(camera_id=cfg["id"], source=cfg["source"])
        cam.cfg = cfg
        self.created.append(cam)
        return cam

    def new_manager(self, factory=None):
        return cm.CameraManager(self.cams, self.meta, self.sources, factory or self.factory,
                                known=lambda: self.known, targets=lambda: self.targets, feed=self.feed)

    def seed(self, cameras=None):
        self.manager.seed_if_needed(cameras or [
            {"id": "entre", "source": 0, "label": "Entrée", "location": "Hall"},
            {"id": "salle", "source": "http://10.0.0.2:8081/video?token=SECRET", "label": "Salle"}])
        self.manager.load_and_start()

    def types(self):
        return [e["type"] for e in reversed(self.feed.recent())]


class SeedTests(ManagerBase):
    def test_seed_imports_once_and_starts_the_enabled_cameras(self):
        self.seed()
        self.assertEqual(sorted(self.cams), ["entre", "salle"])
        self.assertEqual(self.sources, {"entre": 0, "salle": "http://10.0.0.2:8081/video?token=SECRET"})
        self.assertEqual(self.meta["entre"]["location"], "Hall")
        self.assertTrue(all(c.running for c in self.cams.values()))
        self.assertEqual([c.cfg["id"] for c in self.created], ["entre", "salle"])

    def test_seed_does_not_run_twice_and_not_after_everything_was_deleted(self):
        self.seed()
        self.assertEqual(self.manager.seed_if_needed([{"id": "autre", "source": 5}]), 0)
        for cam_id in ("entre", "salle"):
            self.manager.delete(cam_id)
        again = self.new_manager()
        self.assertEqual(again.seed_if_needed([{"id": "entre", "source": 0}]), 0)     # pas de webcam fantôme au redémarrage
        self.assertEqual(again.list_admin(), [])

    def test_seed_with_existing_cameras_only_sets_the_flag(self):
        s = SessionLocal(); s.add(Camera(id="x", label="X", source="3")); s.commit(); s.close()
        self.assertEqual(self.manager.seed_if_needed([{"id": "autre", "source": 5}]), 0)
        self.assertEqual([c["id"] for c in self.manager.list_admin()], ["x"])

    def test_cameras_survive_a_restart_and_json_is_not_read_again(self):
        self.seed()
        self.manager.update("salle", {"label": "Salle B", "process_fps": 4})
        self.manager.stop_all()
        cams2, meta2, sources2 = {}, {}, {}
        again = cm.CameraManager(cams2, meta2, sources2, self.factory, lambda: [], lambda: [], self.feed)
        self.assertEqual(again.seed_if_needed([{"id": "ancien", "source": 9}]), 0)
        again.load_and_start()
        self.assertEqual(sorted(cams2), ["entre", "salle"])
        self.assertEqual(meta2["salle"]["label"], "Salle B")
        self.assertEqual(self.created[-1].cfg["process_fps"], 4.0)


class CreateTests(ManagerBase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_create_starts_the_pipeline_with_the_saved_settings(self):
        view = self.manager.create({"label": "Parking", "source": "rtsp://u:p@10.0.0.9:554/s", "location": "Extérieur",
                                    "capture_width": 1920, "capture_height": 1080, "process_fps": 5, "x": 0.4, "y": 0.2})
        self.assertEqual(view["id"], "parking")
        cam = self.cams["parking"]
        self.assertTrue(cam.running)
        self.assertEqual((cam.cfg["source"], cam.cfg["capture_width"], cam.cfg["capture_height"], cam.cfg["process_fps"]),
                         ("rtsp://u:p@10.0.0.9:554/s", 1920, 1080, 5.0))
        self.assertEqual((cam.known, cam.targets), (self.known, self.targets))     # connaît déjà les personnes et les cibles
        self.assertEqual((self.meta["parking"]["x"], self.meta["parking"]["y"]), (0.4, 0.2))
        self.assertEqual(self.sources["parking"], "rtsp://u:p@10.0.0.9:554/s")
        self.assertIn(ev.CAMERA_ADDED, self.types())

    def test_the_view_never_contains_the_secret(self):
        self.manager.create({"label": "Parking", "source": "rtsp://admin:PASSWORD@10.0.0.9:554/s?key=KEY"})
        text = json.dumps(self.manager.list_admin())
        for secret in ("PASSWORD", "KEY", "SECRET"):
            self.assertNotIn(secret, text)
        view = [v for v in self.manager.list_admin() if v["id"] == "parking"][0]
        self.assertEqual((view["kind"], view["has_secret"], view["source_display"]), ("rtsp", True, "rtsp://10.0.0.9:554/s?…"))

    def test_ids_are_unique_and_labels_may_repeat(self):
        a = self.manager.create({"label": "Couloir", "source": 5})["id"]
        b = self.manager.create({"label": "Couloir", "source": 6})["id"]
        self.assertEqual((a, b), ("couloir", "couloir-2"))

    def test_explicit_id(self):
        self.assertEqual(self.manager.create({"label": "A", "source": 5, "id": "cam_a"})["id"], "cam_a")
        with self.assertRaises(cm.CameraConflict):
            self.manager.create({"label": "B", "source": 6, "id": "cam_a"})

    def test_same_source_twice_is_refused(self):
        with self.assertRaises(cm.CameraConflict) as ctx:
            self.manager.create({"label": "Doublon", "source": "0"})
        self.assertIn("Entrée", ctx.exception.message)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(len(self.manager.list_admin()), 2)

    def test_limit(self):
        with mock.patch.object(config, "MAX_CAMERAS", 2), self.assertRaises(cm.CameraConflict):
            self.manager.create({"label": "Trop", "source": 7})

    def test_a_camera_that_cannot_start_is_not_kept(self):
        def broken(cfg):
            raise RuntimeError("modèle introuvable")
        manager = self.new_manager(broken)
        with self.assertRaises(cm.CameraError) as ctx:
            manager.create({"label": "Casse", "source": 8})
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual([c["id"] for c in self.manager.list_admin()], ["entre", "salle"])

    def test_disabled_at_creation_is_stored_but_not_started(self):
        view = self.manager.create({"label": "Plus tard", "source": 9, "enabled": False})
        self.assertEqual((view["enabled"], view["status"]), (False, "disabled"))
        self.assertNotIn("plus-tard", self.cams)
        self.assertNotIn("plus-tard", self.meta)

    def test_version_changes(self):
        before = self.manager.version
        self.manager.create({"label": "Autre", "source": 10})
        self.assertGreater(self.manager.version, before)


class UpdateTests(ManagerBase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.count = len(self.created)

    def test_renaming_or_moving_does_not_restart_the_stream(self):
        before = self.cams["salle"]
        view = self.manager.update("salle", {"label": "Salle des fêtes", "location": "Étage 1", "x": 0.3, "y": 0.9})
        self.assertIs(self.cams["salle"], before)
        self.assertTrue(before.running)
        self.assertEqual(len(self.created), self.count)
        self.assertEqual((view["label"], view["location"]), ("Salle des fêtes", "Étage 1"))
        self.assertEqual((self.meta["salle"]["label"], self.meta["salle"]["x"], self.meta["salle"]["y"]), ("Salle des fêtes", 0.3, 0.9))

    def test_changing_the_source_restarts_only_that_camera(self):
        old, other = self.cams["salle"], self.cams["entre"]
        self.manager.update("salle", {"source": "http://10.0.0.3:8081/video"})
        new = self.cams["salle"]
        self.assertIsNot(new, old)
        self.assertFalse(old.running)
        self.assertTrue(new.running)
        self.assertIs(self.cams["entre"], other)
        self.assertEqual((new.cfg["source"], new.known, new.targets), ("http://10.0.0.3:8081/video", self.known, self.targets))
        self.assertEqual(self.sources["salle"], "http://10.0.0.3:8081/video")

    def test_each_streaming_setting_triggers_a_restart(self):
        for change in ({"process_fps": 3}, {"capture_width": 640, "capture_height": 480}):
            with self.subTest(change=change):
                old = self.cams["entre"]
                self.manager.update("entre", change)
                self.assertIsNot(self.cams["entre"], old)

    def test_an_unchanged_value_does_not_restart(self):
        old = self.cams["entre"]
        self.manager.update("entre", {"source": 0, "label": "Entrée"})
        self.assertIs(self.cams["entre"], old)

    def test_disable_then_enable(self):
        cam = self.cams["salle"]
        view = self.manager.update("salle", {"enabled": False})
        self.assertFalse(cam.running)
        self.assertNotIn("salle", self.cams)
        self.assertNotIn("salle", self.meta)
        self.assertEqual((view["enabled"], view["status"]), (False, "disabled"))
        self.assertEqual(len(self.manager.list_admin()), 2)                       # reste configurée
        view = self.manager.update("salle", {"enabled": True})
        self.assertTrue(self.cams["salle"].running)
        self.assertEqual(view["status"], "online")
        self.assertEqual(self.types()[-2:], [ev.CAMERA_DISABLED, ev.CAMERA_ENABLED])

    def test_clearing_the_position_returns_to_the_default_one(self):
        self.manager.update("entre", {"x": 0.9, "y": 0.9})
        self.manager.update("entre", {"x": None, "y": None})
        self.assertEqual((self.meta["entre"]["x"], self.meta["entre"]["y"]), config._PLAN_SLOTS[0])

    def test_invalid_update_changes_nothing(self):
        with self.assertRaises(cm.CameraError):
            self.manager.update("entre", {"label": "Nouveau", "x": 5})
        self.assertEqual([c["label"] for c in self.manager.list_admin()][0], "Entrée")

    def test_source_already_used_by_another_camera(self):
        with self.assertRaises(cm.CameraConflict):
            self.manager.update("salle", {"source": 0})

    def test_unknown_camera(self):
        with self.assertRaises(cm.CameraNotFound):
            self.manager.update("nope", {"label": "x"})

    def test_a_camera_that_failed_to_start_is_retried_on_update(self):
        s = SessionLocal(); s.add(Camera(id="panne", label="Panne", source="11", position=9)); s.commit(); s.close()
        flaky = {"fail": True}
        def factory(cfg):
            if cfg["id"] == "panne" and flaky["fail"]:
                raise RuntimeError("boum")
            return self.factory(cfg)
        manager = self.new_manager(factory)
        manager.load_and_start()
        view = [v for v in manager.list_admin() if v["id"] == "panne"][0]
        self.assertEqual(view["status"], "error")
        self.assertIn("boum", view["error"])
        flaky["fail"] = False
        manager.update("panne", {"label": "Panne 2"})
        self.assertIn("panne", self.cams)


class RefreshMetaTests(ManagerBase):
    def test_labels_changed_outside_the_app_are_picked_up(self):
        self.seed()
        s = SessionLocal()
        s.get(Camera, "entre").label = "Centre-ville"
        s.commit(); s.close()
        self.assertEqual(self.meta["entre"]["label"], "Entrée")
        self.manager.refresh_meta()
        self.assertEqual(self.meta["entre"]["label"], "Centre-ville")
        self.assertEqual(set(self.cams), {"entre", "salle"})      # les flux ne sont pas touchés


class DeleteRestartTests(ManagerBase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_delete_stops_and_forgets_the_camera(self):
        cam = self.cams["salle"]
        self.manager.delete("salle")
        self.assertFalse(cam.running)
        self.assertEqual((sorted(self.cams), sorted(self.meta), sorted(self.sources)), (["entre"], ["entre"], ["entre"]))
        self.assertEqual([c["id"] for c in self.manager.list_admin()], ["entre"])
        self.assertIn(ev.CAMERA_REMOVED, self.types())
        with self.assertRaises(cm.CameraNotFound):
            self.manager.delete("salle")

    def test_restart(self):
        old = self.cams["entre"]
        self.manager.restart("entre")
        self.assertIsNot(self.cams["entre"], old)
        self.assertFalse(old.running)
        self.assertTrue(self.cams["entre"].running)

    def test_restart_of_a_disabled_camera_is_refused(self):
        self.manager.update("salle", {"enabled": False})
        with self.assertRaises(cm.CameraConflict):
            self.manager.restart("salle")

    def test_helpers(self):
        self.assertEqual(self.manager.source_of("entre"), 0)
        self.assertEqual(self.manager.configured_hosts(), {"10.0.0.2"})
        found = self.manager.find_running("http://10.0.0.2:8081/video?token=SECRET")
        self.assertEqual((found[0], found[1]), ("salle", "Salle"))
        self.assertIsNone(self.manager.find_running("http://10.9.9.9/x"))

    def test_stop_all(self):
        cams = list(self.cams.values())
        self.manager.stop_all()
        self.assertEqual(self.cams, {})
        self.assertTrue(all(not c.running for c in cams))


if __name__ == "__main__":
    unittest.main()
