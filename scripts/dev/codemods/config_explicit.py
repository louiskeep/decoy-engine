"""CONFIG-EXPLICIT fixture codemod: make csv file sources say `has_header`.

Usage: `python scripts/dev/codemods/config_explicit.py MODE PATH...` rewrites in place and
prints one `<mode> <path>:<line>` row per edit, for the evidence record. Modes:

- `has-header`: described below.
- `declare-columns --record out.jsonl PATH...`: applies the records `declare_columns_probe.py`
  wrote (plus the per-worker files of the run its manifest line names). An undeclared column gets a `passthrough` entry in the one dict literal
  `{"name": <table>, "columns": [...]}` (in the recorded test file or a directly imported local
  helper) whose constant column names equal the declared names; an undeclared table gets a new
  `tables` entry beside its `sources` key. Zero or several matches print `hand <node> <table>`.
  Only Python dict literals are handled; YAML fixtures are hand rows.
- `source-schema PATH...`: adds `source_schema=declared_schema(config, table)` to each
  `run_mask_pipeline_chunked(` call that lacks it, when the config is a plain name and `table=`
  is a constant or a name. Other shapes print `hand <path>:<line>`.

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
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

MODES = ("has-header", "declare-columns", "source-schema")


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


def _const_names(columns: ast.List) -> set[str] | None:
    """The constant `name` of every entry of a columns list, or None if any entry is not one."""
    names: set[str] = set()
    for entry in columns.elts:
        value = None
        if isinstance(entry, ast.Dict):
            for k, v in zip(entry.keys, entry.values, strict=True):
                if _is_str(k, "name") and isinstance(v, ast.Constant):
                    value = v.value
        if not isinstance(value, str):
            return None
        names.add(value)
    return names


def _local_modules(tree: ast.AST) -> list[Path]:
    """Files of the `from x.y import ...` modules that exist under the working directory."""
    paths = (
        Path(*node.module.split(".")).with_suffix(".py")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    )
    return [p for p in paths if p.is_file()]


def _fields(node: ast.Dict) -> dict[str, ast.AST]:
    return {
        k.value: v
        for k, v in zip(node.keys, node.values, strict=True)
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


def _list_matches(tree: ast.AST, record: dict) -> list[ast.List]:
    """Column lists of `{"name": <table>, "columns": [...]}` whose names are the declared set."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = _fields(node)
        columns = fields.get("columns")
        if _is_str(fields.get("name"), record["table"]) and isinstance(columns, ast.List):
            if _const_names(columns) == set(record["declared"]):
                out.append(columns)
    return out


def _tables_matches(tree: ast.AST, record: dict) -> list[ast.List]:
    """`tables` lists of the dict literal whose `sources` mapping names the table."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        fields = _fields(node)
        sources, tables = fields.get("sources"), fields.get("tables")
        if (
            isinstance(sources, ast.Dict)
            and isinstance(tables, ast.List)
            and any(_is_str(k, record["table"]) for k in sources.keys)
        ):
            out.append(tables)
    return out


def _entries(names: list[str]) -> list[str]:
    return [f'{{"name": {json.dumps(n)}, "strategy": "passthrough"}}' for n in names]


def _append_edit(target: ast.List, texts: list[str]) -> tuple[int, int, str]:
    """(line, byte column, text) that appends `texts` to the list in its own trailing style."""
    if not target.elts:
        return target.lineno, target.col_offset + 1, ", ".join(texts)
    last = target.elts[-1]
    end = (last.end_lineno or last.lineno, last.end_col_offset or 0)
    if last.lineno > target.lineno:
        indent = " " * last.col_offset
        return end[0], end[1], "".join(f",\n{indent}{t}" for t in texts)
    return end[0], end[1], "".join(f", {t}" for t in texts)


def _record_lines(record_file: Path) -> list[str]:
    """Record rows of one probe run. Rows are in `record_file` itself, or in the per-worker
    `<record>.<run>.<worker>` files of the run its `{"run": id}` manifest line names; files of
    any other run are stale and never read."""
    lines = record_file.read_text(encoding="utf-8").splitlines() if record_file.is_file() else []
    out = [ln for ln in lines if ln.strip() and "run" not in json.loads(ln)]
    for ln in lines:
        if ln.strip() and "run" in json.loads(ln):
            run = re.escape(str(json.loads(ln)["run"]))
            for f in sorted(record_file.parent.glob(f"{record_file.name}.*")):
                if re.fullmatch(rf"{re.escape(record_file.name)}\.{run}\.[^.]+", f.name):
                    out += [x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def _declare(record_file: Path, roots: list[Path]) -> list[str]:
    groups: dict[tuple, dict] = {}
    for line in _record_lines(record_file):
        rec = json.loads(line)
        key = (
            rec["frame"].rsplit(":", 1)[0],
            rec["table"],
            rec["kind"],
            frozenset(rec["declared"]),
        )
        merged = groups.setdefault(key, {**rec, "undeclared": [], "nodes": []})
        merged["undeclared"] += [n for n in rec["undeclared"] if n not in merged["undeclared"]]
        merged["nodes"].append(rec["node"])
    rows: list[str] = []
    # One entry per exact literal: records from different tests that reach the same helper
    # literal add their names to it, so the literal is edited once.
    targets: dict[tuple[Path, int, int], dict] = {}
    for (file, _table, kind, _), rec in groups.items():
        path = Path(file)
        if not path.is_file() or not any(r == path or r in path.parents for r in roots):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        finder = _list_matches if kind == "column" else _tables_matches
        hits = [(path, m) for m in finder(tree, rec)]
        for helper in _local_modules(tree):
            hits += [
                (helper, m) for m in finder(ast.parse(helper.read_text(encoding="utf-8")), rec)
            ]
        if len(hits) != 1:
            rows.append(f"hand {rec['nodes'][0]} {rec['table']}")
            continue
        target_path, target = hits[0]
        entry = targets.setdefault(
            (target_path, target.lineno, target.col_offset),
            {"list": target, "kind": kind, "tables": {}},
        )
        names = entry["tables"].setdefault(rec["table"], [])
        names += [n for n in rec["undeclared"] if n not in names]
    edits: dict[Path, list[tuple[int, int, str]]] = {}
    for (target_path, _, _), entry in targets.items():
        # A column list belongs to one table; a `tables` list takes one entry per table.
        if entry["kind"] == "table":
            texts = [
                f'{{"name": {json.dumps(table)}, "columns": [{", ".join(_entries(names))}]}}'
                for table, names in entry["tables"].items()
            ]
        else:
            texts = _entries([n for names in entry["tables"].values() for n in names])
        edit = _append_edit(entry["list"], texts)
        edits.setdefault(target_path, []).append(edit)
        rows.append(f"declare-columns {target_path}:{edit[0]}")
    for path, items in edits.items():
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for line_no, col, text in sorted(set(items), reverse=True):
            raw = lines[line_no - 1].encode("utf-8")
            lines[line_no - 1] = (raw[:col] + text.encode("utf-8") + raw[col:]).decode("utf-8")
        path.write_text("".join(lines), encoding="utf-8")
    return rows


def _source_schema(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if "run_mask_pipeline_chunked(" not in text:
        return []
    lines = text.splitlines(keepends=True)
    rows: list[str] = []
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(ast.parse(text)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_mask_pipeline_chunked"
        ):
            continue
        keywords = {k.arg: k for k in node.keywords}
        if "source_schema" in keywords:
            continue
        config = node.args[0] if node.args else getattr(keywords.get("config"), "value", None)
        table = getattr(keywords.get("table"), "value", None)
        if (
            None in keywords
            or not isinstance(config, ast.Name)
            or not isinstance(table, ast.Constant | ast.Name)
            or table.end_lineno is None
        ):
            rows.append(f"hand {path}:{node.lineno}")
            continue
        value = ast.get_source_segment(text, table)
        edits.append(
            (
                table.end_lineno,
                table.end_col_offset or 0,
                f", source_schema=declared_schema({config.id}, {value})",
            )
        )
        rows.append(f"source-schema {path}:{node.lineno}")
    for line_no, col, extra in sorted(edits, reverse=True):
        raw = lines[line_no - 1].encode("utf-8")
        lines[line_no - 1] = (raw[:col] + extra.encode("utf-8") + raw[col:]).decode("utf-8")
    if edits:
        import_line = "from tests._declared_schema import declared_schema\n"
        if import_line not in lines:
            imports = [
                n.end_lineno or 0
                for n in ast.parse(text).body
                if isinstance(n, ast.Import | ast.ImportFrom)
            ]
            lines.insert(max(imports, default=0), import_line)
        path.write_text("".join(lines), encoding="utf-8")
    return rows


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in MODES:
        print(f"usage: config_explicit.py {{{','.join(MODES)}}} PATH...", file=sys.stderr)
        return 2
    mode, rest = argv[0], argv[1:]
    record = None
    if mode == "declare-columns":
        if rest[0] != "--record" or len(rest) < 3:
            print(
                "usage: config_explicit.py declare-columns --record OUT.jsonl PATH...",
                file=sys.stderr,
            )
            return 2
        record, rest = Path(rest[1]), rest[2:]
        for row in _declare(record, [Path(a) for a in rest]):
            print(row)
        return 0
    for arg in rest:
        root = Path(arg)
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if mode == "source-schema":
                if path.suffix == ".py" and path.is_file():
                    for row in _source_schema(path):
                        print(row)
            elif path.suffix in {".py", ".yaml", ".yml"} and path.is_file():
                for row in rewrite(path):
                    print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
