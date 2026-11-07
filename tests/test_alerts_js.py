"""Calculs de la page Alertes (alerts-logic.js) et choix du son (shell.js), exécutés avec Node. Ignoré si Node est absent."""
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
eval(fs.readFileSync(process.argv[2], "utf8"));
const L = globalThis.AlertsLogic, S = globalThis.Shell;
const out = {};
out.labels = [L.priorityLabel("critical"), L.priorityLabel("zzz"), L.priorityLabel(undefined), L.statusLabel("acknowledged"), L.statusLabel("new")];
out.classes = [L.priorityClass("high"), L.priorityClass("nope"), L.priorityClass(null)];
out.query = [L.buildQuery({}), L.buildQuery({status:"open", q:"  hall & co ", priority:"", limit:50}), L.buildQuery({camera_id:"a/b c", since:"2026-01-01T00:00:00.000Z"}), L.buildQuery(null)];
out.actions = ["new","seen","acknowledged","closed","x"].map(s => L.actionsFor(s).map(a => a[0]));
const now = Date.parse("2026-01-01T12:00:00Z");
out.age = ["2026-01-01T11:59:50Z","2026-01-01T11:55:00Z","2026-01-01T09:00:00Z","2025-12-30T12:00:00Z","2026-01-01T12:05:00Z","pas une date"].map(d => L.ageText(d, now));
out.toggle = [L.toggleId([1,2], 3, true), L.toggleId([1,2,3], 2, false), L.toggleId([1], 1, true), L.toggleId(null, 5, true)];
out.sound = [S.soundFor([], true), S.soundFor([{priority:"critical", sound:true}], false), S.soundFor([{priority:"critical", sound:false}], true),
             S.soundFor([{priority:"low", sound:true},{priority:"high", sound:true}], true), S.soundFor([{priority:"critical", sound:true}], true),
             S.soundFor([{priority:"medium", sound:true}], true), S.soundFor([{priority:"bogus", sound:true}], true), S.soundFor(null, true)];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js absent : tests de alerts-logic.js ignorés")
class AlertsLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "alerts-logic.js"), str(STATIC / "shell.js")],
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_labels_and_classes(self):
        self.assertEqual(self.out["labels"], ["Critique", "zzz", "", "Acquittée", "Nouvelle"])
        self.assertEqual(self.out["classes"], ["p-high", "p-low", "p-low"])

    def test_query_is_encoded_and_skips_empty_values(self):
        q = self.out["query"]
        self.assertEqual(q[0], "")
        self.assertEqual(q[1], "?status=open&q=hall%20%26%20co&limit=50")
        self.assertEqual(q[2], "?camera_id=a%2Fb%20c&since=2026-01-01T00%3A00%3A00.000Z")
        self.assertEqual(q[3], "")

    def test_actions_only_go_forward_except_reopen(self):
        self.assertEqual(self.out["actions"], [["seen", "acknowledged", "closed"], ["acknowledged", "closed"], ["closed"], ["seen"], []])

    def test_age(self):
        self.assertEqual(self.out["age"], ["à l'instant", "il y a 5 min", "il y a 3 h", "il y a 2 j", "à l'instant", ""])

    def test_toggle(self):
        self.assertEqual(self.out["toggle"], [[1, 2, 3], [1, 3], [1], [5]])

    def test_sound_follows_the_highest_audible_priority(self):
        s = self.out["sound"]
        self.assertIsNone(s[0]); self.assertIsNone(s[1]); self.assertIsNone(s[2]); self.assertIsNone(s[6]); self.assertIsNone(s[7])
        self.assertEqual(s[3], {"freq": 880, "count": 2})
        self.assertEqual(s[4], {"freq": 1040, "count": 3})
        self.assertEqual(s[5], {"freq": 660, "count": 1})


if __name__ == "__main__":
    unittest.main()
