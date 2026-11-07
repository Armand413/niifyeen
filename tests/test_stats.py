import csv
import io
import unittest
from datetime import datetime, timedelta

import tests  # noqa: F401
from backend.app import config, history, stats
from backend.app.db import (Detection, Participant, SessionLocal, TargetMove, Zone, ZoneStat, init_db, utcnow)


def reset():
    init_db()
    s = SessionLocal()
    for m in (ZoneStat, Zone, TargetMove, Detection, Participant):
        s.query(m).delete()
    s.commit(); s.close()


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.db = SessionLocal()
        self.addCleanup(self.db.close)
        # base fixe, un lundi à 10:00 UTC, pour des heures déterministes
        self.t0 = datetime(2026, 1, 5, 10, 0, 0)
        self.since = self.t0 - timedelta(hours=1)
        self.until = self.t0 + timedelta(hours=6)

    def person(self, nom="Doe"):
        p = Participant(nom=nom, prenom="J", consent=True) if hasattr(Participant, "consent") else Participant(nom=nom)
        self.db.add(p); self.db.commit()
        return p.id

    def passage(self, cam, track, at_sec, n=3, mode="UNK", pid=None, x=None, y=None, label=None):
        """n observations espacées d'1 s : un passage de n-1 secondes."""
        for k in range(n):
            self.db.add(Detection(camera_id=cam, track_id=track, mode=mode, label=label, participant_id=pid, x=x, y=y,
                                  timestamp=self.t0 + timedelta(seconds=at_sec + k)))
        self.db.commit()

    def ov(self, **kw):
        return stats.overview(self.db, self.since, self.until, **kw)


class OverviewTests(Base):
    def test_empty(self):
        o = self.ov()
        self.assertEqual((o["observations"], o["passages"], o["unique_enrolled"]), (0, 0, 0))
        self.assertIsNone(o["avg_duration_sec"])
        self.assertEqual(sum(o["by_hour"]), 0)
        self.assertEqual(o["by_camera"], [])

    def test_observations_passages_and_unique_are_distinct(self):
        pid = self.person()
        self.passage("c1", 1, 0, n=5, mode="DB", pid=pid, label="Doe")          # même personne, deux passages
        self.passage("c1", 1, 300, n=4, mode="DB", pid=pid, label="Doe")
        self.passage("c2", 7, 0, n=3)                                           # un inconnu
        self.passage("c2", 8, 0, n=3)                                           # un autre inconnu
        o = self.ov()
        self.assertEqual(o["observations"], 5 + 4 + 3 + 3)
        self.assertEqual(o["passages"], 4)
        self.assertEqual(o["unique_enrolled"], 1)
        self.assertEqual(o["unknown_passages"], 2)
        self.assertEqual(o["by_mode"], {"TARGET": 0, "DB": 2, "UNK": 2})
        by = {c["camera_id"]: c for c in o["by_camera"]}
        self.assertEqual((by["c1"]["passages"], by["c1"]["unique_enrolled"], by["c1"]["observations"]), (2, 1, 9))
        self.assertEqual((by["c2"]["passages"], by["c2"]["unique_enrolled"]), (2, 0))
        self.assertEqual(by["c1"]["avg_duration_sec"], 3.5)

    def test_camera_filter(self):
        self.passage("c1", 1, 0); self.passage("c2", 1, 0)
        o = self.ov(camera_id="c1")
        self.assertEqual(o["passages"], 1)
        self.assertEqual([c["camera_id"] for c in o["by_camera"]], ["c1"])

    def test_hour_profile_follows_timezone(self):
        self.passage("c1", 1, 0)                       # 10:00 UTC, lundi
        self.assertEqual(self.ov()["by_hour"][10], 1)
        o = self.ov(tz_offset_min=120)                 # UTC+2 -> 12:00
        self.assertEqual(o["by_hour"][12], 1)
        self.assertEqual(o["by_weekday_hour"][0][12], 1)
        late = self.ov(tz_offset_min=14 * 60 + 100)    # borné à +14 h -> mardi 00:00
        self.assertEqual(late["by_weekday_hour"][1][0], 1)

    def test_output_is_anonymous(self):
        pid = self.person("Secretnom")
        self.passage("c1", 1, 0, mode="DB", pid=pid, label="Secretnom")
        import json
        blob = json.dumps(self.ov()) + "".join(stats.iter_csv(self.db, self.since, self.until))
        self.assertNotIn("Secretnom", blob)
        self.assertTrue(self.ov()["anonymous"])

    def test_anonymous_even_when_identities_visible(self):
        old = config.HISTORY_SHOW_IDENTITIES
        config.HISTORY_SHOW_IDENTITIES = True
        self.addCleanup(setattr, config, "HISTORY_SHOW_IDENTITIES", old)
        pid = self.person("Visible")
        self.passage("c1", 1, 0, mode="DB", pid=pid, label="Visible")
        self.assertNotIn("Visible", str(self.ov()))

    def test_zones_totals(self):
        z = Zone(camera_id="c1", kind="line", name="Porte", points="[[0,0],[1,1]]")
        other = Zone(camera_id="c2", kind="zone", name="Hall", points="[[0,0],[1,0],[1,1]]")
        self.db.add_all([z, other]); self.db.commit()
        for i in range(2):
            self.db.add(ZoneStat(zone_id=z.id, bucket=self.t0 + timedelta(minutes=5 * i), entries=3, exits=1, occupancy_max=i + 2))
        self.db.add(ZoneStat(zone_id=z.id, bucket=self.t0 - timedelta(days=3), entries=99))     # hors période
        self.db.commit()
        zs = {r["name"]: r for r in self.ov()["zones"]}
        self.assertEqual((zs["Porte"]["entries"], zs["Porte"]["exits"], zs["Porte"]["occupancy_max"]), (6, 2, 3))
        self.assertEqual(zs["Hall"]["entries"], 0)
        self.assertEqual([r["name"] for r in self.ov(camera_id="c2")["zones"]], ["Hall"])


class HeatmapTests(Base):
    def test_requires_camera(self):
        with self.assertRaises(stats.HeatmapError):
            stats.heatmap(self.db, self.since, self.until, "")

    def test_binning_and_clamping(self):
        w, h = config.STREAM_SIZE
        self.passage("c1", 1, 0, n=3, x=1, y=1)                 # coin haut-gauche
        self.passage("c1", 2, 0, n=2, x=w - 1, y=h - 1)         # coin bas-droit
        self.passage("c1", 3, 0, n=1, x=w * 3, y=-50)           # hors image : ramené au bord
        self.passage("c1", 4, 0, n=1, x=None, y=None)           # sans position : ignoré
        self.passage("c2", 5, 0, n=4, x=10, y=10)               # autre caméra : ignorée
        r = stats.heatmap(self.db, self.since, self.until, "c1", cols=4, rows=2)
        cells = {(c, r_): n for c, r_, n in r["cells"]}
        self.assertEqual(cells, {(0, 0): 3, (3, 1): 2, (3, 0): 1})
        self.assertEqual((r["total"], r["max"], r["cols"], r["rows"]), (6, 3, 4, 2))

    def test_grid_is_limited(self):
        r = stats.heatmap(self.db, self.since, self.until, "c1", cols=9999, rows=1)
        self.assertEqual((r["cols"], r["rows"]), (stats.MAX_GRID, 2))


class CompareTests(Base):
    def test_delta(self):
        # période B (référence) : 2 passages ; période A : 3
        b0 = self.t0 - timedelta(hours=2)
        for i in range(2):
            for k in range(3):
                self.db.add(Detection(camera_id="c1", track_id=i, mode="UNK", timestamp=b0 + timedelta(minutes=i * 5, seconds=k)))
        self.passage("c1", 1, 0); self.passage("c1", 2, 600); self.passage("c1", 3, 1200)
        r = stats.compare(self.db, self.t0 - timedelta(minutes=1), self.t0 + timedelta(hours=1),
                          b0 - timedelta(minutes=1), b0 + timedelta(hours=1))
        self.assertEqual((r["a"]["passages"], r["b"]["passages"]), (3, 2))
        self.assertEqual(r["delta"]["passages"], {"a": 3, "b": 2, "diff": 1, "pct": 50.0})

    def test_zero_reference_has_no_percentage(self):
        self.passage("c1", 1, 0)
        r = stats.compare(self.db, self.t0 - timedelta(minutes=1), self.t0 + timedelta(hours=1),
                          self.t0 - timedelta(days=2), self.t0 - timedelta(days=1))
        self.assertEqual(r["delta"]["passages"]["b"], 0)
        self.assertIsNone(r["delta"]["passages"]["pct"])
        self.assertIsNone(r["delta"]["avg_duration_sec"]["pct"])


class TransitionTests(Base):
    def test_enrolled_transitions_respect_gap(self):
        a, b = self.person("A"), self.person("B")
        # A : c1 -> c2 en 60 s (compté), puis c2 -> c3 après 40 min (trop long)
        self.passage("c1", 1, 0, n=3, mode="DB", pid=a, label="A")
        self.passage("c2", 1, 62, n=3, mode="DB", pid=a, label="A")
        self.passage("c3", 1, 3000, n=3, mode="DB", pid=a, label="A")
        # B : même caméra deux fois (pas de transition)
        self.passage("c1", 2, 0, n=3, mode="DB", pid=b, label="B")
        self.passage("c1", 3, 100, n=3, mode="DB", pid=b, label="B")
        # inconnu : jamais relié
        self.passage("c1", 9, 0, n=3); self.passage("c2", 9, 70, n=3)
        r = stats.transitions(self.db, self.since, self.until, max_gap_sec=600)
        self.assertEqual([(e["from"], e["to"], e["count"], e["enrolled"], e["target"]) for e in r["edges"]], [("c1", "c2", 1, 1, 0)])
        self.assertEqual(r["edges"][0]["median_gap_sec"], 60.0)
        wide = stats.transitions(self.db, self.since, self.until, max_gap_sec=99999)
        self.assertEqual(wide["max_gap_sec"], 3600)
        self.assertEqual({(e["from"], e["to"]) for e in wide["edges"]}, {("c1", "c2"), ("c2", "c3")})

    def test_target_moves(self):
        for cam, sec in (("c1", 0), ("c2", 30), ("c2", 40), ("c1", 90)):
            self.db.add(TargetMove(session=1, camera_id=cam, timestamp=self.t0 + timedelta(seconds=sec)))
        self.db.add(TargetMove(session=2, camera_id="c1", timestamp=self.t0))
        self.db.add(TargetMove(session=2, camera_id="c2", timestamp=self.t0 + timedelta(seconds=10)))
        self.db.commit()
        r = stats.transitions(self.db, self.since, self.until)
        got = {(e["from"], e["to"]): (e["count"], e["target"]) for e in r["edges"]}
        self.assertEqual(got, {("c1", "c2"): (2, 2), ("c2", "c1"): (1, 1)})

    def test_empty(self):
        r = stats.transitions(self.db, self.since, self.until)
        self.assertEqual((r["edges"], r["nodes"]), ([], []))


class CsvTests(Base):
    def test_csv_shape_and_injection(self):
        self.passage("=cmd", 1, 0, n=2)
        text = "".join(stats.iter_csv(self.db, self.since, self.until))
        rows = list(csv.reader(io.StringIO(text), delimiter=";"))
        self.assertEqual(rows[0], ["section", "cle", "valeur"])
        self.assertIn(["total", "passages", "1"], rows)
        self.assertTrue(all(not r[0].startswith("=") for r in rows))
        self.assertEqual(sum(1 for r in rows if r[0] == "heure"), 24)


if __name__ == "__main__":
    unittest.main()
