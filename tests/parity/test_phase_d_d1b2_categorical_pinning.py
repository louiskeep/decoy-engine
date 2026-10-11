"""Phase D / D1b-ii: the per-column decision is pinned; a violation aborts, never switches.

A categorical column takes ONE backend for the whole run. A native column is validated against
its pinned source type on every batch, a missing pinned column is rejected rather than skipped,
and a kernel fault ends the run with the sink aborted and nothing committed. The Python loop is
forbidden for any column decided native, so "never half native" holds by construction. The
empty and all-null boundary is covered here too: such a slice answers without a kernel call
and must not hide an earlier native execution. Runs on the reference double, so it holds
without the companion.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import RUST_COMPANION
from decoy_engine.execution.out_of_core import _mask
from tests.parity._d1a_hash_data import run_chain
from tests.parity._d1b2_categorical_data import (
    EXPECTED,
    SOURCE_TEXT,
    WEIGHTS,
    PythonReferenceIndexKernel,
    categorical_seed,
    install_index_companion,
    source_column,
)
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.parity.test_phase_d_d1a_hash_pinning import (
    _DRIVERS,
    RecordingSink,
    _as_large,
    _assert_aborted,
    _without_a,
    mutate_second_batch,
)

_ROWS = pa.table({"a": pa.array(["w", "x", "y", "z", "q"], pa.string())})
_SEEDS = (("a", categorical_seed()),)
KEY = "out_of_core_categorical"


def forbid_python_categorical(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("the Python categorical loop ran for a column decided native")

    monkeypatch.setattr(_mask, "categorical_array", forbidden)


def entry(run: Any) -> dict[str, Any]:
    (found,) = (e for e in run.result.quality_metrics[KEY] if e["column"] == "a")
    return found


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_as_large, "out_of_core_categorical_source_type_drift"),
        (_without_a, "out_of_core_categorical_column_missing"),
    ],
    ids=["type-drift", "missing-column"],
)
@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_pinned_column_that_changes_or_vanishes_aborts_the_sink(
    mode: str,
    module: Any,
    change: Callable[[pa.RecordBatch], pa.RecordBatch],
    code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch)
    forbid_python_categorical(monkeypatch)
    mutate_second_batch(monkeypatch, module, change)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(ExecutionError) as caught:
        run_chain(
            _ROWS,
            _SEEDS,
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert caught.value.code == code
    _assert_aborted(sink, out_dir)


@pytest.mark.parametrize(("mode", "module"), _DRIVERS)
def test_a_mid_run_kernel_fault_aborts_without_a_python_switch(
    mode: str, module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = PythonReferenceIndexKernel(fail_on_call=2)
    install_index_companion(monkeypatch, kernel)
    forbid_python_categorical(monkeypatch)
    out_dir = tmp_path / "out"
    sink = RecordingSink(out_dir)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(
            _ROWS,
            _SEEDS,
            sink=sink,
            out_dir=out_dir,
            batch_rows=2,
            reorder=mode.endswith("reorder"),
        )
    assert kernel.calls == 2  # the fault ended the run; nothing retried it on another path
    _assert_aborted(sink, out_dir)


def test_a_mid_run_kernel_fault_returns_no_partial_resident_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_index_companion(monkeypatch, PythonReferenceIndexKernel(fail_on_call=2))
    forbid_python_categorical(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(_ROWS, _SEEDS, batch_rows=2)


@pytest.mark.parametrize("mode", ["resident", "sink-batch", "sink-reorder"])
@pytest.mark.parametrize("batch_rows", [None, 1, 7, 16])
def test_per_batch_equals_whole_column_and_python_never_runs(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    forbid_python_categorical(monkeypatch)
    seeds = (("u", categorical_seed()), ("w", categorical_seed(weights=WEIGHTS)))
    payload = pa.table({"u": source_column(), "w": source_column()})
    run = run_chain(payload, seeds, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    assert run.tables["t"].column("u").to_pylist() == EXPECTED["c_uniform"]
    assert run.tables["t"].column("w").to_pylist() == EXPECTED["c_weighted"]
    # One kernel call per batch that holds a value, per column, and no more.
    step = batch_rows or len(SOURCE_TEXT)
    populated = sum(
        any(v is not None for v in SOURCE_TEXT[i : i + step])
        for i in range(0, len(SOURCE_TEXT), step)
    )
    assert state["kernel"].calls == 2 * populated


# --- Empty and all-null boundary: zero kernel calls, and the column's whole history counts ---


def _all_null_then_populated(values: list[str | None]) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


@pytest.mark.parametrize("mode", ["resident", "sink-batch", "sink-reorder"])
def test_an_all_null_column_makes_no_kernel_call_and_reports_idle(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    forbid_python_categorical(monkeypatch)
    run = run_chain(
        _all_null_then_populated([None, None, None, None, None]),
        _SEEDS,
        batch_rows=2,
        **mode_kwargs(mode, tmp_path),
    )
    assert state["kernel"].calls == 0
    assert run.tables["t"].column("a").to_pylist() == [None] * 5
    assert entry(run) == {
        "table": "t",
        "column": "a",
        "path": "mask_batch",
        "executed_backend": "none",
        "reason": "no_native_kernel_calls",
    }


@pytest.mark.parametrize("mode", ["resident", "sink-batch", "sink-reorder"])
@pytest.mark.parametrize(
    "values",
    [
        ["x", "y", None, None, None, None],
        [None, None, "x", "y", None, None],
        [None, None, None, None, "x", "y"],
    ],
    ids=["populated-then-null", "null-between-populated", "null-then-populated"],
)
def test_idle_batches_neither_erase_nor_fake_a_native_execution(
    values: list[str | None], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = install_index_companion(monkeypatch)
    forbid_python_categorical(monkeypatch)
    payload = _all_null_then_populated(values)
    run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path))
    calls = state["kernel"].calls
    plain = run_chain(payload, _SEEDS)
    got = run.tables["t"].column("a").to_pylist()
    assert got == plain.tables["t"].column("a").to_pylist()
    assert [v is None for v in got] == [v is None for v in values]
    # Only the batches holding a value reach the kernel, and the entry keeps the native claim
    # whether the all-null batch came before or after them.
    assert calls == 1
    assert entry(run)["executed_backend"] == RUST_COMPANION
    assert "reason" not in entry(run)


def test_an_empty_table_makes_no_kernel_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # A zero-row table is masked whole (no batches), so nothing reaches the kernel or the loop.
    state = install_index_companion(monkeypatch)
    run = run_chain(_ROWS.slice(0, 0), _SEEDS)
    assert state["kernel"].calls == 0
    assert run.tables["t"].num_rows == 0


@pytest.mark.parametrize("mode", ["sink-batch", "sink-reorder"])
def test_lazy_sources_bind_the_prep_time_type_to_the_streamed_type(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A string column keeps the type the Parquet footer promised, so the pinned type and each
    # streamed batch agree and nothing aborts.
    install_index_companion(monkeypatch)
    forbid_python_categorical(monkeypatch)
    lazy = tmp_path / "src"
    lazy.mkdir()
    run = run_chain(
        pa.table({"u": source_column()}),
        (("u", categorical_seed()),),
        lazy_dir=lazy,
        batch_rows=7,
        **mode_kwargs(mode, tmp_path),
    )
    assert run.tables["t"].column("u").to_pylist() == EXPECTED["c_uniform"]
