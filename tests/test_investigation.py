import csv
import io
import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401
from backend.app import alerts, config, events, history, investigation as inv
from backend.app.db import (Alert, AlertAction, Detection, Dossier, DossierItem, Event, Participant, SessionLocal,
                            TargetMove, delete_participant, init_db, purge_old_detections, utcnow)


def reset():
    init_db()
    s = SessionLocal()
    for m in (DossierItem, Dossier, AlertAction, Alert, Event, Detection, TargetMove, Participant):
        s.query(m).delete()
    s.commit(); s.close()


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.feed = events.EventFeed()
        self.now = utcnow()
        self.db = SessionLocal()
        self.addCleanup(self.db.close)

    def emit(self, type_, title, cam="c1", minutes_ago=5, **payload):
        return self.feed.emit(type_, title, camera_id=cam, timestamp=self.now - timedelta(minutes=minutes_ago), **payload)

    def run_tl(self, **kw):
        since = kw.pop("since", self.now - timedelta(hours=1))
        until = kw.pop("until", self.now + timedelta(minutes=1))
        return inv.timeline(self.db, since, until, **kw)

    def detect(self, cam="c1", track=1, minutes_ago=10, n=3, mode="UNK", label=None, pid=None):
        for k in range(n):
            self.db.add(Detection(camera_id=cam, track_id=track, mode=mode, label=label, participant_id=pid,
                                  timestamp=self.now - timedelta(minutes=minutes_ago, seconds=-k)))
        self.db.commit()


class TypesTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(inv.parse_types(""), [])
        self.assertEqual(inv.parse_types("camera_offline, passage"), ["camera_offline", "passage"])
        self.assertIn("zone_intrusion", inv.parse_types("analytics"))
        self.assertEqual(inv.parse_types("passage,passage"), ["passage"])
        with self.assertRaises(inv.InvestigationError):
            inv.parse_types("pas_un_type")


class TimelineTests(Base):
    def test_merges_sources_newest_first(self):
        self.emit("camera_offline", "Hors ligne", minutes_ago=5)
        self.detect(minutes_ago=10)
        r = self.run_tl()
        self.assertEqual([i["kind"] for i in r["items"]], ["event", "passage"])
        self.assertEqual(r["counts"], {"camera_offline": 1, "passage": 1})
        self.assertFalse(r["truncated"])
        self.assertNotIn("_t", r["items"][0])

    def test_filters_combine(self):
        self.emit("camera_offline", "Hors ligne c1", cam="c1")
        self.emit("camera_offline", "Hors ligne c2", cam="c2")
        self.emit("zone_intrusion", "Intrusion c2", cam="c2", zone_id=3)
        self.detect(cam="c2")
        self.assertEqual(len(self.run_tl(camera_id="c2")["items"]), 3)
        self.assertEqual([i["type"] for i in self.run_tl(camera_id="c2", types=["zone_intrusion"])["items"]], ["zone_intrusion"])
        self.assertEqual([i["type"] for i in self.run_tl(types=["passage"])["items"]], ["passage"])
        self.assertEqual(len(self.run_tl(types=["camera_offline"])["items"]), 2)
        self.assertEqual([i["title"] for i in self.run_tl(q="intrusion", types=["analytics"] and inv.parse_types("analytics"))["items"]], ["Intrusion c2"])
        self.assertEqual(self.run_tl(types=["move"])["items"], [])

    def test_text_search_is_literal(self):
        self.emit("camera_offline", "Hall 100%")
        self.emit("camera_offline", "Hall 100 pour cent")
        self.assertEqual(len(self.run_tl(q="100%", types=["camera_offline"])["items"]), 1)
        self.assertEqual(self.run_tl(q="%'; DROP TABLE events; --", types=["camera_offline"])["items"], [])
        self.assertEqual(len(self.run_tl(types=["camera_offline"])["items"]), 2)

    def test_time_window(self):
        self.emit("camera_offline", "récent", minutes_ago=5)
        self.emit("camera_offline", "ancien", minutes_ago=120)
        self.assertEqual([i["title"] for i in self.run_tl(types=["camera_offline"])["items"]], ["récent"])
        wide = self.run_tl(since=self.now - timedelta(hours=3), types=["camera_offline"])
        self.assertEqual([i["title"] for i in wide["items"]], ["récent", "ancien"])

    def test_person_filter_events_and_passages(self):
        a = Participant(nom="Awa"); b = Participant(nom="Moussa"); self.db.add_all([a, b]); self.db.commit()
        self.emit("person_recognized", "Awa vue", participant_id=a.id)
        self.emit("person_recognized", "Moussa vu", participant_id=b.id, person="Moussa")
        self.emit("person_recognized", "Awa vue bis", minutes_ago=4, person="Awa", participant_id=a.id)
        self.detect(mode="DB", label="Awa", pid=a.id, track=1)
        self.detect(mode="DB", label="Moussa", pid=b.id, track=2)
        r = self.run_tl(participant_id=a.id)
        self.assertEqual(sorted(i["kind"] for i in r["items"]), ["event", "event", "passage"])
        self.assertTrue(all(i["participant_id"] == a.id for i in r["items"]))

    def test_person_filter_refused_when_identities_hidden(self):
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            with self.assertRaises(inv.InvestigationError) as ctx:
                self.run_tl(participant_id=1)
            self.assertEqual(ctx.exception.status, 403)
            self.emit("person_recognized", "x", participant_id=7, person="Awa")
            self.assertIsNone(self.run_tl(types=["person_recognized"])["items"][0]["participant_id"])

    def test_target_filter(self):
        self.emit("target_seen", "Cible 1", target_id=1, target_label="Cible 1")
        self.emit("target_seen", "Cible 2", target_id=2)
        self.emit("target_seen", "Cible 12", target_id=12)
        self.assertEqual([i["title"] for i in self.run_tl(target_id=1)["items"]], ["Cible 1"])
        self.assertEqual([i["title"] for i in self.run_tl(target_id=12)["items"]], ["Cible 12"])

    def test_only_alerts_and_alert_link(self):
        center = alerts.AlertCenter(self.feed, lambda: {}); center.start(); self.addCleanup(center.stop)
        self.feed.emit("camera_offline", "Hors ligne", camera_id="c1")                  # déclenche une alerte
        self.feed.emit("target_lost", "Perdue", camera_id="c1", target_id=1)            # alerte aussi (règle active)
        self.feed.emit("line_crossed", "Ligne", camera_id="c1", zone_id=1)              # règle désactivée : pas d'alerte
        everything = self.run_tl(types=["camera_offline", "line_crossed"])["items"]
        self.assertEqual({i["type"]: bool(i["alert"]) for i in everything}, {"camera_offline": True, "line_crossed": False})
        only = self.run_tl(only_alerts=True)["items"]
        self.assertEqual({i["type"] for i in only}, {"camera_offline", "target_lost"})
        self.assertEqual(only[0]["alert"]["status"], "new")

    def test_session_filter(self):
        t0 = self.now - timedelta(minutes=20)
        for k, cam in enumerate(["c1", "c2", "c1"]):
            self.db.add(TargetMove(session=5, camera_id=cam, timestamp=t0 + timedelta(minutes=k), label="Cible 1"))
        self.db.commit()
        self.emit("target_moved", "Cible 1 : c1 -> c2", minutes_ago=19)
        self.emit("camera_offline", "Sans rapport", minutes_ago=19)
        self.emit("target_seen", "Hors session", minutes_ago=2)
        self.detect(mode="TARGET", label="Cible 1", minutes_ago=19, n=2)
        self.detect(mode="DB", label="Awa", minutes_ago=19, n=2, track=9)
        r = self.run_tl(session=5)
        kinds = sorted((i["kind"], i["type"]) for i in r["items"])
        self.assertEqual(kinds, [("event", "target_moved"), ("move", "move"), ("move", "move"), ("move", "move"), ("passage", "passage")])
        with self.assertRaises(inv.InvestigationError) as ctx:
            self.run_tl(session=999)
        self.assertEqual(ctx.exception.status, 404)

    def test_limit_marks_truncation(self):
        for k in range(5):
            self.emit("camera_offline", f"e{k}", minutes_ago=k + 1)
        r = self.run_tl(limit=3, types=["camera_offline"])
        self.assertEqual((len(r["items"]), r["truncated"]), (3, True))
        self.assertEqual(r["items"][0]["title"], "e0")


class EventDetailTests(Base):
    def test_detail_with_alerts_context_and_video_note(self):
        center = alerts.AlertCenter(self.feed, lambda: {}); center.start(); self.addCleanup(center.stop)
        e = self.feed.emit("camera_offline", "Hors ligne", camera_id="c1", timestamp=self.now)
        self.feed.emit("camera_online", "Rétablie", camera_id="c1", timestamp=self.now + timedelta(seconds=20))
        self.feed.emit("camera_online", "Autre caméra", camera_id="c2", timestamp=self.now + timedelta(seconds=20))
        d = inv.event_detail(self.db, e["id"])
        self.assertEqual(d["alerts"][0]["type"], "camera_offline")
        self.assertEqual([c["title"] for c in d["context"]], ["Rétablie"])
        self.assertFalse(d["video"]["available"])
        self.assertIn("observation", d["video"]["note"])
        with self.assertRaises(inv.InvestigationError) as ctx:
            inv.event_detail(self.db, 99999)
        self.assertEqual(ctx.exception.status, 404)

    def test_identity_hidden_in_detail(self):
        e = self.feed.emit("person_recognized", "x", camera_id="c1", person="Awa", participant_id=3, status="probable")
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            p = inv.event_detail(self.db, e["id"])["payload"]
        self.assertEqual(p, {"status": "probable"})


class CsvTests(Base):
    def test_csv_columns_and_formula_protection(self):
        self.emit("camera_offline", "=HYPERLINK(\"http://x\")")
        self.detect()
        text = "".join(inv.iter_csv(self.run_tl()))
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
        self.assertEqual(rows[0], inv.CSV_HEADER)
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(r[6].startswith("'=") for r in rows[1:]))


class DossierTests(Base):
    def setUp(self):
        super().setUp()
        self.e1 = self.emit("camera_offline", "Hors ligne", minutes_ago=10)
        self.e2 = self.emit("zone_intrusion", "Intrusion", minutes_ago=5, zone_id=1)
        self.d = inv.create_dossier(self.db, "  Incident hall  ", "note")

    def test_create_validate(self):
        self.assertEqual(self.d["title"], "Incident hall")
        for bad in (None, "", "   ", "x" * 121, 5):
            with self.subTest(bad=bad), self.assertRaises(inv.InvestigationError):
                inv.create_dossier(self.db, bad)
        with self.assertRaises(inv.InvestigationError):
            inv.create_dossier(self.db, "ok", "n" * 2001)

    def test_items_are_sorted_chronologically_and_unique(self):
        inv.add_item(self.db, self.d["id"], "event", self.e2["id"], "preuve")
        inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
        got = inv.get_dossier(self.db, self.d["id"])
        self.assertEqual([i["title"] for i in got["items"]], ["Hors ligne", "Intrusion"])
        self.assertEqual(got["items"][1]["note"], "preuve")
        with self.assertRaises(inv.InvestigationError) as ctx:
            inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
        self.assertEqual(ctx.exception.status, 409)

    def test_add_item_validation(self):
        for kind, ref in (("video", 1), ("event", 0), ("event", True), ("event", "1"), ("event", 999999), ("alert", 999999)):
            with self.subTest(kind=kind, ref=ref), self.assertRaises(inv.InvestigationError):
                inv.add_item(self.db, self.d["id"], kind, ref)
        with self.assertRaises(inv.InvestigationError) as ctx:
            inv.add_item(self.db, 9999, "event", self.e1["id"])
        self.assertEqual(ctx.exception.status, 404)

    def test_item_limit(self):
        with mock.patch.object(inv, "MAX_DOSSIER_ITEMS", 1):
            inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
            with self.assertRaises(inv.InvestigationError) as ctx:
                inv.add_item(self.db, self.d["id"], "event", self.e2["id"])
            self.assertEqual(ctx.exception.status, 409)

    def test_dossier_limit(self):
        with mock.patch.object(inv, "MAX_DOSSIERS", 1):
            with self.assertRaises(inv.InvestigationError):
                inv.create_dossier(self.db, "second")

    def test_update_note_remove_delete(self):
        item = inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
        inv.update_item(self.db, self.d["id"], item["id"], "  vu  ")
        self.assertEqual(inv.get_dossier(self.db, self.d["id"])["items"][0]["note"], "vu")
        inv.update_item(self.db, self.d["id"], item["id"], "")
        self.assertIsNone(inv.get_dossier(self.db, self.d["id"])["items"][0]["note"])
        self.assertEqual(inv.update_dossier(self.db, self.d["id"], {"title": "Nouveau"})["title"], "Nouveau")
        inv.remove_item(self.db, self.d["id"], item["id"])
        self.assertEqual(inv.get_dossier(self.db, self.d["id"])["items"], [])
        with self.assertRaises(inv.InvestigationError):
            inv.remove_item(self.db, self.d["id"], item["id"])
        inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
        inv.delete_dossier(self.db, self.d["id"])
        self.assertEqual(self.db.query(DossierItem).count(), 0)
        self.assertEqual(inv.list_dossiers(self.db), [])

    def test_purged_event_shows_as_missing(self):
        inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
        self.db.query(Event).filter(Event.id == self.e1["id"]).delete(); self.db.commit()
        item = inv.get_dossier(self.db, self.d["id"])["items"][0]
        self.assertTrue(item["missing"])
        self.assertIn("supprimé", item["title"])

    def test_deleting_a_person_removes_their_name_from_dossiers(self):
        p = Participant(nom="Awa"); self.db.add(p); self.db.commit()
        ev = self.feed.emit("person_recognized", "Personne reconnue : Awa", camera_id="c1", person="Awa", participant_id=p.id)
        inv.add_item(self.db, self.d["id"], "event", ev["id"])
        delete_participant(self.db, p.id)
        title = inv.get_dossier(self.db, self.d["id"])["items"][0]["title"]
        self.assertNotIn("Awa", title)

    def test_list_counts(self):
        inv.add_item(self.db, self.d["id"], "event", self.e1["id"])
        self.assertEqual(inv.list_dossiers(self.db)[0]["items_count"], 1)


class PdfTests(Base):
    def test_timeline_pdf_is_a_real_pdf_with_content(self):
        self.emit("camera_offline", "Caméra hors ligne : Entrée — <b>test</b> & « guillemets » → ok ’", minutes_ago=3)
        self.detect()
        data = inv.timeline_pdf(self.run_tl(), {"c1": "Entrée"}, "Aucun filtre particulier.")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 1500)
        from pypdf import PdfReader
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
        self.assertIn("Rapport d'investigation", text)
        self.assertIn("Entrée", text)
        self.assertIn("<b>test</b>", text)                      # le texte est affiché tel quel, pas interprété

    def test_dossier_pdf_and_missing_items(self):
        e = self.emit("camera_offline", "Hors ligne", minutes_ago=3)
        d = inv.create_dossier(self.db, "Dossier test")
        inv.add_item(self.db, d["id"], "event", e["id"], "note opérateur")
        self.db.query(Event).delete(); self.db.commit()
        data = inv.dossier_pdf(inv.get_dossier(self.db, d["id"]), {})
        from pypdf import PdfReader
        text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
        self.assertIn("Dossier test", text)
        self.assertIn("plus disponibles", text)

    def test_long_report_has_several_pages(self):
        for k in range(150):
            self.emit("camera_offline", f"Événement numéro {k}", minutes_ago=1 + k % 50)
        data = inv.timeline_pdf(self.run_tl(types=["camera_offline"]), {}, "")
        from pypdf import PdfReader
        self.assertGreater(len(PdfReader(io.BytesIO(data)).pages), 2)

    def test_clean_replaces_unsupported_characters(self):
        from backend.app import pdf_report
        self.assertEqual(pdf_report.clean("a→b — c\n中"), "a->b - c ?")
        self.assertEqual(pdf_report.clean(None), "")
        self.assertEqual(len(pdf_report.clean("x" * 500, 10)), 10)


class RetentionTests(Base):
    def test_purge_does_not_touch_dossiers_but_items_become_missing(self):
        e = self.emit("camera_offline", "Vieux", minutes_ago=60 * 24 * 40)
        d = inv.create_dossier(self.db, "D")
        inv.add_item(self.db, d["id"], "event", e["id"])
        purge_old_detections(30)
        self.db.expire_all()
        got = inv.get_dossier(self.db, d["id"])
        self.assertEqual((got["items_count"], got["items"][0]["missing"]), (1, True))


if __name__ == "__main__":
    unittest.main()
