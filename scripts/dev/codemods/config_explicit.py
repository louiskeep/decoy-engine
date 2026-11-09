"""CONFIG-EXPLICIT fixture codemod: make csv file sources say `has_header`.

Usage: `python scripts/dev/codemods/config_explicit.py has-header PATH...` rewrites in place and
prints one `<mode> <path>:<line>` row per edit, for the evidence record.

`has-header` adds `has_header: true` to every csv file source that does not set it, which
restates the old default, so the read does not change. A source is a mapping with
`type: file` and `format: csv` that is a value of a `sources` mapping: a Python dict literal
under a `"sources"` key, a `sources=` keyword or a `sources = {...}` assignment, or a YAML
entry under a block-style `sources:` key (in a .yaml file or a string inside a .py file).
Targets, cloud sources, and sources that already set the field are left alone. Anything else
(comprehensions, helpers that build a descriptor, flow-style YAML) is a hand fix.

Later CONFIG-EXPLICIT parts add their modes here and run this file in place; never copy it.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

MODES = ("has-header",)


def _is_str(node: ast.AST | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _source_maps(tree: ast.AST) -> Iterator[ast.Dict]:
    """Every dict literal that is the value of a `sources` mapping."""
    for node in ast.walk(tree):
        values: list[ast.AST] = []
        if isinstance(node, ast.Dict):
            values = [
                v for k, v in zip(node.keys, node.values, strict=True) if _is_str(k, "sources")
            ]
        elif (isinstance(node, ast.keyword) and node.arg == "sources") or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "sources" for t in node.targets)
        ):
            values = [node.value]
        for value in values:
            if isinstance(value, ast.Dict):
                yield value


def _csv_file_format_node(source: ast.AST) -> ast.Constant | None:
    """The `"csv"` value node of a csv file source without `has_header`, else None."""
    if not isinstance(source, ast.Dict) or any(k is None for k in source.keys):
        return None
    fields = {
        k.value: v
        for k, v in zip(source.keys, source.values, strict=True)
        if isinstance(k, ast.Constant)
    }
    if "has_header" in fields or not _is_str(fields.get("type"), "file"):
        return None
    fmt = fields.get("format")
    return fmt if isinstance(fmt, ast.Constant) and fmt.value == "csv" else None


def python_edits(text: str) -> list[tuple[int, int]]:
    """(line, column) just after each `"csv"` format value that needs the field."""
    edits: set[tuple[int, int]] = set()
    for sources in _source_maps(ast.parse(text)):
        for source in sources.values:
            fmt = _csv_file_format_node(source)
            if fmt is not None and fmt.end_lineno is not None and fmt.end_col_offset is not None:
                edits.add((fmt.end_lineno, fmt.end_col_offset))
    return sorted(edits)


def _apply_python(text: str) -> tuple[str, list[int]]:
    lines = text.splitlines(keepends=True)
    done: list[int] = []
    for line_no, col in reversed(python_edits(text)):
        # AST columns are UTF-8 byte offsets.
        raw = lines[line_no - 1].encode("utf-8")
        lines[line_no - 1] = (raw[:col] + b', "has_header": True' + raw[col:]).decode("utf-8")
        done.append(line_no)
    return "".join(lines), sorted(done)


_KEY = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z_][\w.-]*|\"[^\"]+\"|'[^']+'):[ \t]*(#.*)?$")
_FIELD = re.compile(r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_]+):[ \t]*(?P<value>.*?)[ \t]*$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _block(lines: list[str], start: int, parent: int) -> int:
    """Index after the block of lines indented deeper than `parent`, starting at `start`."""
    end = start
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > parent):
        end += 1
    return end


def yaml_edits(lines: list[str]) -> list[int]:
    """Indexes of the `format: csv` lines that need `has_header` after them."""
    edits: list[int] = []
    for i, line in enumerate(lines):
        match = _KEY.match(line.rstrip("\r\n"))
        if not match or match["key"] != "sources":
            continue
        parent = len(match["indent"])
        end = _block(lines, i + 1, parent)
        j = i + 1
        while j < end:
            if not lines[j].strip() or not _KEY.match(lines[j].rstrip("\r\n")):
                j += 1
                continue
            entry_indent = _indent(lines[j])
            body_end = _block(lines, j + 1, entry_indent)
            fields: dict[str, tuple[int, str]] = {}
            for k in range(j + 1, body_end):
                field = _FIELD.match(lines[k].rstrip("\r\n"))
                if field and lines[k].strip() and _indent(lines[k]) == _indent(lines[j + 1]):
                    fields[field["name"]] = (k, field["value"].strip("'\""))
            if (
                fields.get("type", (0, ""))[1] == "file"
                and fields.get("format", (0, ""))[1] == "csv"
                and "has_header" not in fields
            ):
                edits.append(fields["format"][0])
            j = body_end
    return edits


def _apply_yaml(text: str) -> tuple[str, list[int]]:
    lines = text.splitlines(keepends=True)
    edits = yaml_edits(lines)
    for index in reversed(edits):
        line = lines[index]
        ending = line[len(line.rstrip("\r\n")) :] or "\n"
        lines.insert(index + 1, f"{line[: _indent(line)]}has_header: true{ending}")
    return "".join(lines), [i + 1 for i in edits]


def rewrite(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    rows: list[str] = []
    if path.suffix == ".py":
        text, py_lines = _apply_python(text)
        rows += [f"has-header {path}:{n} python" for n in py_lines]
    text, yaml_lines = _apply_yaml(text)
    rows += [f"has-header {path}:{n} yaml" for n in yaml_lines]
    if rows:
        path.write_text(text, encoding="utf-8")
    return rows


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in MODES:
        print(f"usage: config_explicit.py {{{','.join(MODES)}}} PATH...", file=sys.stderr)
        return 2
    for arg in argv[1:]:
        root = Path(arg)
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.suffix in {".py", ".yaml", ".yml"} and path.is_file():
                for row in rewrite(path):
                    print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
