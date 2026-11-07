"""Calculs de la page Lignes et zones (zones-logic.js), exécutés avec Node. Ignoré si Node est absent."""
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
const L = globalThis.ZonesLogic;
const out = {};
out.arrow = [L.sideArrow([0,0],[1,0],0.1), L.sideArrow([0,0],[0,1],0.1), L.sideArrow([0.5,0.5],[0.5,0.5])];
out.centroid = [L.centroid([[0,0],[1,0],[1,1],[0,1]]), L.centroid([]), L.centroid(null)];
out.finish = [L.canFinish("line",[[0,0],[1,1]]), L.canFinish("line",[[0,0]]), L.canFinish("zone",[[0,0],[1,0]]),
              L.canFinish("zone",[[0,0],[1,0],[1,1]]), L.canFinish("zone", new Array(13).fill([0,0])), L.canFinish("zone", null)];
let line = L.addPoint("line", [], [0.1,0.2]);
line = L.addPoint("line", line, [0.1005,0.2005]);
line = L.addPoint("line", line, [0.8,0.9]);
line = L.addPoint("line", line, [0.5,0.5]);
out.line = line;
let zone = [];
for (let i = 0; i < 20; i++) zone = L.addPoint("zone", zone, [i/20, (i%2)/2]);
out.zoneLen = zone.length;
out.bars = L.bars([{bucket:"a",entries:2},{bucket:"b",entries:4},{bucket:"c"}], "entries", 90, 40);
out.barsEmpty = [L.bars([], "entries", 90, 40), L.bars(null, "entries", 90, 40), L.bars([{x:0}], "entries", 90, 40).bars[0].h];
out.label = [L.bucketLabel("garbage"), L.bucketLabel("2026-01-01T10:05:00").length];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js absent : tests de zones-logic.js ignorés")
class ZonesLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "zones-logic.js")], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_arrow_points_to_side_one(self):
        a = self.out["arrow"]
        self.assertAlmostEqual(a[0]["normal"][0], 0); self.assertAlmostEqual(a[0]["normal"][1], 1)
        self.assertAlmostEqual(a[1]["normal"][0], -1); self.assertAlmostEqual(a[1]["normal"][1], 0)
        self.assertAlmostEqual(a[0]["to"][1], 0.1)
        self.assertIsNone(a[2])

    def test_centroid(self):
        c = self.out["centroid"]
        self.assertEqual(c[0], [0.5, 0.5]); self.assertIsNone(c[1]); self.assertIsNone(c[2])

    def test_can_finish_rules(self):
        self.assertEqual(self.out["finish"], [True, False, False, True, False, False])

    def test_add_point_limits_and_dedup(self):
        self.assertEqual(self.out["line"], [[0.1, 0.2], [0.8, 0.9]])
        self.assertEqual(self.out["zoneLen"], 12)

    def test_bars(self):
        b = self.out["bars"]
        self.assertEqual(b["max"], 4)
        self.assertEqual([x["h"] for x in b["bars"]], [20, 40, 0])
        self.assertEqual(b["bars"][1]["y"], 0)
        self.assertEqual(self.out["barsEmpty"][0], {"bars": [], "max": 0})
        self.assertEqual(self.out["barsEmpty"][1], {"bars": [], "max": 0})
        self.assertEqual(self.out["barsEmpty"][2], 0)

    def test_bucket_label(self):
        self.assertEqual(self.out["label"], ["", 5])


if __name__ == "__main__":
    unittest.main()
