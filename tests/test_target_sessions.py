import io
import unittest
from unittest import mock

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import config, main
from backend.app.db import SessionLocal, Setting, TargetMove


png_bytes = _api.png_bytes


class TargetSessionTests(_api.ApiTests):
    """Module 4 : cible choisie dans la base, session sauvegardée, capacité configurable."""

    def person(self, value, nom, **form):
        r = self.post_person(value, nom, **form)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["participant_id"]

    def photo_target(self, value):
        files = [("photos", ("t.png", io.BytesIO(png_bytes(value=value)), "image/png"))]
        return self.client.post("/search/by-image", files=files)

    # ---- choix d'une personne ---------------------------------------------------
    def test_person_becomes_a_target_without_any_photo(self):
        pid = self.person(1, "Ba", prenom="Awa")
        r = self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.assertEqual(r.status_code, 200, r.text)
        t = r.json()["target"]
        self.assertEqual(t["source"], "person")
        self.assertIsNone(t["photo_url"])
        self.assertTrue(t["profile"]["matched"])
        self.assertEqual(t["profile"]["participant"]["id"], pid)
        self.assertEqual(t["profile"]["participant"]["nom"], "Awa Ba")
        listed = self.client.get("/targets").json()["targets"]
        self.assertEqual([x["id"] for x in listed], [t["id"]])

    def test_cameras_receive_the_person_embedding(self):
        pid = self.person(3, "Sy")
        self.client.post("/api/targets/from-person", json={"participant_id": pid})
        cam = next(iter(main.app.state.cams.values()))
        self.assertEqual(len(cam.targets), 1)
        self.assertAlmostEqual(float(cam.targets[0]["embedding"][3]), 1.0, places=3)

    def test_same_person_twice_is_refused(self):
        pid = self.person(1, "Ba")
        self.client.post("/api/targets/from-person", json={"participant_id": pid})
        r = self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.assertEqual(r.status_code, 409)
        self.assertIn("déjà suivie", r.json()["detail"])

    def test_person_already_added_by_photo_is_refused(self):
        pid = self.person(1, "Ba")
        self.assertEqual(self.photo_target(1).status_code, 200)
        r = self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.assertEqual(r.status_code, 409)

    def test_unknown_person_and_bad_bodies(self):
        self.assertEqual(self.client.post("/api/targets/from-person", json={"participant_id": 999}).status_code, 404)
        for body in ({}, {"participant_id": 0}, {"participant_id": 1, "x": 1}, {"participant_id": "a"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/targets/from-person", json=body).status_code, 422)

    def test_hidden_identities_refuse_choosing_a_person(self):
        pid = self.person(1, "Ba")
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            self.assertEqual(self.client.post("/api/targets/from-person", json={"participant_id": pid}).status_code, 403)

    def test_inactive_person_can_still_be_followed(self):
        pid = self.person(1, "Ba", actif="false")
        r = self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["target"]["profile"]["participant"]["actif"])

    # ---- capacité ---------------------------------------------------------------
    def test_capacity_is_configurable_and_persisted(self):
        self.assertEqual(self.client.get("/api/targets/settings").json()["max"], config.MAX_TARGETS)
        r = self.client.put("/api/targets/settings", json={"max": 2})
        self.assertEqual(r.json()["max"], 2)
        self.assertEqual(self.client.get("/targets").json()["max"], 2)
        s = SessionLocal()
        self.assertEqual(s.get(Setting, "targets_capacity").value, "2")
        s.close()

    def test_capacity_limit_is_enforced(self):
        self.client.put("/api/targets/settings", json={"max": 1})
        a, b = self.person(1, "A"), self.person(2, "B")
        self.assertEqual(self.client.post("/api/targets/from-person", json={"participant_id": a}).status_code, 200)
        r = self.client.post("/api/targets/from-person", json={"participant_id": b})
        self.assertEqual(r.status_code, 409)
        self.assertIn("maximal", r.json()["detail"])

    def test_capacity_cannot_go_below_current_targets(self):
        a, b = self.person(1, "A"), self.person(2, "B")
        for pid in (a, b):
            self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.assertEqual(self.client.put("/api/targets/settings", json={"max": 1}).status_code, 409)
        self.assertEqual(self.client.get("/api/targets/settings").json()["max"], config.MAX_TARGETS)

    def test_capacity_bounds(self):
        for v in (0, -1, config.TARGETS_HARD_LIMIT + 1, "x"):
            with self.subTest(v=v):
                self.assertEqual(self.client.put("/api/targets/settings", json={"max": v}).status_code, 422)

    def test_more_than_eight_targets_get_colours(self):
        self.client.put("/api/targets/settings", json={"max": 10})
        for v in range(1, 11):
            pid = self.person(v, f"P{v}")
            self.assertEqual(self.client.post("/api/targets/from-person", json={"participant_id": pid}).status_code, 200)
        self.assertEqual(len(self.client.get("/targets").json()["targets"]), 10)

    def test_saved_capacity_is_loaded_at_startup(self):
        self.client.put("/api/targets/settings", json={"max": 3})
        from fastapi.testclient import TestClient
        with TestClient(main.app) as again:
            self.assertEqual(again.get("/api/targets/settings").json()["max"], 3)

    # ---- session ----------------------------------------------------------------
    def test_no_session_by_default(self):
        self.assertEqual(self.client.get("/api/targets/session").json(), {"saved": False})
        self.assertEqual(self.client.post("/api/targets/session/resume").status_code, 404)

    def test_save_and_resume_only_enrolled_people(self):
        a, b = self.person(1, "Ba", prenom="Awa"), self.person(2, "Sy")
        for pid in (a, b):
            self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.assertEqual(self.photo_target(50).status_code, 200)             # cible non enrôlée
        saved = self.client.post("/api/targets/session/save").json()
        self.assertEqual((saved["count"], saved["unenrolled_skipped"]), (2, 1))
        self.assertEqual([p["nom"] for p in saved["people"]], ["Awa Ba", "Sy"])
        self.client.post("/target/clear")
        self.assertEqual(self.client.get("/targets").json()["targets"], [])
        r = self.client.post("/api/targets/session/resume").json()
        self.assertEqual((r["added"], r["targets_count"]), (2, 2))
        names = [t["profile"]["participant"]["nom"] for t in self.client.get("/targets").json()["targets"]]
        self.assertEqual(names, ["Awa Ba", "Sy"])

    def test_session_stores_only_ids(self):
        a = self.person(1, "Ba")
        self.client.post("/api/targets/from-person", json={"participant_id": a})
        self.client.post("/api/targets/session/save")
        s = SessionLocal()
        raw = s.get(Setting, "targets_session").value
        s.close()
        self.assertNotIn("embedding", raw)
        self.assertNotIn("Ba", raw)

    def test_resume_twice_does_not_duplicate(self):
        a = self.person(1, "Ba")
        self.client.post("/api/targets/from-person", json={"participant_id": a})
        self.client.post("/api/targets/session/save")
        r = self.client.post("/api/targets/session/resume").json()
        self.assertEqual((r["added"], r["already_tracked"]), (0, 1))
        self.assertEqual(len(self.client.get("/targets").json()["targets"]), 1)

    def test_resume_skips_deleted_people(self):
        a, b = self.person(1, "Ba"), self.person(2, "Sy")
        for pid in (a, b):
            self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.client.post("/api/targets/session/save")
        self.client.post("/target/clear")
        self.client.delete(f"/api/people/{a}")
        r = self.client.post("/api/targets/session/resume").json()
        self.assertEqual((r["added"], r["missing"]), (1, 1))
        shown = self.client.get("/api/targets/session").json()["people"]
        self.assertEqual([p["exists"] for p in shown], [False, True])

    def test_resume_stops_at_capacity(self):
        ids = [self.person(v, f"P{v}") for v in (1, 2, 3)]
        for pid in ids:
            self.client.post("/api/targets/from-person", json={"participant_id": pid})
        self.client.post("/api/targets/session/save")
        self.client.post("/target/clear")
        self.client.put("/api/targets/settings", json={"max": 2})
        r = self.client.post("/api/targets/session/resume").json()
        self.assertEqual((r["added"], r["not_added_capacity"]), (2, 1))

    def test_resumed_target_has_a_fresh_trajectory_session(self):
        a = self.person(1, "Ba")
        t1 = self.client.post("/api/targets/from-person", json={"participant_id": a}).json()["target"]["id"]
        first = main.app.state.targets.get(t1).session
        self.client.post("/api/targets/session/save")
        self.client.post("/target/clear")
        self.client.post("/api/targets/session/resume")
        new = main.app.state.targets.items()[0]
        self.assertNotEqual(new.session, first)

    def test_forget_session(self):
        a = self.person(1, "Ba")
        self.client.post("/api/targets/from-person", json={"participant_id": a})
        self.client.post("/api/targets/session/save")
        self.assertTrue(self.client.delete("/api/targets/session").json()["forgotten"])
        self.assertEqual(self.client.get("/api/targets/session").json(), {"saved": False})

    def test_hidden_identities_hide_names_in_session(self):
        a = self.person(1, "Ba")
        self.client.post("/api/targets/from-person", json={"participant_id": a})
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            saved = self.client.post("/api/targets/session/save").json()
            self.assertNotIn("people", saved)
            self.assertEqual(self.client.post("/api/targets/session/resume").status_code, 403)


# On réutilise le montage de ApiTests sans rejouer ses ~100 tests.
for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in TargetSessionTests.__dict__]:
    setattr(TargetSessionTests, _name, None)

if __name__ == "__main__":
    unittest.main()
