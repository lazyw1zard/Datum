"""Parts and their versions on disk. The file system is the database:

    <home>/parts/<slug>/part.json        what the part is, which version is current
    <home>/parts/<slug>/part.scad        the source (or model.<ext> for an imported mesh)
    <home>/parts/<slug>/versions/v003/   one build, never changed afterwards:
        source.scad  model.stl  preview_*.png  view.html  report.json  version.json  build.log
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, mesh as meshes, scad, three_mf, viewer
from .engines import openscad

SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_INCLUDE = re.compile(r"^\s*(?:include|use)\s*<([^>]+)>", re.MULTILINE)

TEMPLATE = """// {title}
// Parameters: top-level assignments with a comment are what Datum shows and changes.
width = 20;     // mm
depth = 20;     // mm
height = 10;    // mm
hole_d = 5;     // through hole, mm
$fn = 96;

difference() {{
    translate([-width / 2, -depth / 2, 0]) cube([width, depth, height]);
    translate([0, 0, -1]) cylinder(d = hole_d, h = height + 2);
}}
"""


class PartError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Store:
    def __init__(self, home: str | Path | None = None):
        self.home = config.home(home)
        self.root = self.home / "parts"

    # ---------- parts ----------

    def dir(self, slug: str) -> Path:
        if not SLUG.match(slug or ""):
            raise PartError(f"bad part name {slug!r}: lowercase latin letters, digits, '-', '_', '.'")
        return self.root / slug

    def exists(self, slug: str) -> bool:
        return (self.dir(slug) / "part.json").exists()

    def load(self, slug: str) -> dict:
        path = self.dir(slug) / "part.json"
        if not path.exists():
            raise PartError(f"no part {slug!r} in {self.root}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, slug: str, data: dict) -> None:
        path = self.dir(slug) / "part.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def create(self, slug: str, *, source: str | Path | None = None, title: str | None = None,
               description: str = "") -> dict:
        folder = self.dir(slug)
        if self.exists(slug):
            raise PartError(f"part {slug!r} already exists")
        folder.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        src = None
        if source is None:
            engine, name = "openscad", "part.scad"
            (folder / name).write_text(TEMPLATE.format(title=title or slug), encoding="utf-8")
        else:
            src = Path(source).expanduser().resolve()
            if not src.exists():
                raise PartError(f"no such file: {src}")
            suffix = src.suffix.lower()
            if suffix == ".scad":
                engine, name = "openscad", "part.scad"
                shutil.copy2(src, folder / name)
                copied = self._copy_includes(src, folder)
            elif suffix in meshes.SUPPORTED_FORMATS:
                engine, name = "mesh", "model" + suffix
                shutil.copy2(src, folder / name)
            else:
                raise PartError(f"unsupported source {suffix}: use .scad or a mesh/CAD file "
                                f"({', '.join(sorted(meshes.SUPPORTED_FORMATS))})")
        data = {"name": slug, "title": title or slug, "description": description, "engine": engine,
                "source": name, "origin": str(src) if src else None, "created": _now(),
                "updated": _now(), "current": None}
        self._save(slug, data)
        return {**data, "copied_includes": copied}

    @staticmethod
    def _includes(src: Path) -> list[tuple[Path, Path]]:
        """Files the source includes by relative path: (file, path inside the part folder)."""
        found = []
        pending = [(src, Path("."))]
        seen = set()
        while pending:
            file, rel = pending.pop()
            for target in _INCLUDE.findall(file.read_text(encoding="utf-8", errors="replace")):
                candidate = (file.parent / target)
                if not candidate.is_file() or candidate.resolve() in seen:
                    continue            # a library on OPENSCADPATH, or already found
                seen.add(candidate.resolve())
                found.append((candidate, rel / target))
                pending.append((candidate, (rel / target).parent))
        return found

    def _copy_includes(self, src: Path, folder: Path) -> list[str]:
        """Bring along files the source includes by relative path, so the part builds on its own."""
        copied = []
        for file, rel in self._includes(src):
            dest = folder / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest)
            copied.append(rel.as_posix())
        return copied

    def pull(self, slug: str, *, force: bool = False) -> dict:
        """Take the source again from where the part came from (`origin`), e.g. a generator's output, with the
        files it includes. Edits made in the part itself after its last build are not overwritten unless force."""
        part = self.load(slug)
        if not part.get("origin"):
            raise PartError(f"part {slug!r} was not made from a file: there is no origin to pull from")
        src = Path(part["origin"]).expanduser()
        if not src.is_file():
            raise PartError(f"origin of {slug!r} is gone: {src}")
        folder = self.dir(slug)
        dest = folder / part["source"]
        if src.suffix.lower() != dest.suffix.lower():
            raise PartError(f"origin {src.name} and the part source {dest.name} are of different kinds")
        built = self.versions(slug)
        if built and dest.exists() and not force:
            last = json.loads((self.dir(slug) / "versions" / built[-1]["version"] / "version.json")
                              .read_text(encoding="utf-8"))
            if _sha256(dest) != last.get("source_sha256"):
                raise PartError(f"{dest.name} was edited after {last['version']} was built; pulling would lose "
                                "that (force=true to discard it)")
        includes = self._includes(src) if part["engine"] == "openscad" else []
        changed = []
        for file, rel in [(src, Path(dest.name)), *includes]:
            target = folder / rel
            if target.is_file() and target.read_bytes() == file.read_bytes():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            changed.append(rel.as_posix())
        if changed:
            part["updated"] = _now()
            self._save(slug, part)
        return {"origin": str(src), "changed": changed}

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        result = []
        for folder in sorted(self.root.iterdir()):
            if (folder / "part.json").exists():
                data = json.loads((folder / "part.json").read_text(encoding="utf-8"))
                result.append({k: data.get(k) for k in ("name", "title", "engine", "current", "updated")})
        return result

    def source(self, slug: str) -> Path:
        return self.dir(slug) / self.load(slug)["source"]

    # ---------- parameters ----------

    def params(self, slug: str) -> list[scad.Param]:
        if self.load(slug)["engine"] != "openscad":
            return []
        return scad.parse_params(self.source(slug).read_text(encoding="utf-8"))

    def set_params(self, slug: str, updates: dict[str, Any], *, force: bool = False) -> list[str]:
        if self.load(slug)["engine"] != "openscad":
            raise PartError("an imported mesh has no parameters")
        path = self.source(slug)
        text, notes = scad.set_params(path.read_text(encoding="utf-8"), updates, force=force)
        path.write_text(text, encoding="utf-8")
        data = self.load(slug)
        data["updated"] = _now()
        self._save(slug, data)
        return notes

    # ---------- versions ----------

    def versions(self, slug: str) -> list[dict]:
        vroot = self.dir(slug) / "versions"
        if not vroot.exists():
            return []
        out = []
        for folder in sorted(vroot.iterdir()):
            meta = folder / "version.json"
            if meta.exists():
                v = json.loads(meta.read_text(encoding="utf-8"))
                out.append({k: v.get(k) for k in ("version", "status", "created", "note", "changes")})
        return out

    def version_dir(self, slug: str, version: str | None = None) -> Path:
        version = version or self.load(slug).get("current")
        if not version:
            raise PartError(f"part {slug!r} has no successful build yet")
        if re.fullmatch(r"\d+", version):
            version = f"v{int(version):03d}"
        folder = self.dir(slug) / "versions" / version
        if not (folder / "version.json").exists():
            raise PartError(f"part {slug!r} has no version {version}")
        return folder

    def version_meta(self, slug: str, version: str | None = None) -> dict:
        return json.loads((self.version_dir(slug, version) / "version.json").read_text(encoding="utf-8"))

    def _next_version(self, slug: str) -> str:
        vroot = self.dir(slug) / "versions"
        numbers = [int(p.name[1:]) for p in vroot.glob("v[0-9]*") if p.name[1:].isdigit()] if vroot.exists() else []
        return f"v{max(numbers, default=0) + 1:03d}"

    def build(self, slug: str, *, note: str = "", changes: list[str] | None = None,
              views: list[str] | None = None) -> dict:
        part = self.load(slug)
        folder = self.dir(slug)
        version = self._next_version(slug)
        vdir = folder / "versions" / version
        vdir.mkdir(parents=True)
        started = time.monotonic()
        source = folder / part["source"]
        meta: dict[str, Any] = {"part": slug, "title": part["title"], "version": version, "created": _now(),
                                "note": note, "changes": changes or [], "engine": part["engine"],
                                "source_sha256": _sha256(source), "warnings": [], "echo": [], "errors": []}
        log = ""
        try:
            if part["engine"] == "openscad":
                shutil.copy2(source, vdir / "source.scad")
                meta["params"] = [p.as_dict() for p in scad.parse_params(source.read_text(encoding="utf-8"))]
                result = openscad.build(source, vdir)
                log = result["log"]
                for key in ("warnings", "echo", "errors"):
                    meta[key] = result[key]
                if not result["ok"]:
                    raise PartError("; ".join(result["errors"]) or "build failed")
                part_mesh = meshes.load(vdir / "model.stl")
            else:
                part_mesh = meshes.load(source)
                meshes.save(part_mesh, vdir / "model.stl")
                meta["params"] = []
            meta["report"] = meshes.report(part_mesh)
            try:
                previews = openscad.render_views(vdir / "model.stl", vdir, views)
            except openscad.OpenSCADMissing as exc:
                previews = {}
                meta["warnings"].append(f"no previews: {exc}")
            meta["previews"] = {k: p.name for k, p in previews.items()}
            self._write_view(vdir, part_mesh, meta)
            meta["status"] = "ok"
        except (PartError, openscad.OpenSCADMissing, RuntimeError, ValueError, OSError) as exc:
            meta["status"] = "failed"
            if str(exc) not in meta["errors"]:
                meta["errors"].append(str(exc))
        meta["seconds"] = round(time.monotonic() - started, 1)
        (vdir / "build.log").write_text(log + "\n", encoding="utf-8")
        (vdir / "version.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if meta["status"] == "ok":
            (vdir / "report.json").write_text(json.dumps(meta["report"], ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
            part = self.load(slug)
            part["current"], part["updated"] = version, _now()
            self._save(slug, part)
        return {**meta, "dir": str(vdir)}

    @staticmethod
    def _write_view(vdir: Path, part_mesh, meta: dict) -> int:
        size = viewer.write(vdir / "view.html", part_mesh.vertices, part_mesh.faces, meta)
        if size > viewer.PANEL_LIMIT:
            note = (f"view.html {size // 1024} KB > {viewer.PANEL_LIMIT // 1024} KB: a chat side panel will show it "
                    "as code; open it in a browser")
            if note not in meta["warnings"]:
                meta["warnings"].append(note)
        return size

    def refresh_view(self, slug: str, version: str | None = None) -> Path:
        """Rebuild view.html of a built version (a derived file; the version itself stays as it was)."""
        vdir = self.version_dir(slug, version)
        meta = json.loads((vdir / "version.json").read_text(encoding="utf-8"))
        meta["warnings"] = [w for w in meta.get("warnings", []) if not w.startswith("view.html ")]
        self._write_view(vdir, meshes.load(vdir / "model.stl"), meta)
        (vdir / "version.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return vdir / "view.html"

    # ---------- reading a built version ----------

    def mesh(self, slug: str, version: str | None = None):
        return meshes.load(self.version_dir(slug, version) / "model.stl")

    def section(self, slug: str, axis: str, at: float | None, version: str | None = None) -> dict:
        vdir = self.version_dir(slug, version)
        cut = meshes.section(meshes.load(vdir / "model.stl"), axis, at)
        name = f"section_{cut['axis']}_{cut['at']:g}.svg".replace("-", "m")
        (vdir / name).write_text(meshes.section_svg(cut, title=f"{slug} {vdir.name}"), encoding="utf-8")
        return {**meshes.public_section(cut), "svg": str(vdir / name), "version": vdir.name}

    def export(self, slug: str, out: str | Path, version: str | None = None) -> Path:
        vdir = self.version_dir(slug, version)
        out = Path(out).expanduser()
        if out.suffix.lower() == ".stl":
            shutil.copy2(vdir / "model.stl", out)
            return out
        return meshes.save(meshes.load(vdir / "model.stl"), out, name=self.load(slug)["title"])

    def export_into(self, slug: str, out: str | Path, template: str | Path, version: str | None = None) -> dict:
        """The version's mesh inside a slicer project: its print settings, plate and placement stay."""
        out, template = Path(out).expanduser(), Path(template).expanduser()
        if out.suffix.lower() != ".3mf" or template.suffix.lower() != ".3mf":
            raise PartError("a template export takes a .3mf project and writes a .3mf")
        if not template.is_file():
            raise PartError(f"no such template: {template}")
        part_mesh = meshes.load(self.version_dir(slug, version) / "model.stl")
        return {"path": out, **three_mf.replace_mesh(template, out, part_mesh.vertices, part_mesh.faces)}
