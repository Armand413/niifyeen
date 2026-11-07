import json
import unittest
from unittest import mock

import tests  # noqa: F401
from tests import test_api as _api
from backend.app import alerts, events, main
from backend.app.db import Alert, AlertAction, SessionLocal, Setting


class AlertsApiTests(_api.ApiTests):
    def setUp(self):
        super().setUp()
        s = SessionLocal()
        s.query(AlertAction).delete(); s.query(Alert).delete()
        s.query(Setting).filter(Setting.key == alerts.RULES_KEY).delete()
        s.commit(); s.close()
        main.app.state.alerts.invalidate()

    def raise_alert(self, **kw):
        main.app.state.feed.emit(events.CAMERA_OFFLINE, "Caméra hors ligne : Entrée", camera_id=kw.get("camera_id", "entre"))
        return self.client.get("/api/alerts").json()["alerts"][0]

    def test_page_is_served(self):
        self.assertEqual(self.client.get("/alerts").status_code, 200)

    def test_event_to_alert_to_closure(self):
        a = self.raise_alert()
        self.assertEqual((a["status"], a["priority"], a["category"]), ("new", "high", "camera"))
        body = self.client.get("/api/alerts").json()
        self.assertEqual(body["counts"], {"new": 1, "seen": 0, "acknowledged": 0, "closed": 0})
        r = self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "acknowledged", "note": "OK"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "acknowledged")
        self.assertEqual([h["action"] for h in r.json()["history"]], ["created", "acknowledged"])
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "seen"}).status_code, 409)
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "closed"}).json()["status"], "closed")
        self.assertEqual(self.client.get("/api/alerts?status=open").json()["alerts"], [])

    def test_errors(self):
        a = self.raise_alert()
        self.assertEqual(self.client.get("/api/alerts/99999").status_code, 404)
        self.assertEqual(self.client.post("/api/alerts/99999/status", json={"status": "seen"}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "new"}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "seen", "x": 1}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/status", json={"status": "seen", "note": "n" * 600}).status_code, 422)
        self.assertEqual(self.client.get("/api/alerts?status=bof").status_code, 422)
        self.assertEqual(self.client.get("/api/alerts?priority=bof").status_code, 422)
        self.assertEqual(self.client.get("/api/alerts?since=hier").status_code, 422)
        self.assertEqual(self.client.get("/api/alerts?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/api/alerts/abc").status_code, 422)

    def test_notes_and_bulk(self):
        a = self.raise_alert()
        b = self.raise_alert(camera_id="salle")
        r = self.client.post(f"/api/alerts/{a['id']}/note", json={"note": "Appelé le technicien"})
        self.assertEqual(r.json()["history"][-1]["note"], "Appelé le technicien")
        self.assertEqual(self.client.post(f"/api/alerts/{a['id']}/note", json={"note": " "}).status_code, 422)
        r = self.client.post("/api/alerts/bulk", json={"ids": [a["id"], b["id"], 9999], "status": "seen"})
        self.assertEqual(r.json(), {"changed": 2, "skipped": 1})
        self.assertEqual(self.client.post("/api/alerts/bulk", json={"ids": [], "status": "seen"}).status_code, 422)

    def test_filters_and_search(self):
        self.raise_alert(); self.raise_alert(camera_id="salle")
        self.assertEqual(len(self.client.get("/api/alerts?camera_id=salle").json()["alerts"]), 1)
        self.assertEqual(len(self.client.get("/api/alerts?q=hors").json()["alerts"]), 2)
        self.assertEqual(len(self.client.get("/api/alerts?category=analytics").json()["alerts"]), 0)

    def test_summary(self):
        s0 = self.client.get("/api/alerts/summary").json()
        self.raise_alert()
        s1 = self.client.get(f"/api/alerts/summary?after_id={s0['last_id']}").json()
        self.assertEqual((s1["new"], len(s1["fresh"]), s1["fresh"][0]["sound"]), (1, 1, True))
        self.assertEqual(self.client.get("/api/alerts/summary").json()["fresh"], [])

    def test_rules_roundtrip_and_effect(self):
        rules = self.client.get("/api/alerts/rules").json()
        self.assertEqual(len(rules["rules"]), len(alerts.TYPES))
        r = self.client.put("/api/alerts/rules", json={"rules": {"line_crossed": {"enabled": True, "priority": "low", "dedup_sec": 5}}})
        self.assertEqual(r.status_code, 200, r.text)
        row = [x for x in r.json()["rules"] if x["type"] == "line_crossed"][0]
        self.assertEqual((row["enabled"], row["dedup_sec"]), (True, 5))
        main.app.state.feed.emit(events.LINE_CROSSED, "Ligne 1 : Entrée", camera_id="entre", zone_id=1)
        self.assertEqual(self.client.get("/api/alerts?category=analytics").json()["alerts"][0]["type"], "line_crossed")
        for bad in ({"rules": {"nope": {"enabled": True}}}, {"rules": {"line_crossed": {"priority": "x"}}},
                    {"rules": {}}, {"rules": {"line_crossed": {"dedup_sec": -3}}}, {"rules": {"line_crossed": {}}, "x": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.put("/api/alerts/rules", json=bad).status_code, 422)

    def test_channels_expose_no_secret_and_test_is_safe(self):
        env = {"NIIFYEEN_SMTP_HOST": "smtp.secret.test", "NIIFYEEN_SMTP_FROM": "me@secret.test",
               "NIIFYEEN_SMTP_TO": "boss@secret.test", "NIIFYEEN_SMTP_PASSWORD": "PW-SECRET",
               "NIIFYEEN_TELEGRAM_TOKEN": "1:TOKEN-SECRET", "NIIFYEEN_TELEGRAM_CHAT_ID": "9"}
        with mock.patch.dict("os.environ", env):
            r = self.client.get("/api/alerts/channels")
            self.assertTrue(r.json()["email"]["configured"] and r.json()["telegram"]["configured"])
            self.assertNotIn("SECRET", r.text); self.assertNotIn("secret.test", r.text)
            with mock.patch.object(alerts, "send_email") as mail:
                self.assertEqual(self.client.post("/api/alerts/channels/test", json={"channel": "email"}).json()["ok"], True)
                self.assertEqual(mail.call_count, 1)
            with mock.patch.object(alerts, "send_telegram", side_effect=OSError("1:TOKEN-SECRET")):
                r = self.client.post("/api/alerts/channels/test", json={"channel": "telegram"})
                self.assertFalse(r.json()["ok"]); self.assertNotIn("SECRET", r.text)
        r = self.client.post("/api/alerts/channels/test", json={"channel": "email"})
        self.assertEqual((r.json()["ok"], r.json()["result"]), (False, "canal non configuré"))
        self.assertEqual(self.client.post("/api/alerts/channels/test", json={"channel": "sms"}).status_code, 422)

    def test_alert_from_a_real_target_move_reaches_the_center(self):
        main.app.state.feed.emit(events.TARGET_SEEN, "Cible 1 repérée : Entrée", camera_id="entre", target_id=1)
        a = self.client.get("/api/alerts").json()["alerts"][0]
        self.assertEqual((a["type"], a["category"]), ("target_seen", "target"))


for _name in [n for n in dir(_api.ApiTests) if n.startswith("test_") and n not in AlertsApiTests.__dict__]:
    setattr(AlertsApiTests, _name, None)

if __name__ == "__main__":
    unittest.main()
