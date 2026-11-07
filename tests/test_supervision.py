import unittest
from unittest import mock

import numpy as np

import tests  # noqa: F401
from tests.fakes import FakeCamera
from backend.app import config, events as ev, supervision as sup
from backend.app.db import Event, SessionLocal, init_db
from backend.app.targets import TargetRegistry

META = {"entre": {"label": "Entrée", "location": "Hall", "x": .1, "y": .7},
        "salle": {"label": "Salle", "location": "", "x": .6, "y": .3}}


class MaskSourceTests(unittest.TestCase):
    def test_webcam_index(self):
        self.assertEqual(sup.mask_source(0), "Webcam 0")
        self.assertEqual(sup.mask_source(2), "Webcam 2")

    def test_token_and_credentials_never_appear(self):
        for source in ("http://192.168.1.31:8081/video?token=SECRET",
                       "rtsp://admin:SECRET@192.168.1.5:554/stream1",
                       "http://user:SECRET@host/video?token=SECRET&x=1"):
            with self.subTest(source=source):
                self.assertNotIn("SECRET", sup.mask_source(source))
        self.assertEqual(sup.mask_source("http://192.168.1.31:8081/video?token=SECRET"), "http://192.168.1.31:8081/video?…")
        self.assertEqual(sup.mask_source("rtsp://admin:SECRET@192.168.1.5:554/stream1"), "rtsp://192.168.1.5:554/stream1")

    def test_local_file_shows_only_the_file_name(self):
        self.assertEqual(sup.mask_source(r"C:\Users\Moi\Videos\demo.mp4"), "demo.mp4")
        self.assertEqual(sup.mask_source("/home/x/demo.mp4"), "demo.mp4")

    def test_odd_values(self):
        self.assertEqual(sup.mask_source(None), "—")
        self.assertEqual(sup.mask_source(True), "—")


class DescribeTests(unittest.TestCase):
    def test_camera_cards(self):
        cams = {"entre": FakeCamera(camera_id="entre"), "salle": FakeCamera(camera_id="salle")}
        cams["entre"].latest_events = [{"mode": "UNK"}, {"mode": "UNK"}]
        cams["salle"].forced_status = "offline"
        targets = [{"id": 1, "active": True, "camera_id": "entre"}, {"id": 2, "active": False, "camera_id": "salle"}]
        cards = {c["id"]: c for c in sup.describe_cameras(cams, {"entre": 0, "salle": "http://h/v?token=S"}, META, targets)}
        self.assertEqual((cards["entre"]["label"], cards["entre"]["location"]), ("Entrée", "Hall"))
        self.assertEqual((cards["entre"]["status"], cards["entre"]["persons"], cards["entre"]["targets_here"]), ("online", 2, [1]))
        self.assertEqual((cards["salle"]["status"], cards["salle"]["persons"], cards["salle"]["fps"]), ("offline", 0, 0.0))
        self.assertEqual(cards["salle"]["source"], "http://h/v?…")
        self.assertEqual(cards["entre"]["source"], "Webcam 0")

    def test_camera_without_metadata_still_described(self):
        cards = sup.describe_cameras({"x": FakeCamera(camera_id="x")}, {}, {}, [])
        self.assertEqual((cards[0]["label"], cards[0]["location"], cards[0]["source"]), ("x", "", "—"))

    def test_visible_people(self):
        a, b = FakeCamera(camera_id="entre"), FakeCamera(camera_id="salle")
        a.latest_events = [{"mode": "DB", "label": "Awa", "db_score": .5, "participant_id": 1, "match_status": "a_verifier"},
                           {"mode": "DB", "label": "Awa", "db_score": .7, "participant_id": 1, "match_status": "probable"},
                           {"mode": "UNK"}, {"mode": "TARGET", "label": "Cible 1"}]
        b.latest_events = [{"mode": "DB", "label": "Awa", "db_score": .6, "participant_id": 1, "match_status": "probable",
                            "participant_active": False}]
        people = sup.visible_people({"entre": a, "salle": b})
        self.assertEqual(sorted((p["camera_id"], p["participant_id"], p["label"], p["score"], p["status"], p["active"]) for p in people),
                         [("entre", 1, "Awa", 0.7, "probable", True), ("salle", 1, "Awa", 0.6, "probable", False)])
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            masked = sup.visible_people({"entre": a})
            self.assertEqual({p["label"] for p in masked}, {"Personne enrôlée"})
            self.assertEqual({p["participant_id"] for p in masked}, {None})

    def test_two_people_with_the_same_name_are_listed_separately(self):
        a = FakeCamera(camera_id="entre")
        a.latest_events = [{"mode": "DB", "label": "Awa Ba", "db_score": .7, "participant_id": 1},
                           {"mode": "DB", "label": "Awa Ba", "db_score": .6, "participant_id": 2}]
        self.assertEqual(sorted(p["participant_id"] for p in sup.visible_people({"entre": a})), [1, 2])


class MonitorBase(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal(); s.query(Event).delete(); s.commit(); s.close()
        self.feed = ev.EventFeed()
        self.cams = {"entre": FakeCamera(camera_id="entre"), "salle": FakeCamera(camera_id="salle")}
        self.registry = TargetRegistry()
        self.mon = sup.SupervisionMonitor(lambda: self.cams, self.registry.items, self.feed, lambda: META,
                                          offline_after_sec=5, never_online_after_sec=15,
                                          lost_after_sec=8, recognized_cooldown_sec=60)

    def titles(self, **kw):
        return [e["title"] for e in reversed(self.feed.recent(**kw))]

    def types(self):
        return [e["type"] for e in reversed(self.feed.recent())]


class CameraEventsTests(MonitorBase):
    def test_no_event_for_cameras_that_are_online_at_startup(self):
        self.mon.step(100.0); self.mon.step(101.0)
        self.assertEqual(self.types(), [])

    def test_offline_event_only_after_the_delay_and_only_once(self):
        self.mon.step(100.0)
        self.cams["salle"].forced_status = "reconnecting"
        self.mon.step(101.0); self.mon.step(104.0)
        self.assertEqual(self.types(), [])                      # moins de 5 s : tolérance aux coupures brèves
        self.mon.step(106.5); self.mon.step(120.0); self.mon.step(150.0)
        self.assertEqual(self.types(), [ev.CAMERA_OFFLINE])     # une seule alerte, pas une par seconde
        e = self.feed.recent()[0]
        self.assertEqual((e["severity"], e["camera_id"], e["title"]), ("warning", "salle", "Caméra hors ligne : Salle"))

    def test_recovery_event(self):
        self.mon.step(100.0)
        self.cams["salle"].forced_status = "offline"
        self.mon.step(101.0); self.mon.step(110.0)
        self.cams["salle"].forced_status = "online"
        self.mon.step(111.0); self.mon.step(112.0)
        self.assertEqual(self.types(), [ev.CAMERA_OFFLINE, ev.CAMERA_ONLINE])
        self.assertEqual(self.titles()[-1], "Caméra rétablie : Salle")

    def test_a_blink_shorter_than_the_delay_produces_nothing(self):
        self.mon.step(100.0)
        self.cams["salle"].forced_status = "reconnecting"
        self.mon.step(101.0)
        self.cams["salle"].forced_status = "online"
        self.mon.step(103.0); self.mon.step(110.0)
        self.assertEqual(self.types(), [])

    def test_a_camera_that_never_came_online_gets_a_longer_delay(self):
        self.cams["salle"].forced_status = "connecting"
        self.cams["salle"].last_frame_ts = 0.0                   # jamais d'image
        self.mon.step(100.0); self.mon.step(110.0)
        self.assertEqual(self.types(), [])
        self.mon.step(116.0)
        self.assertEqual(self.types(), [ev.CAMERA_OFFLINE])

    def test_a_removed_camera_is_forgotten_and_a_new_one_is_not_reported_at_startup(self):
        self.mon.step(100.0)
        self.assertIn("salle", self.mon._cam_state)
        del self.cams["salle"]
        self.mon.step(101.0)
        self.assertNotIn("salle", self.mon._cam_state)
        self.cams["parking"] = FakeCamera(camera_id="parking")
        self.mon.step(102.0); self.mon.step(103.0)
        self.assertEqual(self.types(), [])

    def test_stopped_camera_is_ignored(self):
        self.cams["salle"].forced_status = "stopped"
        self.mon.step(100.0); self.mon.step(300.0)
        self.assertEqual(self.types(), [])


class PeopleEventsTests(MonitorBase):
    def see(self, cam="entre", name="Awa Ba", pid=7, status="probable", active=True, score=.62, ambiguous=False):
        self.cams[cam].latest_events = [{"mode": "DB", "label": name, "db_score": score, "participant_id": pid,
                                         "match_status": status, "participant_active": active,
                                         "match_ambiguous": ambiguous}]

    def test_a_probable_match_is_announced_once_per_cooldown(self):
        self.see()
        self.mon.step(100.0); self.mon.step(101.0); self.mon.step(130.0)
        self.assertEqual(self.types(), [ev.PERSON_RECOGNIZED])
        self.mon.step(161.0)
        self.assertEqual(self.types(), [ev.PERSON_RECOGNIZED, ev.PERSON_RECOGNIZED])
        self.assertEqual(self.titles()[0], "Personne reconnue : Awa Ba — Entrée")
        payload = self.feed.recent()[0]["payload"]
        self.assertEqual((payload["participant_id"], payload["status"], payload["person"]), (7, "probable", "Awa Ba"))

    def test_a_weak_match_is_never_presented_as_a_recognition(self):
        self.see(status="a_verifier", score=.41)
        self.mon.step(100.0)
        self.assertEqual(self.titles(), ["Correspondance à vérifier : Awa Ba — Entrée"])
        self.assertEqual(self.feed.recent()[0]["payload"]["status"], "a_verifier")
        self.assertEqual(self.feed.recent()[0]["severity"], "info")

    def test_an_inactive_person_recognised_probably_raises_a_warning(self):
        self.see(active=False)
        self.mon.step(100.0)
        e = self.feed.recent()[0]
        self.assertEqual((e["type"], e["severity"]), (ev.PERSON_INACTIVE, "warning"))
        self.assertEqual(e["title"], "Personne désactivée reconnue : Awa Ba — Entrée")

    def test_an_inactive_person_with_a_weak_match_is_not_a_warning(self):
        self.see(active=False, status="a_verifier", score=.4)
        self.mon.step(100.0)
        self.assertEqual((self.types(), self.feed.recent()[0]["severity"]), ([ev.PERSON_RECOGNIZED], "info"))

    def test_two_people_with_the_same_name_are_two_events(self):
        self.see(pid=7); self.cams["salle"].latest_events = [dict(self.cams["entre"].latest_events[0], participant_id=8)]
        self.mon.step(100.0)
        self.assertEqual(len(self.feed.recent()), 2)
        self.cams["salle"].latest_events = []
        self.cams["entre"].latest_events = [dict(self.cams["entre"].latest_events[0], participant_id=9)]
        self.mon.step(101.0)
        self.assertEqual(len(self.feed.recent()), 3)           # même nom, autre fiche : annoncé

    def test_same_person_on_two_cameras_is_two_events(self):
        self.see("entre"); self.see("salle")
        self.mon.step(100.0)
        self.assertEqual(len(self.feed.recent()), 2)

    def test_identity_is_masked_when_configured(self):
        self.see()
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            self.mon.step(100.0)
        self.assertNotIn("Awa", self.titles()[0])
        payload = self.feed.recent()[0]["payload"]
        self.assertNotIn("Awa", str(payload))
        self.assertNotIn("participant_id", payload)            # pas d'identifiant diffusé non plus

    def test_unknown_and_target_detections_are_not_person_events(self):
        self.cams["entre"].latest_events = [{"mode": "UNK"}, {"mode": "TARGET", "label": "Cible 1"}]
        self.mon.step(100.0)
        self.assertEqual(self.types(), [])


class TargetEventsTests(MonitorBase):
    def add_target(self, vec=(1, 0, 0)):
        return self.registry.add(np.array(vec, dtype=np.float32), duplicate_threshold=2)

    def see(self, target, cam, now, score=.7):
        target.tracker.update({"camera_id": cam, "track_id": 1, "global_id": 1, "score": score}, now)

    def test_first_sighting_then_camera_change(self):
        t = self.add_target()
        self.see(t, "entre", 100.0)
        self.mon.on_target_move(t, {"camera_id": "entre", "ts": 100.0, "score": .7})
        self.see(t, "salle", 101.0); self.see(t, "salle", 102.0)
        self.mon.on_target_move(t, {"camera_id": "salle", "ts": 102.0, "score": .7})
        self.assertEqual(self.types(), [ev.TARGET_SEEN, ev.TARGET_MOVED])
        self.assertEqual(self.titles(), ["Cible 1 repérée : Entrée", "Cible 1 : Entrée → Salle"])
        moved = self.feed.recent()[0]
        self.assertEqual((moved["camera_id"], moved["payload"]["from_camera"], moved["payload"]["target_id"]), ("salle", "entre", t.id))
        self.assertEqual(moved["payload"]["color"], t.color)

    def quiet(self, target, now):
        """Le thread de suivi (0,1 s) constate l'absence de détection : la cible devient inactive après 3 s."""
        target.tracker.update(None, now)

    def test_lost_after_the_delay_once_then_seen_again(self):
        t = self.add_target()
        self.see(t, "entre", 100.0)
        self.mon.step(100.5)                                        # active
        self.quiet(t, 106.0); self.mon.step(106.0)                  # inactive (> 3 s), pas encore « perdue »
        self.assertEqual(self.types(), [])
        self.quiet(t, 115.0); self.mon.step(115.0); self.mon.step(130.0)
        self.assertEqual(self.types(), [ev.TARGET_LOST])            # une seule fois
        self.assertEqual(self.titles(), ["Cible 1 perdue de vue (dernière caméra : Entrée)"])
        self.see(t, "entre", 140.0)
        self.mon.step(140.2); self.mon.step(141.0)
        self.assertEqual(self.types(), [ev.TARGET_LOST, ev.TARGET_SEEN])
        self.assertTrue(self.feed.recent()[0]["payload"]["reappeared"])

    def test_a_short_gap_is_not_a_loss(self):
        t = self.add_target()
        self.see(t, "entre", 100.0); self.mon.step(100.5)
        self.quiet(t, 104.0); self.mon.step(104.0)                  # inactive depuis < 8 s
        self.see(t, "entre", 106.0); self.mon.step(106.5)
        self.quiet(t, 125.0); self.mon.step(125.0)
        self.assertEqual(self.types(), [])

    def test_target_never_seen_is_never_reported_lost(self):
        self.add_target()
        self.mon.step(100.0); self.mon.step(500.0)
        self.assertEqual(self.types(), [])

    def test_removed_target_state_is_forgotten(self):
        t = self.add_target()
        self.see(t, "entre", 100.0); self.mon.step(100.5)
        self.registry.remove(t.id)
        self.mon.step(500.0)
        self.assertEqual(self.types(), [])
        self.assertEqual(self.mon._target_state, {})

    def test_two_targets_are_tracked_independently(self):
        a, b = self.add_target((1, 0, 0)), self.add_target((0, 1, 0))
        self.see(a, "entre", 100.0); self.see(b, "salle", 100.0)
        self.mon.step(100.5)
        self.quiet(a, 106.0); self.see(b, "salle", 106.0); self.mon.step(106.0)
        self.quiet(a, 115.0); self.see(b, "salle", 115.0); self.mon.step(115.0)    # b reste visible, a disparaît
        self.assertEqual(self.titles(), ["Cible 1 perdue de vue (dernière caméra : Entrée)"])


class MonitorThreadTests(MonitorBase):
    def test_thread_runs_and_stops(self):
        self.cams["salle"].forced_status = "offline"
        mon = sup.SupervisionMonitor(lambda: self.cams, self.registry.items, self.feed, lambda: META,
                                     interval=0.02, offline_after_sec=0.05, never_online_after_sec=0.05)
        mon.start()
        import time
        end = time.time() + 3
        while time.time() < end and not self.feed.recent():
            time.sleep(0.02)
        mon.stop()
        self.assertEqual(self.types(), [ev.CAMERA_OFFLINE])
        self.assertFalse(mon._thread.is_alive())

    def test_an_error_in_one_pass_does_not_kill_the_thread(self):
        calls = []
        def boom():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("panne")
            return {}
        mon = sup.SupervisionMonitor(boom, self.registry.items, self.feed, lambda: META, interval=0.01)
        with self.assertLogs(level="ERROR"):
            mon.start()
            import time
            end = time.time() + 3
            while time.time() < end and len(calls) < 4:
                time.sleep(0.01)
            mon.stop()
        self.assertGreaterEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
