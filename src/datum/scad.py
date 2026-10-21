"""Read and rewrite the top-level parameters of an OpenSCAD file.

A parameter is a top-level assignment `name = value; // comment`. Literals
(numbers, booleans, strings, vectors of literals) can be changed; expressions
such as `L = L_BODY + BOSS_H;` are reported as derived and left alone unless
forced. The source stays the single source of truth: Datum edits the literal in
place and keeps the comment, so the file opens in OpenSCAD with current values.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any

_HEAD = re.compile(r"^\s*(?P<name>[A-Za-z_$][\w$]*)\s*=(?!=)\s*")
_LEADING_DOT = re.compile(r"(?<![\w.])(-?)\.(\d)")


def _assignment(code: str) -> tuple[str, int, int] | None:
    """`name = value;` -> (name, value_start, value_end), with the ';' found outside strings."""
    head = _HEAD.match(code)
    if not head:
        return None
    in_str = False
    i = head.end()
    while i < len(code):
        ch = code[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == ";":
            end = i
            while end > head.end() and code[end - 1].isspace():
                end -= 1
            return head["name"], head.end(), end
        i += 1
    return None


@dataclass
class Param:
    name: str
    value: Any          # python value for literals, None for expressions
    raw: str            # the text between '=' and ';'
    literal: bool
    comment: str
    line: int           # 1-based

    def as_dict(self) -> dict:
        return asdict(self)


def _code_and_comment(line: str) -> tuple[str, str]:
    """Split a line at the first // that is not inside a string."""
    in_str = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and in_str:
            i += 2
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str and line.startswith("//", i):
            return line[:i], line[i + 2:].strip()
        i += 1
    return line, ""


def parse_literal(text: str) -> tuple[bool, Any]:
    """OpenSCAD literal -> (True, value); anything else -> (False, None)."""
    text = text.strip()
    if not text or text == "undef":
        return False, None
    try:
        value = json.loads(_LEADING_DOT.sub(r"\g<1>0.\2", text))
    except ValueError:
        return False, None
    if value is None or isinstance(value, dict):
        return False, None
    return True, value


def to_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text[:-2] if text.endswith(".0") else text
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(to_literal(v) for v in value) + "]"
    raise ValueError(f"cannot write {value!r} as an OpenSCAD literal")


def parse_cli_value(text: str) -> Any:
    """`k=v` from a command line: numbers, true/false, [vectors] and "strings"; a bare word is a string."""
    ok, value = parse_literal(text)
    return value if ok else text


def _top_level_lines(source: str):
    """Yield (index, line) for lines that start at brace depth 0, outside block comments."""
    depth = 0
    in_block = False
    for index, line in enumerate(source.splitlines()):
        start_depth, start_block = depth, in_block
        i = 0
        in_str = False
        while i < len(line):
            if in_block:
                end = line.find("*/", i)
                if end < 0:
                    break
                in_block, i = False, end + 2
                continue
            ch = line[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif line.startswith("//", i):
                break
            elif line.startswith("/*", i):
                in_block, i = True, i + 2
                continue
            elif ch in "{(":
                depth += 1
            elif ch in "})":
                depth = max(0, depth - 1)
            i += 1
        if start_depth == 0 and not start_block:
            yield index, line


def parse_params(source: str) -> list[Param]:
    params: list[Param] = []
    seen: set[str] = set()
    for index, line in _top_level_lines(source):
        code, comment = _code_and_comment(line)
        found = _assignment(code)
        if not found:
            continue
        name, start, end = found
        if name in seen:          # OpenSCAD: the last assignment wins; report the first, rewrite it too
            continue
        seen.add(name)
        raw = code[start:end].strip()
        literal, value = parse_literal(raw)
        params.append(Param(name, value, raw, literal, comment, index + 1))
    return params


def set_params(source: str, updates: dict[str, Any], *, force: bool = False) -> tuple[str, list[str]]:
    """Return (new source, change notes). Unknown names and derived parameters raise unless forced."""
    lines = source.splitlines(keepends=True)
    by_name = {p.name: p for p in parse_params(source)}
    notes = []
    for name, value in updates.items():
        param = by_name.get(name)
        if param is None:
            raise KeyError(f"no top-level parameter {name!r}")
        if not param.literal and not force:
            raise ValueError(f"{name} is derived ({param.raw}); change what it depends on, or force")
        original = lines[param.line - 1]
        ending = original[len(original.rstrip("\r\n")):]
        code, _ = _code_and_comment(original.rstrip("\r\n"))
        _, start, end = _assignment(code)
        new_code = code[:start] + to_literal(value) + code[end:]
        lines[param.line - 1] = new_code + original.rstrip("\r\n")[len(code):] + ending
        notes.append(f"{name}: {param.raw} -> {to_literal(value)}")
    return "".join(lines), notes
