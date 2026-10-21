"""End to end through OpenSCAD. Skipped where OpenSCAD is not installed."""
import json
import tempfile
import unittest
from pathlib import Path

from datum import config, ops
from datum.parts import Store

HAS_OPENSCAD = config.find_openscad() is not None


@unittest.skipUnless(HAS_OPENSCAD, "OpenSCAD not installed")
class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_set_measure(self):
        result = ops.new(self.store, "probe")
        self.assertTrue(result["ok"], result["text"])
        vdir = Path(result["dir"])
        for name in ("model.stl", "view.html", "version.json", "report.json", "source.scad"):
            self.assertTrue((vdir / name).exists(), name)
        result = ops.set_params(self.store, "probe", {"hole_d": 6.5, "height": 12}, note="wider")
        self.assertTrue(result["ok"], result["text"])
        self.assertEqual(result["version"], "v002")
        self.assertEqual(result["report"]["bbox"]["size"][2], 12.0)
        cut = ops.section(self.store, "probe", "z", 6)
        holes = [lp for lp in cut["loops"] if lp["role"] == "hole"]
        self.assertEqual(len(holes), 1)
        self.assertAlmostEqual(holes[0]["diameter"], 6.5, delta=0.05)
        history = self.store.versions("probe")
        self.assertEqual(history[1]["changes"], ["hole_d: 5 -> 6.5", "height: 10 -> 12"])

    def test_failed_build_keeps_current(self):
        ops.new(self.store, "probe")
        source = self.store.source("probe")
        source.write_text(source.read_text(encoding="utf-8") + "\nthis is not openscad (\n", encoding="utf-8")
        result = ops.build(self.store, "probe", note="broken")
        self.assertFalse(result["ok"])
        self.assertEqual(self.store.load("probe")["current"], "v001")
        meta = json.loads((Path(result["dir"]) / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["errors"])

    def test_includes_come_along(self):
        src = Path(self.tmp.name) / "src"
        (src / "lib").mkdir(parents=True)
        (src / "lib" / "shapes.scad").write_text("module peg(d) { cylinder(d = d, h = 5, $fn = 48); }\n", encoding="utf-8")
        (src / "main.scad").write_text("use <lib/shapes.scad>\nd = 4; // peg\npeg(d);\n", encoding="utf-8")
        result = ops.new(self.store, "peg", source=str(src / "main.scad"))
        self.assertTrue(result["ok"], result["text"])
        self.assertEqual(result["part"]["copied_includes"], ["lib/shapes.scad"])


if __name__ == "__main__":
    unittest.main()
