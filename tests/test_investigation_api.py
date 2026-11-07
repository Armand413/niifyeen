import io
import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import config, events, main, pdf_report
from backend.app.db import (Alert, AlertAction, Detection, Dossier, DossierItem, Event, Participant, SessionLocal,
                            TargetMove, utcnow)


class InvestigationApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        for m in (DossierItem, Dossier, AlertAction, Alert, Event, Detection, TargetMove):
            s.query(m).delete()
        s.commit(); s.close()

    def emit(self, type_="camera_offline", title="Hors ligne", cam="entre", **kw):
        return main.app.state.feed.emit(type_, title, camera_id=cam, **kw)

    def test_page_is_served(self):
        self.assertEqual(self.client.get("/investigation").status_code, 200)

    def test_timeline_filters_and_errors(self):
        self.emit(); self.emit("zone_intrusion", "Intrusion", cam="salle", zone_id=1)
        r = self.client.get("/api/investigation/timeline?minutes=60").json()
        self.assertEqual(r["total"], 2)
        self.assertEqual(self.client.get("/api/investigation/timeline?camera_id=salle").json()["items"][0]["type"], "zone_intrusion")
        self.assertEqual(self.client.get("/api/investigation/timeline?types=analytics").json()["total"], 1)
        self.assertEqual(self.client.get("/api/investigation/timeline?only_alerts=true").json()["total"], 2)
        for url in ("types=nope", "minutes=0", "start=hier", "limit=501", "session=424242", "participant_id=0"):
            with self.subTest(url=url):
                self.assertIn(self.client.get("/api/investigation/timeline?" + url).status_code, (404, 422))
        self.assertEqual(self.client.get("/api/investigation/timeline?start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z").status_code, 422)

    def test_identities_hidden_blocks_person_search(self):
        with mock.patch.object(config, "HISTORY_SHOW_IDENTITIES", False):
            self.assertEqual(self.client.get("/api/investigation/timeline?participant_id=3").status_code, 403)

    def test_event_detail(self):
        e = self.emit()
        d = self.client.get(f"/api/investigation/events/{e['id']}").json()
        self.assertFalse(d["video"]["available"])
        self.assertEqual(d["alerts"][0]["type"], "camera_offline")
        self.assertEqual(self.client.get("/api/investigation/events/99999").status_code, 404)

    def test_csv_export(self):
        self.emit(title="=cmd()")
        r = self.client.get("/api/investigation/timeline.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("'=cmd()", r.content.decode("utf-8-sig"))
        self.assertEqual(self.client.get("/api/investigation/timeline.csv?types=nope").status_code, 422)

    def test_pdf_reports(self):
        self.emit()
        r = self.client.get("/api/investigation/report.pdf?minutes=60")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertEqual(r.headers["content-type"], "application/pdf")
        s = SessionLocal()
        t0 = utcnow() - timedelta(minutes=5)
        for k, cam in enumerate(["entre", "salle"]):
            s.add(TargetMove(session=9, camera_id=cam, timestamp=t0 + timedelta(minutes=k), label="Cible 1"))
        s.commit(); s.close()
        r = self.client.get("/api/investigation/report.pdf?session=9")
        from pypdf import PdfReader
        text = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
        self.assertIn("session de cible", text)
        with mock.patch.object(pdf_report, "build_pdf", side_effect=pdf_report.PdfUnavailable("Export PDF indisponible")):
            self.assertEqual(self.client.get("/api/investigation/report.pdf").status_code, 501)

    def test_dossier_lifecycle(self):
        e1 = self.emit(); e2 = self.emit("zone_intrusion", "Intrusion", zone_id=2)
        d = self.client.post("/api/dossiers", json={"title": "Incident", "note": "n"})
        self.assertEqual(d.status_code, 201, d.text)
        did = d.json()["id"]
        self.assertEqual(self.client.post("/api/dossiers", json={"title": ""}).status_code, 422)
        self.assertEqual(self.client.post("/api/dossiers", json={"title": "x", "bad": 1}).status_code, 422)
        r = self.client.post(f"/api/dossiers/{did}/items", json={"kind": "event", "ref_id": e2["id"], "note": "clé"})
        self.assertEqual(r.status_code, 201, r.text)
        item = r.json()["id"]
        self.assertEqual(self.client.post(f"/api/dossiers/{did}/items", json={"kind": "event", "ref_id": e2["id"]}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/dossiers/{did}/items", json={"kind": "event", "ref_id": 99999}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/dossiers/{did}/items", json={"kind": "x", "ref_id": 1}).status_code, 422)
        self.client.post(f"/api/dossiers/{did}/items", json={"kind": "event", "ref_id": e1["id"]})
        alert_id = self.client.get("/api/alerts").json()["alerts"][0]["id"]
        self.assertEqual(self.client.post(f"/api/dossiers/{did}/items", json={"kind": "alert", "ref_id": alert_id}).status_code, 201)
        got = self.client.get(f"/api/dossiers/{did}").json()
        self.assertEqual(got["items_count"], 3)
        self.assertEqual([i["kind"] for i in got["items"]].count("event"), 2)
        self.assertEqual(self.client.put(f"/api/dossiers/{did}/items/{item}", json={"note": "mise à jour"}).status_code, 200)
        self.assertEqual(self.client.put(f"/api/dossiers/{did}", json={"title": "Renommé"}).json()["title"], "Renommé")
        self.assertEqual(self.client.get("/api/dossiers").json()["dossiers"][0]["items_count"], 3)
        pdf = self.client.get(f"/api/dossiers/{did}/report.pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("dossier-", pdf.headers["content-disposition"])
        self.assertEqual(self.client.delete(f"/api/dossiers/{did}/items/{item}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/dossiers/{did}/items/{item}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/dossiers/{did}").json(), {"deleted": True})
        self.assertEqual(self.client.get(f"/api/dossiers/{did}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/dossiers/{did}/report.pdf").status_code, 404)


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in InvestigationApiTests.__dict__]:
    setattr(InvestigationApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
