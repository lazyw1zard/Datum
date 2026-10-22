"""Meshes: load any format, summarize dimensions, cut sections and read holes off them.

Grew out of CADRelay's worker converter: STEP/IGES still go through gmsh (optional), STL/OBJ/GLB
through trimesh; 3MF now has its own reader (three_mf.py) so no networkx/lxml are needed.
"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
import trimesh

from . import three_mf

MESH_FORMATS = {".stl", ".obj", ".glb", ".gltf", ".ply", ".off"}
CAD_FORMATS = {".step", ".stp", ".iges", ".igs"}
SUPPORTED_FORMATS = MESH_FORMATS | CAD_FORMATS | {".3mf"}
AXES = {"x": 0, "y": 1, "z": 2}
GMSH_PROFILES = {"fast": 2.0, "balanced": 1.0, "high": 0.6}


# ---------- loading ----------

def _single_mesh(loaded) -> trimesh.Trimesh:
    if isinstance(loaded, trimesh.Scene):
        geometries = [g for g in loaded.dump() if isinstance(g, trimesh.Trimesh)]
        if not geometries:
            raise RuntimeError("the file has no triangle geometry")
        loaded = trimesh.util.concatenate(geometries)
    if loaded.is_empty:
        raise RuntimeError("the mesh is empty")
    return loaded


def _load_cad(path: Path, profile: str) -> trimesh.Trimesh:
    try:
        import gmsh  # type: ignore  # optional: pip install gmsh
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("STEP/IGES need the optional 'gmsh' package (pip install gmsh)") from exc
    with tempfile.TemporaryDirectory(prefix="datum_cad_") as tmp:
        stl = Path(tmp) / "mesh.stl"
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.option.setNumber("Mesh.CharacteristicLengthFactor", GMSH_PROFILES.get(profile, 1.0))
            gmsh.model.add("datum")
            gmsh.model.occ.importShapes(str(path))
            gmsh.model.occ.synchronize()
            gmsh.model.mesh.generate(2)
            gmsh.write(str(stl))
        finally:
            gmsh.finalize()
        return _single_mesh(trimesh.load(stl, force="mesh"))


def load(path: str | Path, *, profile: str = "balanced") -> trimesh.Trimesh:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".3mf":
        vertices, faces = three_mf.read_mesh(path)
        return trimesh.Trimesh(vertices=vertices, faces=faces, process=True)
    if suffix in CAD_FORMATS:
        return _load_cad(path, profile)
    if suffix in MESH_FORMATS:
        return _single_mesh(trimesh.load(path, force="scene" if suffix in {".obj", ".glb", ".gltf"} else "mesh"))
    raise ValueError(f"unsupported format {suffix}; known: {', '.join(sorted(SUPPORTED_FORMATS))}")


# ---------- summary ----------

def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def bodies(mesh: trimesh.Trimesh) -> int:
    """Connected pieces, by shared vertices (no scipy/networkx needed)."""
    parent = list(range(len(mesh.vertices)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b, c in mesh.faces:
        ra, rb, rc = find(a), find(b), find(c)
        if rb != ra:
            parent[rb] = ra
        if rc != ra:
            parent[rc] = ra
    return len({find(i) for f in mesh.faces for i in f[:1]})


def report(mesh: trimesh.Trimesh) -> dict:
    lo, hi = mesh.bounds
    watertight = bool(mesh.is_watertight)
    return {
        "triangles": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "watertight": watertight,
        "bodies": bodies(mesh),
        "bbox": {"min": [_r(v) for v in lo], "max": [_r(v) for v in hi], "size": [_r(v) for v in hi - lo]},
        "volume_mm3": _r(mesh.volume, 2) if watertight else None,
        "area_mm2": _r(mesh.area, 2),
    }


# ---------- sections ----------

def _chain(segments: np.ndarray, tol: float = 1e-5) -> list[tuple[np.ndarray, bool]]:
    """Join 2D segments into polylines; returns (points, closed)."""
    keys = np.round(segments / tol).astype(np.int64)
    ends: dict[tuple, list[tuple[int, int]]] = {}
    for i, seg in enumerate(keys):
        for j in (0, 1):
            ends.setdefault(tuple(seg[j]), []).append((i, j))
    used = np.zeros(len(segments), dtype=bool)
    loops = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        points = [segments[start][0], segments[start][1]]
        first, current = tuple(keys[start][0]), tuple(keys[start][1])
        closed = False
        while True:
            if current == first and len(points) > 2:
                closed = True
                points.pop()
                break
            nxt = next(((i, j) for i, j in ends.get(current, []) if not used[i]), None)
            if nxt is None:
                break
            i, j = nxt
            used[i] = True
            points.append(segments[i][1 - j])
            current = tuple(keys[i][1 - j])
        loops.append((np.array(points), closed))
    return loops


def _area(points: np.ndarray) -> float:
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _centroid(points: np.ndarray) -> np.ndarray:
    x, y = points[:, 0], points[:, 1]
    cross = x * np.roll(y, -1) - np.roll(x, -1) * y
    a = cross.sum() / 2
    if abs(a) < 1e-12:
        return points.mean(axis=0)
    return np.array([((x + np.roll(x, -1)) * cross).sum(), ((y + np.roll(y, -1)) * cross).sum()]) / (6 * a)


def _inside(point: np.ndarray, polygon: np.ndarray) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, np.roll(polygon, -1, axis=0)):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _corners(points: np.ndarray, angle_deg: float = 2.0) -> np.ndarray:
    """Drop vertices where the outline goes straight on."""
    keep = []
    n = len(points)
    for i in range(n):
        a, b, c = points[i - 1], points[i], points[(i + 1) % n]
        u, v = b - a, c - b
        nu, nv = np.linalg.norm(u), np.linalg.norm(v)
        if nu < 1e-9 or nv < 1e-9:
            continue
        cos = np.clip(np.dot(u, v) / (nu * nv), -1, 1)
        if math.degrees(math.acos(cos)) > angle_deg:
            keep.append(b)
    return np.array(keep) if keep else points


def _shape(points: np.ndarray, center: np.ndarray) -> dict:
    radii = np.linalg.norm(points - center, axis=1)
    r_mean = float(radii.mean())
    # Corners alone can sit on one circle (a rectangle's four do); edge midpoints give a polygon away.
    midpoints = (points + np.roll(points, -1, axis=0)) / 2
    all_radii = np.concatenate([radii, np.linalg.norm(midpoints - center, axis=1)])
    spread = float(all_radii.max() - all_radii.min()) / 2
    if len(points) >= 8 and r_mean > 0 and spread <= max(0.01 * r_mean, 0.02):
        return {"kind": "circle", "diameter": _r(2 * r_mean), "roundness": _r(spread)}
    corners = _corners(points)
    n = len(corners)
    if 3 <= n <= 12:
        edges = np.linalg.norm(np.roll(corners, -1, axis=0) - corners, axis=1)
        if edges.max() - edges.min() <= 0.02 * edges.mean():
            # distance from the centre to each edge line; across flats = 2 * apothem
            d = []
            for p, q in zip(corners, np.roll(corners, -1, axis=0)):
                e = q - p
                d.append(abs(e[0] * (center[1] - p[1]) - e[1] * (center[0] - p[0])) / np.linalg.norm(e))
            apothem = float(np.mean(d))
            shape = {"kind": f"{n}-gon", "across_corners": _r(2 * float(np.linalg.norm(corners - center, axis=1).mean()))}
            if n % 2 == 0:
                shape["across_flats"] = _r(2 * apothem)
            return shape
    # Anything else (a cavity with teeth, a slot): the nearest edge and the farthest corner from the centre,
    # i.e. "diameter over the tips" and "diameter of the cavity".
    a, b = points, np.roll(points, -1, axis=0)
    e = b - a
    t = np.clip(((center - a) * e).sum(axis=1) / np.maximum((e * e).sum(axis=1), 1e-12), 0, 1)
    nearest = np.linalg.norm(a + e * t[:, None] - center, axis=1).min()
    return {"kind": "outline", "d_inner": _r(2 * nearest), "d_outer": _r(2 * float(radii.max()))}


def section(mesh: trimesh.Trimesh, axis: str = "z", at: float | None = None) -> dict:
    """Cut the mesh with a plane across `axis` at `at` (default: middle) and describe the outlines."""
    k = AXES[axis.lower()]
    lo, hi = mesh.bounds
    if at is None:
        at = float(lo[k] + hi[k]) / 2
    # A plane lying exactly on a face gives degenerate cuts; nudge it off.
    coords = mesh.vertices[:, k]
    actual = float(at)
    if np.any(np.abs(coords - actual) < 1e-6):
        actual += 1e-4
    normal = np.zeros(3)
    normal[k] = 1.0
    origin = np.zeros(3)
    origin[k] = actual
    segments3 = trimesh.intersections.mesh_plane(mesh, normal, origin)
    plane_axes = [i for i in range(3) if i != k]
    names = "xyz"
    result = {"axis": names[k], "at": _r(at), "plane": [names[i] for i in plane_axes], "loops": []}
    if len(segments3) == 0:
        return result
    segments = np.asarray(segments3)[:, :, plane_axes]
    chains = _chain(segments)
    polygons = [(pts, closed) for pts, closed in chains if len(pts) >= 3]
    for idx, (pts, closed) in enumerate(polygons):
        depth = sum(1 for j, (other, oc) in enumerate(polygons) if j != idx and oc and _inside(pts[0], other))
        encloses = closed and any(j != idx and _inside(other[0], pts) for j, (other, _) in enumerate(polygons))
        center = _centroid(pts)
        lo2, hi2 = pts.min(axis=0), pts.max(axis=0)
        shape = _shape(pts, center)
        if shape["kind"] == "outline" and depth % 2 == 0 and not encloses:
            # A solid outline with nothing inside (a wall in a lengthwise cut): diameters "from the centre"
            # mean nothing there, its size says it all.
            shape = {"kind": "outline"}
        loop = {
            "role": "outer" if depth % 2 == 0 else "hole",
            "closed": closed,
            "center": [_r(v) for v in center],
            "size": [_r(v) for v in hi2 - lo2],
            "area_mm2": _r(abs(_area(pts)), 2),
            **shape,
            "points": pts,
        }
        result["loops"].append(loop)
    result["loops"].sort(key=lambda lp: -lp["area_mm2"])
    return result


def describe_loop(loop: dict) -> str:
    size = "×".join(f"{v:g}" for v in loop["size"])
    shape = loop["kind"]
    if shape == "circle":
        what = f"Ø{loop['diameter']:g}"
    elif "across_flats" in loop:
        what = f"{shape}, S{loop['across_flats']:g} (по углам {loop['across_corners']:g})"
    elif shape.endswith("-gon"):
        what = f"{shape}, по углам {loop['across_corners']:g}"
    elif "d_inner" in loop:
        what = f"контур {size}, от центра: Ø{loop['d_inner']:g} до Ø{loop['d_outer']:g}"
    else:
        what = f"контур {size}"
    where = ", ".join(f"{v:g}" for v in loop["center"])
    role = "отверстие" if loop["role"] == "hole" else "наружный"
    open_note = "" if loop["closed"] else " (не замкнут!)"
    return f"{role}: {what}, центр ({where}){open_note}"


def section_svg(cut: dict, *, title: str = "") -> str:
    """The section as a standalone SVG with labels; y up, millimetres."""
    loops = cut["loops"]
    if not loops:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="80"><text x="10" y="40">пустое сечение</text></svg>'
    allpts = np.vstack([lp["points"] for lp in loops])
    lo, hi = allpts.min(axis=0), allpts.max(axis=0)
    span = max(float((hi - lo).max()), 1.0)
    margin = span * 0.25
    scale = 600 / (span + 2 * margin)
    width = (float(hi[0] - lo[0]) + 2 * margin) * scale
    height = (float(hi[1] - lo[1]) + 2 * margin) * scale + 40

    def tx(p):
        return (float(p[0] - lo[0]) + margin) * scale, (float(hi[1] - p[1]) + margin) * scale + 40

    path = []
    for lp in loops:
        pts = [tx(p) for p in lp["points"]]
        path.append("M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in pts) + (" Z" if lp["closed"] else ""))
    labels = []
    for lp in loops:
        if lp["kind"] == "circle":
            text = f"Ø{lp['diameter']:g}"
        elif "across_flats" in lp:
            text = f"S{lp['across_flats']:g}"
        elif lp["role"] == "hole" and "d_inner" in lp:
            text = f"Ø{lp['d_inner']:g} / Ø{lp['d_outer']:g}"
        else:
            continue
        x, y = tx(lp["center"])
        labels.append(f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" class="d">{text}</text>')
    outer = loops[0]
    size = "×".join(f"{v:g}" for v in outer["size"])
    head = f"{title} · сечение {cut['axis']} = {cut['at']:g} · габарит {size} мм".strip(" ·")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="system-ui, sans-serif">'
        "<style>.d{font-size:13px;fill:#1d4ed8;font-weight:600}.h{font-size:14px;fill:#334155}</style>"
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<text x="12" y="24" class="h">{head}</text>'
        f'<path d="{" ".join(path)}" fill="#f5c76b" fill-opacity="0.85" fill-rule="evenodd" stroke="#7c5a12" stroke-width="1.2"/>'
        + "".join(labels) + "</svg>"
    )


def public_section(cut: dict) -> dict:
    """The section without point arrays, for JSON output."""
    return {**cut, "loops": [{k: v for k, v in lp.items() if k != "points"} for lp in cut["loops"]]}


# ---------- export ----------

def save(mesh: trimesh.Trimesh, path: str | Path, *, name: str = "part") -> Path:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".3mf":
        three_mf.write_mesh(path, mesh.vertices, mesh.faces, name=name)
    elif suffix in {".stl", ".glb", ".obj", ".ply"}:
        mesh.export(path)
    else:
        raise ValueError(f"cannot export {suffix}; use .stl, .3mf, .glb, .obj or .ply")
    return path
