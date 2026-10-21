"""OpenSCAD as a build engine: source -> STL, STL -> preview PNGs.

PNGs are rendered from the built STL through a tiny `import()` file, so the model is
evaluated by CGAL once and every view after that is cheap.
"""
from __future__ import annotations

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config

# OpenSCAD gimbal camera: translate x,y,z, rotate x,y,z, distance (viewall/autocenter fix the rest)
VIEWS = {
    "iso": ("55,0,25", "p"),
    "under": ("125,0,25", "p"),      # the same corner seen from below: pockets and cavities on the bed face
    "front": ("90,0,0", "o"),
    "right": ("90,0,90", "o"),
    "top": ("0,0,0", "o"),
}
DEFAULT_VIEWS = ["iso", "under", "front", "top"]
PART_COLOR = "[0.86, 0.66, 0.26]"
_LINE = re.compile(r"^(WARNING|ERROR|ECHO|TRACE|DEPRECATED):\s*(.*)$")


class OpenSCADMissing(RuntimeError):
    pass


def executable() -> str:
    exe = config.find_openscad()
    if not exe:
        raise OpenSCADMissing(
            "OpenSCAD not found: install it (openscad.org) or point $"
            f"{config.ENV_PREFIX}OPENSCAD at the binary")
    return exe


def version() -> str | None:
    try:
        out = subprocess.run([executable(), "--version"], capture_output=True, text=True, timeout=60)
    except (OpenSCADMissing, OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout + out.stderr).strip()
    return text.splitlines()[-1] if text else None


def _run(args: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    started = time.monotonic()
    try:
        out = subprocess.run(config.headless_prefix() + [executable(), *args], cwd=cwd, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=timeout,
                             env=config.openscad_env())
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f} s"
    log = (out.stdout + out.stderr).strip()
    return out.returncode, log + f"\n[{time.monotonic() - started:.1f} s]"


def parse_log(log: str) -> dict:
    found = {"warnings": [], "errors": [], "echo": []}
    for line in log.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        kind, text = m.groups()
        if kind == "ECHO":
            found["echo"].append(text)
        elif kind == "ERROR":
            found["errors"].append(text)
        else:
            found["warnings"].append(f"{kind}: {text}")
    if "Current top level object is empty" in log or "No top level geometry to render" in log:
        found["errors"].append("the model is empty: no top-level geometry")
    return found


def build(source: Path, out_dir: Path, *, timeout: float = 600) -> dict:
    """Render `source` to out_dir/model.stl. Returns {ok, stl, log, warnings, errors, echo}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stl = out_dir / "model.stl"
    code, log = _run(["-o", str(stl), "--export-format", "binstl", str(source)], cwd=source.parent, timeout=timeout)
    found = parse_log(log)
    ok = code == 0 and stl.exists() and stl.stat().st_size > 84 and not found["errors"]
    if code != 0 and not found["errors"]:
        found["errors"].append(f"openscad exited with {code}")
    return {"ok": ok, "stl": stl if ok else None, "log": log, **found}


def render_views(stl: Path, out_dir: Path, views: list[str] | None = None, *, size: tuple[int, int] = (1200, 900),
                 timeout: float = 300) -> dict[str, Path]:
    """Preview PNGs of an STL: {view: path}. Views that fail are left out."""
    views = [v for v in (views or DEFAULT_VIEWS) if v in VIEWS]
    scene = out_dir / "_render.scad"
    scene.write_text(f'color({PART_COLOR}) import("{stl.name}");\n', encoding="utf-8")

    def one(view: str):
        rotation, projection = VIEWS[view]
        png = out_dir / f"preview_{view}.png"
        code, _ = _run(["-o", png.name, f"--imgsize={size[0]},{size[1]}", f"--camera=0,0,0,{rotation},0",
                        "--viewall", "--autocenter", f"--projection={projection}", "--colorscheme=Tomorrow",
                        scene.name], cwd=out_dir, timeout=timeout)
        return view, png if code == 0 and png.exists() else None

    try:
        with ThreadPoolExecutor(max_workers=min(4, len(views))) as pool:
            results = dict(pool.map(one, views))
    finally:
        scene.unlink(missing_ok=True)
    return {view: path for view, path in results.items() if path}
