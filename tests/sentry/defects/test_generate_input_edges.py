"""GEN-REL: a generate column's inputs have one registry, one resolver and one parent lookup.

Which column a generate option reads is declared once, as `OptionSpec.ref_kind` (with
`ref_context_key` and `ref_phase`) in `config/_generate_options.py`, and resolved once, by
`iter_generate_input_refs`. The plan check, numeric typing and the reproducibility report all
consume the resolved edges. A second option-key table or a consumer that resolves an option
value by itself would read a same-named column of the wrong table, so this sentry fails on:

1. a read of a descriptor field outside the registry module;
2. a consumer that stops calling the resolver, or a new caller that is not inventoried;
3. any of the retired per-consumer tables and resolvers coming back;
4. a cross-table descriptor turned back into a same-table `column`: the acceptance cells that
   depend on it are run against that descriptor and must fail.

The row a child's foreign key points at is found once too, by `lookup_parent_values` in
`generation/relational/_parent_lookup.py`:

5. no other generation module builds a key-to-row map over a `pools[...]` column;
6. the lookup's callers are inventoried by exact file and function.

The meta-tests seed one realistic bypass of each scan and require the scan to see it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.config import _generate_options as registry
from decoy_engine.generation._numeric_typing import numeric_plan
from decoy_engine.generation._reproducibility import reproducibility_exclusions
from decoy_engine.plan import PlanCompileError

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
REGISTRY_FILE = "config/_generate_options.py"
RESOLVER = "iter_generate_input_refs"
DESCRIPTOR_FIELDS = frozenset({"ref_kind", "ref_context_key", "ref_phase"})
# Every function that asks the resolver for a column's inputs.
RESOLVER_CALLERS = frozenset(
    {
        ("plan/checks/_generate_columns.py", "_check_input_edges"),
        ("generation/_numeric_typing.py", "_readers"),
        ("generation/_reproducibility.py", "reproducibility_exclusions"),
        ("generation/_grouped_windowed_generators.py", "_parent_anchor_cells"),
    }
)
# The per-consumer tables and resolvers the one registry replaced.
RETIRED = frozenset(
    {
        "READER_KEYS",
        "_names_read",
        "_SIBLING_KEYS",
        "_sibling_inputs",
        "_referenced",
        "_check_forward_references",
        "_expression_refs",
    }
)
LOOKUP = "lookup_parent_values"
LOOKUP_FILE = "generation/relational/_parent_lookup.py"
LOOKUP_CALLERS = frozenset({("generation/_grouped_windowed_generators.py", "_parent_anchor_cells")})
POOL_NAMES = frozenset({"pools"})


def _sources() -> dict[str, str]:
    return {p.relative_to(SRC).as_posix(): p.read_text() for p in sorted(SRC.rglob("*.py"))}


SOURCES = _sources()


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _owners(tree: ast.AST) -> dict[int, str]:
    """Node id -> the innermost enclosing function's qualified name."""
    owner: dict[int, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(child):
                    owner[id(sub)] = f"{prefix}{child.name}"
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return owner


def callers_of(sources: dict[str, str], name: str) -> set[tuple[str, str]]:
    """(file, enclosing function) of every call to `name`."""
    found: set[tuple[str, str]] = set()
    for rel, text in sources.items():
        tree = ast.parse(text)
        owner = _owners(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee(node) == name:
                found.add((rel, owner.get(id(node), "<module>")))
    return found


def descriptor_reads(sources: dict[str, str]) -> list[str]:
    """`file:line` of each read of a descriptor field outside the registry module."""
    found: list[str] = []
    for rel, text in sources.items():
        if rel == REGISTRY_FILE:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Attribute) and node.attr in DESCRIPTOR_FIELDS:
                found.append(f"{rel}:{node.lineno}")
    return found


def retired_names(sources: dict[str, str]) -> list[str]:
    found: list[str] = []
    for rel, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            name = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.name
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                else None
            )
            if name in RETIRED:
                found.append(f"{rel}:{getattr(node, 'lineno', 0)} {name}")
    return found


# ---- 1 to 3: one registry, one resolver --------------------------------------------------------


def test_descriptor_fields_are_read_only_by_the_registry_module() -> None:
    assert descriptor_reads(SOURCES) == []


def test_the_resolver_is_defined_once_and_called_by_the_inventoried_consumers() -> None:
    defined = [
        rel
        for rel, text in SOURCES.items()
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.FunctionDef) and node.name == RESOLVER
    ]
    assert defined == [REGISTRY_FILE]
    assert callers_of(SOURCES, RESOLVER) == RESOLVER_CALLERS


def test_the_retired_tables_and_resolvers_are_gone() -> None:
    assert retired_names(SOURCES) == []


def test_meta_a_second_resolver_or_table_is_seen() -> None:
    rogue = (
        "from decoy_engine.config._generate_options import GENERATE_OPTIONS\n"
        "_SIBLING_KEYS = ('anchor',)\n"
        "def inputs(col):\n"
        "    spec = GENERATE_OPTIONS[col['type']]\n"
        "    return [col[k] for k, o in spec.keys.items() if o.ref_kind == 'column']\n"
    )
    sources = {**SOURCES, "generation/_rogue.py": rogue}
    assert descriptor_reads(sources) == ["generation/_rogue.py:5"]
    assert retired_names(sources) == ["generation/_rogue.py:2 _SIBLING_KEYS"]
    extra = "def order(table, col):\n    return iter_generate_input_refs(table, col)\n"
    assert callers_of({**SOURCES, "generation/_rogue.py": extra}, RESOLVER) - RESOLVER_CALLERS == {
        ("generation/_rogue.py", "order")
    }


# ---- 4: a cross-table descriptor is what the acceptance cells stand on -------------------------

CROSS_TABLE = sorted(
    (type_, key)
    for type_, spec in registry.GENERATE_OPTIONS.items()
    for key, option in spec.keys.items()
    if option.ref_kind in {"column_via_reference", "column_in_named_table"}
)
DATE = {"name": "born", "type": "categorical", "categories": ["2001-02-03"]}
COUNTER = {"name": "born", "type": "sequence", "start": 500}
FK = {"name": "pid", "type": "reference", "reference_table": "p", "reference_column": "id"}
VISIT = {
    "name": "visit",
    "type": "windowed_date",
    "anchor": "born",
    "anchor_via": "pid",
    "max_days": 3,
}


def _config(kids: list[dict[str, Any]], parent_born: dict[str, Any] | None = DATE) -> dict:
    parents = [{"name": "id", "type": "sequence", "start": 1}]
    if parent_born is not None:
        parents.append(parent_born)
    return {
        "global_settings": {"seed": 1},
        "tables": [
            {"name": "p", "row_count": 3, "generate_columns": parents},
            {"name": "t", "row_count": 6, "generate_columns": kids},
        ],
    }


def _accepted(config: dict[str, Any]) -> bool:
    try:
        run_config_only_checks(config)
    except PlanCompileError:
        return False
    return True


def _cells() -> dict[str, bool]:
    """The acceptance cells that hold only while the anchor is resolved across tables."""
    with_sibling = _config([COUNTER, FK, VISIT])
    return {
        "a parent anchor with no same-named child column is accepted": _accepted(
            _config([FK, VISIT])
        ),
        "an anchor missing from the parent is refused": not _accepted(
            _config([DATE, FK, VISIT], parent_born=None)
        ),
        "the unrelated child column keeps its numeric type": numeric_plan(with_sibling).get(
            ("t", "born")
        )
        == pa.int64(),
        "the date depends on the parent anchor": reproducibility_exclusions(
            _config([FK, VISIT], parent_born={**DATE, "determinism": "fresh"})
        ).get(("t", "visit"))
        is not None,
    }


def test_the_cross_table_descriptors_are_the_ones_this_sentry_knows() -> None:
    assert CROSS_TABLE == [("windowed_date", "anchor")]


def test_the_acceptance_cells_hold_on_the_registered_descriptors() -> None:
    assert _cells() == dict.fromkeys(_cells(), True)


@pytest.mark.parametrize(("type_", "key"), CROSS_TABLE)
def test_each_cross_table_descriptor_turned_back_to_column_fails_every_cell(
    type_: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = registry.GENERATE_OPTIONS[type_]
    as_sibling = registry.OptionSpec(spec.keys[key].check, ref_kind="column")
    mutated = registry.TypeOptions(
        {**spec.keys, key: as_sibling},
        required=spec.required,
        closed_by=spec.closed_by,
        plan_required=spec.plan_required,
        one_of=spec.one_of,
    )
    monkeypatch.setitem(registry.GENERATE_OPTIONS, type_, mutated)  # type: ignore[index]
    assert _cells() == dict.fromkeys(_cells(), False)


# ---- 5 and 6: one parent lookup ----------------------------------------------------------------


def _mentions_pool(node: ast.AST, tainted: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            base = sub.value
            name = (
                base.id
                if isinstance(base, ast.Name)
                else base.attr
                if isinstance(base, ast.Attribute)
                else ""
            )
            if name in POOL_NAMES:
                return True
        if isinstance(sub, ast.Name) and sub.id in tainted:
            return True
    return False


def _tainted_names(fn: ast.AST) -> set[str]:
    """Local names bound (directly or through another such name) to a `pools[...]` value."""
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            if not _mentions_pool(node.value, tainted):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name) and sub.id not in tainted:
                        tainted.add(sub.id)
                        changed = True
    return tainted


def _builds_an_index(loop: ast.For) -> bool:
    for node in ast.walk(loop):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Subscript) for t in targets):
            return True
        if isinstance(node, ast.Call) and _callee(node) == "setdefault":
            return True
    return False


def pool_key_maps(source: str) -> list[int]:
    """Lines that build a key-to-row map (or index) over the values of a `pools[...]` column."""
    tree = ast.parse(source)
    lines: set[int] = set()
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in scopes or [tree]:
        tainted = _tainted_names(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.DictComp):
                if any(_mentions_pool(g.iter, tainted) for g in node.generators):
                    lines.add(node.lineno)
            elif isinstance(node, ast.Call) and _callee(node) == "dict":
                if any(_mentions_pool(a, tainted) for a in node.args):
                    lines.add(node.lineno)
            elif isinstance(node, ast.For):
                if _mentions_pool(node.iter, tainted) and _builds_an_index(node):
                    lines.add(node.lineno)
    return sorted(lines)


def _generation_sources() -> dict[str, str]:
    return {rel: text for rel, text in SOURCES.items() if rel.startswith("generation/")}


def test_only_the_parent_lookup_builds_a_key_to_row_map_over_a_pool_column() -> None:
    builders = {rel for rel, text in _generation_sources().items() if pool_key_maps(text)}
    assert builders == {LOOKUP_FILE}


def test_the_parent_lookup_is_defined_once_and_called_by_the_inventoried_generators() -> None:
    defined = [
        rel
        for rel, text in SOURCES.items()
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.FunctionDef) and node.name == LOOKUP
    ]
    assert defined == [LOOKUP_FILE]
    assert callers_of(SOURCES, LOOKUP) == LOOKUP_CALLERS


INLINE_LOOKUPS = {
    "dict_comprehension": (
        "def anchors(pools, fks):\n"
        "    parent = pools['parents']\n"
        "    keys = parent.column('id').to_pylist()\n"
        "    row_of = {k: i for i, k in enumerate(keys)}\n"
        "    births = parent.column('birth').to_pylist()\n"
        "    return [births[row_of[fk]] for fk in fks]\n"
    ),
    "dict_of_zip": (
        "def anchors(pools, fks):\n"
        "    born = dict(zip(pools['parents'].column('id').to_pylist(),\n"
        "                    pools['parents'].column('birth').to_pylist()))\n"
        "    return [born.get(fk) for fk in fks]\n"
    ),
    "loop_index": (
        "def anchors(pools, fks):\n"
        "    row_of = {}\n"
        "    for i, key in enumerate(pools['parents'].column('id').to_pylist()):\n"
        "        row_of[key] = i\n"
        "    return [row_of[fk] for fk in fks]\n"
    ),
    "method_pool": (
        "class Stream:\n"
        "    def anchors(self, fks):\n"
        "        table = self.pools['parents']\n"
        "        return {k: v for k, v in zip(table['id'].to_pylist(), table['b'].to_pylist())}\n"
    ),
}


@pytest.mark.parametrize("shape", sorted(INLINE_LOOKUPS))
def test_meta_an_inline_parent_lookup_is_seen(shape: str) -> None:
    assert pool_key_maps(INLINE_LOOKUPS[shape]), f"{shape}: the scan missed an inline lookup"


def test_meta_reading_a_pool_column_without_a_map_is_not_flagged() -> None:
    clean = (
        "def keys(pools, table, column):\n"
        "    seen, out = set(), []\n"
        "    for v in pools[table].column(column).to_pylist():\n"
        "        if v is not None and v not in seen:\n"
        "            seen.add(v)\n"
        "            out.append(v)\n"
        "    return out\n"
    )
    assert pool_key_maps(clean) == []


def test_meta_the_scan_sees_the_real_lookup() -> None:
    assert pool_key_maps(SOURCES[LOOKUP_FILE])
