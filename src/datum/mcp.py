"""MCP server over stdio, standard library only: any MCP client (Claude Code, Codex, Hermes, ...) can drive
the same operations as the CLI. Results are short text plus preview images, so the agent sees the part.

Register, e.g.:  claude mcp add datum -- datum-mcp
                 codex:  [mcp_servers.datum] command = "datum-mcp"  (~/.codex/config.toml)
"""
from __future__ import annotations

import base64
import json
import sys
import traceback
from typing import Any, Callable

from . import NAME, TITLE, __version__, ops
from .engines.openscad import OpenSCADMissing, VIEWS
from .parts import PartError, Store

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = (
    f"{TITLE} keeps parametric 3D parts as folders with immutable versions. Typical loop: list_parts / show_part -> "
    "part_params -> set_params (rewrites the OpenSCAD source and builds a new version, returns previews) -> "
    "measure or section to check real dimensions on the mesh (holes as Ø, hex pockets as S) -> view_path gives a "
    "self-contained 3D view (view.html) to hand to the human. Builds that fail are kept as versions with errors; "
    "the current version stays the last good one. Units are millimetres, Z is up."
)

_PART = {"part": {"type": "string", "description": "part name (folder), e.g. 'vtulka'"}}
_VERSION = {"version": {"type": "string", "description": "v001, 3, ...; default: current"}}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS: dict[str, dict[str, Any]] = {
    "doctor": {"description": "Where parts live, whether OpenSCAD and libraries are found.", "schema": _schema({})},
    "list_parts": {"description": "All parts with their current version.", "schema": _schema({})},
    "show_part": {"description": "A part and its current (or given) version: size, warnings, paths; with preview images.",
                  "schema": _schema({**_PART, **_VERSION}, ["part"])},
    "part_params": {"description": "Top-level OpenSCAD parameters: name = value // comment; derived ones are marked '='.",
                    "schema": _schema(_PART, ["part"])},
    "new_part": {"description": "Create a part from the template, from a .scad file, or from a mesh/CAD file "
                                "(STL, 3MF, OBJ, GLB, STEP/IGES with gmsh); builds v001 unless build=false.",
                 "schema": _schema({**_PART, "source": {"type": "string", "description": "path to .scad or a mesh file"},
                                    "title": {"type": "string"}, "description": {"type": "string"},
                                    "build": {"type": "boolean", "default": True}}, ["part"])},
    "set_params": {"description": "Change parameters in the source and build a new version. Values: numbers, "
                                  "booleans, strings, arrays. Derived parameters need force=true.",
                   "schema": _schema({**_PART, "params": {"type": "object", "description": "{name: value}"},
                                      "note": {"type": "string"}, "force": {"type": "boolean", "default": False},
                                      "build": {"type": "boolean", "default": True}}, ["part", "params"])},
    "build_part": {"description": "Build a new version from the current source (after you edited the .scad yourself).",
                   "schema": _schema({**_PART, "note": {"type": "string"},
                                      "views": {"type": "array", "items": {"type": "string", "enum": list(VIEWS)}}},
                                     ["part"])},
    "part_versions": {"description": "Version history with notes and parameter changes.", "schema": _schema(_PART, ["part"])},
    "section": {"description": "Cut the built mesh with a plane and read outlines: round holes as Ø, regular polygons "
                               "(hex nut pockets) as S across flats, other outlines by size. Also writes an SVG.",
                "schema": _schema({**_PART, "axis": {"type": "string", "enum": ["x", "y", "z"], "default": "z"},
                                   "at": {"type": "number", "description": "plane coordinate, mm; default: middle"},
                                   **_VERSION}, ["part"])},
    "measure": {"description": "Bounding box, watertightness, bodies, plus sections at given planes.",
                "schema": _schema({**_PART, "cuts": {"type": "array", "description": "[{axis, at}]", "items": {
                    "type": "object", "properties": {"axis": {"type": "string", "enum": ["x", "y", "z"]},
                                                     "at": {"type": "number"}}, "required": ["axis", "at"]}},
                                   **_VERSION}, ["part"])},
    "view_path": {"description": "Path of the self-contained 3D view (view.html: rotate, sections by eye, measure "
                                 "two points) to send to the human.", "schema": _schema({**_PART, **_VERSION}, ["part"])},
    "export_mesh": {"description": "Save a version's mesh as .stl, .3mf, .glb, .obj or .ply.",
                    "schema": _schema({**_PART, "out": {"type": "string", "description": "target file path"},
                                       **_VERSION}, ["part", "out"])},
}


def _images(store: Store, part: str, version: str | None = None) -> list[dict]:
    try:
        paths = ops.preview_paths(store, part, version)
    except PartError:
        return []
    return [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(p.read_bytes()).decode("ascii")}
            for p in paths]


def call_tool(store: Store, name: str, args: dict) -> tuple[str, list[dict], bool]:
    """-> (text, images, is_error)"""
    part = args.get("part")
    version = args.get("version")
    handlers: dict[str, Callable[[], dict]] = {
        "doctor": lambda: ops.doctor(store),
        "list_parts": lambda: ops.list_parts(store),
        "show_part": lambda: ops.show(store, part, version),
        "part_params": lambda: ops.params(store, part),
        "new_part": lambda: ops.new(store, part, source=args.get("source"), title=args.get("title"),
                                    description=args.get("description", ""), build_after=args.get("build", True)),
        "set_params": lambda: ops.set_params(store, part, args.get("params") or {}, force=args.get("force", False),
                                             note=args.get("note", ""), build_after=args.get("build", True)),
        "build_part": lambda: ops.build(store, part, note=args.get("note", ""), views=args.get("views")),
        "part_versions": lambda: ops.versions(store, part),
        "section": lambda: ops.section(store, part, args.get("axis", "z"), args.get("at"), version),
        "measure": lambda: ops.measure(store, part, [(c["axis"], float(c["at"])) for c in args.get("cuts") or []],
                                       version),
        "view_path": lambda: ops.view(store, part, version),
        "export_mesh": lambda: ops.export(store, part, args["out"], version),
    }
    if name not in handlers:
        return f"unknown tool {name}", [], True
    try:
        result = handlers[name]()
    except (PartError, OpenSCADMissing, KeyError, ValueError, OSError) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return f"{NAME}: {message}", [], True
    images = []
    if name in {"show_part", "new_part", "build_part"} or (name == "set_params" and args.get("build", True)):
        if result.get("ok", True):
            images = _images(store, part, version if name == "show_part" else None)
    return result["text"], images, not result.get("ok", True)


def _tool_list() -> list[dict]:
    return [{"name": n, "description": t["description"], "inputSchema": t["schema"]} for n, t in TOOLS.items()]


def handle(store: Store, message: dict) -> dict | None:
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:          # a notification (initialized, cancelled, ...): nothing to answer
        return None
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        chosen = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        result = {"protocolVersion": chosen, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": NAME, "title": TITLE, "version": __version__}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tool_list()}
    elif method == "tools/call":
        text, images, is_error = call_tool(store, params.get("name", ""), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}, *images], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(store: Store | None = None, stdin=None, stdout=None) -> None:
    store = store or Store()
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            stdout.write((json.dumps(reply) + "\n").encode("utf-8"))
            stdout.flush()
            continue
        batch = message if isinstance(message, list) else [message]
        replies = []
        for item in batch:
            try:
                reply = handle(store, item)
            except Exception as exc:  # noqa: BLE001  # never let one bad call kill the server
                traceback.print_exc(file=sys.stderr)
                reply = {"jsonrpc": "2.0", "id": item.get("id"), "error": {"code": -32603, "message": str(exc)}}
            if reply is not None:
                replies.append(reply)
        if replies:
            out = replies if isinstance(message, list) else replies[0]
            stdout.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
            stdout.flush()


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog=f"{NAME}-mcp", description=f"{TITLE} MCP server (stdio)")
    p.add_argument("--home", help="folder with parts (default: $DATUM_HOME or ~/Datum)")
    serve(Store(p.parse_args().home))


if __name__ == "__main__":
    main()
