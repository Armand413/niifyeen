"""Calculs de la page Carte (map-logic.js), exécutés avec Node. Ignoré si Node est absent."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "backend" / "app" / "static"
NODE = shutil.which("node")

SCRIPT = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const L = globalThis.MapLogic;
const out = {};
out.size = [L.planSize(900, 510), L.planSize(2000, 1000), L.planSize(100, 1000), L.planSize(1000, 10), L.planSize(0, 5), L.planSize(null, null)];
out.image = [L.safeImage("/api/floors/3/image"), L.safeImage("http://evil.test/a.png"), L.safeImage("/api/floors/x/image"),
             L.safeImage("//evil.test/api/floors/3/image"), L.safeImage("javascript:alert(1)"), L.safeImage(undefined)];
const meta = {a:{floor_id:1,placed:true}, b:{floor_id:2,placed:true}, c:{floor_id:1,placed:false}, d:{floor_id:null,placed:false}};
out.on1 = Object.keys(L.camerasOn(meta, 1));
out.on2 = Object.keys(L.camerasOn(meta, 2));
out.onDefault = Object.keys(L.camerasOn(meta, null));
out.unplaced = [L.unplaced(meta, true), L.unplaced(meta, false)];
out.wedge = [L.wedgePath(100,100,50,0,90), L.wedgePath(100,100,50,90,90), L.wedgePath(100,100,50,0,270), L.wedgePath(100,100,50,0,360),
             L.wedgePath(100,100,0,0,90), L.wedgePath(100,100,50,0,0)];
out.frac = [L.toFraction({left:10,top:20,width:200,height:100}, 110, 70), L.toFraction({left:10,top:20,width:200,height:100}, -50, 500),
            L.toFraction({left:0,top:0,width:0,height:0}, 1, 1), L.toFraction(null, 1, 1)];
out.heading = [L.headingFrom(0,0,0,-10), L.headingFrom(0,0,10,0), L.headingFrom(0,0,0,10), L.headingFrom(0,0,-10,0), L.headingFrom(0,0,10,-10)];
const t1 = {moves:[{camera_id:"a",ts:0},{camera_id:"b",ts:10},{camera_id:"b",ts:12},{camera_id:"a",ts:20}]};
const t2 = {moves:[{camera_id:"b",ts:100},{camera_id:"a",ts:130}]};
out.analysis = L.analyse([t1, t2, {moves:[]}, {}, {moves:[null, {camera_id:"x"}, {ts:5}]}]);
out.empty = L.analyse([]);
out.unsorted = L.analyse([{moves:[{camera_id:"b",ts:10},{camera_id:"a",ts:0}]}]).transitions;
out.span = [L.span(t1.moves), L.span([]), L.span(null)];
out.until = [L.movesUntil(t1.moves, -1).length, L.movesUntil(t1.moves, 10).length, L.movesUntil(t1.moves, 999).length];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "Node.js absent : tests de map-logic.js ignorés")
class MapLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "-e", SCRIPT, str(STATIC / "map-logic.js")], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_plan_size_keeps_proportions_within_bounds(self):
        s = self.out["size"]
        self.assertEqual(s[0], {"width": 900, "height": 510})
        self.assertEqual(s[1], {"width": 900, "height": 450})
        self.assertEqual(s[2]["height"], 1800)                  # plan très haut : borné
        self.assertEqual(s[3]["height"], 300)                   # plan très large : borné
        self.assertEqual(s[4], {"width": 900, "height": 510})
        self.assertEqual(s[5], {"width": 900, "height": 510})

    def test_only_our_own_image_routes_are_accepted(self):
        self.assertEqual(self.out["image"], ["/api/floors/3/image"] + ["/static/site-plan.svg"] * 5)

    def test_cameras_are_filtered_by_floor(self):
        self.assertEqual(self.out["on1"], ["a"])
        self.assertEqual(self.out["on2"], ["b"])
        self.assertEqual(self.out["onDefault"], ["a", "b", "c", "d"])
        self.assertEqual(self.out["unplaced"], [["c", "d"], []])

    def test_field_of_view_geometry(self):
        w = self.out["wedge"]
        self.assertEqual(w[0], "M100 100L64.6 64.6A50 50 0 0 1 135.4 64.6Z")          # vers le haut
        self.assertEqual(w[1], "M100 100L135.4 64.6A50 50 0 0 1 135.4 135.4Z")        # vers la droite
        self.assertIn(" 0 1 1 ", w[2])                                                 # > 180° : grand arc
        self.assertTrue(w[3].startswith("M50 100a50 50"))                              # 360° : disque
        self.assertEqual(w[4:], ["", ""])

    def test_pointer_to_fraction_is_clamped(self):
        f = self.out["frac"]
        self.assertEqual(f[0], {"x": 0.5, "y": 0.5})
        self.assertEqual(f[1], {"x": 0, "y": 1})
        self.assertEqual(f[2:], [None, None])

    def test_heading_from_pointer(self):
        self.assertEqual(self.out["heading"], [0, 90, 180, 270, 45])

    def test_analysis_counts_visits_dwell_and_transitions(self):
        a = self.out["analysis"]
        self.assertEqual(a["trajectories"], 2)
        cams = {c["camera_id"]: c for c in a["cameras"]}
        self.assertEqual((cams["a"]["visits"], cams["a"]["dwell_sec"]), (3, 10))
        self.assertEqual((cams["b"]["visits"], cams["b"]["dwell_sec"]), (3, 2 + 8 + 30))
        trans = {(t["from"], t["to"]): t["count"] for t in a["transitions"]}
        self.assertEqual(trans, {("a", "b"): 1, ("b", "a"): 2})
        self.assertNotIn("x", cams)                                                    # étape sans heure ignorée

    def test_analysis_edge_cases(self):
        self.assertEqual(self.out["empty"], {"trajectories": 0, "cameras": [], "transitions": []})
        self.assertEqual(self.out["unsorted"], [{"from": "a", "to": "b", "count": 1}])   # remis dans l'ordre du temps

    def test_replay_helpers(self):
        self.assertEqual(self.out["span"], [[0, 20], None, None])
        self.assertEqual(self.out["until"], [0, 2, 4])

    def test_no_dom_injection_in_map_scripts(self):
        for name in ("map.js", "map-logic.js"):
            self.assertIsNone(re.search(r"innerHTML|insertAdjacentHTML|document\.write", (STATIC / name).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
