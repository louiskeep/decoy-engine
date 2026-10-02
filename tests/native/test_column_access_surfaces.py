"""B8 guards G1 to G6: every pandas dispatch surface declares the columns it touches.

`read_set` (the carry plan's input) is the union of `column_access` declarations. A
strategy, composite provider, dispatch branch, node kind or frame-setup helper that has no
declaration fails here, so "a handler reads or writes a column the scanner did not know
about" cannot return as a review hope. G5 runs the handlers and compares what they really
touch with what was declared. These tests are written before the implementation.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pytest

from decoy_engine.execution import _column_access
from decoy_engine.execution._column_access import (
    SIBLING_REFERENCE_KEYS,
    SURFACE_DECLARATIONS,
    ColumnAccess,
    column_access,
)
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution._runner import build_work_list
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._strategies._composite import CompositeHandler
from decoy_engine.generation.composite import _generator
from decoy_engine.generation.composite._address import CompositeAddress
from decoy_engine.generation.composite._city_state_zip import CompositeCityStateZip
from decoy_engine.generation.composite._name_email import CompositeNameEmail
from decoy_engine.generation.composite._person import CompositePerson
from decoy_engine.generation.composite._provider import CompositeProvider

_FIXED_CLASSES = {
    c.composite_name: c
    for c in (
        CompositeNameEmail,
        CompositeCityStateZip,
        CompositePerson,
        CompositeAddress,
        CompositeProvider,
    )
}


def _keys(prefix: str) -> set[str]:
    return {k.split(":", 1)[1] for k in SURFACE_DECLARATIONS if k.startswith(prefix + ":")}


# ---------------------------------------------------------------------------
# G1 and G2: coverage, with the checks themselves proven to bite
# ---------------------------------------------------------------------------


def _scalar_gap(handler_keys: set[str], declared: set[str]) -> set[str]:
    return handler_keys ^ declared


def test_g1_every_scalar_handler_has_a_declaration_and_no_more() -> None:
    assert _scalar_gap(set(SCALAR_HANDLERS), _keys("scalar")) == set()
    assert len(SCALAR_HANDLERS) == 24


def test_g1_check_fails_for_a_stub_strategy_without_a_declaration() -> None:
    stubbed = {*SCALAR_HANDLERS, "stub_strategy"}
    assert _scalar_gap(stubbed, _keys("scalar")) == {"stub_strategy"}
    assert _scalar_gap(set(SCALAR_HANDLERS), {*_keys("scalar"), "gone"}) == {"gone"}


def _handler_provider_literals(source: str) -> set[str]:
    tree = ast.parse(textwrap.dedent(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "provider"
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        ):
            found.add(node.comparators[0].value)
    return found


def test_g2_composite_providers_agree_across_handler_generator_and_declarations() -> None:
    literals = _handler_provider_literals(inspect.getsource(CompositeHandler.run))
    declared = _keys("composite")
    assert literals == set(_generator._COMPOSITE_NAMES) == declared


def test_g2_check_fails_for_a_stub_provider_branch() -> None:
    source = inspect.getsource(CompositeHandler.run)
    marker = '        else:\n            raise ExecutionError(\n                code="unsupported_strategy",'
    assert marker in source
    patched = source.replace(
        marker, "        elif node.provider == 'composite_stub':\n            pass\n" + marker
    )
    literals = _handler_provider_literals(patched)
    assert literals - _keys("composite") == {"composite_stub"}


@pytest.mark.parametrize("provider", sorted(_FIXED_CLASSES))
def test_g2_fixed_provider_writes_equal_the_generator_output_columns(provider: str) -> None:
    cls = _FIXED_CLASSES[provider]
    own = sorted(cls.output_columns)[0]
    entry = {"name": own, "strategy": "<composite>", "provider": provider}
    access = column_access(entry)
    assert access.writes == frozenset(sorted(cls.output_columns))
    assert access.everything is False


# ---------------------------------------------------------------------------
# G3: dispatch-branch inventory and mixed-surface precedence
# ---------------------------------------------------------------------------

_PINNED_DISPATCH_CALLS = frozenset(
    {
        "ExecutionError",
        "object.__setattr__",
        "relationship_graph.parents_of",
        "self._resolve_fk_node",
        "timed_strategy",
        "self._composite_handler.run",
        "self._handlers.get",
        "isinstance",
        "run_with_when_gate",
    }
)
_PINNED_NODE_KINDS = frozenset({"scalar", "composite", "composite_fk_group"})


def _call_names(func: Callable[..., Any]) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            parts: list[str] = []
            target: ast.expr = node.func
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
                names.add(".".join(reversed(parts)))
    return names


def test_g3_dispatch_mask_node_calls_are_the_pinned_inventory() -> None:
    assert _call_names(PandasExecutionAdapter._dispatch_mask_node) == _PINNED_DISPATCH_CALLS


def test_g3_work_node_kinds_are_the_pinned_set() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(build_work_list)))
    kinds = {
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "kind" and isinstance(kw.value, ast.Constant)
    }
    assert kinds == _PINNED_NODE_KINDS
    surface_of_kind = {
        "scalar": any(k.startswith("scalar:") for k in SURFACE_DECLARATIONS),
        "composite": any(k.startswith("composite:") for k in SURFACE_DECLARATIONS),
        "composite_fk_group": "surface:composite_fk_group" in SURFACE_DECLARATIONS,
    }
    assert all(surface_of_kind[k] for k in kinds)
    assert "surface:fk" in SURFACE_DECLARATIONS and "surface:when" in SURFACE_DECLARATIONS


def test_g3_a_new_dispatch_call_would_be_noticed() -> None:
    source = inspect.getsource(PandasExecutionAdapter._dispatch_mask_node)
    edited = source.replace(
        "        df = frames[node.table]\n",
        "        df = frames[node.table]\n        self._new_branch(df)\n",
        1,
    )
    assert edited != source

    def parse_calls(text: str) -> set[str]:
        tree = ast.parse(textwrap.dedent(text))
        out: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                out.add(n.func.attr)
        return out

    assert "_new_branch" in parse_calls(edited) - parse_calls(source)


def test_g3_mixed_surface_precedence() -> None:
    composite = {
        "name": "first_name",
        "strategy": "<composite>",
        "provider": "composite_name_email",
        "when": "first_name == 'x'",
    }
    got = column_access(composite)
    assert got.writes >= {"first_name", "last_name", "email"}
    scalar = {
        "name": "v",
        "strategy": "derived",
        "provider_config": {"expression": "a + 1"},
        "when": "b > 1",
    }
    got = column_access(scalar)
    assert {"a", "b"} <= got.reads and got.everything is False
    unparsable = {**scalar, "when": "`oops"}
    assert column_access(unparsable).everything is True


# ---------------------------------------------------------------------------
# G4: adapter frame setup
# ---------------------------------------------------------------------------

_PINNED_SETUP_HELPERS = frozenset(
    {
        "fk_columns_for_table",
        "date_shift_group_columns",
        "top_code_columns",
        "group_key_group_by_columns",
    }
)


def test_g4_frame_setup_helpers_are_the_pinned_four() -> None:
    names = {n.split(".")[-1] for n in _call_names(PandasExecutionAdapter.run)}
    helpers = {n for n in names if n.endswith("_columns") or n.endswith("_columns_for_table")}
    assert helpers == _PINNED_SETUP_HELPERS


# ---------------------------------------------------------------------------
# G6: one shared constant
# ---------------------------------------------------------------------------


def test_g6_sibling_reference_keys_is_the_tuple_requirements_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _requirements

    assert SIBLING_REFERENCE_KEYS == ("group_by", "order_by", "anchor", "reference_column")
    assert _requirements.SIBLING_REFERENCE_KEYS is SIBLING_REFERENCE_KEYS
    monkeypatch.setattr(_requirements, "SIBLING_REFERENCE_KEYS", ("only_this",))
    node = type("N", (), {})()
    node.columns = ("c",)
    node.plan_slice = type("S", (), {"coherent_with": ()})()
    got = _requirements._required_input_columns(node, {"group_by": "g", "only_this": "z"})
    assert got == ("c", "z")


def test_declaration_values_are_column_access_callables() -> None:
    for key, declaration in SURFACE_DECLARATIONS.items():
        assert callable(declaration), key
    assert isinstance(column_access({"name": "x", "strategy": "redact"}), ColumnAccess)
    assert _column_access.SURFACE_DECLARATIONS is SURFACE_DECLARATIONS


# ---------------------------------------------------------------------------
# G5: behavioral cross-check
# ---------------------------------------------------------------------------

_SEED = 7
_VERSION = "b8-g5"


def _compile(columns: list[dict[str, Any]], data: dict[str, pa.Array]) -> Any:
    from decoy_engine.execution._chunked_profile import first_chunk_profile
    from decoy_engine.plan import compile_plan

    config = {
        "global_settings": {"seed": _SEED, "unconfigured_column_policy": "warn"},
        "tables": [{"name": "t", "columns": columns}],
    }
    profile = first_chunk_profile(pa.table(data), table="t", engine_version=_VERSION)
    return compile_plan(config, profile, decoy_engine_version=_VERSION, no_profile=True)


def _run_adapter(columns: list[dict[str, Any]], data: dict[str, pa.Array]) -> pa.Table:
    from decoy_engine.execution._chunked_profile import first_chunk_profile
    from decoy_engine.plan import compile_plan
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import RelationshipGraph, build_namespace_registry
    from tests.native._chunked_entry_support import key_provider

    table = pa.table(data)
    config = {
        "global_settings": {"seed": _SEED, "unconfigured_column_policy": "warn"},
        "tables": [{"name": "t", "columns": columns}],
    }
    profile = first_chunk_profile(table, table="t", engine_version=_VERSION)
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION, no_profile=True)
    result = PandasExecutionAdapter().run(
        plan,
        {"t": table},
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=build_namespace_registry(config, profile),
        key_provider=key_provider(),
    )
    return result.outputs["t"]


def _variants(arr: pa.Array) -> list[pa.Array]:
    """Different values of the same type: a rotation (changes groupings and orderings) and,
    for numbers, a shift by one (changes a sum a rotation would leave alone)."""
    out = [pa.concat_arrays([arr.slice(1), arr.slice(0, 1)])]
    if pa.types.is_integer(arr.type) or pa.types.is_floating(arr.type):
        out.append(pc.add(arr, pa.scalar(1, arr.type)))
    return out


def _corpus() -> dict[str, tuple[list[dict[str, Any]], dict[str, pa.Array], set[str]]]:
    """case -> (configured columns, source data, columns that must be observed accessed)."""
    from tests.unit.execution._auto_chunk_strategies import STRATEGY_FIXTURES

    n = 12
    dates = pa.array([f"2020-01-{1 + (i % 28):02d}" for i in range(n)])
    ids = pa.array([f"H-{i % 4}" for i in range(n)])
    nums = pa.array([i * 3 % 17 for i in range(n)], pa.int64())
    strs = pa.array([f"s{i}" for i in range(n)])
    cases: dict[str, tuple[list[dict[str, Any]], dict[str, pa.Array], set[str]]] = {}
    for key, (columns, source) in STRATEGY_FIXTURES.items():
        required = {"g"} if key == "group_key" else {"start"} if key == "windowed_date" else set()
        cases[f"fixture:{key}"] = (columns, dict(source), required)
    cases["date_shift_group_by"] = (
        [
            {"name": "g", "strategy": "passthrough"},
            {
                "name": "val",
                "strategy": "date_shift",
                "namespace": "dob_ns",
                "provider_config": {
                    "min_days": -30,
                    "max_days": 30,
                    "date_format": "%Y-%m-%d",
                    "group_by": "g",
                },
            },
        ],
        {"g": ids, "val": dates},
        {"g"},
    )
    cases["shuffle"] = (
        [{"name": "val", "strategy": "shuffle", "namespace": "sh_ns", "deterministic": True}],
        {"val": strs},
        set(),
    )
    cases["formula"] = (
        [{"name": "val", "strategy": "formula", "provider_config": {"formula": "value"}}],
        {"val": strs},
        set(),
    )
    cases["geo_generalize"] = (
        [
            {
                "name": "val",
                "strategy": "geo_generalize",
                "provider_config": {
                    "type": "zip",
                    "cascade": ["zip5", "zip3", "state", "suppress"],
                    "k_threshold": 20000,
                },
            }
        ],
        {"val": pa.array([f"{10000 + i * 137:05d}" for i in range(n)])},
        set(),
    )
    cases["derived"] = (
        [
            {"name": "a", "strategy": "passthrough"},
            {"name": "b", "strategy": "passthrough"},
            {"name": "val", "strategy": "derived", "provider_config": {"expression": "a + b"}},
        ],
        {"a": nums, "b": nums, "val": nums},
        {"a", "b"},
    )
    cases["derived_aggregate"] = (
        [
            {"name": "g", "strategy": "passthrough"},
            {
                "name": "val",
                "strategy": "derived_aggregate",
                "provider_config": {"op": "sum", "column": "g"},
            },
        ],
        {"g": nums, "val": nums},
        {"g"},
    )
    cases["grouped_series"] = (
        [
            {"name": "g", "strategy": "passthrough"},
            {"name": "o", "strategy": "passthrough"},
            {
                "name": "val",
                "strategy": "grouped_series",
                "provider_config": {"group_by": "g", "order_by": "o", "generator": "cumcount"},
            },
        ],
        {
            "g": pa.array([0, 0, 1, 0, 2, 2, 1, 0, 1, 2, 2, 0], pa.int64()),
            "o": pa.array([(i * 7) % 5 for i in range(n)], pa.int64()),
            "val": nums,
        },
        {"g", "o"},
    )
    cases["nested_redact"] = (
        [
            {"name": "g", "strategy": "passthrough"},
            {
                "name": "val",
                "strategy": "nested",
                "namespace": "n_ns",
                "provider_config": {"target": "$.ssn", "strategy": "redact"},
            },
        ],
        {"g": ids, "val": pa.array([f'{{"ssn": "12{i}-45-6789"}}' for i in range(n)])},
        set(),
    )
    cases["joint_mask"] = (
        [
            {"name": "k", "strategy": "passthrough"},
            {
                "name": "city",
                "strategy": "joint_mask",
                "namespace": "jm_ns",
                "provider_config": {
                    "reference": "us_zip5_city_state",
                    "columns": ["city", "state"],
                    "key_by": "k",
                },
            },
            {"name": "state", "strategy": "passthrough"},
        ],
        {
            "k": ids,
            "city": pa.array(["Boston", "Miami"] * (n // 2)),
            "state": pa.array(["MA", "FL"] * (n // 2)),
        },
        {"k", "state"},
    )
    for provider, cls in sorted(_FIXED_CLASSES.items()):
        own = sorted(cls.output_columns)[0]
        others = sorted(set(cls.output_columns) - {own})
        cases[f"{provider}_lone"] = (
            [
                {
                    "name": own,
                    "strategy": "<composite>",
                    "provider": provider,
                    "deterministic": True,
                    "namespace": "ns",
                }
            ],
            {c: pa.array([f"{c}-{i}" for i in range(n)]) for c in [own, *others]},
            set(others),
        )
        cases[f"{provider}_lone_when"] = (
            [
                {
                    "name": own,
                    "strategy": "<composite>",
                    "provider": provider,
                    "deterministic": True,
                    "namespace": "ns",
                    "when": f"{own} == 'never'",
                }
            ],
            {c: pa.array([f"{c}-{i}" for i in range(n)]) for c in [own, *others]},
            set(others),
        )
    bundle = [
        {"column": "a", "provider": "person_first_name"},
        {"column": "b", "provider": "person_last_name"},
        {"column": "c", "provider": "person_phone"},
    ]
    cases["composite_custom_outside_group"] = (
        [
            {
                "name": name,
                "strategy": "<composite>",
                "provider": "composite_custom",
                "deterministic": True,
                "namespace": "cns",
                "coherent_with": [other],
                "provider_config": {"bundle": bundle},
            }
            for name, other in (("a", "b"), ("b", "a"))
        ],
        {"a": strs, "b": strs, "c": strs},
        {"c"},
    )
    cases["when_predicate"] = (
        [
            {"name": "w", "strategy": "passthrough"},
            {"name": "val", "strategy": "redact", "when": "w == 's3' or w == 's5'"},
        ],
        {"w": strs, "val": strs},
        {"w"},
    )
    return cases


def _declared(columns: list[dict[str, Any]]) -> tuple[set[str], bool]:
    names: set[str] = set()
    everything = False
    for entry in columns:
        access = column_access(entry)
        names |= access.reads | access.writes
        everything = everything or access.everything
    return names, everything


def _observe(columns: list[dict[str, Any]], data: dict[str, pa.Array], own: set[str]) -> set[str]:
    """Probe columns (every source column outside the configured entries' own names) that
    the handlers wrote, or whose value changed another column's output."""
    base = _run_adapter(columns, data)
    touched: set[str] = set()
    probes = [c for c in data if c not in own]
    for probe in probes:
        if not base.column(probe).equals(pa.chunked_array([data[probe]])):
            touched.add(probe)
        for variant in _variants(data[probe]):
            again = _run_adapter(columns, {**data, probe: variant})
            if any(
                not base.column(name).equals(again.column(name))
                for name in base.column_names
                if name != probe
            ):
                touched.add(probe)
    return touched


@pytest.mark.parametrize("case", sorted(_corpus()))
def test_g5_every_observed_access_is_declared(case: str) -> None:
    columns, data, required = _corpus()[case]
    own = {c["name"] for c in columns if c.get("strategy") != "passthrough"}
    touched = _observe(columns, data, own)
    declared, everything = _declared(columns)
    if not everything:
        assert touched <= declared, (case, sorted(touched - declared))
    assert required <= touched, (case, sorted(required - touched))


@pytest.mark.parametrize("case", sorted(_corpus()))
def test_g4_frame_setup_columns_are_declared_reads_or_own_columns(case: str) -> None:
    from decoy_engine.execution._runner import (
        date_shift_group_columns,
        group_key_group_by_columns,
        top_code_columns,
    )
    from decoy_engine.providers_v2 import get_default_registry

    columns, data, _required = _corpus()[case]
    plan = _compile(columns, data)
    registry = get_default_registry()
    setup: set[str] = set()
    for helper in (date_shift_group_columns, top_code_columns, group_key_group_by_columns):
        setup |= helper(plan, registry).get("t", set())
    declared, everything = _declared(columns)
    own = {c["name"] for c in columns}
    assert everything or setup <= declared | own, (case, sorted(setup - declared - own))
