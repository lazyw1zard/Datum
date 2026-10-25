# Datum — notes for agents

Datum keeps parametric 3D parts as folders with immutable versions. You drive it through the `datum` CLI or the
`datum-mcp` MCP server; both call the same operations (`src/datum/ops.py`). Units are millimetres, Z is up.

## Working on a part

1. `datum list` / `datum show <part>` — what exists, the current version, warnings.
2. `datum params <part>` — the top-level parameters of the OpenSCAD source with their comments. A `=` mark means
   derived (an expression); change what it depends on.
3. Small changes: `datum set <part> NAME=value ... --note "why"`. This edits the literal in the source and builds a
   new version. Bigger changes: edit `<home>/parts/<part>/part.scad` yourself, then `datum build <part> --note "why"`.
4. Check the result on the mesh, not on the source: `datum measure <part> --cut z=...` or `datum section <part> --at ...`
   gives round holes as Ø, regular polygons (nut pockets) as S across flats, other outlines by their inner/outer
   diameters around the centre. Look at `preview_iso.png` / `preview_under.png`.
5. Hand the human `view.html` of the version (`datum view <part>`): a self-contained 3D view, no server needed.
6. For printing, if the human has a slicer project for the part: `datum export <part> out.3mf --template
   <project>.3mf` puts the mesh in place of the project's object; print settings, placement and scale stay theirs.
   Write next to the project, not over it, unless they ask.

A part whose source a generator writes (its `origin` in `part.json`) is updated with `datum pull <part>`: it takes
the source again and builds only if it changed. Do not copy the file into the part by hand.

A failed build stays as a version with `errors` and `build.log`; the current version stays the last good one.
Notes and parameter changes are recorded in `version.json` and shown by `datum versions <part>`.

## Rules for this repository

- Keep it cross-platform: Python with `pathlib`, no shell-specific scripts, LF line endings (`.gitattributes`).
  OpenSCAD lookup and headless rendering live in `src/datum/config.py`.
- Keep dependencies minimal: `numpy` and `trimesh`; anything else optional. The MCP server is standard library on
  purpose.
- The project name lives only in `src/datum/__init__.py`; derive names from `NAME`, `TITLE`, `ENV_PREFIX`.
  `tools/rename.py` renames everything else.
- Models (parts, STL, 3MF) are data, not code: they live in the Datum home, never in this repository.
- It is a general tool: no particular slicer, printer or anyone's parts in code, tests or docs. Name a slicer only
  where its file format needs handling, and handle the others too (e.g. a template export covers PrusaSlicer and
  the Bambu Studio family, not just the one at hand).
- Tests: `python -m unittest discover -s tests`. Build tests need OpenSCAD and skip without it.
