import json
import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401
from backend.app import alerts, events
from backend.app.alerts import AlertCenter, AlertError, Notifier
from backend.app.db import (Alert, AlertAction, Event, Participant, SessionLocal, Setting, delete_participant,
                            init_db, purge_old_detections, utcnow)


def reset():
    init_db()
    s = SessionLocal()
    for model in (AlertAction, Alert, Event, Participant):
        s.query(model).delete()
    s.query(Setting).filter(Setting.key == alerts.RULES_KEY).delete()
    s.commit(); s.close()


def make_center(settings=None):
    feed = events.EventFeed()
    sent = []
    center = AlertCenter(feed, lambda: {"c1": {"label": "Entrée principale"}})
    center.notifier = Notifier(lambda: {"c1": {"label": "Entrée principale"}}, center.record_notification,
                               settings=settings or alerts.notify_settings)
    return feed, center


class RulesTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_defaults_cover_every_type_and_are_valid(self):
        self.assertEqual(set(alerts.DEFAULT_RULES), set(alerts.TYPES))
        for t, rule in alerts.DEFAULT_RULES.items():
            self.assertEqual(alerts.validate_rule(t, rule), rule)

    def test_validation(self):
        for bad in ({"priority": "urgent"}, {"dedup_sec": -1}, {"dedup_sec": 1.5}, {"dedup_sec": True},
                    {"dedup_sec": 999999}, {"enabled": "oui"}, {"colour": 1}, "x"):
            with self.subTest(bad=bad), self.assertRaises(AlertError):
                alerts.validate_rule("camera_offline", bad)

    def test_save_is_partial_persistent_and_rejects_unknown_types(self):
        s = SessionLocal()
        rules = alerts.save_rules(s, {"line_crossed": {"enabled": True, "priority": "medium"}})
        self.assertTrue(rules["line_crossed"]["enabled"])
        self.assertEqual(rules["line_crossed"]["dedup_sec"], 10)          # le reste est conservé
        self.assertTrue(alerts.load_rules(s)["line_crossed"]["enabled"])
        with self.assertRaises(AlertError):
            alerts.save_rules(s, {"nope": {"enabled": True}})
        with self.assertRaises(AlertError):
            alerts.save_rules(s, {})
        self.assertFalse(alerts.load_rules(s)["person_recognized"]["enabled"])
        s.close()

    def test_corrupted_saved_rules_fall_back_to_defaults(self):
        s = SessionLocal()
        s.add(Setting(key=alerts.RULES_KEY, value="{pas du json")); s.commit()
        self.assertEqual(alerts.load_rules(s)["camera_offline"], alerts.DEFAULT_RULES["camera_offline"])
        s.query(Setting).filter(Setting.key == alerts.RULES_KEY).update({"value": json.dumps({"camera_offline": {"priority": "x"}, "zzz": {}})})
        s.commit()
        self.assertEqual(alerts.load_rules(s)["camera_offline"], alerts.DEFAULT_RULES["camera_offline"])
        s.close()


class CenterTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.feed, self.center = make_center()
        self.center.start()
        self.addCleanup(self.center.stop)

    def count(self, **filters):
        s = SessionLocal()
        try:
            return s.query(Alert).filter_by(**filters).count()
        finally:
            s.close()

    def test_event_becomes_alert_with_history(self):
        self.feed.emit(events.CAMERA_OFFLINE, "Caméra hors ligne : Entrée", severity="warning", camera_id="c1")
        s = SessionLocal()
        a = alerts.list_alerts(s)[0]
        self.assertEqual((a["type"], a["category"], a["priority"], a["status"], a["camera_id"], a["count"]),
                         ("camera_offline", "camera", "high", "new", "c1", 1))
        self.assertEqual(alerts.get_alert(s, a["id"])["history"][0]["action"], "created")
        s.close()

    def test_disabled_and_unknown_types_make_no_alert(self):
        self.feed.emit(events.LINE_CROSSED, "Ligne 1 : Entrée", camera_id="c1", zone_id=1)
        self.feed.emit(events.CAMERA_ONLINE, "Caméra rétablie", camera_id="c1")
        self.assertEqual(self.count(), 0)

    def test_repeated_events_are_grouped(self):
        for _ in range(5):
            self.feed.emit(events.ZONE_INTRUSION, "Intrusion : Hall", camera_id="c1", zone_id=3)
        self.assertEqual(self.count(), 1)
        s = SessionLocal(); a = alerts.list_alerts(s)[0]; s.close()
        self.assertEqual((a["count"], a["priority"]), (5, "critical"))

    def test_different_cameras_zones_or_people_are_not_grouped(self):
        self.feed.emit(events.ZONE_INTRUSION, "x", camera_id="c1", zone_id=3)
        self.feed.emit(events.ZONE_INTRUSION, "x", camera_id="c1", zone_id=4)
        self.feed.emit(events.ZONE_INTRUSION, "x", camera_id="c2", zone_id=3)
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        self.assertEqual(self.count(), 4)

    def test_grouping_stops_after_window_or_closure(self):
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        s = SessionLocal()
        s.query(Alert).update({"last_at": utcnow() - timedelta(seconds=alerts.DEFAULT_RULES["camera_offline"]["dedup_sec"] + 5)})
        s.commit(); s.close()
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        self.assertEqual(self.count(), 2)                                 # fenêtre dépassée
        s = SessionLocal(); alerts.set_status(s, alerts.list_alerts(s)[0]["id"], "closed"); s.close()
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        self.assertEqual(self.count(), 3)                                 # une alerte clôturée ne « avale » pas la suivante

    def test_zero_window_never_groups(self):
        s = SessionLocal(); alerts.save_rules(s, {"camera_offline": {"dedup_sec": 0}}); s.close()
        self.center.invalidate()
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        self.assertEqual(self.count(), 2)

    def test_rule_change_applies_after_invalidate_and_priority_is_used(self):
        s = SessionLocal(); alerts.save_rules(s, {"line_crossed": {"enabled": True, "priority": "medium"}}); s.close()
        self.center.invalidate()
        self.feed.emit(events.LINE_CROSSED, "Ligne 1 : Entrée", camera_id="c1", zone_id=1)
        self.assertEqual(self.count(priority="medium", type="line_crossed"), 1)

    def test_listener_failure_never_breaks_the_feed(self):
        self.feed.add_listener(lambda ev: 1 / 0)
        self.assertIsNotNone(self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1"))
        with mock.patch.object(self.center, "_on_event", side_effect=RuntimeError("boum")):
            self.assertIsNone(self.center.on_event({"type": "camera_offline"}))

    def test_stop_unregisters(self):
        self.center.stop()
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
        self.assertEqual(self.count(), 0)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.feed, self.center = make_center()
        self.center.start()
        self.addCleanup(self.center.stop)
        self.feed.emit(events.CAMERA_OFFLINE, "Caméra hors ligne : Entrée", camera_id="c1")
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        self.id = alerts.list_alerts(self.db)[0]["id"]

    def test_forward_transitions_with_history(self):
        self.assertEqual(alerts.set_status(self.db, self.id, "seen")["status"], "seen")
        self.assertEqual(alerts.set_status(self.db, self.id, "acknowledged", "Je m'en occupe")["status"], "acknowledged")
        self.assertEqual(alerts.set_status(self.db, self.id, "closed", "Câble rebranché")["status"], "closed")
        h = alerts.get_alert(self.db, self.id)["history"]
        self.assertEqual([x["action"] for x in h], ["created", "seen", "acknowledged", "closed"])
        self.assertEqual(h[2]["note"], "Je m'en occupe")

    def test_skipping_states_is_allowed_but_going_back_is_not(self):
        alerts.set_status(self.db, self.id, "acknowledged")
        with self.assertRaises(AlertError) as ctx:
            alerts.set_status(self.db, self.id, "seen")
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(AlertError):
            alerts.set_status(self.db, self.id, "new")
        with self.assertRaises(AlertError):
            alerts.set_status(self.db, self.id, "n'importe quoi")

    def test_reopen_closed_alert(self):
        alerts.set_status(self.db, self.id, "closed")
        self.assertEqual(alerts.set_status(self.db, self.id, "seen")["status"], "seen")
        self.assertEqual(alerts.get_alert(self.db, self.id)["history"][-1]["action"], "reopened")

    def test_same_status_is_a_noop_and_keeps_note(self):
        alerts.set_status(self.db, self.id, "seen")
        alerts.set_status(self.db, self.id, "seen", "doublon")
        h = alerts.get_alert(self.db, self.id)["history"]
        self.assertEqual([x["action"] for x in h], ["created", "seen", "note"])

    def test_notes(self):
        out = alerts.add_note(self.db, self.id, "  vérifié sur place  ")
        self.assertEqual(out["history"][-1]["note"], "vérifié sur place")
        for bad in ("", "   ", "x" * 501):
            with self.subTest(bad=len(bad)), self.assertRaises(AlertError):
                alerts.add_note(self.db, self.id, bad)
        with self.assertRaises(AlertError) as ctx:
            alerts.add_note(self.db, 99999, "x")
        self.assertEqual(ctx.exception.status, 404)

    def test_unknown_alert(self):
        with self.assertRaises(AlertError) as ctx:
            alerts.set_status(self.db, 99999, "seen")
        self.assertEqual(ctx.exception.status, 404)

    def test_bulk(self):
        self.feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c2")
        ids = [a["id"] for a in alerts.list_alerts(self.db)]
        self.assertEqual(alerts.bulk_status(self.db, ids + [99999], "acknowledged"), {"changed": 2, "skipped": 1})
        self.assertEqual(alerts.bulk_status(self.db, ids, "seen"), {"changed": 0, "skipped": 2})   # retour impossible
        for bad in ([], list(range(1, 202))):
            with self.assertRaises(AlertError):
                alerts.bulk_status(self.db, bad, "seen")

    def test_counts_and_summary(self):
        self.feed.emit(events.ZONE_INTRUSION, "x", camera_id="c1", zone_id=1)
        s = alerts.summary(self.db)
        self.assertEqual((s["new"], s["new_urgent"], s["fresh"]), (2, 2, []))
        first = s["last_id"] - 1
        fresh = alerts.summary(self.db, after_id=first)["fresh"]
        self.assertEqual([f["priority"] for f in fresh], ["critical"])
        self.assertTrue(fresh[0]["sound"])
        alerts.set_status(self.db, self.id, "seen")
        self.assertEqual(alerts.summary(self.db)["counts"], {"new": 1, "seen": 1, "acknowledged": 0, "closed": 0})


class FilterTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.feed, self.center = make_center()
        self.center.start()
        self.addCleanup(self.center.stop)
        self.feed.emit(events.CAMERA_OFFLINE, "Caméra hors ligne : Entrée", camera_id="c1")
        self.feed.emit(events.TARGET_SEEN, "Cible 1 repérée : Hall_100%", camera_id="c2", target_id=1)
        self.feed.emit(events.ZONE_INTRUSION, "Intrusion : Labo", camera_id="c2", zone_id=2)
        self.db = SessionLocal()
        self.addCleanup(self.db.close)

    def types(self, **kw):
        return sorted(a["type"] for a in alerts.list_alerts(self.db, **kw))

    def test_filters(self):
        self.assertEqual(len(self.types()), 3)
        self.assertEqual(self.types(priority="critical"), ["zone_intrusion"])
        self.assertEqual(self.types(category="camera"), ["camera_offline"])
        self.assertEqual(self.types(camera_id="c2"), ["target_seen", "zone_intrusion"])
        self.assertEqual(self.types(status="closed"), [])
        self.assertEqual(len(self.types(status="open")), 3)
        a = alerts.list_alerts(self.db, camera_id="c1")[0]
        alerts.set_status(self.db, a["id"], "closed")
        self.assertEqual(len(self.types(status="open")), 2)

    def test_search_is_literal_and_case_insensitive(self):
        self.assertEqual(self.types(q="labo"), ["zone_intrusion"])
        self.assertEqual(self.types(q="100%"), ["target_seen"])
        self.assertEqual(self.types(q="%"), ["target_seen"])               # % n'est pas un joker
        self.assertEqual(self.types(q="__"), [])
        self.assertEqual(self.types(q="'; DROP TABLE alerts; --"), [])
        self.assertEqual(len(self.types()), 3)

    def test_invalid_values_and_paging(self):
        for kw in ({"status": "x"}, {"priority": "x"}, {"category": "x"}, {"since": "hier"}):
            with self.subTest(kw=kw), self.assertRaises(AlertError):
                alerts.list_alerts(self.db, **kw)
        ids = [a["id"] for a in alerts.list_alerts(self.db)]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual([a["id"] for a in alerts.list_alerts(self.db, before_id=ids[0], limit=1)], [ids[1]])
        self.assertEqual([a["id"] for a in alerts.list_alerts(self.db, after_id=ids[1])], [ids[0]])

    def test_date_filters(self):
        now = utcnow()
        future = (now + timedelta(hours=1)).isoformat() + "Z"
        past = (now - timedelta(hours=1)).isoformat() + "Z"
        self.assertEqual(len(self.types(since=past)), 3)
        self.assertEqual(self.types(since=future), [])
        self.assertEqual(self.types(until=past), [])
        self.assertEqual(len(self.types(until=future)), 3)


class NotifierTests(unittest.TestCase):
    CFG = dict(smtp_host="smtp.test", smtp_port=587, smtp_user="u", smtp_password="TOP-SECRET", smtp_from="a@b.c",
               smtp_to=["x@y.z"], smtp_security="starttls", telegram_token="123:TOKEN-SECRET", telegram_chat="42",
               min_priority="high", identities=False, per_minute=2)

    def setUp(self):
        reset()
        self.cfg = dict(self.CFG)
        self.results = []
        self.n = Notifier(lambda: {"c1": {"label": "Entrée principale"}},
                          lambda i, ch, res: self.results.append((i, ch, res)), settings=lambda: self.cfg)
        self.alert = {"id": 7, "priority": "high", "type_label": "Caméra hors ligne", "camera_id": "c1", "count": 1,
                      "last_at": "2026-01-01T10:00:00Z", "title": "Personne reconnue : Awa Traoré"}

    def test_channels_status_exposes_no_secret(self):
        st = alerts.channels_status(self.cfg)
        self.assertEqual((st["email"]["configured"], st["telegram"]["configured"]), (True, True))
        blob = json.dumps(st)
        for secret in ("TOP-SECRET", "TOKEN-SECRET", "x@y.z", "smtp.test", "a@b.c"):
            self.assertNotIn(secret, blob)
        self.cfg.update(smtp_host="", telegram_token="")
        st = alerts.channels_status(self.cfg)
        self.assertEqual((st["email"]["configured"], st["telegram"]["configured"]), (False, False))

    def test_wanted_needs_channel_rule_and_priority(self):
        rule = {"notify": True}
        self.assertTrue(self.n.wanted(self.alert, rule))
        self.assertFalse(self.n.wanted(self.alert, {"notify": False}))
        self.assertFalse(self.n.wanted({**self.alert, "priority": "medium"}, rule))
        self.cfg.update(min_priority="low")
        self.assertTrue(self.n.wanted({**self.alert, "priority": "medium"}, rule))
        self.cfg.update(smtp_host="", telegram_token="")
        self.assertFalse(self.n.wanted(self.alert, rule))

    def test_message_hides_identities_by_default(self):
        subject, body = self.n.message(self.alert)
        self.assertIn("Entrée principale", body)
        self.assertNotIn("Awa", body + subject)
        self.cfg["identities"] = True
        self.assertIn("Awa Traoré", self.n.message(self.alert)[1])

    def test_delivery_to_both_channels(self):
        with mock.patch.object(alerts, "send_email") as mail, mock.patch.object(alerts, "send_telegram") as tg:
            self.n.deliver(self.alert)
        self.assertEqual(mail.call_count, 1); self.assertEqual(tg.call_count, 1)
        self.assertEqual(self.results, [(7, "email", "envoyée"), (7, "telegram", "envoyée")])

    def test_failures_are_recorded_without_secrets(self):
        with mock.patch.object(alerts, "send_email", side_effect=OSError("auth TOP-SECRET refused")), \
             mock.patch.object(alerts, "send_telegram", side_effect=RuntimeError("https://api.telegram.org/bot123:TOKEN-SECRET")):
            self.n.deliver(self.alert)
        text = json.dumps(self.results)
        self.assertNotIn("SECRET", text)
        self.assertEqual([r[2] for r in self.results], ["échec (OSError)", "échec (RuntimeError)"])

    def test_only_configured_channels_are_used(self):
        self.cfg.update(telegram_token="")
        with mock.patch.object(alerts, "send_email") as mail, mock.patch.object(alerts, "send_telegram") as tg:
            self.n.deliver(self.alert)
        self.assertEqual((mail.call_count, tg.call_count), (1, 0))

    def test_rate_limit(self):
        with mock.patch.object(alerts, "send_email"), mock.patch.object(alerts, "send_telegram"):
            for _ in range(4):
                self.n.deliver(self.alert)
        self.assertEqual([r for r in self.results if r[1] == "all"].__len__(), 2)

    def test_test_message(self):
        with mock.patch.object(alerts, "send_email") as mail:
            self.assertEqual(self.n.test("email"), "envoyée")
        self.assertEqual(mail.call_count, 1)
        with mock.patch.object(alerts, "send_telegram", side_effect=OSError("TOKEN-SECRET")):
            self.assertEqual(self.n.test("telegram"), "échec (OSError)")
        self.cfg.update(smtp_host="")
        self.assertEqual(self.n.test("email"), "canal non configuré")
        self.assertEqual(self.n.test("sms"), "canal non configuré")

    def test_worker_thread_delivers_and_records(self):
        feed = events.EventFeed()
        center = AlertCenter(feed, lambda: {})
        center.notifier = Notifier(lambda: {}, center.record_notification, settings=lambda: self.cfg)
        center.start(); self.addCleanup(center.stop)
        with mock.patch.object(alerts, "send_email") as mail, mock.patch.object(alerts, "send_telegram"):
            feed.emit(events.CAMERA_OFFLINE, "x", camera_id="c1")
            for _ in range(100):
                if mail.call_count:
                    break
                import time; time.sleep(0.05)
            center.stop()
        s = SessionLocal()
        a = alerts.list_alerts(s)[0]
        self.assertEqual(a["notified"].get("email"), "envoyée")
        self.assertIn("notified", [h["action"] for h in alerts.get_alert(s, a["id"])["history"]])
        s.close()

    def test_no_notification_for_low_rules(self):
        feed = events.EventFeed()
        center = AlertCenter(feed, lambda: {})
        center.notifier = Notifier(lambda: {}, center.record_notification, settings=lambda: self.cfg)
        with mock.patch.object(center.notifier, "submit") as submit:
            center.on_event({"id": 1, "type": "target_seen", "camera_id": "c1", "title": "t", "payload": {}})
            self.assertEqual(submit.call_count, 0)
            center.on_event({"id": 2, "type": "zone_intrusion", "camera_id": "c1", "title": "t", "payload": {"zone_id": 1}})
            self.assertEqual(submit.call_count, 1)


class PrivacyAndRetentionTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_deleting_a_person_erases_their_name_from_alerts(self):
        s = SessionLocal()
        p = Participant(nom="Awa", prenom="Traoré"); s.add(p); s.commit()
        feed, center = make_center()
        center.on_event({"id": 1, "type": "person_inactive_seen", "camera_id": "c1", "title": "Personne désactivée reconnue : Awa Traoré — Entrée",
                         "payload": {"person": "Awa Traoré", "participant_id": p.id}})
        a = s.query(Alert).one()
        self.assertEqual(a.participant_id, p.id)
        delete_participant(s, p.id)
        s.expire_all()
        a = s.query(Alert).one()
        self.assertNotIn("Awa", (a.title or "") + (a.payload or "") + (a.description or ""))
        self.assertIsNone(a.participant_id)
        s.close()

    def test_purge_removes_old_alerts_and_their_history(self):
        s = SessionLocal()
        old = Alert(type="camera_offline", category="camera", priority="high", title="vieille", dedup_key="k1",
                    created_at=utcnow() - timedelta(days=60), last_at=utcnow() - timedelta(days=60))
        new = Alert(type="camera_offline", category="camera", priority="high", title="récente", dedup_key="k2")
        s.add_all([old, new]); s.flush()
        s.add_all([AlertAction(alert_id=old.id, action="created"), AlertAction(alert_id=new.id, action="created")])
        s.commit()
        purge_old_detections(30)
        s.expire_all()
        self.assertEqual([a.title for a in s.query(Alert).all()], ["récente"])
        self.assertEqual(s.query(AlertAction).count(), 1)
        s.close()


if __name__ == "__main__":
    unittest.main()
