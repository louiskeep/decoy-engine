"""Every read of a date_shift anchor option goes through ``transforms/dates/entity_offset.py``.

The anchor column of a ``date_shift`` column (``entity_key``, or the older ``group_by``) has to
be found the same way by every site that needs it: the snapshot set both adapters build, the
lossless-ingest set, the native gate, the column-access declaration, the transform reference set
and the handler. A site that reads the option itself can forget ``entity_key`` and run the column
with per-value offsets, silently breaking every entity's intervals.

The walk parses ``src/decoy_engine`` and counts READS of a key: ``.get(k)``, ``.pop(k)``,
``.setdefault(k)``, ``x[k]``, ``k in x``, ``getattr(x, k)`` / ``hasattr(x, k)``, and a string in a
tuple, list or set display (a membership table). ``k`` is the literal or any name or attribute
bound to it (module and function constants, and imported aliases of the option constant). Dict
keys, docstrings and messages are not reads.

(a) ``entity_key`` may be read only in the accessor module.
(b) ``group_by`` reads are pinned per ``(module, function)`` to the readers that are not
    ``date_shift`` anchor sites (group_key, grouped_series, generation, config, and the V1
    ``group_by`` validation). The table may only shrink.
"""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
ACCESSOR = "transforms/dates/entity_offset.py"
OPTION_CONSTANTS = {"entity_key": "ENTITY_KEY_OPTION", "group_by": "GROUP_BY_OPTION"}

_READ_METHODS = frozenset({"get", "pop", "setdefault"})
_ATTR_BUILTINS = frozenset({"getattr", "hasattr"})

# (module, function) -> group_by read count, outside the date_shift anchor sites.
GROUP_BY_READERS: dict[tuple[str, str], int] = {
    ("config/_tables.py", "GenerateColumnConfig._type_params_present"): 2,
    ("execution/_chunked_group_key.py", "group_by_effective_type"): 1,
    ("execution/_column_access.py", "<module>"): 1,
    ("execution/_runner.py", "group_key_group_by_columns"): 1,
    ("execution/_runner.py", "_masked_driver_columns"): 1,
    ("execution/_transforms.py", "_structured_references"): 1,
    ("execution/_unified_slice_admission.py", "cheap_admission"): 1,
    ("execution/native/_chunked_group_key_gate.py", "group_by_columns"): 1,
    ("execution/native/_chunked_schema_rule.py", "group_key_pinned_columns"): 1,
    ("execution/native/_operator_config_rejections.py", "group_key_config_rejection"): 1,
    ("execution/native/_operator_params.py", "resolve_operator_params"): 1,
    ("execution/physical/_shadow_bindings.py", "execution_binding_for_slice_node"): 1,
    ("generation/_grouped_windowed_generators.py", "_grouped_series_generate"): 1,
    ("generation/_grouped_windowed_generators.py", "_group_key_generate"): 1,
    ("generation/_reproducibility.py", "<module>"): 1,
    ("plan/_checks_date_shift.py", "check_date_shift_group_by_refs"): 2,
    ("plan/_checks_date_shift.py", "check_date_preserve_values"): 1,
    ("plan/_checks_group_key.py", "check_group_key_refs"): 2,
    ("plan/_checks_grouped_series.py", "check_grouped_series_refs"): 2,
    ("transforms/group_key.py", "GroupKeyConfig.from_dict"): 1,
    ("transforms/grouped_series.py", "GroupedSeriesConfig.from_dict"): 1,
}


class _Reads(ast.NodeVisitor):
    def __init__(self, key: str) -> None:
        self.key = key
        self.names: set[str] = {OPTION_CONSTANTS[key]}
        self.scope: list[str] = []
        self.found: Counter[str] = Counter()

    def _is_key(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return node.value == self.key
        if isinstance(node, ast.Name):
            return node.id in self.names
        if isinstance(node, ast.Attribute):
            return node.attr in self.names
        return False

    def _hit(self) -> None:
        self.found[".".join(self.scope) or "<module>"] += 1

    def _bind(self, targets: list[ast.expr], value: ast.AST) -> None:
        if self._is_key(value):
            for target in targets:
                if isinstance(target, ast.Name):
                    self.names.add(target.id)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in self.names and alias.asname:
                self.names.add(alias.asname)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._bind(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind([node.target], node.value)
        self.generic_visit(node)

    def _scoped(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _READ_METHODS
            and node.args
            and self._is_key(node.args[0])
        ):
            self._hit()
        if (
            isinstance(func, ast.Name)
            and func.id in _ATTR_BUILTINS
            and len(node.args) > 1
            and self._is_key(node.args[1])
        ):
            self._hit()
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_key(node.slice):
            self._hit()
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and self._is_key(node.left):
            self._hit()
        self.generic_visit(node)

    def _display(self, node: ast.Tuple | ast.List | ast.Set) -> None:
        self.found[".".join(self.scope) or "<module>"] += sum(
            1 for elt in node.elts if isinstance(elt, ast.Constant) and elt.value == self.key
        )
        self.generic_visit(node)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        self._display(node)

    def visit_List(self, node: ast.List) -> None:
        self._display(node)

    def visit_Set(self, node: ast.Set) -> None:
        self._display(node)


def reads(source: str, key: str) -> Counter[str]:
    """Read sites of ``key`` in one module's source, by enclosing function."""
    visitor = _Reads(key)
    visitor.visit(ast.parse(source))
    return +visitor.found


def _walk(key: str) -> Counter[tuple[str, str]]:
    out: Counter[tuple[str, str]] = Counter()
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        rel = path.relative_to(ENGINE_ROOT).as_posix()
        for scope, count in reads(path.read_text(encoding="utf-8"), key).items():
            out[(rel, scope)] += count
    return out


def test_entity_key_is_read_only_by_the_accessor() -> None:
    sites = _walk("entity_key")
    outside = {site: n for site, n in sites.items() if site[0] != ACCESSOR}
    assert not outside, (
        "read entity_key through transforms.dates.entity_offset (date_shift_anchor, "
        f"entity_key_present, entity_column, parse_entity_key), not directly: {outside}"
    )
    assert any(site[0] == ACCESSOR for site in sites), "the walk sees the accessor's own reads"


def test_group_by_readers_match_the_pinned_table() -> None:
    sites = {site: n for site, n in _walk("group_by").items() if site[0] != ACCESSOR}
    assert sites == GROUP_BY_READERS, (
        "a group_by read outside the date_shift accessor was added or removed. A date_shift "
        "site must use date_shift_anchor; a removed reader shrinks this table."
    )


_SEEDED = {
    "literal_get": 'def f(cfg):\n    return cfg.get("entity_key")\n',
    "local_constant": 'K = "entity_key"\ndef f(cfg):\n    return cfg.get(K)\n',
    "aliased_import": (
        "from decoy_engine.transforms.dates.entity_offset import ENTITY_KEY_OPTION as K\n"
        "def f(cfg):\n    return cfg[K]\n"
    ),
    "attribute_constant": "def f(cfg, m):\n    return cfg.get(m.ENTITY_KEY_OPTION)\n",
    "pop": 'def f(cfg):\n    return cfg.pop("entity_key", None)\n',
    "getattr": 'def f(cfg):\n    return getattr(cfg, "entity_key")\n',
    "membership_table": (
        'KEYS = ("entity_key",)\ndef f(cfg):\n    return [k for k in cfg if k in KEYS]\n'
    ),
    "in_test": 'def f(cfg):\n    return "entity_key" in cfg\n',
}


@pytest.mark.parametrize("form", sorted(_SEEDED))
def test_the_walk_sees_each_read_form(form: str) -> None:
    assert sum(reads(textwrap.dedent(_SEEDED[form]), "entity_key").values()) >= 1


def test_the_walk_sees_a_new_group_by_read_in_a_date_shift_function() -> None:
    src = 'def date_shift_anchor_set(cfg):\n    return cfg.get("group_by")\n'
    assert reads(src, "group_by") == Counter({"date_shift_anchor_set": 1})


def test_messages_docstrings_and_dict_keys_are_not_reads() -> None:
    src = (
        'def f(cfg):\n    """Reads entity_key."""\n'
        '    msg = "set entity_key to a column"\n'
        '    return {"entity_key": cfg}, f"{msg}.entity_key"\n'
    )
    assert not reads(src, "entity_key")
