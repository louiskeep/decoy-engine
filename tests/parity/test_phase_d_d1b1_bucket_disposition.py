"""Phase D / D1b-i: caller-visible per-column bucket_perturb disposition on the OOC route.

Each bucket_perturb column the route masks is reported in
`ExecutionResult.quality_metrics["out_of_core_bucket_perturb"]`, keyed by table and column, with
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
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._operator_registry import (
    PANDAS_ORACLE,
    PYTHON_PANDAS,
    PYTHON_SCALAR,
    RUST_COMPANION,
)
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from tests.parity._d1a_hash_data import chain_plan, column_seed, run_chain
from tests.parity._d1b1_bucket_data import (
    TZ_COLUMNS,
    bucket_seed,
    declined_sources,
    install_index_companion,
)
from tests.parity._kernel_witness import force_companion_unavailable
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.unit.execution.test_out_of_core_routing import _fk_ooc_config, _sources

KEY = "out_of_core_bucket_perturb"
UNAVAILABLE = "index_extension_unavailable"
MODES = ["resident", "sink-batch", "sink-reorder"]
_PAYLOAD = pa.table(
    {
        "a": pa.array(["2024-03-05", "2023-07-19", None, "bad"], pa.string()),
        "b": pa.array(["2021-01-01", None, "2020-02-29", "2019-12-31"], pa.string()),
    }
)
_SEEDS = (("a", bucket_seed("month")), ("b", bucket_seed("week")))


def by_column(run: Any) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["table"], e["column"]): e for e in run.result.quality_metrics[KEY]}


def decisions_for(schema: pa.Schema, **seeds: Any) -> dict[str, Any]:
    """The per-column decisions the route would make for `seeds`, without running a batch."""
    plan = chain_plan(tuple(seeds.items()))
    table_plan = OocHashRun().prepare_table(plan, "t", schema, frozenset(), (), site="test")
    assert table_plan.bucket is not None
    return dict(table_plan.bucket.decisions)


def test_the_declined_backend_token_is_its_own_truthful_label() -> None:
    assert PYTHON_PANDAS == "python_pandas"
    assert PYTHON_PANDAS not in (PYTHON_SCALAR, PANDAS_ORACLE, RUST_COMPANION)


@pytest.mark.parametrize("mode", MODES)
def test_companion_absent_declines_every_column_with_the_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_companion_unavailable(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD, _SEEDS, **mode_kwargs(mode, tmp_path)))
    assert set(entries) == {("t", "a"), ("t", "b")}
    for entry in entries.values():
        assert entry["executed_backend"] == PYTHON_PANDAS
        assert entry["reason"] == UNAVAILABLE
        assert entry["path"] == "mask_batch"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", sorted(declined_sources()))
def test_a_non_string_source_stays_python_and_reports_its_type(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    column = declined_sources()[name]
    run = run_chain(
        pa.table({name: column}),
        ((name, bucket_seed("month")),),
        batch_rows=2,
        **mode_kwargs(mode, tmp_path),
    )
    entry = by_column(run)[("t", name)]
    assert entry["executed_backend"] == PYTHON_PANDAS
    assert entry["reason"] == f"bucket_perturb_source_not_string:{name}:{column.type}"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", sorted(TZ_COLUMNS))
def test_a_tz_directive_on_a_valid_format_declines_with_the_resolver_code(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    fmt, values = TZ_COLUMNS[name]
    run = run_chain(
        pa.table({name: pa.array(values, pa.string())}),
        ((name, bucket_seed("month", fmt)),),
        batch_rows=2,
        **mode_kwargs(mode, tmp_path),
    )
    entry = by_column(run)[("t", name)]
    assert entry["executed_backend"] == PYTHON_PANDAS
    assert (
        entry["reason"]
        == f"bucket_perturb_config_declined:{name}:bucket_perturb_timezone_directive"
    )
    assert "%" not in entry["reason"]


def test_a_large_string_source_is_declined_because_only_string_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch)
    decision = decisions_for(pa.schema([pa.field("c", pa.large_string())]), c=bucket_seed("month"))
    assert decision["c"].reason == "bucket_perturb_source_not_string:c:large_string"
    assert decision["c"].backend == PYTHON_PANDAS


def _write_dates(config: dict[str, Any]) -> None:
    """Make the parent `note` column a date string column the bucket_perturb config can mask."""
    path = config["sources"]["parent"]["path"]
    table = pq.read_table(path)
    dates = pa.array([f"2024-0{1 + i % 9}-1{i % 9}" for i in range(table.num_rows)], pa.string())
    pq.write_table(table.set_column(1, "note", dates), path)


def _entries_through_run_pipeline(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    config = _fk_ooc_config(tmp_path)
    _write_dates(config)
    config["tables"][0]["columns"][1] = {
        "name": "note",
        "strategy": "bucket_perturb",
        "namespace": "ns_note",
        "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
    }
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
        (PYTHON_PANDAS, UNAVAILABLE)
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


def test_a_plan_with_no_bucket_column_never_loads_the_index_kernel(
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
        (PYTHON_PANDAS, UNAVAILABLE)
    }


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


def test_disposition_is_visible_through_run_pipeline_when_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_index_companion(monkeypatch)
    entries = _entries_through_run_pipeline(tmp_path)
    assert {(e["executed_backend"], e.get("reason")) for e in entries.values()} == {
        (RUST_COMPANION, None)
    }
