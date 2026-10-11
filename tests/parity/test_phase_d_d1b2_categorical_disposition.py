"""Phase D / D1b-ii: caller-visible per-column categorical disposition on the OOC route.

Each deterministic categorical column the route masks is reported in
`ExecutionResult.quality_metrics["out_of_core_categorical"]`, keyed by table and column, with
the backend that executed it and (for a declined column) a value-free reason. The entry must
survive both drivers and both return paths and reach the caller of `run_pipeline`. Every case
pins the companion state itself (hidden, or a reference double), so it holds on a box with or
without the real companion.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from tests.parity._d1a_hash_data import chain_plan, column_seed, run_chain
from tests.parity._d1b2_categorical_data import (
    CATEGORIES,
    EXPECTED_NON_STRING,
    WEIGHTS,
    categorical_seed,
    install_index_companion,
    non_string_sources,
)
from tests.parity._kernel_witness import force_companion_unavailable
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.unit.execution.test_out_of_core_routing import _fk_ooc_config, _sources

KEY = "out_of_core_categorical"
UNAVAILABLE = "index_extension_unavailable"
MODES = ["resident", "sink-batch", "sink-reorder"]
_PAYLOAD = pa.table(
    {
        "a": pa.array(["x", "y", None, "z"], pa.string()),
        "b": pa.array(["p", "q", "r", None], pa.string()),
    }
)
_SEEDS = (("a", categorical_seed()), ("b", categorical_seed(weights=WEIGHTS)))


def by_column(run: Any) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["table"], e["column"]): e for e in run.result.quality_metrics[KEY]}


def decisions_for(schema: pa.Schema, **seeds: Any) -> dict[str, Any]:
    """The per-column decisions the route would make for `seeds`, without running a batch."""
    plan = chain_plan(tuple(seeds.items()))
    table_plan = OocHashRun().prepare_table(plan, "t", schema, frozenset(), (), site="test")
    assert table_plan.categorical is not None
    return dict(table_plan.categorical.decisions)


@pytest.mark.parametrize("mode", MODES)
def test_companion_absent_declines_every_column_with_the_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_companion_unavailable(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD, _SEEDS, **mode_kwargs(mode, tmp_path)))
    assert set(entries) == {("t", "a"), ("t", "b")}
    for entry in entries.values():
        assert entry["executed_backend"] == PYTHON_SCALAR
        assert entry["reason"] == UNAVAILABLE
        assert entry["path"] == "mask_batch"


@pytest.mark.parametrize("mode", MODES)
def test_eligible_columns_report_the_companion_with_no_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD, _SEEDS, **mode_kwargs(mode, tmp_path)))
    assert set(entries) == {("t", "a"), ("t", "b")}
    for entry in entries.values():
        assert entry["executed_backend"] == RUST_COMPANION
        assert "reason" not in entry
        assert entry["path"] == "mask_batch"


@pytest.mark.parametrize("mode", MODES)
def test_a_non_string_category_pool_stays_python_with_its_stage_a_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    seeds = (("a", categorical_seed(categories=(1, 2, 3))),)
    run = run_chain(_PAYLOAD.select(["a"]), seeds, **mode_kwargs(mode, tmp_path))
    entry = by_column(run)[("t", "a")]
    assert entry["executed_backend"] == PYTHON_SCALAR
    assert entry["reason"] == "categorical_categories_not_all_string:a"


@pytest.mark.parametrize(
    ("seed", "reason"),
    [
        (categorical_seed(namespace=None), "categorical_not_deterministic:c"),
        (categorical_seed(namespace=""), "categorical_requires_namespace:c"),
        (categorical_seed(categories=()), "categorical_categories_not_nonempty_list:c"),
        (categorical_seed(categories=(1, 2)), "categorical_categories_not_all_string:c"),
        (categorical_seed(weights=(1, 2)), "categorical_weights_shape:c"),
        (
            categorical_seed(categories=("x", "y"), weights=("a", 1)),
            "categorical_weights_not_numeric:c",
        ),
        (
            categorical_seed(categories=("x", "y"), weights=(-1, 2)),
            "categorical_weights_negative:c",
        ),
        (
            categorical_seed(categories=("x", "y"), weights=(0, 0)),
            "categorical_weights_unbuildable_cdf:c",
        ),
        (
            categorical_seed(categories=("x", "y"), weights=(1e-9, 1)),
            "categorical_weights_unbuildable_cdf:c",
        ),
        # A NaN, an infinity and an overflowing sum escape the oracle's CDF builder as a bare
        # ValueError / OverflowError; the decision declines them instead of crashing.
        (
            categorical_seed(categories=("x", "y"), weights=(float("nan"), 1)),
            "categorical_weights_unbuildable_cdf:c",
        ),
        (
            categorical_seed(categories=("x", "y"), weights=(float("inf"), 1)),
            "categorical_weights_unbuildable_cdf:c",
        ),
        (
            categorical_seed(categories=("x", "y"), weights=(1e308, 1e308)),
            "categorical_weights_unbuildable_cdf:c",
        ),
    ],
    ids=[
        "non-deterministic",
        "no-namespace",
        "empty-categories",
        "numeric-categories",
        "weights-shape",
        "weights-not-numeric",
        "weights-negative",
        "weights-zero-total",
        "weight-below-resolution",
        "weight-nan",
        "weight-inf",
        "weight-overflow",
    ],
)
def test_every_stage_a_rejection_declines_with_a_value_free_reason(
    seed: Any, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    decision = decisions_for(pa.schema([pa.field("c", pa.string())]), c=seed)["c"]
    assert decision.backend == PYTHON_SCALAR
    assert decision.reason == reason


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", sorted(EXPECTED_NON_STRING))
def test_a_non_string_source_stays_python_and_reports_its_type(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    column = non_string_sources()[name]
    seeds = ((name, categorical_seed(categories=CATEGORIES[:3])),)
    run = run_chain(pa.table({name: column}), seeds, batch_rows=2, **mode_kwargs(mode, tmp_path))
    entry = by_column(run)[("t", name)]
    assert entry["executed_backend"] == PYTHON_SCALAR
    assert entry["reason"] == f"categorical_source_not_string:{name}:{column.type}"
    assert run.tables["t"].column(name).to_pylist() == EXPECTED_NON_STRING[name]


def test_a_large_string_source_is_declined_because_only_string_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch)
    decision = decisions_for(pa.schema([pa.field("c", pa.large_string())]), c=categorical_seed())
    assert decision["c"].reason == "categorical_source_not_string:c:large_string"


def _entries_through_run_pipeline(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    config = _fk_ooc_config(tmp_path)
    note = {
        "name": "note",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "ns_note",
        "provider_config": {"categories": list(CATEGORIES)},
    }
    config["tables"][0]["columns"][1] = note
    result = run_pipeline(
        config, _sources(config), engine_version="0.1.0", execution_mode="out_of_core"
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    return {(e["table"], e["column"]): e for e in result.quality_metrics[KEY]}


def test_disposition_is_visible_through_run_pipeline_when_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_companion_unavailable(monkeypatch)
    entries = _entries_through_run_pipeline(tmp_path)
    assert set(entries) == {("parent", "note")}
    assert {(e["executed_backend"], e["reason"]) for e in entries.values()} == {
        (PYTHON_SCALAR, UNAVAILABLE)
    }


def test_zero_row_table_reports_no_kernel_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install_index_companion(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD.slice(0, 0), _SEEDS))
    assert {e["executed_backend"] for e in entries.values()} == {"none"}
    assert {e["reason"] for e in entries.values()} == {"no_native_kernel_calls"}
    assert state["kernel"].calls == 0


def test_a_declined_zero_row_table_keeps_its_decline_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    force_companion_unavailable(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD.slice(0, 0), _SEEDS))
    assert {(e["executed_backend"], e["reason"]) for e in entries.values()} == {
        ("none", UNAVAILABLE)
    }


def test_a_plan_with_no_categorical_column_never_loads_the_index_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = install_index_companion(monkeypatch)
    run = run_chain(_PAYLOAD.select(["a"]), (("a", column_seed("redact", namespace=None)),))
    assert KEY not in run.result.quality_metrics
    assert state["loads"] == 0


def test_the_index_kernel_is_loaded_once_per_run_across_tables_and_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    run_chain(_PAYLOAD, _SEEDS, **mode_kwargs("sink-reorder", tmp_path))
    assert state["loads"] == 1
    state["loads"] = 0
    run_chain(_PAYLOAD, _SEEDS)
    assert state["loads"] == 1


def test_an_abi_incompatible_companion_declines_with_the_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = ModuleType("decoy_engine_native._kernel")
    kernel.abi_version = lambda: "decoy-native-abi-0"  # type: ignore[attr-defined]
    package = ModuleType("decoy_engine_native")
    package._kernel = kernel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", package)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", kernel)
    entries = by_column(run_chain(_PAYLOAD, _SEEDS))
    assert {(e["executed_backend"], e["reason"]) for e in entries.values()} == {
        (PYTHON_SCALAR, UNAVAILABLE)
    }
