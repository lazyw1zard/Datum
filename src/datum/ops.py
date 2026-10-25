"""Operations shared by the CLI and the MCP server. Each returns a dict with the data and a
short human/agent-readable `text`; the CLI prints one or the other, MCP sends both."""
from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any

from . import NAME, __version__, config, mesh as meshes
from .engines import openscad
from .parts import PartError, Store


def _fmt_size(size: list[float]) -> str:
    return " × ".join(f"{v:g}" for v in size)


def _version_text(meta: dict, vdir: Path) -> list[str]:
    lines = []
    rep = meta.get("report")
    if rep:
        lines.append(f"габарит {_fmt_size(rep['bbox']['size'])} мм, треугольников {rep['triangles']}, "
                     f"замкнута: {'да' if rep['watertight'] else 'НЕТ'}"
                     + (f", объём {rep['volume_mm3'] / 1000:.2f} см³" if rep.get("volume_mm3") else "")
                     + (f", тел: {rep['bodies']}" if rep.get("bodies", 1) != 1 else ""))
    for w in meta.get("warnings", [])[:8]:
        lines.append(f"предупреждение: {w}")
    for e in meta.get("echo", [])[:8]:
        lines.append(f"echo: {e}")
    for e in meta.get("errors", []):
        lines.append(f"ОШИБКА: {e}")
    if (vdir / "view.html").exists():
        lines.append(f"3D: {vdir / 'view.html'}")
    previews = meta.get("previews") or {}
    if previews:
        lines.append(f"превью ({', '.join(previews)}): {vdir / 'preview_<вид>.png'}")
    return lines


def preview_paths(store: Store, slug: str, version: str | None = None, views: list[str] | None = None) -> list[Path]:
    vdir = store.version_dir(slug, version)
    meta = store.version_meta(slug, version)
    previews = meta.get("previews") or {}
    names = [previews[v] for v in (views or ["iso", "under"]) if v in previews]
    return [vdir / n for n in names if (vdir / n).exists()]


def doctor(store: Store) -> dict:
    exe = config.find_openscad()
    try:
        import gmsh  # type: ignore  # noqa: F401
        has_gmsh = True
    except Exception:  # noqa: BLE001
        has_gmsh = False
    import trimesh
    data = {"name": NAME, "version": __version__, "home": str(store.home), "openscad": exe,
            "openscad_version": openscad.version() if exe else None,
            "openscadpath": config.openscad_env().get("OPENSCADPATH"), "python": sys.version.split()[0],
            "platform": platform.platform(), "trimesh": trimesh.__version__, "step_import": has_gmsh,
            "headless": config.headless_prefix()}
    text = [f"{NAME} {__version__} · дом: {store.home}",
            f"OpenSCAD: {exe or 'НЕ НАЙДЕН (поставьте или задайте $' + config.ENV_PREFIX + 'OPENSCAD)'}"
            + (f" ({data['openscad_version']})" if data["openscad_version"] else ""),
            f"OPENSCADPATH: {data['openscadpath'] or '—'}",
            f"python {data['python']}, trimesh {data['trimesh']}, STEP/IGES: {'да' if has_gmsh else 'нет (pip install gmsh)'}"]
    return {**data, "text": "\n".join(text)}


def list_parts(store: Store) -> dict:
    parts = store.list()
    if not parts:
        return {"parts": [], "text": f"деталей нет ({store.root})"}
    lines = [f"{p['name']:<24} {p['current'] or '—':<6} {p['engine']:<8} {p['title']}" for p in parts]
    return {"parts": parts, "text": "\n".join(lines)}


def show(store: Store, slug: str, version: str | None = None) -> dict:
    part = store.load(slug)
    lines = [f"{part['title']} ({slug}) · движок {part['engine']} · исходник {store.source(slug)}"]
    if part.get("description"):
        lines.append(part["description"])
    versions = store.versions(slug)
    lines.append("версии: " + (", ".join(f"{v['version']}{'' if v['status'] == 'ok' else ' (сбой)'}" for v in versions) or "нет"))
    data: dict[str, Any] = {"part": part, "versions": versions}
    target = version or part.get("current")
    if target:
        vdir = store.version_dir(slug, target)
        meta = store.version_meta(slug, target)
        data["version"] = {k: v for k, v in meta.items() if k != "params"}
        lines.append(f"{meta['version']} · {meta['created'][:16].replace('T', ' ')}" + (f" · {meta['note']}" if meta.get("note") else ""))
        lines += _version_text(meta, vdir)
    return {**data, "text": "\n".join(lines)}


def params(store: Store, slug: str) -> dict:
    items = store.params(slug)
    if not items:
        return {"params": [], "text": "параметров нет (импортированная сетка или пустой исходник)"}
    width = max(len(p.name) for p in items)
    lines = []
    for p in items:
        mark = " " if p.literal else "="
        comment = f"  // {p.comment}" if p.comment else ""
        lines.append(f"{mark} {p.name:<{width}} = {p.raw}{comment}")
    lines.append("(= — производный параметр: меняется через то, от чего зависит)")
    return {"params": [p.as_dict() for p in items], "text": "\n".join(lines)}


def build(store: Store, slug: str, *, note: str = "", changes: list[str] | None = None,
          views: list[str] | None = None) -> dict:
    meta = store.build(slug, note=note, changes=changes, views=views)
    vdir = Path(meta["dir"])
    head = f"{slug} {meta['version']}: {'собрано' if meta['status'] == 'ok' else 'СБОЙ СБОРКИ'} за {meta['seconds']} с"
    lines = [head] + [f"изменено: {c}" for c in meta.get("changes", [])] + _version_text(meta, vdir)
    if meta["status"] != "ok":
        lines.append(f"лог: {vdir / 'build.log'}")
    return {"ok": meta["status"] == "ok", "version": meta["version"], "dir": str(vdir),
            "report": meta.get("report"), "warnings": meta.get("warnings"), "errors": meta.get("errors"),
            "text": "\n".join(lines)}


def set_params(store: Store, slug: str, updates: dict[str, Any], *, force: bool = False, note: str = "",
               build_after: bool = True) -> dict:
    notes = store.set_params(slug, updates, force=force)
    if not build_after:
        return {"changes": notes, "text": "\n".join(["изменено (без сборки):"] + notes)}
    return build(store, slug, note=note, changes=notes)


def pull(store: Store, slug: str, *, note: str = "", force: bool = False, views: list[str] | None = None) -> dict:
    """Source again from the part's origin (e.g. a generator wrote it anew) and a new version, if it changed."""
    pulled = store.pull(slug, force=force)
    origin = Path(pulled["origin"])
    if not pulled["changed"]:
        return {"ok": True, "changed": [], "text": f"{slug}: {origin} не изменился — новая версия не нужна"}
    result = build(store, slug, note=note or f"из {origin.name}", views=views)
    head = f"{slug}: взято из {origin} ({', '.join(pulled['changed'])})"
    return {**result, "changed": pulled["changed"], "text": head + "\n" + result["text"]}


def new(store: Store, slug: str, *, source: str | None = None, title: str | None = None,
        description: str = "", build_after: bool = True) -> dict:
    created = store.create(slug, source=source, title=title, description=description)
    lines = [f"создана деталь {slug} ({created['engine']}): {store.source(slug)}"]
    if created.get("copied_includes"):
        lines.append("взяты включаемые файлы: " + ", ".join(created["copied_includes"]))
    if not build_after:
        return {"part": created, "text": "\n".join(lines)}
    result = build(store, slug, note="первая сборка")
    return {**result, "part": created, "text": "\n".join(lines + [result["text"]])}


def section(store: Store, slug: str, axis: str = "z", at: float | None = None, version: str | None = None) -> dict:
    cut = store.section(slug, axis, at, version)
    lines = [f"{slug} {cut['version']}: сечение {cut['axis']} = {cut['at']:g} (плоскость {'/'.join(cut['plane'])})"]
    if not cut["loops"]:
        lines.append("плоскость не пересекает деталь")
    lines += [f"  {meshes.describe_loop(lp)}" for lp in cut["loops"]]
    lines.append(f"svg: {cut['svg']}")
    return {**cut, "text": "\n".join(lines)}


def measure(store: Store, slug: str, cuts: list[tuple[str, float]] | None = None, version: str | None = None) -> dict:
    meta = store.version_meta(slug, version)
    rep = meta["report"]
    lines = [f"{slug} {meta['version']}: габарит {_fmt_size(rep['bbox']['size'])} мм "
             f"(от {_fmt_size(rep['bbox']['min'])} до {_fmt_size(rep['bbox']['max'])}), "
             f"замкнута: {'да' if rep['watertight'] else 'НЕТ'}, тел: {rep['bodies']}"]
    sections = []
    for axis, at in cuts or []:
        cut = section(store, slug, axis, at, version)
        sections.append({k: v for k, v in cut.items() if k != "text"})
        lines.append(cut["text"])
    return {"report": rep, "sections": sections, "text": "\n".join(lines)}


def view(store: Store, slug: str, version: str | None = None, *, open_browser: bool = False,
         refresh: bool = False) -> dict:
    path = store.refresh_view(slug, version) if refresh else store.version_dir(slug, version) / "view.html"
    if open_browser:
        import webbrowser
        webbrowser.open(path.as_uri())
    return {"path": str(path), "text": str(path)}


def export(store: Store, slug: str, out: str, version: str | None = None, template: str | None = None) -> dict:
    if not template:
        path = store.export(slug, out, version)
        return {"path": str(path), "text": f"сохранено: {path}"}
    info = store.export_into(slug, out, template, version)
    lines = [f"сохранено: {info['path']} — сетка вместо объекта проекта {Path(template).name}, "
             f"{'настройки печати, ' if info['print_settings'] else ''}стол и место на нём — оттуда"]
    if any(abs(s - 1) > 1e-3 for s in info["scale"]):
        lines.append("масштаб объекта в проекте сохранён: " + " × ".join(f"{s:g}" for s in info["scale"])
                     + " (по x, y, z)")
    lines.append("превью внутри проекта старое: слайсер обновит его при нарезке")
    return {**info, "path": str(info["path"]), "text": "\n".join(lines)}


def versions(store: Store, slug: str) -> dict:
    items = store.versions(slug)
    lines = [f"{v['version']}  {v['status']:<6} {v['created'][:16].replace('T', ' ')}  {v.get('note') or ''}"
             + (f"  [{'; '.join(v['changes'])}]" if v.get("changes") else "") for v in items]
    return {"versions": items, "text": "\n".join(lines) or "версий нет"}


__all__ = ["PartError", "Store", "doctor", "list_parts", "show", "params", "build", "set_params", "pull", "new",
           "section", "measure", "view", "export", "versions", "preview_paths"]
