"""Calculs de la page Investigation (investigation-logic.js), exécutés avec Node. Ignoré si Node est absent."""
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
const L = globalThis.InvestigationLogic;
const out = {};
out.kind = [L.kindLabel("event"), L.kindLabel("passage"), L.kindLabel("move"), L.kindLabel("x"), L.kindLabel(null)];
out.query = [L.buildQuery({minutes:"60", types:[]}), L.buildQuery({minutes:"60", types:["targets","passage"], camera_id:"a b", q:" x&y ", only_alerts:true}),
             L.buildQuery({start:"2026-01-01T00:00:00.000Z", end:"2026-01-02T00:00:00.000Z", minutes:"60", participant_id:"3", session:"0"}), L.buildQuery(null)];
const since = "2026-01-01T10:00:00Z", until = "2026-01-01T11:00:00Z";
const items = [
  {key:"a", kind:"event", type:"camera_offline", severity:"warning", camera_id:"salle", ts:"2026-01-01T10:30:00Z", end_ts:null},
  {key:"b", kind:"passage", type:"passage", severity:"info", camera_id:"entre", ts:"2026-01-01T10:00:00Z", end_ts:"2026-01-01T10:30:00Z"},
  {key:"c", kind:"event", type:"zone_intrusion", severity:"critical", camera_id:null, ts:"2026-01-01T11:00:00Z", end_ts:null},
  {key:"d", kind:"event", type:"x", severity:"info", camera_id:"entre", ts:"2025-12-31T00:00:00Z", end_ts:null},
  {key:"e", kind:"event", type:"x", severity:"info", camera_id:"entre", ts:"pas une date", end_ts:null}];
out.layout = L.layout(items, since, until, 900, 20);
out.layoutEmpty = [L.layout([], since, until, 900), L.layout(items, until, since, 900), L.layout(null, "x", "y", 900)];
out.ticksHour = L.ticks(Date.parse(since), Date.parse(until), 900).length;
out.ticksDay = L.ticks(Date.parse("2026-01-01T00:00:00Z"), Date.parse("2026-01-08T00:00:00Z"), 900).length;
out.ticksBad = [L.ticks(5, 5, 900), L.ticks(0, 10, 0)];
out.toggle = [L.toggleType(["a"], "b", true), L.toggleType(["a","b"], "a", false), L.toggleType(["a"], "a", true), L.toggleType(null, "z", true)];
out.duration = [L.duration(0.2), L.duration(5), L.duration(125), L.duration(7500), L.duration(-1), L.duration(NaN)];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js absent : tests de investigation-logic.js ignorés")
class InvestigationLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "investigation-logic.js")], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_labels(self):
        self.assertEqual(self.out["kind"], ["Événement", "Passage", "Étape de cible", "x", ""])

    def test_query(self):
        q = self.out["query"]
        self.assertEqual(q[0], "?minutes=60")
        self.assertEqual(q[1], "?minutes=60&camera_id=a%20b&types=targets%2Cpassage&q=x%26y&only_alerts=true")
        self.assertEqual(q[2], "?start=2026-01-01T00%3A00%3A00.000Z&end=2026-01-02T00%3A00%3A00.000Z&participant_id=3&session=0")
        self.assertEqual(q[3], "")

    def test_layout_lanes_and_positions(self):
        lay = self.out["layout"]
        self.assertEqual([l["id"] for l in lay["lanes"]], ["entre", "salle", ""])       # « Autres » en dernier
        self.assertEqual(lay["height"], 60)
        marks = {m["key"]: m for m in lay["marks"]}
        self.assertEqual(set(marks), {"a", "b", "c", "d"})                              # date invalide ignorée
        self.assertEqual(marks["a"]["x"], 450); self.assertEqual(marks["a"]["y"], 30)
        self.assertEqual((marks["b"]["x"], marks["b"]["w"]), (0, 450))                  # un passage a une durée
        self.assertEqual(marks["c"]["x"], 900); self.assertEqual(marks["c"]["lane"], "")
        self.assertEqual(marks["d"]["x"], 0)                                            # hors période : bord
        self.assertGreaterEqual(marks["a"]["w"], 4)

    def test_layout_degenerate(self):
        for lay in self.out["layoutEmpty"]:
            self.assertEqual(lay, {"lanes": [], "marks": [], "height": 0})

    def test_ticks(self):
        self.assertTrue(4 <= self.out["ticksHour"] <= 11)
        self.assertTrue(4 <= self.out["ticksDay"] <= 11)
        self.assertEqual(self.out["ticksBad"], [[], []])

    def test_toggle_and_duration(self):
        self.assertEqual(self.out["toggle"], [["a", "b"], ["b"], ["a"], ["z"]])
        self.assertEqual(self.out["duration"], ["< 1 s", "5 s", "2 min 5 s", "2 h 5 min", "", ""])


if __name__ == "__main__":
    unittest.main()
