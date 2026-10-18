"""Phase F FR0: semantic eligibility (which values) is decided from the plan alone.

Covers the total admission matrix, the declared Arrow type per family, every decline reason, and
the property that eligibility never reads a budget, a sink or the host.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from typing import Any, get_args

import pyarrow as pa
import pytest

from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.generation import _admission
from decoy_engine.generation._admission import ADMISSION_MATRIX, classify_plan
from decoy_engine.internal.faker_setup import register_faker_provider, unregister_faker_provider
from tests.parity import _f0_fixtures as fx


def _cfg(*tables: dict[str, Any]) -> dict[str, Any]:
    return fx.config(list(tables))


def _one(col: dict[str, Any], rows: int = 10) -> dict[str, Any]:
    return _cfg(fx.table("t", rows, [{"name": "c", **col}]))


def _reason(col: dict[str, Any]) -> str:
    adm = classify_plan(_one(col))
    return adm.columns[0].reason


def test_matrix_has_a_row_for_every_generator_kind() -> None:
    kinds = set(get_args(GenerateColumnConfig.model_fields["type"].annotation))
    assert set(ADMISSION_MATRIX) == kinds
    assert all(reason for _, reason in ADMISSION_MATRIX.values())


def test_only_the_prefix_stable_families_are_admitted() -> None:
    admitted = {k for k, (ok, _) in ADMISSION_MATRIX.items() if ok}
    assert admitted == {"sequence", "categorical", "faker", "reference"}


@pytest.mark.parametrize(
    "col",
    [
        {"type": "formula", "formula": "i"},
        {"type": "derived", "expression": "a + 1"},
        {"type": "derived_aggregate", "op": "sum", "column": "a"},
        {"type": "statistical", "snapshot_file": "x"},
        {"type": "grouped_series", "group_by": "a", "order_by": "b"},
        {"type": "windowed_date", "anchor": "a", "min_days": 1, "max_days": 2},
        {"type": "group_key", "group_by": "a"},
    ],
)
def test_declined_families_are_declined_with_a_reason(col: dict[str, Any]) -> None:
    adm = classify_plan(_one(col)).columns[0]
    assert not adm.eligible
    assert adm.reason == f"family_declined:{col['type']}"


@pytest.mark.parametrize(
    ("col", "arrow_type"),
    [
        ({"type": "sequence", "start": 1}, pa.string()),
        ({"type": "categorical", "categories": ["a", "b"]}, pa.string()),
        ({"type": "categorical", "categories": [1, 2, 3]}, pa.int64()),
        ({"type": "categorical", "categories": [1.5, 2.0]}, pa.float64()),
        ({"type": "categorical", "categories": [True, False]}, pa.bool_()),
        ({"type": "faker", "faker_type": "city"}, pa.string()),
        ({"type": "faker", "faker_type": "pyint", "faker_kwargs": {"min_value": 1}}, pa.string()),
        ({"type": "faker", "faker_type": "no_such_provider"}, pa.string()),
    ],
)
def test_declared_arrow_types(col: dict[str, Any], arrow_type: pa.DataType) -> None:
    adm = classify_plan(_one(col)).columns[0]
    assert adm.eligible and adm.arrow_type == arrow_type


@pytest.mark.parametrize(
    ("col", "reason"),
    [
        ({"type": "categorical", "categories": [1, "a"]}, "mixed_or_unsupported_category_types"),
        ({"type": "categorical", "categories": [1, 2.5]}, "mixed_or_unsupported_category_types"),
        ({"type": "categorical", "categories": [None, "a"]}, "mixed_or_unsupported_category_types"),
        ({"type": "categorical", "categories": [2**70]}, "int_category_outside_int64"),
        ({"type": "categorical", "categories": []}, "categories_not_a_nonempty_list"),
        (
            {"type": "categorical", "categories": ["a", "b"], "weights": [1]},
            "weights_do_not_match_categories",
        ),
        ({"type": "sequence", "start": 1, "determinism": "fresh"}, "determinism_fresh"),
    ],
)
def test_decline_reasons(col: dict[str, Any], reason: str) -> None:
    assert _reason(col) == reason


@pytest.fixture
def custom_provider() -> Iterator[str]:
    register_faker_provider("fr0_custom", lambda f: "x")
    yield "fr0_custom"
    unregister_faker_provider("fr0_custom")


def test_custom_provider_declines_without_being_probed(custom_provider: str) -> None:
    calls = []
    unregister_faker_provider(custom_provider)
    register_faker_provider(custom_provider, lambda f: calls.append(1) or "x")
    adm = classify_plan(_one({"type": "faker", "faker_type": custom_provider})).columns[0]
    assert not adm.eligible and adm.reason == "faker_custom_or_unresolved_provider"
    assert calls == [], "eligibility must not invoke the provider"


def test_snapshot_pinned_custom_provider_declines_too() -> None:
    snapshot = {"fr0_snap": lambda f: "x"}
    adm = classify_plan(
        _one({"type": "faker", "faker_type": "fr0_snap"}), provider_snapshot=snapshot
    ).columns[0]
    assert not adm.eligible


def _ref(**extra: Any) -> dict[str, Any]:
    child = fx.table(
        "child",
        8,
        [
            {
                "name": "r",
                "type": "reference",
                "reference_table": "parent",
                "reference_column": "k",
                **extra,
            }
        ],
    )
    return _cfg(child, fx.table("parent", 5, [{"name": "k", "type": "sequence", "start": 1}]))


def test_reference_inherits_the_parent_declared_type() -> None:
    cfg = _cfg(
        fx.table(
            "c",
            3,
            [{"name": "r", "type": "reference", "reference_table": "p", "reference_column": "k"}],
        ),
        fx.table("p", 3, [{"name": "k", "type": "categorical", "categories": [1, 2]}]),
    )
    by_name = {(c.table, c.column): c for c in classify_plan(cfg).columns}
    assert by_name[("c", "r")].eligible and by_name[("c", "r")].arrow_type == pa.int64()


def test_reference_without_repair_is_eligible_and_with_repair_is_not() -> None:
    assert classify_plan(_ref()).eligible
    assert classify_plan(_ref(distribution="weighted", weights=[1, 2, 3, 4, 5])).eligible
    for knob in ("min_per_parent", "max_per_parent"):
        adm = classify_plan(_ref(**{knob: 2}))
        assert not adm.eligible and adm.declines[0].reason == "reference_cardinality_repair"


def test_reference_to_an_ineligible_or_missing_parent_declines() -> None:
    parent_formula = _cfg(
        fx.table(
            "c",
            3,
            [{"name": "r", "type": "reference", "reference_table": "p", "reference_column": "k"}],
        ),
        fx.table("p", 3, [{"name": "k", "type": "formula", "formula": "i"}]),
    )
    adm = classify_plan(parent_formula)
    assert not adm.eligible
    assert {c.reason for c in adm.declines} == {
        "family_declined:formula",
        "reference_parent_ineligible",
    }
    missing = _cfg(
        fx.table(
            "c",
            3,
            [
                {
                    "name": "r",
                    "type": "reference",
                    "reference_table": "nope",
                    "reference_column": "k",
                }
            ],
        )
    )
    assert classify_plan(missing).declines[0].reason == "reference_parent_unresolved"


def test_duplicate_column_names_decline() -> None:
    cfg = _cfg(
        fx.table("t", 3, [{"name": "a", "type": "sequence"}, {"name": "a", "type": "sequence"}])
    )
    assert not classify_plan(cfg).eligible


def test_one_ineligible_column_makes_the_whole_plan_ineligible() -> None:
    cfg = _cfg(
        fx.table(
            "t",
            3,
            [{"name": "a", "type": "sequence"}, {"name": "b", "type": "formula", "formula": "i"}],
        )
    )
    adm = classify_plan(cfg)
    assert not adm.eligible and adm.reason() == "t.b:family_declined:formula"


def test_eligibility_has_no_access_to_budget_sink_or_host() -> None:
    params = inspect.signature(classify_plan).parameters
    assert set(params) == {"config", "provider_snapshot"}
    tree = ast.parse(inspect.getsource(_admission))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    for module in imported:
        for forbidden in ("execution", "_budget", "_streamed", "_bounds", "psutil", "os"):
            assert forbidden not in module.split("."), f"{module} reaches the environment"
