"""Phase D / D1a: the per-column decision is pinned; a violation aborts, never switches.

A hash column takes ONE backend for the whole run. A native column is validated against
its pinned input type on every batch, a missing pinned column is rejected rather than
skipped, and a kernel fault ends the run with the sink aborted and nothing committed.
The Python hash is forbidden for any column decided native, so "never half native" is
asserted by construction. Runs on the reference double, so it holds without the companion.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _mask, _runner, _stream_driver
from tests.parity._d1a_hash_data import (
    EXPECTED_TOKENS,
    PythonReferenceKernel,
    hash_seeds,
    install_companion,
    run_chain,
    source_columns,
)
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_ROWS = pa.table({"a": pa.array(["w", "x", "y", "z", "q"], pa.string())})
_SEEDS = hash_seeds(["a"])
# Two driver modules, each with its own batch iterator; the mode names the one in use.
_DRIVERS = [("sink-batch", _runner), ("sink-reorder", _stream_driver)]


class RecordingSink(ParquetTransactionalSink):
    def __init__(self, target: Path) -> None:
        super().__init__(target)
        self.commits = 0
        self.aborts = 0

    def commit(self) -> None:
        self.commits += 1
        super().commit()

    def abort(self) -> None:
        self.aborts += 1
        super().abort()


def forbid_python_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("the Python hash ran for a column decided native")

    monkeypatch.setattr(_mask, "hash_array", forbidden)


def mutate_second_batch(
    monkeypatch: pytest.MonkeyPatch, module: Any, change: Callable[[pa.RecordBatch], pa.RecordBatch]
) -> None:
    real = module._iter_source_batches

    def patched(src: Any, batch_rows: int) -> Iterator[pa.RecordBatch]:
        for i, batch in enumerate(real(src, batch_rows)):
            yield change(batch) if i == 1 else batch

    monkeypatch.setattr(module, "_iter_source_batches", patched)


def _as_large(batch: pa.RecordBatch) -> pa.RecordBatch:
    idx = batch.schema.get_field_index("a")
    if idx < 0:  # another table's batch
        return batch
    cols = list(batch.columns)
    cols[idx] = cols[idx].cast(pa.large_string())
    return pa.record_batch(cols, names=batch.schema.names)


def _without_a(batch: pa.RecordBatch) -> pa.RecordBatch:
    return batch.drop_columns(["a"]) if "a" in batch.schema.names else batch


def _assert_aborted(sink: RecordingSink, out_dir: Path) -> None:
    assert (sink.commits, sink.aborts) == (0, 1)
    assert not out_dir.exists()
    assert not list(out_dir.parent.glob("_decoy_stage_*"))


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (_as_large, "out_of_core_hash_input_type_drift"),
        (_without_a, "out_of_core_hash_column_missing"),
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
    install_companion(monkeypatch)
    forbid_python_hash(monkeypatch)
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
    kernel = PythonReferenceKernel(fail_on_call=2)
    install_companion(monkeypatch, kernel)
    forbid_python_hash(monkeypatch)
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
    install_companion(monkeypatch, PythonReferenceKernel(fail_on_call=2))
    forbid_python_hash(monkeypatch)
    with pytest.raises(RuntimeError, match="injected kernel fault"):
        run_chain(_ROWS, _SEEDS, batch_rows=2)


@pytest.mark.parametrize("mode", ["resident", "sink-batch", "sink-reorder"])
@pytest.mark.parametrize("batch_rows", [None, 1, 2, 3])
def test_per_batch_equals_whole_column_and_python_never_runs(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    forbid_python_hash(monkeypatch)
    cols = source_columns()
    payload = pa.table({"c_utf8": cols["c_utf8"], "c_i64": cols["c_i64"], "c_bool": cols["c_bool"]})
    seeds = hash_seeds(["c_utf8", "c_i64", "c_bool"])
    run = run_chain(payload, seeds, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    for name in ("c_utf8", "c_i64", "c_bool", "hk"):
        assert run.tables["t"].column(name).to_pylist() == EXPECTED_TOKENS[name], name


def test_a_typed_all_null_batch_followed_by_a_populated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_companion(monkeypatch)
    forbid_python_hash(monkeypatch)
    values = [None, None, "x", None, "y", "z", None]
    payload = pa.table({"a": pa.array(values, pa.string())})
    run = run_chain(payload, _SEEDS, batch_rows=2, **mode_kwargs("sink-batch", tmp_path))
    plain = run_chain(payload, _SEEDS)
    assert run.tables["t"].column("a").to_pylist() == plain.tables["t"].column("a").to_pylist()
    got = run.tables["t"].column("a").to_pylist()
    assert [v is None for v in got] == [v is None for v in values]


@pytest.mark.parametrize("mode", ["sink-batch", "sink-reorder"])
def test_lazy_sources_bind_the_prep_time_type_to_the_streamed_type(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every admitted type survives a Parquet round trip with the type the footer promised,
    # so the pinned type and each streamed batch agree and nothing aborts.
    install_companion(monkeypatch)
    forbid_python_hash(monkeypatch)
    lazy = tmp_path / "src"
    lazy.mkdir()
    cols = source_columns()
    names = ["c_utf8", "c_large", "c_bool", "c_i8", "c_i16", "c_i32", "c_i64"]
    names += ["c_u8", "c_u16", "c_u32", "c_u64"]
    run = run_chain(
        pa.table({n: cols[n] for n in names}),
        hash_seeds(names),
        lazy_dir=lazy,
        batch_rows=3,
        **mode_kwargs(mode, tmp_path),
    )
    for name in names:
        assert run.tables["t"].column(name).to_pylist() == EXPECTED_TOKENS[name], name
