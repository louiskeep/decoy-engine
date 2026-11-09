"""AT-E1-9: the csv dialect keys of a source are read in one place.

`profile/_csv_reader.py` (`resolve_csv_dialect`) and `config/_sources.py` (`FileSource`) are
the only engine modules that read `has_header`, `column_names` or `column_names_file` from a
source mapping, build a `CsvDialect` or `CsvFileSource` without saying `has_header`, or splat
a mapping into either. A second reader would bring back a silent `has_header` default.

Covered shapes (accidental spellings, not deliberate evasion): a key read by `.get`, by
subscript or by `in`, the key written as a literal or as a module-level constant; a `**mapping`
splat into `CsvDialect(...)` or `CsvFileSource(...)`; either constructor called without
`has_header` (keyword, or the positional slot: first for `CsvDialect`, second for
`CsvFileSource`), through any import alias; more than one `csv.reader` call in
`profile/_csv_reader.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.sentry.test_csv_scan_single_copy import csv_reader_calls

SRC = Path(__file__).parents[3] / "src" / "decoy_engine"
RESOLVER = "profile/_csv_reader.py"
ALLOWED = frozenset({RESOLVER, "config/_sources.py"})
KEYS = frozenset({"has_header", "column_names", "column_names_file"})
# The positional slot of `has_header` in each constructor.
CONSTRUCTORS = {"CsvDialect": 0, "CsvFileSource": 1}


def _key_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound to one of the dialect keys."""
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Constant)
            and node.value.value in KEYS
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _constructor_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> constructor, for `from x import CsvDialect as D` and plain imports."""
    aliases = {name: name for name in CONSTRUCTORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in CONSTRUCTORS:
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _is_key(node: ast.AST, constants: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in KEYS
    return isinstance(node, ast.Name) and node.id in constants


def _constructor(func: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(func, ast.Name):
        return aliases.get(func.id)
    if isinstance(func, ast.Attribute) and func.attr in CONSTRUCTORS:
        return func.attr
    return None


def dialect_reads(source: str) -> list[str]:
    """Each place in `source` that reads a dialect key or builds a dialect around the resolver."""
    tree = ast.parse(source)
    constants = _key_constants(tree)
    aliases = _constructor_aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and node.args
                and _is_key(node.args[0], constants)
            ):
                found.append(f"{node.lineno}: .get of a dialect key")
            name = _constructor(func, aliases)
            if name is not None:
                if any(k.arg is None for k in node.keywords):
                    found.append(f"{node.lineno}: ** splat into {name}")
                elif not any(k.arg == "has_header" for k in node.keywords) and (
                    len(node.args) <= CONSTRUCTORS[name]
                    or isinstance(node.args[CONSTRUCTORS[name]], ast.Starred)
                ):
                    found.append(f"{node.lineno}: {name} without has_header")
        elif isinstance(node, ast.Subscript) and _is_key(node.slice, constants):
            found.append(f"{node.lineno}: subscript of a dialect key")
        elif isinstance(node, ast.Compare) and _is_key(node.left, constants):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                found.append(f"{node.lineno}: `in` test of a dialect key")
    return found


def test_the_dialect_keys_are_read_only_by_the_resolver_and_the_model() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        found = dialect_reads(path.read_text(encoding="utf-8"))
        if found:
            offenders[rel] = found
    assert not offenders, f"csv dialect read outside the resolver: {offenders}"


def test_the_resolver_module_keeps_one_csv_reader() -> None:
    assert len(csv_reader_calls((SRC / RESOLVER).read_text(encoding="utf-8"))) == 1


SEEDED = {
    "get-literal": 'def f(d):\n    return d.get("column_names")\n',
    "get-constant": '_K = "has_header"\ndef f(d):\n    return d.get(_K)\n',
    "subscript": 'def f(d):\n    return d["column_names_file"]\n',
    "in": 'def f(d):\n    return "has_header" in d\n',
    "splat": "from x import CsvDialect\ndef f(d):\n    return CsvDialect(**d)\n",
    "dialect-bare": "from x import CsvDialect as D\ndef f():\n    return D()\n",
    "dialect-delimiter-only": "from x import CsvDialect\ndef f():\n    return CsvDialect(delimiter=',')\n",
    "source-path-only": "from x import CsvFileSource\ndef f(p):\n    return CsvFileSource(p)\n",
    "source-attribute": "import x\ndef f(p):\n    return x.CsvFileSource(p, delimiter=',')\n",
}

CLEAN = {
    "dialect-positional": "from x import CsvDialect\ndef f():\n    return CsvDialect(True)\n",
    "dialect-keyword": "from x import CsvDialect\ndef f():\n    return CsvDialect(has_header=False, column_names=['a'])\n",
    "source-positional": "from x import CsvFileSource\ndef f(p):\n    return CsvFileSource(p, True)\n",
    "other-key": 'def f(d):\n    return d.get("delimiter"), d["path"], "format" in d\n',
}


def test_the_walk_sees_every_seeded_shape() -> None:
    for name, source in SEEDED.items():
        assert dialect_reads(source), name
    for name, source in CLEAN.items():
        assert dialect_reads(source) == [], name


def test_a_second_csv_reader_call_is_seen() -> None:
    source = (SRC / RESOLVER).read_text(encoding="utf-8")
    assert len(csv_reader_calls(source + "\nx = csv.reader([])\n")) == 2
