import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import numpy as np
from sqlalchemy import create_engine, inspect, text

import tests  # noqa: F401
from backend.app import config, history, people
from backend.app import db as dbmod
from backend.app.db import (Detection, EmbeddingRef, Event, Participant, SessionLocal, delete_participant, init_db,
                            utcnow)

META = {"entre": {"label": "Entrée", "location": "Hall"}, "salle": {"label": "Salle", "location": "Hall"},
        "sortie": {"label": "Sortie", "location": ""}}


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


class PeopleBase(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        for model in (Detection, EmbeddingRef, Event, Participant):
            self.db.query(model).delete()
        self.db.commit()
        self.addCleanup(self.db.close)

    def add(self, nom="Awa", prenom=None, vec=(1, 0, 0), **extra):
        fields = people.clean_person_input({"nom": nom, "prenom": prenom or "", **extra}, creating=True)
        return people.create_person(self.db, fields, [(unit(*vec), f"{nom}.jpg")])

    def see(self, person, cam="entre", ago=100, track=1, n=3, status="probable"):
        for i in range(n):
            self.db.add(Detection(camera_id=cam, track_id=track, mode="DB", label=person.display_name,
                                  participant_id=person.id, match_status=status, db_score=.6,
                                  timestamp=utcnow() - timedelta(seconds=ago - i)))
        self.db.commit()


class ValidationTests(unittest.TestCase):
    def test_creation_needs_a_name(self):
        for data in ({}, {"nom": ""}, {"nom": "   "}):
            with self.subTest(data=data), self.assertRaises(people.PersonError) as ctx:
                people.clean_person_input(data, creating=True)
            self.assertEqual(ctx.exception.field, "nom")

    def test_values_are_normalised(self):
        out = people.clean_person_input({"nom": "  Ba ", "prenom": " Awa ", "matricule": "  ", "infos": " équipe\nA ",
                                         "actif": False}, creating=True)
        self.assertEqual(out, {"nom": "Ba", "prenom": "Awa", "matricule": None, "infos": "équipe\nA", "actif": False})

    def test_partial_update_only_returns_the_given_fields(self):
        self.assertEqual(people.clean_person_input({"prenom": "Awa"}, creating=False), {"prenom": "Awa"})
        self.assertEqual(people.clean_person_input({"prenom": ""}, creating=False), {"prenom": None})
        self.assertEqual(people.clean_person_input({}, creating=False), {})

    def test_limits_and_forbidden_characters(self):
        bad = [{"nom": "x" * 101}, {"nom": "A", "prenom": "x" * 61}, {"nom": "A", "matricule": "x" * 41},
               {"nom": "A", "infos": "x" * 501}, {"nom": "Aw\na"}, {"nom": "A", "prenom": "Aw\x00a"},
               {"nom": "A", "matricule": "12\t3"}, {"nom": "A", "infos": "a\x07b"}, {"nom": 5}, {"nom": "A", "actif": "oui"},
               {"nom": "A", "actif": 1}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(people.PersonError):
                people.clean_person_input(data, creating=True)

    def test_a_name_cannot_be_blanked_by_an_update(self):
        with self.assertRaises(people.PersonError):
            people.clean_person_input({"nom": " "}, creating=False)


class CrudTests(PeopleBase):
    def test_create(self):
        p = self.add("Ba", "Awa", matricule="E-042", infos="Équipe A")
        self.assertEqual((p.display_name, p.matricule, p.actif), ("Awa Ba", "E-042", True))
        self.assertIsNotNone(p.date_enrolement); self.assertIsNotNone(p.date_consentement)
        self.assertEqual(people.reference_count(self.db, p.id), 1)

    def test_display_name_without_first_name(self):
        self.assertEqual(self.add("Awa Ba").display_name, "Awa Ba")           # fiches créées avant la 2.0

    def test_creation_without_a_usable_photo_is_refused(self):
        with self.assertRaises(people.PersonError) as ctx:
            people.create_person(self.db, people.clean_person_input({"nom": "A"}, creating=True), [])
        self.assertEqual(ctx.exception.field, "photos")
        self.assertEqual(self.db.query(Participant).count(), 0)

    def test_matricule_is_unique_whatever_the_case_and_never_names_the_owner(self):
        first = self.add("Ba", matricule="E-042")
        with self.assertRaises(people.PersonConflict) as ctx:
            self.add("Sy", matricule="e-042", vec=(0, 1, 0))
        self.assertEqual((ctx.exception.status, ctx.exception.field), (409, "matricule"))
        self.assertIn(str(first.id), ctx.exception.message)
        self.assertNotIn("Ba", ctx.exception.message)
        self.assertEqual(self.db.query(Participant).count(), 1)

    def test_people_without_matricule_do_not_conflict(self):
        self.add("A"); self.add("B", vec=(0, 1, 0))
        self.assertEqual(self.db.query(Participant).count(), 2)

    def test_update(self):
        p = self.add("Ba", "Awa")
        before = p.updated_at
        out = people.update_person(self.db, p.id, {"prenom": "Awa-Marie", "actif": False, "matricule": "X1"})
        self.assertEqual((out.display_name, out.actif, out.matricule), ("Awa-Marie Ba", False, "X1"))
        self.assertGreaterEqual(out.updated_at, before)

    def test_update_matricule_conflict_and_own_matricule(self):
        a = self.add("A", matricule="M1"); self.add("B", matricule="M2", vec=(0, 1, 0))
        with self.assertRaises(people.PersonConflict):
            people.update_person(self.db, a.id, {"matricule": "m2"})
        people.update_person(self.db, a.id, {"matricule": "M1", "infos": "ok"})      # le sien ne compte pas

    def test_update_unknown_person(self):
        with self.assertRaises(people.PersonNotFound):
            people.update_person(self.db, 999, {"infos": "x"})

    def test_references_can_be_added_and_removed_but_never_down_to_zero(self):
        p = self.add("A")
        self.assertEqual(people.add_references(self.db, p.id, [(unit(1, .1, 0), "b.jpg"), (unit(1, 0, .1), "c.jpg")]), 2)
        refs = self.db.query(EmbeddingRef).filter_by(participant_id=p.id).order_by(EmbeddingRef.id).all()
        people.delete_reference(self.db, p.id, refs[1].id)
        people.delete_reference(self.db, p.id, refs[2].id)
        with self.assertRaises(people.PersonConflict):
            people.delete_reference(self.db, p.id, refs[0].id)
        self.assertEqual(people.reference_count(self.db, p.id), 1)

    def test_a_reference_of_another_person_cannot_be_deleted(self):
        a = self.add("A"); b = self.add("B", vec=(0, 1, 0))
        ref_b = self.db.query(EmbeddingRef).filter_by(participant_id=b.id).one()
        people.add_references(self.db, a.id, [(unit(1, 1, 0), "x.jpg")])
        with self.assertRaises(people.PersonError) as ctx:
            people.delete_reference(self.db, a.id, ref_b.id)
        self.assertEqual(ctx.exception.status, 404)

    def test_reference_limit(self):
        p = self.add("A")
        with mock.patch.object(config, "MAX_REFERENCES", 2):
            people.add_references(self.db, p.id, [(unit(1, 0, .1), "b.jpg")])
            with self.assertRaises(people.PersonConflict):
                people.add_references(self.db, p.id, [(unit(1, .1, 0), "c.jpg")])

    def test_adding_nothing_is_refused(self):
        with self.assertRaises(people.PersonError):
            people.add_references(self.db, self.add("A").id, [])


class DuplicateTests(PeopleBase):
    def test_a_similar_face_is_reported(self):
        self.add("Awa", vec=(1, 0, 0))
        dup = people.find_duplicate(self.db, [unit(1, .1, 0)])
        self.assertEqual(dup["nom"], "Awa")
        self.assertGreater(dup["score"], .9)

    def test_a_different_face_is_not(self):
        self.add("Awa", vec=(1, 0, 0))
        self.assertIsNone(people.find_duplicate(self.db, [unit(0, 1, 0)]))

    def test_the_person_being_edited_is_excluded(self):
        p = self.add("Awa", vec=(1, 0, 0))
        self.assertIsNone(people.find_duplicate(self.db, [unit(1, 0, 0)], exclude_id=p.id))

    def test_nothing_to_compare(self):
        self.assertIsNone(people.find_duplicate(self.db, []))


class ListTests(PeopleBase):
    def setUp(self):
        super().setUp()
        self.awa = self.add("Ba", "Awa", matricule="E-001", vec=(1, 0, 0))
        self.moussa = self.add("Sy", "Moussa", matricule="E-002", vec=(0, 1, 0), actif=False)
        self.add("Diallo", None, vec=(0, 0, 1))

    def names(self, **kw):
        return [i["display_name"] for i in people.list_people(self.db, **kw)["items"]]

    def test_sorted_by_name(self):
        self.assertEqual(self.names(), ["Awa Ba", "Diallo", "Moussa Sy"])           # trié par nom de famille

    def test_search_on_every_field_whatever_the_case(self):
        for q, expected in (("awa", ["Awa Ba"]), ("SY", ["Moussa Sy"]), ("e-002", ["Moussa Sy"]), ("moussa sy", ["Moussa Sy"]),
                            ("awa ba", ["Awa Ba"]), ("zzz", [])):
            with self.subTest(q=q):
                self.assertEqual(self.names(q=q), expected)

    def test_wildcards_are_literal(self):
        self.assertEqual(self.names(q="%"), [])
        self.assertEqual(self.names(q="_wa"), [])

    def test_status_filter(self):
        self.assertEqual(self.names(statut="inactif"), ["Moussa Sy"])
        self.assertEqual(self.names(statut="actif"), ["Awa Ba", "Diallo"])

    def test_pagination(self):
        page = people.list_people(self.db, page=2, page_size=2)
        self.assertEqual((page["total"], [i["display_name"] for i in page["items"]]), (3, ["Moussa Sy"]))

    def test_summary_columns(self):
        self.see(self.awa, "salle", ago=50, n=4)
        self.see(self.awa, "entre", ago=10, track=2, n=2)
        item = [i for i in people.list_people(self.db)["items"] if i["id"] == self.awa.id][0]
        self.assertEqual((item["references"], item["detections"], item["last_camera"]), (1, 6, "entre"))
        self.assertTrue(item["last_seen"].endswith("Z"))
        quiet = [i for i in people.list_people(self.db)["items"] if i["id"] == self.moussa.id][0]
        self.assertEqual((quiet["detections"], quiet["last_seen"], quiet["last_camera"]), (0, None, None))

    def test_the_list_never_contains_free_text_or_embeddings(self):
        item = people.list_people(self.db)["items"][0]
        self.assertNotIn("infos", item)
        self.assertNotIn("embedding", json.dumps(item))


class DetailTests(PeopleBase):
    def test_full_card(self):
        p = self.add("Ba", "Awa", matricule="E-001", infos="Équipe A")
        people.add_references(self.db, p.id, [(unit(1, .1, 0), "profil.jpg")])
        self.see(p, "entre", ago=300, track=1, n=5)
        self.see(p, "salle", ago=100, track=2, n=3, status="a_verifier")
        self.see(p, "sortie", ago=30, track=3, n=2)
        d = people.person_detail(self.db, p.id, META)
        self.assertEqual((d["display_name"], d["matricule"], d["infos"], d["actif"]), ("Awa Ba", "E-001", "Équipe A", True))
        self.assertEqual([r["source"] for r in d["references"]], ["Ba.jpg", "profil.jpg"])
        a = d["activity"]
        self.assertEqual((a["passages"], a["probable_passages"], a["to_verify_passages"]), (3, 2, 1))
        self.assertEqual({z["zone"]: z["passages"] for z in a["zones"]}, {"Hall": 2, "Sortie": 1})   # zone = lieu, sinon nom de la caméra
        self.assertEqual([i["camera_id"] for i in d["timeline"]["items"]], ["sortie", "salle", "entre"])
        self.assertEqual([i["camera_label"] for i in d["timeline"]["items"]], ["Sortie", "Salle", "Entrée"])
        self.assertEqual(d["timeline"]["items"][1]["match_status"], "a_verifier")

    def test_nothing_secret_in_the_card(self):
        p = self.add("Ba")
        self.assertNotIn("embedding", json.dumps(people.person_detail(self.db, p.id, META)))

    def test_events_of_this_person_only(self):
        a, b = self.add("A"), self.add("B", vec=(0, 1, 0))
        for pid, title in ((a.id, "pour A"), (b.id, "pour B"), (a.id, "encore A")):
            self.db.add(Event(type="person_recognized", severity="info", camera_id="entre", title=title,
                              payload=json.dumps({"person": "x", "participant_id": pid, "status": "probable"})))
        self.db.add(Event(type="target_seen", severity="info", title="autre type",
                          payload=json.dumps({"participant_id": a.id})))
        self.db.commit()
        titles = [e["title"] for e in people.person_detail(self.db, a.id, META)["events"]]
        self.assertEqual(titles, ["encore A", "pour A"])

    def test_unknown_person(self):
        with self.assertRaises(people.PersonNotFound):
            people.person_detail(self.db, 999, META)

    def test_no_activity(self):
        a = people.person_detail(self.db, self.add("A").id, META)["activity"]
        self.assertEqual((a["passages"], a["zones"], a["last_seen"]), (0, [], None))


class HistoryLinkTests(PeopleBase):
    def window(self):
        return utcnow() - timedelta(hours=1), utcnow() + timedelta(seconds=1)

    def test_history_shows_the_current_name_after_a_rename(self):
        p = self.add("Awa")
        self.see(p, ago=100)
        people.update_person(self.db, p.id, {"prenom": "Awa-Marie", "nom": "Ba"})
        item = history.read_episodes(self.db, *self.window())["items"][0]
        self.assertEqual((item["label"], item["participant_id"], item["match_status"]), ("Awa-Marie Ba", p.id, "probable"))

    def test_filter_by_person_separates_homonyms(self):
        a = self.add("Awa Ba"); b = self.add("Awa Ba", vec=(0, 1, 0))
        self.see(a, "entre", track=1); self.see(b, "salle", track=2)
        only_a = history.read_episodes(self.db, *self.window(), participant_id=a.id)
        self.assertEqual([i["camera_id"] for i in only_a["items"]], ["entre"])
        self.assertEqual(history.read_episodes(self.db, *self.window())["total"], 2)

    def test_old_unlinked_rows_keep_their_stored_label(self):
        self.db.add(Detection(camera_id="entre", track_id=1, mode="DB", label="Ancien nom", db_score=.5,
                              timestamp=utcnow() - timedelta(seconds=30)))
        self.db.commit()
        item = history.read_episodes(self.db, *self.window())["items"][0]
        self.assertEqual((item["label"], item["participant_id"]), ("Ancien nom", None))

    def test_identifier_is_not_published_when_identities_are_hidden(self):
        p = self.add("Awa")
        self.see(p)
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            item = history.read_episodes(self.db, *self.window())["items"][0]
            csv_text = "".join(history.iter_csv(self.db, *self.window()))
        self.assertEqual((item["label"], item["participant_id"]), ("Personne enrôlée", None))
        self.assertNotIn("Awa", csv_text)

    def test_csv_has_person_id_and_match_status(self):
        p = self.add("Awa")
        self.see(p, status="a_verifier")
        rows = "".join(history.iter_csv(self.db, *self.window())).lstrip("\ufeff").splitlines()
        self.assertTrue(rows[0].endswith("id_personne;correspondance"))
        self.assertTrue(rows[1].endswith(f";{p.id};à vérifier"))

    def test_summary_counts_people_by_identifier(self):
        a = self.add("Awa Ba"); b = self.add("Awa Ba", vec=(0, 1, 0))
        self.see(a, track=1); self.see(b, track=2)
        self.assertEqual(history.read_summary(self.db, *self.window())["enrolled_people"], 2)


class DeleteTests(PeopleBase):
    def test_everything_about_the_person_is_erased(self):
        a, b = self.add("Awa Ba"), self.add("Moussa Sy", vec=(0, 1, 0))
        self.see(a, track=1); self.see(b, track=2)
        for person, title in ((a, "Personne reconnue : Awa Ba — Entrée"), (b, "Personne reconnue : Moussa Sy — Entrée")):
            self.db.add(Event(type="person_recognized", severity="info", camera_id="entre", title=title,
                              payload=json.dumps({"person": person.display_name, "participant_id": person.id, "score": .6},
                                                 ensure_ascii=False)))
        self.db.commit()
        result = delete_participant(self.db, a.id)
        self.assertEqual(result, {"embeddings_deleted": 1, "detections_anonymised": 3, "events_anonymised": 1})
        self.assertIsNone(self.db.get(Participant, a.id))
        rows = self.db.query(Detection).order_by(Detection.id).all()
        self.assertEqual([(r.mode, r.label, r.participant_id) for r in rows[:3]], [("UNK", None, None)] * 3)
        self.assertEqual(rows[3].label, "Moussa Sy")                              # l'autre personne n'est pas touchée
        everything = " ".join(f"{e.title} {e.payload}" for e in self.db.query(Event))
        self.assertNotIn("Awa", everything)
        self.assertIn("Moussa Sy", everything)

    def test_homonyms_are_not_wiped_by_name(self):
        a, b = self.add("Awa Ba"), self.add("Awa Ba", vec=(0, 1, 0))
        self.see(a, track=1)
        self.see(b, track=2)
        # ligne ancienne jamais rattachée : son nom appartient aussi à l'autre personne -> intouchée
        self.db.add(Detection(camera_id="entre", track_id=9, mode="DB", label="Awa Ba", timestamp=utcnow()))
        self.db.commit()
        delete_participant(self.db, a.id)
        remaining = self.db.query(Detection).filter(Detection.mode == "DB").all()
        self.assertEqual(sum(1 for r in remaining if r.participant_id == b.id), 3)
        self.assertEqual(sum(1 for r in remaining if r.participant_id is None), 1)        # la ligne ancienne

    def test_unlinked_old_rows_are_wiped_when_the_name_is_unique(self):
        a = self.add("Awa Ba")
        self.db.add(Detection(camera_id="entre", track_id=9, mode="DB", label="Awa Ba", timestamp=utcnow()))
        self.db.commit()
        self.assertEqual(delete_participant(self.db, a.id)["detections_anonymised"], 1)
        self.assertIsNone(self.db.query(Detection).one().label)

    def test_unknown_person(self):
        self.assertIsNone(delete_participant(self.db, 12345))


class MigrationTests(unittest.TestCase):
    """Une base créée avant NiifYéén 2.0 doit être mise à niveau sans perdre de données."""

    def legacy_engine(self):
        path = os.path.join(tempfile.mkdtemp(), "legacy.db")
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as c:
            c.execute(text("CREATE TABLE participants (id INTEGER PRIMARY KEY, nom VARCHAR NOT NULL, infos TEXT, date_consentement DATETIME)"))
            c.execute(text("CREATE TABLE embeddings_reference (id INTEGER PRIMARY KEY, participant_id INTEGER NOT NULL, embedding BLOB NOT NULL, source VARCHAR)"))
            c.execute(text("CREATE TABLE detections (id INTEGER PRIMARY KEY, camera_id VARCHAR NOT NULL, timestamp DATETIME, track_id INTEGER, "
                           "global_id INTEGER, mode VARCHAR NOT NULL, label VARCHAR, target_score FLOAT, db_score FLOAT, x FLOAT, y FLOAT)"))
            c.execute(text("INSERT INTO participants (id, nom, infos, date_consentement) VALUES (1, 'Awa Ba', 'équipe A', '2026-10-30 12:00:00'),"
                           "(2, 'Moussa Sy', '', '2026-10-30 12:05:00'), (3, 'Awa Diop', '', '2026-10-30 12:06:00'), (4, 'Awa Diop', '', '2026-10-30 12:07:00')"))
            c.execute(text("INSERT INTO embeddings_reference (participant_id, embedding, source) VALUES (1, x'00', 'a.jpg')"))
            rows = [("entre", "DB", "Awa Ba"), ("entre", "DB", "Awa Ba"), ("salle", "DB", "Moussa Sy"),
                    ("salle", "DB", "Awa Diop"), ("entre", "DB", "Inconnu supprimé"), ("entre", "UNK", None), ("entre", "TARGET", "CIBLE")]
            for cam, mode, label in rows:
                c.execute(text("INSERT INTO detections (camera_id, timestamp, track_id, mode, label) VALUES (:c, '2026-10-30 12:10:00', 1, :m, :l)"),
                          {"c": cam, "m": mode, "l": label})
        self.addCleanup(engine.dispose)
        return engine

    def test_old_database_is_upgraded_without_losing_anything(self):
        engine = self.legacy_engine()
        with mock.patch.object(dbmod, "engine", engine):
            init_db()
        with engine.connect() as c:
            cols = {r["name"] for r in inspect(c).get_columns("participants")}
            self.assertTrue({"prenom", "matricule", "actif", "date_enrolement", "updated_at"} <= cols)
            self.assertTrue({"participant_id", "match_status"} <= {r["name"] for r in inspect(c).get_columns("detections")})
            rows = c.execute(text("SELECT id, nom, infos, actif, date_enrolement, date_consentement, prenom, matricule FROM participants ORDER BY id")).fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual([r.actif for r in rows], [1, 1, 1, 1])                      # actives par défaut
            self.assertEqual(rows[0].date_enrolement, rows[0].date_consentement)         # seule date connue
            self.assertEqual((rows[0].nom, rows[0].infos, rows[0].prenom, rows[0].matricule), ("Awa Ba", "équipe A", None, None))
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM embeddings_reference")).scalar(), 1)
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM detections")).scalar(), 7)

    def test_old_detections_are_linked_by_name_only_when_unambiguous(self):
        engine = self.legacy_engine()
        with mock.patch.object(dbmod, "engine", engine):
            init_db()
        with engine.connect() as c:
            linked = {r.label: r.participant_id for r in c.execute(text("SELECT label, participant_id FROM detections WHERE mode = 'DB'"))}
        self.assertEqual(linked["Awa Ba"], 1)
        self.assertEqual(linked["Moussa Sy"], 2)
        self.assertIsNone(linked["Awa Diop"])                  # deux personnes de ce nom : on ne devine pas
        self.assertIsNone(linked["Inconnu supprimé"])          # plus aucune fiche de ce nom
        with engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM detections WHERE mode != 'DB' AND participant_id IS NOT NULL")).scalar(), 0)

    def test_the_backfill_runs_only_once(self):
        engine = self.legacy_engine()
        with mock.patch.object(dbmod, "engine", engine):
            init_db()
            with engine.begin() as c:
                c.execute(text("INSERT INTO detections (camera_id, timestamp, track_id, mode, label) VALUES ('entre', '2026-10-30 13:00:00', 2, 'DB', 'Moussa Sy')"))
            init_db(); init_db()
        with engine.connect() as c:
            self.assertEqual(c.execute(text("SELECT COUNT(*) FROM detections WHERE participant_id IS NULL AND label = 'Moussa Sy'")).scalar(), 1)

    def test_matricule_uniqueness_is_enforced_by_the_database(self):
        engine = self.legacy_engine()
        with mock.patch.object(dbmod, "engine", engine):
            init_db()
        with engine.begin() as c:
            c.execute(text("UPDATE participants SET matricule = 'E-1' WHERE id = 1"))
            c.execute(text("UPDATE participants SET matricule = NULL WHERE id IN (2, 3)"))       # plusieurs NULL : autorisé
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError), engine.begin() as c:
            c.execute(text("UPDATE participants SET matricule = 'e-1' WHERE id = 2"))


if __name__ == "__main__":
    unittest.main()
