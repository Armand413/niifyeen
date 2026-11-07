"""Calculs de la page Statistiques (statistics-logic.js), exécutés avec Node. Ignoré si Node est absent."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "backend" / "app" / "static"
NODE = shutil.which("node")

SCRIPT = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const L = globalThis.StatisticsLogic;
console.log(JSON.stringify({
  dur: [L.formatDuration(0), L.formatDuration(42.4), L.formatDuration(59.7), L.formatDuration(61), L.formatDuration(125.2), L.formatDuration(null), L.formatDuration(-3), L.formatDuration("x")],
  delta: [L.formatDelta({pct: 50, diff: 1}), L.formatDelta({pct: -12.5}), L.formatDelta({pct: 0}), L.formatDelta({pct: null, diff: 3}), L.formatDelta({pct: null, diff: 0}), L.formatDelta(null), L.formatDelta({pct: null, diff: null})],
  intensity: [L.intensity(0, 10), L.intensity(10, 10), L.intensity(2.5, 10), L.intensity(50, 10), L.intensity(5, 0), L.intensity(-1, 5)],
  color: [L.heatColor(0, 10), L.heatColor(10, 10), L.heatColor(1, 100)],
  bars: [L.bars([], 100, 50), L.bars(null, 100, 50), L.bars([0, 0], 100, 50), L.bars([1, 4, 0, 1000], 40, 100)],
  graph: [L.graph([], [], 360, 260), L.graph([{camera_id: "a"}], [], 360, 260),
          L.graph([{camera_id: "a"}], [{from: "a", to: "b", count: 2}, {from: "b", to: "a", count: 4}], 360, 260)],
  query: [L.query(60, "", 120), L.query(60, "c 1&x", NaN), L.query(10, "a", -90.4)],
  tz: [L.tzOffsetFromDate(new Date(2026, 0, 5, 12, 0, 0))],
  periods: L.PERIODS.map(p => p.minutes), week: L.WEEKDAYS.length
}));
"""


@unittest.skipUnless(NODE, "Node.js absent : tests de statistics-logic.js ignorés")
class StatisticsLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "statistics-logic.js")], capture_output=True, text=True, timeout=30,
                              env={"TZ": "Etc/GMT-2", "PATH": "/usr/bin:/bin:/usr/local/bin"})
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_duration(self):
        self.assertEqual(self.out["dur"], ["0 s", "42 s", "1 min 00 s", "1 min 01 s", "2 min 05 s", "—", "—", "—"])

    def test_delta(self):
        self.assertEqual(self.out["delta"], ["+50 %", "−12,5 %", "=", "nouveau", "n/a", "n/a", "n/a"])

    def test_intensity_and_color(self):
        self.assertEqual(self.out["intensity"], [0, 1, 0.5, 1, 0, 0])
        c = self.out["color"]
        self.assertEqual(c[0], "rgba(0,0,0,0)")
        self.assertEqual(c[1], "rgba(255,50,30,0.90)")
        self.assertTrue(c[2].startswith("rgba(255,") and c[2] != c[0])

    def test_bars(self):
        e, n, z, b = self.out["bars"]
        self.assertEqual((e, n), ({"max": 0, "bars": []}, {"max": 0, "bars": []}))
        self.assertTrue(all(x["h"] == 0 for x in z["bars"]))
        self.assertEqual([x["h"] for x in b["bars"]], [1, 1, 0, 100])        # une valeur non nulle reste visible
        self.assertTrue(all(x["y"] + x["h"] == 100 for x in b["bars"]))

    def test_graph(self):
        empty, single, pair = self.out["graph"]
        self.assertEqual((empty["nodes"], empty["edges"]), ([], []))
        self.assertEqual(single["nodes"], [{"id": "a", "x": 180, "y": 130}])
        self.assertEqual([n["id"] for n in pair["nodes"]], ["a", "b"])
        ab, ba = pair["edges"]
        self.assertGreater(ba["width"], ab["width"])
        self.assertNotEqual(ab["d"], ba["d"])                                   # les deux sens ne se superposent pas
        for n in pair["nodes"]:
            self.assertTrue(0 <= n["x"] <= 360 and 0 <= n["y"] <= 260)

    def test_query_is_encoded(self):
        self.assertEqual(self.out["query"], ["minutes=60&tz_offset_min=120", "minutes=60&camera_id=c%201%26x", "minutes=10&camera_id=a&tz_offset_min=-90"])

    def test_timezone_sign(self):
        self.assertEqual(self.out["tz"], [120])                                  # UTC+2 -> +120

    def test_constants(self):
        self.assertEqual(self.out["periods"], [60, 1440, 10080, 43200])
        self.assertEqual(self.out["week"], 7)


if __name__ == "__main__":
    unittest.main()
