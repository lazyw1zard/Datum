"""Rename the project in place: package folder, imports, CLI names, env prefix, docs.

    python tools/rename.py kalibr [--title Kalibr] [--dry-run]

Replaces the current name as a whole word in three spellings (datum / Datum / DATUM, so
DATUM_HOME becomes KALIBR_HOME) in every text file of the repository, except vendored code,
and moves src/<old>/ to src/<new>/. Afterwards: reinstall (pip install -e .), re-register the
MCP server under the new command, rename the remote (gh repo rename) and the folder if you like.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "build", "dist", "__pycache__", "vendor", "node_modules", ".pytest_cache"}
NAME_RE = re.compile(r'^NAME = "([a-z][a-z0-9_]*)"$', re.M)
TITLE_RE = re.compile(r'^TITLE = "([^"]+)"$', re.M)


def current(root: Path) -> tuple[str, str, Path]:
    packages = [p for p in (root / "src").iterdir() if (p / "__init__.py").exists()]
    if len(packages) != 1:
        raise SystemExit(f"expected one package in {root / 'src'}, found {[p.name for p in packages]}")
    text = (packages[0] / "__init__.py").read_text(encoding="utf-8")
    return NAME_RE.search(text).group(1), TITLE_RE.search(text).group(1), packages[0]


def word(token: str) -> re.Pattern:
    # letters and digits bound a word; '_', '-', '.', '/' do not, so DATUM_HOME and datum-mcp are caught
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])")


def text_files(root: Path):
    for path in root.rglob("*"):
        if path.is_dir() or any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.relative_to(root).parts):
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def rename(root: Path, new: str, title: str | None = None, dry_run: bool = False) -> list[str]:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", new):
        raise SystemExit("the new name must be a lowercase Python identifier, e.g. kalibr")
    old, old_title, package = current(root)
    if new == old:
        raise SystemExit(f"already named {old}")
    title = title or new.capitalize()
    pairs = [(word(old.upper()), new.upper()), (word(old_title), title), (word(old), new)]
    changed = []
    for path, text in text_files(root):
        if path.resolve() == Path(__file__).resolve():
            continue
        result = text
        for pattern, replacement in pairs:
            result = pattern.sub(replacement, result)
        if result != text:
            changed.append(str(path.relative_to(root)))
            if not dry_run:
                path.write_text(result, encoding="utf-8", newline="\n")
    if not dry_run:
        package.rename(package.with_name(new))
    changed.append(f"{package.relative_to(root)} -> src/{new}")
    return changed


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("new")
    p.add_argument("--title")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    for line in rename(ROOT, a.new, a.title, a.dry_run):
        print(line)
    if not a.dry_run:
        print("\nnext: pip install -e .  ·  re-register the MCP server  ·  gh repo rename", file=sys.stderr)


if __name__ == "__main__":
    main()
