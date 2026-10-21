"""Where parts live and where OpenSCAD is: environment first, then sane defaults per OS."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import ENV_PREFIX, TITLE


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + key, default)


def home(override: str | os.PathLike | None = None) -> Path:
    """The folder that holds parts/: --home, then $DATUM_HOME, then ~/Datum."""
    value = override or env("HOME")
    if value:
        return Path(value).expanduser().resolve()
    return Path.home() / TITLE


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


OPENSCAD_CANDIDATES = {
    "win32": [
        r"C:\Program Files\OpenSCAD\openscad.com",
        r"C:\Program Files\OpenSCAD\openscad.exe",
        r"C:\Program Files (x86)\OpenSCAD\openscad.com",
    ],
    "darwin": [
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        "/opt/homebrew/bin/openscad",
        "/usr/local/bin/openscad",
    ],
    "linux": ["/usr/bin/openscad", "/usr/local/bin/openscad", "/snap/bin/openscad"],
}


def _console_variant(path: str) -> str:
    # On Windows openscad.exe is a GUI binary and prints nothing to a pipe; openscad.com is its console twin.
    p = Path(path)
    if p.suffix.lower() == ".exe" and p.with_suffix(".com").exists():
        return str(p.with_suffix(".com"))
    return path


def find_openscad() -> str | None:
    explicit = env("OPENSCAD")
    if explicit:
        found = explicit if Path(explicit).exists() else shutil.which(explicit)
        return _console_variant(found) if found else None
    names = ["openscad.com", "openscad"] if platform_key() == "win32" else ["openscad"]
    for name in names:
        found = shutil.which(name)
        if found:
            return _console_variant(found)
    for candidate in OPENSCAD_CANDIDATES[platform_key()]:
        if Path(candidate).exists():
            return candidate
    return None


def openscad_env() -> dict[str, str]:
    """Environment for OpenSCAD: OPENSCADPATH plus $DATUM_OPENSCADPATH (extra library folders)."""
    result = dict(os.environ)
    extra = env("OPENSCADPATH")
    if extra:
        current = result.get("OPENSCADPATH")
        result["OPENSCADPATH"] = os.pathsep.join(p for p in (current, extra) if p)
    return result


def headless_prefix() -> list[str]:
    """OpenSCAD needs an OpenGL context for PNG; on a Linux box without a display, borrow one from xvfb-run."""
    if platform_key() != "linux" or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return []
    xvfb = shutil.which("xvfb-run")
    return [xvfb, "-a"] if xvfb else []
