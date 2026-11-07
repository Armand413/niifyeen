"""Logique du tracé sur le plan (plan.js), exécutée avec Node. Ignoré si Node est absent."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

PLAN_JS = Path(__file__).resolve().parents[1] / "backend" / "app" / "static" / "plan.js"
NODE = shutil.which("node")

SCRIPT = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const P = globalThis.SitePlan;
const meta = {a:{label:"A",x:.1,y:.7}, b:{label:"B",x:.6,y:.3}, c:{label:"C",x:.9,y:.7}};
const out = {};

out.visits = P.visits([{camera_id:"a"},{camera_id:"a"},{camera_id:"b"},{camera_id:"a"},{},null]).map(v => v.camera_id);

const abc = P.buildSegments([{camera_id:"a",ts:1},{camera_id:"b",ts:2},{camera_id:"c",ts:3}], meta);
out.abc = {n: abc.segments.length, steps: abc.segments.map(s => s.step), numbers: abc.numbers, last: abc.last};

const back = P.buildSegments([{camera_id:"a"},{camera_id:"b"},{camera_id:"a"}], meta);
out.back = {numbers: back.numbers, curves: back.segments.map(s => s.d)};

out.unknown = P.buildSegments([{camera_id:"zzz"},{camera_id:"a"}], meta).segments.length;
out.none = P.buildSegments([], meta);

const long = [];
for (let i = 0; i < 60; i++) long.push({camera_id: ["a","b","c"][i % 3], ts: i});
const lg = P.buildSegments(long, meta, 20);
out.long = {n: lg.segments.length, firstStep: lg.segments[0].step, lastStep: lg.segments[lg.segments.length-1].step};

const mk = (id, label, active, cam, path, score) => ({id, label, color:"#d92d20", active, camera_id: cam, score, path});
out.status = [
  P.statusText(meta, [mk(1,"Cible 1",true,"b",[{camera_id:"a"},{camera_id:"b"}],.5)]),
  P.statusText(meta, [mk(1,"Cible 1",false,null,[{camera_id:"a"},{camera_id:"c"}])]),
  P.statusText(meta, []),
  P.statusText(meta, [mk(1,"Cible 1",false,null,[]), mk(2,"Cible 2",true,"a",[{camera_id:"a"}],.4)]),
  P.statusText(meta, [mk(1,"Cible 1",false,null,[])]),
].map(s => [s.kind, s.text]);
const spread = P.buildSegments([{camera_id:"a"},{camera_id:"b"}], meta, 20, 0).segments[0].d;
const wider = P.buildSegments([{camera_id:"a"},{camera_id:"b"}], meta, 20, 11).segments[0].d;
out.bend = [spread, wider];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js introuvable")
class PlanJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run = subprocess.run([NODE, "-e", SCRIPT, str(PLAN_JS)], capture_output=True, text=True, timeout=30)
        if run.returncode != 0:
            raise AssertionError(run.stderr)
        cls.r = json.loads(run.stdout)

    def test_consecutive_duplicates_and_invalid_entries_are_dropped(self):
        self.assertEqual(self.r["visits"], ["a", "b", "a"])

    def test_path_through_three_cameras(self):
        abc = self.r["abc"]
        self.assertEqual(abc["n"], 2)
        self.assertEqual(abc["steps"], [2, 3])
        self.assertEqual(abc["numbers"], {"a": [1], "b": [2], "c": [3]})
        self.assertEqual(abc["last"], "c")

    def test_revisited_camera_gets_every_step_number(self):
        self.assertEqual(self.r["back"]["numbers"]["a"], [1, 3])

    def test_opposite_moves_use_different_curves(self):
        d1, d2 = self.r["back"]["curves"]
        self.assertNotEqual(d1, d2)

    def test_unknown_cameras_are_ignored(self):
        self.assertEqual(self.r["unknown"], 0)
        self.assertEqual(self.r["none"]["segments"], [])
        self.assertIsNone(self.r["none"]["last"])

    def test_only_recent_moves_are_drawn_but_numbering_stays_absolute(self):
        long = self.r["long"]
        self.assertEqual(long["n"], 20)
        self.assertEqual(long["lastStep"], 60)
        self.assertEqual(long["firstStep"], 41)

    def test_status_messages(self):
        active, lost, none, mixed, unseen = self.r["status"]
        self.assertEqual(active[0], "active")
        self.assertIn("Cible 1", active[1])
        self.assertIn("« B »", active[1])
        self.assertEqual(lost[0], "lost")
        self.assertIn("« C »", lost[1])
        self.assertEqual(none, ["idle", "Aucune cible définie."])
        self.assertEqual(mixed[0], "active")
        self.assertTrue(mixed[1].startswith("2 cibles"))
        self.assertIn("Cible 1 : pas encore repérée", mixed[1])
        self.assertIn("Cible 2 identifiée", mixed[1])
        self.assertEqual(unseen[0], "idle")

    def test_two_targets_on_the_same_route_do_not_overlap(self):
        base, wider = self.r["bend"]
        self.assertNotEqual(base, wider)


if __name__ == "__main__":
    unittest.main()
