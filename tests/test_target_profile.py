import unittest
from datetime import timedelta
from unittest import mock

import numpy as np

import tests  # noqa: F401
from backend.app import config, people, target_profile
from backend.app.db import (SessionLocal, Participant, EmbeddingRef, Detection, TargetMove,
                            init_db, utcnow)

META = {"entre": {"label": "Entrée"}, "salle": {"label": "Salle"}}


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


class ProfileBase(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        for model in (Detection, TargetMove, EmbeddingRef, Participant):
            self.db.query(model).delete()
        self.db.commit()
        self.addCleanup(self.db.close)

    def enroll(self, nom, *vectors, infos="", sources=None):
        p = Participant(nom=nom, infos=infos)
        self.db.add(p)
        self.db.flush()
        sources = sources or [f"{nom}{i}.jpg" for i in range(len(vectors))]
        for v, source in zip(vectors, sources):
            self.db.add(EmbeddingRef(participant_id=p.id, embedding=unit(*v).tobytes(), source=source))
        self.db.commit()
        return p.id

    def see(self, person_id, cam, seconds_ago, track, n=3, status="probable"):
        """Détections d'une personne enrôlée, reliées à sa fiche par l'identifiant (comme le fait la caméra)."""
        person = self.db.get(Participant, person_id)
        for i in range(n):
            self.db.add(Detection(camera_id=cam, track_id=track, mode="DB", label=person.display_name,
                                  participant_id=person_id, match_status=status, db_score=.6,
                                  timestamp=utcnow() - timedelta(seconds=seconds_ago - i)))
        self.db.commit()


class MatchTests(ProfileBase):
    def test_best_person_above_threshold_is_matched(self):
        awa = self.enroll("Awa", (1, 0, 0), (0.9, 0.1, 0))
        self.enroll("Moussa", (0, 1, 0))
        m = target_profile.find_match(self.db, unit(1, 0.05, 0), threshold=0.5)
        self.assertTrue(m["matched"])
        self.assertEqual(m["participant_id"], awa)
        self.assertEqual(m["enrolled_count"], 2)
        self.assertEqual(m["others"], [])

    def test_below_threshold_is_not_matched_and_reports_best_score(self):
        self.enroll("Awa", (1, 0, 0))
        m = target_profile.find_match(self.db, unit(0, 1, 0), threshold=0.5)
        self.assertFalse(m["matched"])
        self.assertIsNone(m["participant_id"])
        self.assertAlmostEqual(m["best_score"], 0.0, places=3)

    def test_empty_database(self):
        m = target_profile.find_match(self.db, unit(1, 0, 0), threshold=0.5)
        self.assertEqual((m["matched"], m["enrolled_count"], m["best_score"]), (False, 0, None))

    def test_close_candidates_are_reported(self):
        self.enroll("Awa", (1, 0, 0))
        self.enroll("Aïssa", (0.95, 0.3, 0))
        self.enroll("Moussa", (0, 1, 0))
        m = target_profile.find_match(self.db, unit(1, 0.1, 0), threshold=0.5)
        self.assertEqual([o["nom"] for o in m["others"]], ["Aïssa"])

    def test_embeddings_of_another_size_are_ignored(self):
        self.enroll("Ancien", (1, 0))                       # dimension différente
        m = target_profile.find_match(self.db, unit(1, 0, 0), threshold=0.1)
        self.assertEqual(m["enrolled_count"], 0)

    def test_default_threshold_is_the_configured_one(self):
        self.assertEqual(target_profile.find_match(self.db, unit(1, 0, 0))["threshold"], config.PROFILE_THRESHOLD)


class ActivityTests(ProfileBase):
    def test_activity_is_summarised_per_camera(self):
        awa = self.enroll("Awa", (1, 0, 0))
        self.see(awa, "entre", 600, track=1, n=5)
        self.see(awa, "salle", 100, track=2, n=3)
        self.see(awa, "salle", 20, track=3, n=2, status="a_verifier")
        a = people.person_activity(self.db, awa, META)
        self.assertEqual((a["passages"], a["observations"]), (3, 10))
        self.assertEqual((a["probable_passages"], a["to_verify_passages"]), (2, 1))
        self.assertEqual((a["last_camera"], a["last_camera_label"]), ("salle", "Salle"))
        self.assertEqual([c["camera_id"] for c in a["by_camera"]], ["salle", "entre"])   # plus récent d'abord
        self.assertEqual(a["by_camera"][0]["passages"], 2)
        self.assertTrue(a["first_seen"].endswith("Z") and a["last_seen"].endswith("Z"))

    def test_two_people_with_the_same_name_are_no_longer_confused(self):
        first = self.enroll("Awa Ba", (1, 0, 0))
        second = self.enroll("Awa Ba", (0, 1, 0))
        self.see(first, "entre", 100, track=1, n=4)
        self.see(second, "salle", 100, track=2, n=2)
        self.assertEqual(people.person_activity(self.db, first, META)["observations"], 4)
        self.assertEqual(people.person_activity(self.db, second, META)["observations"], 2)

    def test_a_renamed_person_keeps_their_history(self):
        awa = self.enroll("Awa", (1, 0, 0))
        self.see(awa, "entre", 100, track=1)
        self.db.get(Participant, awa).prenom = "Awa-Marie"
        self.db.commit()
        self.assertEqual(people.person_activity(self.db, awa, META)["passages"], 1)

    def test_no_detection(self):
        awa = self.enroll("Awa", (1, 0, 0))
        a = people.person_activity(self.db, awa, META)
        self.assertEqual((a["passages"], a["last_seen"], a["by_camera"], a["zones"]), (0, None, [], []))


class RenderTests(ProfileBase):
    def match_for(self, vec):
        return target_profile.find_match(self.db, unit(*vec), threshold=0.5)

    def test_full_profile(self):
        pid = self.enroll("Awa", (1, 0, 0), (0.9, 0.1, 0), infos="Équipe A\nbadge 12", sources=["a.jpg", "b.jpg"])
        self.see(pid, "entre", 100, track=1)
        p = target_profile.render_profile(self.db, self.match_for((1, 0, 0)), META)
        self.assertTrue(p["matched"])
        self.assertFalse(p["hidden"])
        self.assertEqual(p["participant"]["id"], pid)
        self.assertEqual((p["participant"]["nom"], p["participant"]["infos"]), ("Awa", "Équipe A\nbadge 12"))
        self.assertTrue(p["participant"]["date_consentement"].endswith("Z"))
        self.assertEqual((p["participant"]["actif"], p["participant"]["matricule"]), (True, ""))
        self.assertTrue(p["participant"]["date_enrolement"].endswith("Z"))
        self.assertEqual(p["enrolment"], {"embeddings": 2, "sources": ["a.jpg", "b.jpg"]})
        self.assertEqual(p["activity"]["passages"], 1)
        self.assertAlmostEqual(p["score"], 1.0, places=2)

    def test_profile_never_contains_the_embedding_itself(self):
        self.enroll("Awa", (1, 0, 0))
        p = target_profile.render_profile(self.db, self.match_for((1, 0, 0)), META)
        self.assertNotIn("embedding", repr(p).replace("embeddings", ""))

    def test_hidden_identities(self):
        self.enroll("Awa", (1, 0, 0), infos="secret")
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            p = target_profile.render_profile(self.db, self.match_for((1, 0, 0)), META)
        self.assertEqual((p["matched"], p["hidden"]), (True, True))
        self.assertNotIn("Awa", repr(p))
        self.assertNotIn("secret", repr(p))

    def test_person_deleted_after_the_search(self):
        pid = self.enroll("Awa", (1, 0, 0))
        match = self.match_for((1, 0, 0))
        self.db.query(EmbeddingRef).delete()
        self.db.query(Participant).filter(Participant.id == pid).delete()
        self.db.commit()
        p = target_profile.render_profile(self.db, match, META)
        self.assertEqual((p["matched"], p["reason"]), (False, "deleted"))

    def test_not_enrolled_and_no_target(self):
        self.enroll("Awa", (1, 0, 0))
        p = target_profile.render_profile(self.db, self.match_for((0, 1, 0)), META)
        self.assertEqual((p["matched"], p["reason"], p["enrolled_count"]), (False, "not_enrolled", 1))
        self.assertNotIn("Awa", repr(p))                    # une non-correspondance ne révèle aucun nom
        self.assertEqual(target_profile.render_profile(self.db, None, META)["reason"], "no_target")

    def test_other_candidates_are_listed_in_the_profile(self):
        self.enroll("Awa", (1, 0, 0)); self.enroll("Aïssa", (0.95, 0.3, 0))
        p = target_profile.render_profile(self.db, self.match_for((1, 0.1, 0)), META)
        self.assertEqual([o["nom"] for o in p["other_candidates"]], ["Aïssa"])


if __name__ == "__main__":
    unittest.main()
