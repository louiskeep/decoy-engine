"""Phase D / D1a: caller-visible per-column hash disposition on the OOC route.

Each hash column the route masks is reported in
`ExecutionResult.quality_metrics["out_of_core_hash"]`, keyed by table and column,
with the backend that executed it and (for a declined column) the reason. The entry
must survive both drivers and both return paths and reach the caller of
`run_pipeline`. Every case here pins the companion state itself (hidden, or a
reference double), so it holds on a box with or without the real companion.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import PANDAS_ORACLE, PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from decoy_engine.kernel import hash_array
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity._d1a_hash_data import (
    MASK_KEY,
    NS,
    column_seed,
    hash_seeds,
    install_companion,
    run_chain,
)
from tests.parity._kernel_witness import force_companion_unavailable
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.unit.execution.test_out_of_core_routing import _fk_ooc_config, _sources

KEY = "out_of_core_hash"
UNAVAILABLE = "crypto_extension_unavailable"
MODES = ["resident", "sink-batch", "sink-reorder"]
_PAYLOAD = pa.table(
    {
        "a": pa.array(["x", "y", None], pa.string()),
        "b": pa.array([1, 2, 3], pa.int64()),
    }
)
_SEEDS = hash_seeds(["a", "b"])


def by_column(run: Any) -> dict[tuple[str, str], dict[str, Any]]:
    return {(e["table"], e["column"]): e for e in run.result.quality_metrics[KEY]}


def test_python_scalar_is_a_distinct_label() -> None:
    assert PYTHON_SCALAR == "python_scalar"
    assert PYTHON_SCALAR not in (PANDAS_ORACLE, RUST_COMPANION)


@pytest.mark.parametrize("mode", MODES)
def test_companion_absent_declines_every_column_with_the_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_companion_unavailable(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD, _SEEDS, **mode_kwargs(mode, tmp_path)))
    # Payload columns and the parent-key column are masked by mask_batch; the FK
    # child columns (t.fk, child.ck) are replaced by the join and are not hashed.
    assert set(entries) == {("t", "a"), ("t", "b"), ("t", "hk")}
    for entry in entries.values():
        assert entry["executed_backend"] == PYTHON_SCALAR
        assert entry["reason"] == UNAVAILABLE
        assert entry["path"] == "mask_batch"


@pytest.mark.parametrize("mode", MODES)
def test_eligible_columns_report_the_companion_with_no_reason(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD, _SEEDS, **mode_kwargs(mode, tmp_path)))
    assert set(entries) == {("t", "a"), ("t", "b"), ("t", "hk")}
    for entry in entries.values():
        assert entry["executed_backend"] == RUST_COMPANION
        assert "reason" not in entry


def test_the_reorder_mode_really_uses_the_reorder_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = _runner.stream_table

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs["table_name"])
        return real(*args, **kwargs)

    monkeypatch.setattr(_runner, "stream_table", spy)
    run_chain(_PAYLOAD, _SEEDS, **mode_kwargs("sink-reorder", tmp_path))
    assert "t" in calls


def test_zero_row_table_reports_no_kernel_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    install_companion(monkeypatch)
    entries = by_column(run_chain(_PAYLOAD.slice(0, 0), _SEEDS))
    assert {e["executed_backend"] for e in entries.values()} == {"none"}
    assert {e["reason"] for e in entries.values()} == {"no_rows_hashed"}


@pytest.mark.parametrize(
    ("mode", "site"), [("sink-batch", "batch_join"), ("sink-reorder", "stream_join")]
)
def test_remap_sites_report_the_deferred_reason(
    mode: str, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    run = run_chain(_PAYLOAD, _SEEDS, hk_policy=OrphanPolicy.REMAP, **mode_kwargs(mode, tmp_path))
    remap = [e for e in run.result.quality_metrics[KEY] if e["path"] == "fk_remap"]
    assert remap == [
        {
            "table": "child",
            "column": "ck",
            "path": "fk_remap",
            "executed_backend": PYTHON_SCALAR,
            "reason": f"fk_key_hash_remap_native_deferred:{site}",
        }
    ]


def _entries_through_run_pipeline(tmp_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    config = _fk_ooc_config(tmp_path)
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
    # parent.id is a hash parent key, child.cid a hash payload; child.parent_id is the FK.
    assert set(entries) == {("parent", "id"), ("child", "cid")}
    assert {(e["executed_backend"], e["reason"]) for e in entries.values()} == {
        (PYTHON_SCALAR, UNAVAILABLE)
    }


def test_disposition_is_visible_through_run_pipeline_when_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    entries = _entries_through_run_pipeline(tmp_path)
    assert {e["executed_backend"] for e in entries.values()} == {RUST_COMPANION}
    assert set(entries) == {("parent", "id"), ("child", "cid")}


def test_non_hash_payload_columns_are_not_dispositioned(monkeypatch: pytest.MonkeyPatch) -> None:
    install_companion(monkeypatch)
    seeds = (("a", column_seed("redact", namespace=None)),)
    run = run_chain(_PAYLOAD.select(["a"]), seeds)
    assert [e["column"] for e in run.result.quality_metrics[KEY]] == ["hk"]


def test_a_plan_with_no_hash_column_never_loads_the_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = install_companion(monkeypatch)
    # `hk` is always a hash parent key in the chain, so build the no-hash case directly.
    run = OocHashRun()
    assert run.metrics() == {}
    assert state["loads"] == 0


# --- Declined types: the column stays Python with a value-free reason -------------------


def _declined(
    monkeypatch: pytest.MonkeyPatch, column: pa.Array, **kwargs: Any
) -> tuple[Any, dict[str, Any]]:
    install_companion(monkeypatch)
    run = run_chain(pa.table({"c": column}), (("c", column_seed()),), **kwargs)
    return run, by_column(run)[("t", "c")]


def test_tz_aware_timestamp_is_declined_even_with_the_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamp = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    column = pa.array([stamp, None], pa.timestamp("us", tz="UTC"))
    run, entry = _declined(monkeypatch, column)
    assert entry["executed_backend"] == PYTHON_SCALAR
    assert entry["reason"] == "hash_input_type_tz_timestamp_deferred:c"
    want = hash_array(column, seed=MASK_KEY, namespace=NS).to_pylist()
    assert run.tables["t"].column("c").to_pylist() == want


def test_non_admitted_type_runs_python_and_reports_its_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    column = pa.array([dt.date(2020, 1, 1), None], pa.date32())
    run, entry = _declined(monkeypatch, column)
    assert entry["executed_backend"] == PYTHON_SCALAR
    assert entry["reason"] == "hash_input_type_not_native_admitted:c:date32[day]"
    want = hash_array(column, seed=MASK_KEY, namespace=NS).to_pylist()
    assert run.tables["t"].column("c").to_pylist() == want


def test_null_typed_column_is_declined_but_an_all_null_string_column_is_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, null_typed = _declined(monkeypatch, pa.array([None, None], pa.null()))
    assert null_typed["reason"] == "hash_input_type_not_native_admitted:c:null"
    _, all_null = _declined(monkeypatch, pa.array([None, None], pa.string()))
    assert all_null["executed_backend"] == RUST_COMPANION


@pytest.mark.parametrize("companion", ["present", "absent"])
@pytest.mark.parametrize(
    ("column", "code"),
    [
        (pa.array([1.5, None], pa.float64()), "float_canonicalization_unsupported"),
        (pa.array([dt.datetime(2020, 1, 1), None], pa.timestamp("us")), "timezone_naive_datetime"),
    ],
    ids=["float", "tz-naive"],
)
def test_non_admitted_types_keep_their_typed_error(
    companion: str, column: pa.Array, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if companion == "present":
        install_companion(monkeypatch)
    else:
        force_companion_unavailable(monkeypatch)
    with pytest.raises(Exception) as caught:
        run_chain(pa.table({"c": column}), (("c", column_seed()),))
    assert caught.value.code == code  # type: ignore[attr-defined]


# --- The companion is loaded once, and an incompatible one declines ------------------


def test_the_companion_is_loaded_once_per_run_across_tables_and_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_companion(monkeypatch)
    run_chain(_PAYLOAD, _SEEDS, **mode_kwargs("sink-reorder", tmp_path))
    assert state["loads"] == 1
    # `t` and `child` both carry hash columns, but the loader ran for the run, not per table.
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
