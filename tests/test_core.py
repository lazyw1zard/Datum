"""Tests that need no OpenSCAD: parameters, sections, 3MF and slicer templates, the viewer, the MCP protocol,
parts made from meshes."""
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import trimesh

from datum import NAME, mcp, mesh, ops, scad, three_mf, viewer
from datum.parts import PartError, Store

SOURCE = """// demo
D = 25;             // outer diameter
L_BODY = 20.5;
L = L_BODY + 1.5;   // derived
name = "втулка; v2"; // a string with ; and //
on = true;
v = [1, 2.5, -.5];
$fn = 64;
module m(x = 3) { inner = 7; cylinder(d = x); }
difference() { cylinder(d = D, h = L); }
"""


class Params(unittest.TestCase):
    def test_parse(self):
        params = {p.name: p for p in scad.parse_params(SOURCE)}
        self.assertEqual(params["D"].value, 25)
        self.assertEqual(params["D"].comment, "outer diameter")
        self.assertFalse(params["L"].literal)
        self.assertEqual(params["name"].value, "втулка; v2")
        self.assertEqual(params["v"].value, [1, 2.5, -0.5])
        self.assertIs(params["on"].value, True)
        self.assertNotIn("inner", params)          # inside a module
        self.assertNotIn("x", params)

    def test_set_keeps_comment_and_rest(self):
        text, notes = scad.set_params(SOURCE, {"D": 26.5, "on": False, "name": 'a "q"'})
        params = {p.name: p for p in scad.parse_params(text)}
        self.assertEqual(params["D"].value, 26.5)
        self.assertEqual(params["D"].comment, "outer diameter")
        self.assertIs(params["on"].value, False)
        self.assertEqual(params["name"].value, 'a "q"')
        self.assertEqual(len(notes), 3)
        self.assertEqual(text.count("\n"), SOURCE.count("\n"))

    def test_set_refuses_derived_and_unknown(self):
        with self.assertRaises(ValueError):
            scad.set_params(SOURCE, {"L": 30})
        with self.assertRaises(KeyError):
            scad.set_params(SOURCE, {"nope": 1})
        text, _ = scad.set_params(SOURCE, {"L": 30}, force=True)
        self.assertIn("L = 30;", text)

    def test_cli_values(self):
        self.assertEqual(scad.parse_cli_value("8.3"), 8.3)
        self.assertEqual(scad.parse_cli_value(".5"), 0.5)
        self.assertEqual(scad.parse_cli_value("[1,2]"), [1, 2])
        self.assertEqual(scad.parse_cli_value("cut"), "cut")
        self.assertEqual(scad.parse_cli_value('"cut"'), "cut")


def tube(outer=25.0, inner=5.0, height=10.0, sections=128):
    return trimesh.creation.annulus(r_min=inner / 2, r_max=outer / 2, height=height, sections=sections)


class Sections(unittest.TestCase):
    def test_round_hole_and_outline(self):
        cut = mesh.section(tube(), "z", 0.0)
        roles = sorted((lp["role"], lp["kind"]) for lp in cut["loops"])
        self.assertEqual(roles, [("hole", "circle"), ("outer", "circle")])
        outer, hole = cut["loops"]
        self.assertAlmostEqual(outer["diameter"], 25.0, delta=0.05)
        self.assertAlmostEqual(hole["diameter"], 5.0, delta=0.05)

    def test_hex(self):
        hex_prism = trimesh.creation.cylinder(radius=8.3 / 2 / np.cos(np.pi / 6), height=4, sections=6)
        cut = mesh.section(hex_prism, "z", 0.5)
        loop = cut["loops"][0]
        self.assertEqual(loop["kind"], "6-gon")
        self.assertAlmostEqual(loop["across_flats"], 8.3, delta=0.01)
        self.assertIn("S8.3", mesh.describe_loop(loop))
        self.assertIn("<svg", mesh.section_svg(cut))

    def test_lengthwise_cut_reports_walls_by_size(self):
        """A cut along the axis gives two walls; 'diameters from the centre' make no sense for them."""
        cut = mesh.section(tube(height=40), "y", 0.0)
        self.assertEqual(len(cut["loops"]), 2)
        for loop in cut["loops"]:
            self.assertEqual(loop["kind"], "outline")
            self.assertNotIn("d_inner", loop)
            self.assertAlmostEqual(loop["size"][1], 40.0, delta=0.01)
            self.assertNotIn("от центра", mesh.describe_loop(loop))

    def test_plane_on_a_face_is_nudged(self):
        box = trimesh.creation.box(extents=[10, 10, 10])
        cut = mesh.section(box, "z", 5.0 - 5.0)    # through the middle
        self.assertEqual(len(cut["loops"]), 1)
        top = mesh.section(box, "z", -5.0)          # exactly on the bottom face
        self.assertLessEqual(len(top["loops"]), 1)

    def test_report(self):
        rep = mesh.report(tube())
        self.assertTrue(rep["watertight"])
        self.assertEqual(rep["bodies"], 1)
        self.assertEqual(rep["bbox"]["size"], [25.0, 25.0, 10.0])


class ThreeMF(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.3mf"
            original = tube()
            three_mf.write_mesh(path, original.vertices, original.faces, name="t & <x>")
            loaded = mesh.load(path)
            self.assertTrue(np.allclose(loaded.bounds, original.bounds, atol=1e-4))

    def test_components_and_transforms(self):
        """Slicers (QIDI, Bambu) put meshes in separate model files and reference them as components."""
        ns, prod = three_mf.CORE_NS, three_mf.PROD_NS
        part = (f'<model xmlns="{ns}" unit="millimeter"><resources><object id="1" type="model"><mesh><vertices>'
                '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>'
                '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>')
        root = (f'<model xmlns="{ns}" xmlns:p="{prod}" unit="centimeter"><resources>'
                '<object id="2" type="model"><components>'
                '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 10 0 0"/>'
                '</components></object></resources>'
                '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 0 0 5"/></build></model>')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("3D/3dmodel.model", root)
                z.writestr("3D/Objects/object_1.model", part)
            v, f = three_mf.read_mesh(path)
            self.assertEqual(f.shape, (1, 3))
            # translated by (10, 0, 5) in centimetres -> millimetres
            self.assertTrue(np.allclose(v.min(axis=0), [100, 0, 50]))


def slicer_project(path: Path, items: int = 1) -> None:
    """A project the way QIDI/Bambu save it: the mesh in its own model file, centred, under a component;
    the item squashed to half height and placed on the plate; settings beside it."""
    ns, prod = three_mf.CORE_NS, three_mf.PROD_NS
    box = trimesh.creation.box(extents=[10, 10, 2])
    verts = "".join(f'<vertex x="{x:g}" y="{y:g}" z="{z:g}"/>' for x, y, z in box.vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in box.faces)
    part = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{ns}" xmlns:p="{prod}">\n'
            ' <metadata name="QIDIStudio:3mfVersion">1</metadata>\n <resources>\n'
            f'  <object id="1" p:UUID="00010000-aaaa" type="model">\n   <mesh><vertices>{verts}</vertices>'
            f'<triangles>{tris}</triangles></mesh>\n  </object>\n </resources>\n <build/>\n</model>')
    item = '<item objectid="2" transform="1 0 0 0 1 0 0 0 0.5 100 100 0.5" printable="1"/>'
    root = (f'<model unit="millimeter" xmlns="{ns}" xmlns:p="{prod}" requiredextensions="p"><resources>'
            '<object id="2" type="model"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
            f'</components></object></resources><build>{item * items}</build></model>')
    settings = ('<config><object id="2"><metadata face_count="12"/><part id="1">'
                '<metadata key="source_offset_x" value="0"/><metadata key="source_offset_y" value="80"/>'
                '<metadata key="source_offset_z" value="1"/><mesh_stat face_count="12" edges_fixed="0"/>'
                '</part></object></config>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{three_mf.MODEL_REL_TYPE}"/>'
                   '</Relationships>')
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/object_1.model", part)
        z.writestr("Metadata/model_settings.config", settings)
        z.writestr("Metadata/project_settings.config", '{"layer_height": "0.2", "brim_width": "5"}')


class Template(unittest.TestCase):
    def test_mesh_goes_into_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, out = Path(tmp) / "project.3mf", Path(tmp) / "out.3mf"
            slicer_project(project)
            new = tube()                                     # 25 × 25 × 10, centred on the origin
            info = three_mf.replace_mesh(project, out, new.vertices, new.faces)
            self.assertEqual(info["scale"], [1.0, 1.0, 0.5])
            self.assertTrue(info["print_settings"])
            with zipfile.ZipFile(project) as a, zipfile.ZipFile(out) as b:
                self.assertEqual(a.namelist(), b.namelist())
                self.assertEqual(a.read("Metadata/project_settings.config"), b.read("Metadata/project_settings.config"))
                model = b.read("3D/Objects/object_1.model").decode("utf-8")
                config = b.read("Metadata/model_settings.config").decode("utf-8")
            self.assertIn('<metadata name="QIDIStudio:3mfVersion">1</metadata>', model)
            self.assertIn('p:UUID="00010000-aaaa"', model)
            # on the same spot of the bed: bottom on the plate, centre at (100, 100), the human's half height kept
            v, f = three_mf.read_mesh(out)
            self.assertEqual(len(f), len(new.faces))
            self.assertTrue(np.allclose(v.min(axis=0), [87.5, 87.5, 0.0], atol=1e-4))
            self.assertTrue(np.allclose(v.max(axis=0), [112.5, 112.5, 5.0], atol=1e-4))
            self.assertEqual(config.count(f'face_count="{len(new.faces)}"'), 2)
            self.assertIn('key="source_offset_z" value="-4"', config)     # mesh z -5 moved to the old bottom, -1
            self.assertIn('key="source_offset_y" value="0"', config)

    def test_one_object_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "two.3mf"
            slicer_project(project, items=2)
            with self.assertRaises(ValueError):
                three_mf.replace_mesh(project, Path(tmp) / "out.3mf", tube().vertices, tube().faces)


def _unvarints(b64: str, n: int) -> np.ndarray:
    import base64
    import gzip
    data, out, i = gzip.decompress(base64.b64decode(b64)), [], 0
    while len(out) < n:
        x = shift = 0
        while True:
            b = data[i]
            i += 1
            x |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        out.append((x >> 1) ^ -(x & 1))
    return np.array(out)


class Viewer(unittest.TestCase):
    def test_self_contained(self):
        m = tube()
        html = viewer.build_html(m.vertices, m.faces, {"part": "t", "version": "v001", "title": "Труба </script>"})
        self.assertNotIn("https://", html)
        self.assertLess(len(html.encode("utf-8")), viewer.PANEL_LIMIT)
        data = re.search(r'<script type="application/json" id="datum-data">(.*?)</script>', html, re.S).group(1)
        payload = json.loads(data.replace("<\\/", "</"))
        self.assertEqual(payload["meta"]["title"], "Труба </script>")

    def test_mesh_packing_is_lossless_enough(self):
        """Every triangle kept; vertices within a micron after 16-bit quantization."""
        m = tube()
        e = viewer.encode_mesh(m.vertices, m.faces)
        nv, nf = e["vertices"], e["triangles"]
        self.assertEqual(nf, len(m.faces))
        pos = np.array(e["lo"]) + _unvarints(e["v"], nv * 3).reshape(3, nv).cumsum(axis=1).T * np.array(e["span"]) / 65535
        idx = _unvarints(e["f"], nf * 3).cumsum().reshape(nf, 3)
        self.assertLess(np.abs(pos[idx] - m.vertices[m.faces]).max(), 0.001)


class MCP(unittest.TestCase):
    def _talk(self, store, *messages):
        out = io.BytesIO()
        mcp.serve(store, io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_handshake_and_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            replies = self._talk(
                store,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_parts", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "show_part", "arguments": {"part": "nope"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
            )
            self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
            self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
            names = {t["name"] for t in replies[1]["result"]["tools"]}
            self.assertTrue({"set_params", "section", "view_path"} <= names)
            self.assertFalse(replies[2]["result"]["isError"])
            self.assertTrue(replies[3]["result"]["isError"])
            self.assertEqual(replies[4]["error"]["code"], -32601)

    def test_stdio_process(self):
        """The real entry point speaks clean JSON lines on stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "doctor", "arguments": {}}}]
            proc = subprocess.run([sys.executable, "-m", f"{NAME}.mcp", "--home", tmp],
                                  input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True,
                                  timeout=120)
            lines = proc.stdout.decode("utf-8").splitlines()
            self.assertEqual(len(lines), 2, proc.stderr.decode("utf-8", "replace"))
            self.assertIn(NAME, json.loads(lines[1])["result"]["content"][0]["text"])


class StoreBasics(unittest.TestCase):
    def test_names_and_mesh_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            with self.assertRaises(Exception):
                store.create("Bad Name")
            stl = Path(tmp) / "t.stl"
            tube().export(stl)
            store.create("tube", source=stl)
            self.assertEqual(store.load("tube")["engine"], "mesh")
            self.assertEqual(store.params("tube"), [])
            self.assertEqual([p["name"] for p in store.list()], ["tube"])
            self.assertTrue(Path(store.load("tube")["origin"]).is_absolute())

    def test_pull_from_origin(self):
        """A generator rewrites its output; the part takes it again, but never over edits made in the part."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            origin = Path(tmp) / "gen_out.stl"
            tube().export(origin)
            ops.new(store, "tube", source=str(origin))
            same = ops.pull(store, "tube")
            self.assertEqual(same["changed"], [])
            self.assertEqual(len(store.versions("tube")), 1)
            tube(outer=30).export(origin)
            pulled = ops.pull(store, "tube", note="шире")
            self.assertEqual(pulled["changed"], ["model.stl"])
            self.assertEqual(store.versions("tube")[-1]["note"], "шире")
            self.assertAlmostEqual(store.version_meta("tube")["report"]["bbox"]["size"][0], 30.0, delta=0.01)
            tube(outer=20).export(store.source("tube"))            # a hand edit in the part itself
            with self.assertRaises(PartError):
                store.pull("tube")
            self.assertEqual(store.pull("tube", force=True)["changed"], ["model.stl"])


if __name__ == "__main__":
    unittest.main()
