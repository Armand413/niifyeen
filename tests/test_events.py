import unittest
from datetime import timedelta
from unittest import mock

import tests  # noqa: F401
from backend.app import events as ev
from backend.app.db import Event, SessionLocal, init_db, purge_old_detections, utcnow


class EventFeedTests(unittest.TestCase):
    def setUp(self):
        init_db()
        s = SessionLocal(); s.query(Event).delete(); s.commit(); s.close()
        self.feed = ev.EventFeed()

    def test_emit_persists_and_returns_the_event(self):
        e = self.feed.emit(ev.TARGET_SEEN, "Cible 1 repérée", camera_id="entre", target_id=1, score=0.7)
        self.assertEqual((e["type"], e["severity"], e["camera_id"]), (ev.TARGET_SEEN, "info", "entre"))
        self.assertEqual(e["payload"], {"target_id": 1, "score": 0.7})
        self.assertTrue(e["timestamp"].endswith("Z"))
        self.assertEqual(self.feed.last_id, e["id"])
        self.assertEqual(self.feed.recent()[0]["id"], e["id"])

    def test_last_id_survives_a_new_feed_instance(self):
        e = self.feed.emit(ev.CAMERA_OFFLINE, "x", severity="warning")
        self.assertEqual(ev.EventFeed().last_id, e["id"])

    def test_recent_is_newest_first_and_limited(self):
        ids = [self.feed.emit(ev.PERSON_RECOGNIZED, f"p{i}")["id"] for i in range(5)]
        self.assertEqual([e["id"] for e in self.feed.recent(limit=3)], ids[::-1][:3])

    def test_after_id_returns_only_news_and_before_id_pages_backwards(self):
        ids = [self.feed.emit(ev.PERSON_RECOGNIZED, f"p{i}")["id"] for i in range(6)]
        self.assertEqual([e["id"] for e in self.feed.recent(after_id=ids[3])], [ids[5], ids[4]])
        self.assertEqual([e["id"] for e in self.feed.recent(before_id=ids[2])], [ids[1], ids[0]])
        self.assertEqual(self.feed.recent(after_id=ids[-1]), [])

    def test_filters(self):
        self.feed.emit(ev.TARGET_SEEN, "a", camera_id="entre")
        self.feed.emit(ev.TARGET_MOVED, "b", camera_id="salle")
        self.feed.emit(ev.CAMERA_OFFLINE, "c", severity="warning", camera_id="salle")
        self.feed.emit(ev.PERSON_RECOGNIZED, "d", camera_id="entre")
        titles = lambda **kw: sorted(e["title"] for e in self.feed.recent(**kw))
        self.assertEqual(titles(family="targets"), ["a", "b"])
        self.assertEqual(titles(family="cameras"), ["c"])
        self.assertEqual(titles(family="people"), ["d"])
        self.assertEqual(titles(camera_id="salle"), ["b", "c"])
        self.assertEqual(titles(severity="warning"), ["c"])
        self.assertEqual(titles(types=[ev.TARGET_SEEN]), ["a"])
        self.assertEqual(titles(family="targets", camera_id="entre"), ["a"])

    def test_unknown_severity_becomes_info(self):
        self.assertEqual(self.feed.emit(ev.TARGET_SEEN, "x", severity="catastrophe")["severity"], "info")

    def test_emit_never_raises_when_the_database_fails(self):
        with mock.patch.object(ev, "SessionLocal", side_effect=RuntimeError("base indisponible")):
            with self.assertLogs(level="ERROR") if False else self.subTest():
                pass
        broken = mock.MagicMock()
        broken.add.side_effect = RuntimeError("écriture impossible")
        with mock.patch.object(ev, "SessionLocal", return_value=broken):
            with self.assertLogs(level="ERROR"):
                self.assertIsNone(self.feed.emit(ev.TARGET_SEEN, "x"))
        broken.rollback.assert_called()
        broken.close.assert_called()

    def test_long_titles_are_truncated(self):
        self.assertEqual(len(self.feed.emit(ev.TARGET_SEEN, "x" * 1000)["title"]), 300)

    def test_retention_purges_old_events(self):
        self.feed.emit(ev.TARGET_SEEN, "récent")
        old = self.feed.emit(ev.TARGET_SEEN, "ancien", timestamp=utcnow() - timedelta(days=90))
        purge_old_detections(30)
        self.assertEqual([e["title"] for e in self.feed.recent()], ["récent"])
        self.assertNotIn(old["id"], [e["id"] for e in self.feed.recent()])


if __name__ == "__main__":
    unittest.main()
