import json
import unittest

from fastapi.testclient import TestClient

import tests  # noqa: F401
from backend.app import dispatch, events, main
from backend.app.db import Authority, Dispatch, Participant, SessionLocal, Setting, init_db
from backend.app.target_path import TargetTracker

META = {"c1": {"label": "Centre-ville", "location": "Cœur de ville", "x": 0.5, "y": 0.5},
        "c2": {"label": "Aéroport", "location": "", "x": 0.9, "y": 0.9},
        "c3": {"label": "Sans position"}}


def auth(**kw):
    base = {"id": 1, "name": "Poste A", "service": "", "phone": "", "telegram_chat_id": "", "cameras": [], "x": None,
            "y": None, "enabled": True, "channel": "simulation"}
    base.update(kw)
    return base


class FakeTarget:
    def __init__(self, match=None, photo=None):
        self.match, self.photo, self.tracker = match, photo, TargetTracker()


class NearestTests(unittest.TestCase):
    def test_explicit_camera_wins_over_distance(self):
        a, b = auth(id=1, x=0.5, y=0.5), auth(id=2, cameras=["c1"], x=0.0, y=0.0)
        self.assertEqual(dispatch.nearest_authority([a, b], "c1", META)["id"], 2)

    def test_nearest_by_distance(self):
        a, b = auth(id=1, x=0.4, y=0.4), auth(id=2, x=0.8, y=0.8)
        self.assertEqual(dispatch.nearest_authority([a, b], "c1", META)["id"], 1)
        self.assertEqual(dispatch.nearest_authority([a, b], "c2", META)["id"], 2)

    def test_disabled_ignored_and_none_without_position(self):
        a = auth(id=1, x=0.5, y=0.5, enabled=False)
        self.assertIsNone(dispatch.nearest_authority([a], "c1", META))
        self.assertIsNone(dispatch.nearest_authority([auth(x=0.5, y=0.5)], "c3", META))
        self.assertIsNone(dispatch.nearest_authority([auth()], "c1", META))


class CleanTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.clean_authority({"name": " "})
        with self.assertRaises(dispatch.DispatchError):
            dispatch.clean_authority({"name": "A", "phone": "abc"})
        with self.assertRaises(dispatch.DispatchError):
            dispatch.clean_authority({"name": "A", "telegram_chat_id": "x1"})
        with self.assertRaises(dispatch.DispatchError):
            dispatch.clean_authority({"name": "A", "x": 2})
        ok = dispatch.clean_authority({"name": " A ", "phone": "+226 70 00 00 00", "cameras": ["b", "a", "a"]})
        self.assertEqual((ok["name"], ok["cameras"]), ("A", ["a", "b"]))


class DispatchBase(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal()
        for m in (Authority, Dispatch, Participant, Setting):
            s.query(m).delete()
        s.commit(); s.close()

    def add_authority(self, **kw):
        s = SessionLocal()
        row = Authority(name=kw.pop("name", "Poste A"), cameras=json.dumps(kw.pop("cameras", [])), **kw)
        s.add(row); s.commit(); s.close()

    def ev(self, type_=events.TARGET_SEEN, cam="c1", **payload):
        return {"id": 7, "timestamp": "2026-11-10T16:00:00Z", "type": type_, "camera_id": cam,
                "payload": {"target_id": 1, "target_label": "Cible 1", "score": 0.81, **payload}}


class MessageTests(DispatchBase):
    def test_message_contains_identity_but_no_embedding(self):
        s = SessionLocal()
        p = Participant(nom="Ouedraogo", prenom="Awa", matricule="M-12", infos="Cheveux courts", actif=True)
        s.add(p); s.commit()
        target = FakeTarget(match={"matched": True, "participant_id": p.id, "score": 0.77})
        target.tracker._path.append({"camera_id": "c1", "ts": 1762790400.0, "score": 0.8})
        text = dispatch.compose_message(s, self.ev(), target, META)
        s.close()
        for expected in ("Cible 1", "Centre-ville", "Cœur de ville", "Awa Ouedraogo", "M-12", "Cheveux courts", "77%", "Derniers passages"):
            self.assertIn(expected, text)
        self.assertIn("à vérifier", text)

    def test_unknown_person_says_so(self):
        s = SessionLocal()
        text = dispatch.compose_message(s, self.ev(), FakeTarget(match={"matched": False}), META)
        s.close()
        self.assertIn("identité inconnue", text)

    def test_moved_mentions_previous_camera(self):
        s = SessionLocal()
        text = dispatch.compose_message(s, self.ev(events.TARGET_MOVED, cam="c2", from_camera="c1"), None, META)
        s.close()
        self.assertIn("Vue juste avant : Centre-ville", text)


class DispatcherTests(DispatchBase):
    def make(self, target=None):
        return dispatch.Dispatcher(lambda: META, lambda tid: target)

    def test_simulated_dispatch_is_logged(self):
        self.add_authority(x=0.5, y=0.5)
        out = self.make(FakeTarget()).handle(self.ev(), now=1000.0)
        self.assertEqual((out["channel"], out["status"]), ("interne", "déposé"))
        s = SessionLocal(); self.assertEqual(s.query(Dispatch).count(), 1); s.close()

    def test_only_live_images_are_attached_never_the_uploaded_reference_photo(self):
        self.add_authority(x=0.5, y=0.5)
        sent = []
        orig = dispatch.send_to_authority
        dispatch.send_to_authority = lambda a, text, photos=None: (sent.append(photos) or ("interne", "déposé"))
        try:
            d = dispatch.Dispatcher(lambda: META, lambda tid: FakeTarget(photo=b"photo-uploadee"),
                                    lambda cid: b"scene-" + cid.encode(), lambda cid, tid: b"crop-%d" % tid)
            out = d.handle(self.ev(), now=5.0)
        finally:
            dispatch.send_to_authority = orig
        self.assertEqual(sent, [[("Gros plan de la cible", b"crop-1"), ("Image de la caméra", b"scene-c1")]])
        self.assertNotIn(b"photo-uploadee", [data for _, data in sent[0]])
        self.assertIn("2 photo(s) jointe(s)", out["message"])
        self.assertEqual(dispatch.PHOTOS.labels(out["id"]), ["Gros plan de la cible", "Image de la caméra"])

    def test_scene_only_when_no_crop_yet(self):
        self.add_authority(x=0.5, y=0.5)
        d = dispatch.Dispatcher(lambda: META, lambda tid: FakeTarget(), lambda cid: b"scene")
        out = d.handle(self.ev(), now=7.0)
        self.assertEqual(dispatch.PHOTOS.labels(out["id"]), ["Image de la caméra"])

    def test_no_photo_available_is_stated(self):
        self.add_authority(x=0.5, y=0.5)
        out = self.make(FakeTarget()).handle(self.ev(), now=9.0)
        self.assertIn("Aucune photo disponible", out["message"])

    def test_cooldown_per_target_and_camera(self):
        self.add_authority(x=0.5, y=0.5)
        d = self.make(FakeTarget())
        self.assertIsNotNone(d.handle(self.ev(), now=1000.0))
        self.assertIsNone(d.handle(self.ev(), now=1010.0))
        self.assertIsNotNone(d.handle(self.ev(cam="c2"), now=1010.0))
        self.assertIsNotNone(d.handle(self.ev(), now=1000.0 + dispatch.COOLDOWN_SEC + 1))

    def test_switch_off_and_no_authority(self):
        d = self.make(FakeTarget())
        self.assertIsNone(d.handle(self.ev(), now=1.0))           # aucune autorité
        self.add_authority(x=0.5, y=0.5)
        s = SessionLocal(); dispatch.set_enabled(s, False); s.close()
        self.assertIsNone(d.handle(self.ev(), now=2.0))

    def test_only_target_events_are_queued(self):
        d = self.make()
        d.on_event({"type": events.TARGET_LOST, "payload": {}})
        d.on_event({"type": "camera_offline", "payload": {}})
        self.assertTrue(d._queue.empty())
        d.on_event(self.ev())
        self.assertFalse(d._queue.empty())

    def test_telegram_needs_token_and_chat(self):
        import os
        os.environ.pop("NIIFYEEN_TELEGRAM_TOKEN", None)
        self.assertEqual(dispatch.send_to_authority(auth(telegram_chat_id="123"), "x"), ("interne", "déposé"))


class TelegramSendTests(unittest.TestCase):
    def test_photos_use_sendphoto_with_caption_then_reference(self):
        import os
        calls = []
        orig = dispatch._telegram_post
        dispatch._telegram_post = lambda method, fields, photo=None: calls.append((method, fields, photo))
        os.environ["NIIFYEEN_TELEGRAM_TOKEN"] = "t"
        try:
            res = dispatch.send_to_authority(auth(telegram_chat_id="42"), "texte", [("Gros plan", b"live"), ("Photo de référence", b"ref")])
        finally:
            dispatch._telegram_post = orig
            os.environ.pop("NIIFYEEN_TELEGRAM_TOKEN", None)
        self.assertEqual(res, ("telegram", "envoyé"))
        self.assertEqual([c[0] for c in calls], ["sendPhoto", "sendPhoto"])
        self.assertEqual((calls[0][1]["caption"], calls[0][2]), ("texte", b"live"))
        self.assertEqual((calls[1][1]["caption"], calls[1][2]), ("Photo de référence", b"ref"))

    def test_without_photo_sends_plain_message(self):
        import os
        calls = []
        orig = dispatch._telegram_post
        dispatch._telegram_post = lambda method, fields, photo=None: calls.append(method)
        os.environ["NIIFYEEN_TELEGRAM_TOKEN"] = "t"
        try:
            dispatch.send_to_authority(auth(telegram_chat_id="42"), "texte", [])
        finally:
            dispatch._telegram_post = orig
            os.environ.pop("NIIFYEEN_TELEGRAM_TOKEN", None)
        self.assertEqual(calls, ["sendMessage"])


class ApiTests(DispatchBase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app)

    def test_crud_and_log(self):
        r = self.client.post("/api/authorities", json={"name": "Commissariat", "phone": "+226 70 00 00 00", "cameras": ["c1"]})
        self.assertEqual(r.status_code, 201, r.text)
        aid = r.json()["id"]
        self.assertEqual(r.json()["channel"], "interne")
        self.assertIn("/a/", r.json()["inbox_url"])
        self.assertNotIn("inbox_token", r.json())
        self.assertEqual(self.client.put(f"/api/authorities/{aid}", json={"x": 0.3, "y": 0.4}).json()["x"], 0.3)
        bad = self.client.post("/api/authorities", json={"name": ""})
        self.assertEqual(bad.status_code, 422)
        t = self.client.post(f"/api/authorities/{aid}/test").json()
        self.assertEqual(t["status"], "déposé")
        self.assertEqual(self.client.get("/api/dispatches").json()["dispatches"][0]["target_label"], "essai")
        listing = self.client.get("/api/authorities").json()
        self.assertEqual(len(listing["authorities"]), 1)
        self.assertTrue(listing["enabled"])
        self.assertEqual(self.client.put("/api/authorities-scope", json={"enabled": False}).json(), {"enabled": False})
        self.assertEqual(self.client.delete(f"/api/authorities/{aid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/authorities/{aid}").status_code, 404)

    def test_page_is_served(self):
        r = self.client.get("/authorities")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Journal des envois", r.text)


if __name__ == "__main__":
    unittest.main()


class InboxTests(DispatchBase):
    """Boîte de réception interne : lien secret par autorité, isolement, photos en mémoire, accusé de prise en charge."""

    def setUp(self):
        super().setUp()
        from backend.app import inbox
        self.client = TestClient(inbox.build_app())
        main_client = TestClient(main.app)
        self.a = main_client.post("/api/authorities", json={"name": "Poste A", "x": 0.5, "y": 0.5}).json()
        self.b = main_client.post("/api/authorities", json={"name": "Poste B"}).json()
        self.main_client = main_client
        self.ta, self.tb = (u["inbox_url"].rsplit("/a/", 1)[1] for u in (self.a, self.b))

    def send(self, photos=(("Gros plan de la cible", b"jpeg-bytes"),)):
        s = SessionLocal()
        view = dispatch.authority_view(s.get(Authority, self.a["id"]))
        row = dispatch.log_dispatch(s, view, "interne", "déposé", "ALERTE test", {"id": 1, "camera_id": "c1"}, "Cible 1")
        s.close()
        dispatch.PHOTOS.put(row["id"], list(photos))
        return row

    def test_wrong_or_missing_token_is_404_everywhere(self):
        for path in ("/a/inconnu", "/a/inconnu/api/messages", "/a/inconnu/api/photo/1/0"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/a/inconnu/api/ack/1").status_code, 404)

    def test_only_reception_routes_exist(self):
        for path in ("/", "/api/cameras", "/api/authorities", "/people", "/docs", "/openapi.json", "/api/dispatches"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_authority_sees_only_its_own_messages_and_photos(self):
        row = self.send()
        mine = self.client.get(f"/a/{self.ta}/api/messages").json()
        self.assertEqual([m["id"] for m in mine["messages"]], [row["id"]])
        self.assertEqual(mine["messages"][0]["photos"], ["Gros plan de la cible"])
        self.assertEqual(mine["messages"][0]["camera_label"], "c1")
        self.assertEqual(self.client.get(f"/a/{self.tb}/api/messages").json()["messages"], [])
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/photo/{row['id']}/0").content, b"jpeg-bytes")
        self.assertEqual(self.client.get(f"/a/{self.tb}/api/photo/{row['id']}/0").status_code, 404)
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/photo/{row['id']}/5").status_code, 404)

    def test_acknowledge_only_own_message(self):
        row = self.send()
        self.assertEqual(self.client.post(f"/a/{self.tb}/api/ack/{row['id']}").status_code, 404)
        self.assertEqual(self.client.post(f"/a/{self.ta}/api/ack/{row['id']}").status_code, 200)
        msg = self.client.get(f"/a/{self.ta}/api/messages").json()["messages"][0]
        self.assertTrue(msg["acknowledged"])

    def test_delete_one_and_all_only_own_and_journal_keeps_them(self):
        r1, r2 = self.send(), self.send()
        self.assertEqual(self.client.delete(f"/a/{self.tb}/api/messages/{r1['id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/a/inconnu/api/messages/{r1['id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/a/{self.ta}/api/messages/{r1['id']}").status_code, 200)
        ids = [m["id"] for m in self.client.get(f"/a/{self.ta}/api/messages").json()["messages"]]
        self.assertEqual(ids, [r2["id"]])
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/photo/{r1['id']}/0").status_code, 404)
        res = self.client.delete(f"/a/{self.ta}/api/messages").json()
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/messages").json()["messages"], [])
        journal = self.main_client.get("/api/dispatches").json()["dispatches"]
        self.assertEqual({d["id"] for d in journal}, {r1["id"], r2["id"]})

    def test_lan_addresses_skip_virtual_cards_and_put_wsl_last(self):
        from unittest import mock
        from backend.app import inbox
        infos = [(2, 1, 6, "", (ip, 0)) for ip in ("192.168.56.1", "172.31.16.1", "169.254.10.84", "192.168.11.100", "127.0.0.1")]
        with mock.patch.object(inbox, "lan_address", return_value="172.31.16.1"), \
                mock.patch.object(inbox.socket, "getaddrinfo", return_value=infos):
            self.assertEqual(inbox.lan_addresses(), ["192.168.11.100", "172.31.16.1"])

    def test_lan_toggle_changes_links_and_persists(self):
        from backend.app import inbox
        try:
            res = self.main_client.put("/api/inbox-lan", json={"enabled": True})
            self.assertEqual(res.status_code, 200)
            self.assertTrue(inbox.lan_open())
            self.assertEqual(inbox.current_host(), "0.0.0.0")
            s = SessionLocal()
            self.assertTrue(dispatch.lan_enabled(s))
            s.close()
            info = self.main_client.get("/api/authorities").json()["inbox"]
            self.assertTrue(info["lan_open"])
            self.assertFalse(info["lan_forced"])
            self.main_client.put("/api/inbox-lan", json={"enabled": False})
            self.assertFalse(inbox.lan_open())
        finally:
            inbox.set_lan(False)

    def test_disabled_authority_and_new_link(self):
        self.main_client.put(f"/api/authorities/{self.a['id']}", json={"enabled": False})
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/messages").status_code, 404)
        self.main_client.put(f"/api/authorities/{self.a['id']}", json={"enabled": True})
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/messages").status_code, 200)
        new = self.main_client.post(f"/api/authorities/{self.a['id']}/inbox-token").json()["inbox_url"].rsplit("/a/", 1)[1]
        self.assertNotEqual(new, self.ta)
        self.assertEqual(self.client.get(f"/a/{self.ta}/api/messages").status_code, 404)
        self.assertEqual(self.client.get(f"/a/{new}/api/messages").status_code, 200)

    def test_page_headers_and_no_cache(self):
        r = self.client.get(f"/a/{self.ta}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertEqual(self.client.get("/static/inbox.js").status_code, 200)

    def test_tokens_are_long_and_distinct(self):
        self.assertNotEqual(self.ta, self.tb)
        self.assertGreaterEqual(len(self.ta), 30)

    def test_photo_store_expires_and_is_bounded(self):
        store = dispatch.PhotoStore(capacity=2, ttl_sec=10)
        store.put(1, [("x", b"a")], now=0); store.put(2, [("x", b"b")], now=1); store.put(3, [("x", b"c")], now=2)
        self.assertEqual(store.get_all(1, now=3), [])               # plus ancien évincé
        self.assertEqual(store.get_all(3, now=3), [b"c"])
        self.assertEqual(store.get_all(3, now=20), [])              # expiré


class InboxServerTests(unittest.TestCase):
    def test_default_is_local_only_and_disabled_in_tests(self):
        from backend.app import config, inbox
        self.assertEqual(config.INBOX_HOST, "127.0.0.1")
        self.assertFalse(inbox.InboxServer().start())               # NIIFYEEN_INBOX_ENABLED=0 sous test
