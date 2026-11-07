import io
import unittest
from datetime import datetime, timedelta

import tests  # noqa: F401
from backend.app import pdf_report, reports
from backend.app.db import (Alert, Detection, Event, Participant, Setting, SessionLocal, TargetMove, Zone, ZoneStat, init_db)


def reset():
    init_db()
    s = SessionLocal()
    for m in (Alert, Event, ZoneStat, Zone, TargetMove, Detection, Participant):
        s.query(m).delete()
    s.query(Setting).filter(Setting.key == reports.SETTINGS_KEY).delete()
    s.commit(); s.close()


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.t0 = datetime(2026, 1, 5, 10, 0, 0)
        self.since, self.until = self.t0 - timedelta(hours=1), self.t0 + timedelta(hours=5)

    def passage(self, cam, track, at, n=3, mode="UNK", pid=None, label=None, base=None):
        for k in range(n):
            self.db.add(Detection(camera_id=cam, track_id=track, mode=mode, participant_id=pid, label=label,
                                  timestamp=(base or self.t0) + timedelta(seconds=at + k)))
        self.db.commit()

    def build(self, **kw):
        params = reports.parse_params(**kw)
        return reports.build(self.db, self.since, self.until, params, {"c1": "Entrée", "c2": "Salle"})

    @staticmethod
    def sec(report, sid):
        return next(s for s in report["sections"] if s["id"] == sid)


class ParamsTests(unittest.TestCase):
    def test_defaults(self):
        p = reports.parse_params()
        self.assertEqual(p["title"], reports.DEFAULT_TITLE)
        self.assertEqual(p["sections"], list(reports.SECTIONS))

    def test_sections_are_validated_ordered_and_deduplicated(self):
        self.assertEqual(reports.parse_params(sections="comparison, hours,summary,hours")["sections"], ["summary", "hours", "comparison"])
        for bad in ("nimporte", "summary,;drop", ",,"):
            with self.subTest(bad=bad), self.assertRaises(reports.ReportError):
                reports.parse_params(sections=bad)

    def test_title_is_normalized_and_bounded(self):
        self.assertEqual(reports.parse_params(title="  Rapport \n  du   jour ")["title"], "Rapport du jour")
        self.assertEqual(len(reports.parse_params(title="x" * 500)["title"]), 100)

    def test_timezone_is_bounded(self):
        self.assertEqual(reports.parse_params(tz_offset_min=99999)["tz_offset_min"], 840)


class BuildTests(Base):
    def test_empty_period_is_still_a_valid_report(self):
        r = self.build()
        self.assertEqual([s["id"] for s in r["sections"]], list(reports.SECTIONS))
        self.assertIn("0 passage", self.sec(r, "summary")["paragraphs"][0])
        self.assertIn("Aucune détection", self.sec(r, "cameras")["paragraphs"][0])
        self.assertTrue(r["anonymous"])

    def test_summary_and_cameras_numbers(self):
        p = Participant(nom="Secret", prenom="Zoe"); self.db.add(p); self.db.commit()
        self.passage("c1", 1, 0, n=5, mode="DB", pid=p.id, label="Zoe Secret")
        self.passage("c1", 2, 100, n=3)
        self.passage("c2", 3, 0, n=3)
        r = self.build()
        self.assertIn("3 passage(s) et 11 observation(s)", self.sec(r, "summary")["paragraphs"][0])
        rows = {row[0]: row for row in self.sec(r, "cameras")["table"]["rows"]}
        self.assertEqual(rows["Entrée"][1:4], [8, 2, 1])
        self.assertEqual(rows["Salle"][2], 1)

    def test_camera_filter_and_subtitle(self):
        self.passage("c1", 1, 0); self.passage("c2", 2, 0)
        r = self.build(camera_id="c2")
        self.assertIn("Salle", r["subtitle"])
        self.assertEqual([row[0] for row in self.sec(r, "cameras")["table"]["rows"]], ["Salle"])
        self.assertIn("toutes les caméras", self.build()["subtitle"])

    def test_only_selected_sections(self):
        r = self.build(sections="hours")
        self.assertEqual([s["id"] for s in r["sections"]], ["hours"])
        self.assertEqual(len(self.sec(r, "hours")["bars"]["values"]), 24)

    def test_hours_follow_timezone(self):
        self.passage("c1", 1, 0)                                  # 10:00 UTC
        self.assertEqual(self.sec(self.build(sections="hours"), "hours")["bars"]["values"][10], 1)
        self.assertEqual(self.sec(self.build(sections="hours", tz_offset_min=60), "hours")["bars"]["values"][11], 1)

    def test_alerts_counts_without_titles(self):
        for typ, prio, st, cnt in (("zone_intrusion", "critical", "new", 3), ("camera_offline", "high", "closed", 1), ("camera_offline", "high", "new", 1)):
            self.db.add(Alert(type=typ, category="analytics", priority=prio, status=st, title="Zoe Secret reconnue", dedup_key=typ + st,
                              count=cnt, created_at=self.t0, last_at=self.t0))
        self.db.commit()
        sec = self.sec(self.build(sections="alerts"), "alerts")
        self.assertIn("3 alerte(s)", sec["paragraphs"][0])
        self.assertIn("5 événement(s)", sec["paragraphs"][0])
        self.assertIn("Critique 1", sec["paragraphs"][0])
        self.assertEqual({row[0]: row[1] for row in sec["table"]["rows"]}["Caméra hors ligne"], 2)
        self.assertNotIn("Secret", str(sec))

    def test_incidents_only_safe_types_and_limited(self):
        self.db.add(Event(type="person_recognized", severity="info", camera_id="c1", title="Zoe Secret reconnue", timestamp=self.t0))
        self.db.add(Event(type="camera_offline", severity="warning", camera_id="c1", title="Caméra hors ligne : Entrée", timestamp=self.t0))
        for i in range(40):
            self.db.add(Event(type="zone_intrusion", severity="warning", camera_id="c2", title=f"Intrusion {i}", timestamp=self.t0 + timedelta(seconds=i)))
        self.db.commit()
        sec = self.sec(self.build(sections="incidents"), "incidents")
        self.assertEqual(len(sec["table"]["rows"]), reports.MAX_INCIDENTS)
        self.assertIn("41 incident(s)", sec["paragraphs"][0])
        self.assertNotIn("Secret", str(sec))
        self.assertEqual(sec["table"]["rows"][0][2], "Intrusion 39")             # les plus récents d'abord
        only = self.sec(self.build(sections="incidents", camera_id="c1"), "incidents")
        self.assertEqual([r[2] for r in only["table"]["rows"]], ["Caméra hors ligne : Entrée"])

    def test_zones_table(self):
        z = Zone(camera_id="c1", kind="line", name="Porte", points="[[0,0],[1,1]]"); self.db.add(z); self.db.commit()
        self.db.add(ZoneStat(zone_id=z.id, bucket=self.t0, entries=4, exits=2, occupancy_max=3)); self.db.commit()
        row = self.sec(self.build(sections="zones"), "zones")["table"]["rows"][0]
        self.assertEqual(row, ["Porte", "Entrée", 4, 2, 3])

    def test_transitions_and_comparison(self):
        p = Participant(nom="A"); self.db.add(p); self.db.commit()
        self.passage("c1", 1, 0, mode="DB", pid=p.id, label="A")
        self.passage("c2", 1, 60, mode="DB", pid=p.id, label="A")
        r = self.build(sections="transitions,comparison")
        self.assertEqual(self.sec(r, "transitions")["table"]["rows"][0][:3], ["Entrée", "Salle", 1])
        rows = {row[0]: row for row in self.sec(r, "comparison")["table"]["rows"]}
        self.assertEqual(rows["Passages"][1:], [2, 0, "n/a"])

    def test_anonymity_whole_report(self):
        p = Participant(nom="Confidentiel", prenom="Zoe"); self.db.add(p); self.db.commit()
        self.passage("c1", 1, 0, mode="DB", pid=p.id, label="Zoe Confidentiel")
        self.assertNotIn("Confidentiel", str(self.build()))


class PdfTests(Base):
    def test_pdf_is_valid_and_contains_text(self):
        from pypdf import PdfReader
        self.passage("c1", 1, 0, n=6)
        pdf = reports.render_pdf(self.build(title="Rapport « test » <b>&"))
        self.assertTrue(pdf.startswith(b"%PDF"))
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn("Synth", text)
        self.assertIn("test", text)
        self.assertIn("anonyme", text.lower())

    def test_pdf_with_every_section_and_empty_data(self):
        self.assertTrue(reports.render_pdf(self.build()).startswith(b"%PDF"))

    def test_bar_chart_handles_zero_and_negative_values(self):
        pdf = pdf_report.build_pdf("t", "s", [{"heading": "h", "bars": {"labels": ["a", "b"], "values": [0, -5]}}])
        self.assertTrue(pdf.startswith(b"%PDF"))


class TemplateTests(Base):
    def test_save_list_overwrite_delete(self):
        p = reports.parse_params(title="Hebdo", camera_id="c1", sections="summary,hours")
        reports.save_template(self.db, "  Hebdo  ", 10080, p)
        t = reports.list_templates(self.db)
        self.assertEqual(t["Hebdo"], {"title": "Hebdo", "camera_id": "c1", "sections": ["summary", "hours"], "minutes": 10080})
        reports.save_template(self.db, "Hebdo", 60, p)
        self.assertEqual(reports.list_templates(self.db)["Hebdo"]["minutes"], 60)
        self.assertTrue(reports.delete_template(self.db, "Hebdo"))
        self.assertFalse(reports.delete_template(self.db, "Hebdo"))
        self.assertEqual(reports.list_templates(self.db), {})

    def test_limits_and_names(self):
        p = reports.parse_params()
        for bad in ("", "   ", "x" * 41):
            with self.subTest(bad=bad), self.assertRaises(reports.ReportError):
                reports.save_template(self.db, bad, 60, p)
        for i in range(reports.MAX_TEMPLATES):
            reports.save_template(self.db, f"m{i}", 60, p)
        with self.assertRaises(reports.ReportError):
            reports.save_template(self.db, "de trop", 60, p)
        reports.save_template(self.db, "m0", 30, p)                      # remplacer reste permis

    def test_corrupted_setting_is_ignored(self):
        self.db.add(Setting(key=reports.SETTINGS_KEY, value="{pas du json")); self.db.commit()
        self.assertEqual(reports.list_templates(self.db), {})
        self.db.query(Setting).filter(Setting.key == reports.SETTINGS_KEY).update({"value": "[1,2]"}); self.db.commit()
        self.assertEqual(reports.list_templates(self.db), {})


if __name__ == "__main__":
    unittest.main()
