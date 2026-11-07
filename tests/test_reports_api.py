import io
import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import pdf_report, reports
from backend.app.db import Detection, Setting, SessionLocal, utcnow


class ReportsApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(Detection).delete()
        s.query(Setting).filter(Setting.key == reports.SETTINGS_KEY).delete()
        now = utcnow()
        for k in range(4):
            s.add(Detection(camera_id="entre", track_id=1, mode="UNK", timestamp=now - timedelta(minutes=10, seconds=-k)))
        s.commit(); s.close()

    def test_page_is_served(self):
        self.assertEqual(self.client.get("/reports").status_code, 200)

    def test_options(self):
        d = self.client.get("/api/reports/options").json()
        self.assertEqual([s["id"] for s in d["sections"]], list(reports.SECTIONS))
        self.assertEqual(d["templates"], {})

    def test_preview_uses_camera_labels(self):
        d = self.client.get("/api/reports/preview?minutes=120&sections=cameras,summary&title=Mon%20rapport").json()
        self.assertEqual(d["title"], "Mon rapport")
        self.assertEqual([s["id"] for s in d["sections"]], ["summary", "cameras"])
        self.assertIn("1 passage(s)", d["sections"][0]["paragraphs"][0])
        self.assertTrue(d["anonymous"])

    def test_validation(self):
        for q in ("sections=bidon", "minutes=0", "tz_offset_min=5000", "start=hier", "sections=,,"):
            with self.subTest(q=q):
                self.assertEqual(self.client.get("/api/reports/preview?" + q).status_code, 422)
        self.assertEqual(self.client.get("/api/reports/report.pdf?sections=bidon").status_code, 422)

    def test_pdf(self):
        from pypdf import PdfReader
        r = self.client.get("/api/reports/report.pdf?minutes=120&tz_offset_min=0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertGreaterEqual(len(PdfReader(io.BytesIO(r.content)).pages), 1)

    def test_pdf_unavailable_gives_501(self):
        with mock.patch.object(pdf_report, "build_pdf", side_effect=pdf_report.PdfUnavailable("Export PDF indisponible")):
            self.assertEqual(self.client.get("/api/reports/report.pdf").status_code, 501)

    def test_templates_roundtrip(self):
        body = {"title": "Hebdo", "camera_id": "entre", "sections": ["summary", "alerts"], "minutes": 10080}
        r = self.client.put("/api/reports/templates/Mon%20mod%C3%A8le", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/reports/options").json()["templates"]["Mon modèle"]["sections"], ["summary", "alerts"])
        self.assertEqual(self.client.put("/api/reports/templates/x", json={**body, "sections": ["nope"]}).status_code, 422)
        self.assertEqual(self.client.put("/api/reports/templates/x", json={**body, "minutes": 0}).status_code, 422)
        self.assertEqual(self.client.put("/api/reports/templates/" + "n" * 41, json=body).status_code, 422)
        self.assertEqual(self.client.delete("/api/reports/templates/Mon%20mod%C3%A8le").status_code, 200)
        self.assertEqual(self.client.delete("/api/reports/templates/Mon%20mod%C3%A8le").status_code, 404)


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in ReportsApiTests.__dict__]:
    setattr(ReportsApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
