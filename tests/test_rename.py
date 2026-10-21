"""tools/rename.py on a copy of the repository: the renamed package imports and keeps working.
Written against whatever the current name is, so it survives a real rename too."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = next((ROOT / "src").glob("*/__init__.py"))
OLD = re.search(r'^NAME = "(\w+)"$', INIT.read_text(encoding="utf-8"), re.M).group(1)
OLD_TITLE = re.search(r'^TITLE = "([^"]+)"$', INIT.read_text(encoding="utf-8"), re.M).group(1)
NEW = "renamedpkg"


class Rename(unittest.TestCase):
    def test_rename_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "repo"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.egg-info"))
            subprocess.run([sys.executable, str(copy / "tools" / "rename.py"), NEW], check=True,
                           capture_output=True, timeout=120)
            package = copy / "src" / NEW
            self.assertTrue((package / "__init__.py").exists())
            self.assertFalse((copy / "src" / OLD).exists())
            init = (package / "__init__.py").read_text(encoding="utf-8")
            self.assertIn(f'NAME = "{NEW}"', init)
            self.assertIn(f'TITLE = "{NEW.capitalize()}"', init)
            self.assertIn(f'{NEW} = "{NEW}.cli:main"', (copy / "pyproject.toml").read_text(encoding="utf-8"))
            leftover = re.compile(rf"(?<![A-Za-z0-9])({OLD}|{OLD_TITLE}|{OLD.upper()})(?![A-Za-z0-9])")
            for path in package.rglob("*.py"):
                self.assertNotRegex(path.read_text(encoding="utf-8"), leftover, str(path))
            env = {**os.environ, "PYTHONPATH": str(copy / "src")}
            out = subprocess.run([sys.executable, "-c", f"import {NEW}, {NEW}.cli, {NEW}.mcp; print({NEW}.ENV_PREFIX)"],
                                 env=env, capture_output=True, text=True, timeout=120, cwd=tmp)
            self.assertEqual(out.stdout.strip(), NEW.upper() + "_", out.stderr)


if __name__ == "__main__":
    unittest.main()
