"""Calculs de la page Enregistrements (recordings-logic.js), exécutés avec Node. Ignoré si Node est absent."""
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
const L = globalThis.RecordingsLogic;
const t = [0, 0.5, 1.0, 3.0, 3.5];
console.log(JSON.stringify({
  bytes: [L.formatBytes(0), L.formatBytes(1536), L.formatBytes(5 * 1048576), L.formatBytes(3 * 1073741824), L.formatBytes(-1), L.formatBytes(null)],
  frame: [L.frameAt(t, -5), L.frameAt(t, 0), L.frameAt(t, 0.75), L.frameAt(t, 2.9), L.frameAt(t, 3.0), L.frameAt(t, 99), L.frameAt([], 1), L.frameAt(null, 1)],
  delay: [L.nextDelay(t, 0, 1), L.nextDelay(t, 2, 1), L.nextDelay(t, 2, 4), L.nextDelay(t, 3, 100), L.nextDelay(t, 4, 1), L.nextDelay(t, 0, 0), L.nextDelay([0, 0.001], 0, 1)],
  labels: [L.triggerLabel("event"), L.triggerLabel("zzz"), L.statusLabel("done"), L.statusLabel(null)],
  schedule: [L.scheduleLabel({camera_id: "*", days: [0, 6], start: "08:00", end: "18:00"}), L.scheduleLabel({camera_id: "c1", days: [9], start: "22:00", end: "06:00"}, "Entrée")],
  clock: [L.formatClock(0), L.formatClock(75.9), L.formatClock(-3), L.formatClock(undefined)],
  deep: [L.parseDeepLink("?id=12&t=34.5"), L.parseDeepLink("?id=abc&t=-1"), L.parseDeepLink(""), L.parseDeepLink("?id=1234567890"), L.parseDeepLink("?id=%3Cscript%3E")],
  link: [L.clipLink(12, 34.56), L.clipLink(0, 1), L.clipLink("12", 1), L.clipLink(5, -2), L.clipLink(5, undefined)],
  days: L.DAYS.length
}));
"""


@unittest.skipUnless(NODE, "Node.js absent : tests de recordings-logic.js ignorés")
class RecordingsLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "recordings-logic.js")], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_bytes(self):
        self.assertEqual(self.out["bytes"], ["0 o", "1.5 Ko", "5 Mo", "3 Go", "", ""])

    def test_frame_lookup(self):
        self.assertEqual(self.out["frame"], [0, 0, 1, 2, 3, 4, -1, -1])

    def test_delay_is_clamped_and_scaled(self):
        d = self.out["delay"]
        self.assertEqual(d[0], 500)
        self.assertEqual(d[1], 2000)      # 2 s plafonné
        self.assertEqual(d[2], 500)       # vitesse x4
        self.assertEqual(d[3], 30)        # plancher
        self.assertEqual(d[4], 0)         # dernière image
        self.assertEqual(d[5], 500)       # vitesse 0 -> 1
        self.assertEqual(d[6], 30)

    def test_labels(self):
        self.assertEqual(self.out["labels"], ["Sur événement", "zzz", "Terminé", ""])
        self.assertIn("toutes les caméras", self.out["schedule"][0])
        self.assertIn("lun., dim.", self.out["schedule"][0])
        self.assertIn("?", self.out["schedule"][1])
        self.assertIn("Entrée", self.out["schedule"][1])
        self.assertEqual(self.out["days"], 7)

    def test_clock(self):
        self.assertEqual(self.out["clock"], ["0:00", "1:15", "0:00", "0:00"])

    def test_deep_link_rejects_garbage(self):
        d = self.out["deep"]
        self.assertEqual(d[0], {"id": 12, "t": 34.5})
        self.assertEqual(d[1], {"id": None, "t": 0})
        self.assertEqual(d[2], {"id": None, "t": 0})
        self.assertEqual(d[3], {"id": None, "t": 0})
        self.assertEqual(d[4], {"id": None, "t": 0})

    def test_clip_link_numeric_only(self):
        self.assertEqual(self.out["link"], ["/recordings?id=12&t=34.6", "", "", "/recordings?id=5&t=0", "/recordings?id=5&t=0"])


if __name__ == "__main__":
    unittest.main()
