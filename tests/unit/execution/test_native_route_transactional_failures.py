"""Acceptance test 7, docs/plans/2026-09-04-native-route-production-seam.md
section 4: transactional failure behavior for the single-pass streaming
native lane -- late kernel failure, a schema-drift batch, a ledger-
validation failure, and a commit failure. Every case: no final artifact,
staged data cleaned up, zero oracle retry, and (where the failure happens
before the exception unwinds past this module's own frames) the ledger's
attempted-vs-completed counts correctly reflect the partial state.

Kernel/schema-drift/ledger-validation failures happen DURING
`sink.write_batches`, before `run_pipeline` can return anything -- so the
production-entry cases below assert via `pytest.raises` (no final
`ExecutionResult` exists on a raise) plus the sink's on-disk state and an
oracle-entry spy. The ledger-state-at-failure-point assertions drive the
module's own building blocks directly (`_masked_batches`), since a raised
exception has nothing to attach evidence to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.execution import _native_route_exec as _exec_mod
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._native_route import NativeRouteLedger
from decoy_engine.profile._readers import LazySource

_ENGINE_VERSION = "native-route-transactional-failure-test"
_TABLE = "t"


def _write_source(tmp_path: Path, table: pa.Table, name: str = "src") -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _config(tmp_path: Path, table: pa.Table) -> tuple[dict[str, Any], Path]:
    source_path = _write_source(tmp_path, table)
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260904},
        "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            _TABLE: {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
        },
        "tables": [
            {
                "name": _TABLE,
                "columns": [
                    {"name": "pt", "strategy": "passthrough"},
                    {
                        "name": "tr",
                        "strategy": "truncate",
                        "provider_config": {"length": 4, "keep": "head"},
                    },
                ],
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump(), source_path


def _multi_batch_table(n: int) -> pa.Table:
    return pa.table(
        {
            "pt": pa.array([f"v{i}" for i in range(n)], type=pa.utf8()),
            "tr": pa.array([f"tailval{i:06d}" for i in range(n)], type=pa.utf8()),
        }
    )


@pytest.fixture
def _oracle_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from decoy_engine.execution import _chunked as _chunked_mod

    calls = {"n": 0}
    orig = _chunked_mod.run_mask_pipeline_chunked

    def _spy(*args: Any, **kwargs: Any):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(_chunked_mod, "run_mask_pipeline_chunked", _spy)
    return calls


def test_late_kernel_failure_aborts_no_artifact_no_oracle_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _oracle_spy: dict[str, int]
) -> None:
    n = 60_000  # 2 batches at the lane's default 50,000-row batch size
    config, source_path = _config(tmp_path, _multi_batch_table(n))
    sink_dir = tmp_path / "sink"

    calls = {"n": 0}
    orig_truncate = _exec_mod.native_truncate

    def _flaky_truncate(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] >= 2:  # first batch's call succeeds; the second batch's fails
            raise RuntimeError("injected late kernel failure")
        return orig_truncate(*args, **kwargs)

    monkeypatch.setattr(_exec_mod, "native_truncate", _flaky_truncate)

    with pytest.raises(RuntimeError, match="injected late kernel failure"):
        run_pipeline(
            config,
            {_TABLE: LazySource(path=source_path)},
            engine_version=_ENGINE_VERSION,
            native_route_enabled=True,
            execution_mode="auto",
            sink=ParquetTransactionalSink(sink_dir),
        )

    assert not sink_dir.exists(), "no final artifact should be published on a mid-stream failure"
    assert not any(sink_dir.parent.glob("_decoy_stage_*")), "staged data must be cleaned up"
    assert _oracle_spy["n"] == 0, "a failure past admission must never fall back to the oracle"


def test_first_batch_read_failure_never_enters_the_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _oracle_spy: dict[str, int]
) -> None:
    """A widened (two-read) admit whose EXECUTION read raises while fetching its
    first batch must fail before the sink is ever entered: the second-read
    schema already matched, so no output is staged. This pins the ordering that
    `first = next(execution_batches, None)` runs before `_run_native_streaming`.
    Skipping that fetch (leaving `first = None`) would instead raise inside
    `write_batches`, having already staged the sink and forcing an abort -- a
    weaker guarantee, so the sink must record zero write_batches calls here.
    The spy patches the class method (not a subclass) because admission's sink
    predicate is an exact `type(sink) is ParquetTransactionalSink` check."""
    source_path = _write_source(tmp_path, pa.table({"pt": pa.array([1, 2, 3], type=pa.int64())}))
    raw = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            _TABLE: {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
        },
        "tables": [{"name": _TABLE, "columns": [{"name": "pt", "strategy": "passthrough"}]}],
    }
    config = PipelineConfig.model_validate(raw).model_dump()

    class _FirstReadFailingSource(LazySource):
        # Preflight reads the real data via iter_batches (admits); the execution
        # read via open_batches returns the real footer schema (so the drift
        # guard passes) paired with an iterator that raises on its first batch.
        def open_batches(self, batch_rows: int):
            schema, _batches = super().open_batches(batch_rows)

            def _raise_on_first() -> Any:
                raise RuntimeError("injected first-read failure")
                yield  # pragma: no cover - only to make this a generator

            return schema, _raise_on_first()

    write_calls = {"n": 0}
    orig_write = ParquetTransactionalSink.write_batches

    def _counting_write(self: ParquetTransactionalSink, *args: Any, **kwargs: Any):
        write_calls["n"] += 1
        return orig_write(self, *args, **kwargs)

    monkeypatch.setattr(ParquetTransactionalSink, "write_batches", _counting_write)

    sink_dir = tmp_path / "sink"
    with pytest.raises(RuntimeError, match="injected first-read failure"):
        run_pipeline(
            config,
            {_TABLE: _FirstReadFailingSource(path=source_path)},
            engine_version=_ENGINE_VERSION,
            native_route_enabled=True,
            execution_mode="auto",
            sink=ParquetTransactionalSink(sink_dir),
            use_byte_estimate_routing=False,
        )

    assert write_calls["n"] == 0, "a first-read failure must raise before the sink is entered"
    assert not sink_dir.exists(), "no artifact on a first-read failure"
    assert not any(sink_dir.parent.glob("_decoy_stage_*")), "nothing staged on a first-read failure"
    assert _oracle_spy["n"] == 0, "a failure past admission must never fall back to the oracle"


def test_ledger_validation_failure_aborts_no_artifact_no_oracle_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _oracle_spy: dict[str, int]
) -> None:
    config, source_path = _config(tmp_path, _multi_batch_table(10))
    sink_dir = tmp_path / "sink"

    def _always_invalid(ledger: NativeRouteLedger, *, table: str) -> None:
        raise ExecutionError(code="native_route_ledger_invalid", message="forced for this test")

    monkeypatch.setattr(_exec_mod, "_validate_ledger", _always_invalid)

    with pytest.raises(ExecutionError) as excinfo:
        run_pipeline(
            config,
            {_TABLE: LazySource(path=source_path)},
            engine_version=_ENGINE_VERSION,
            native_route_enabled=True,
            execution_mode="auto",
            sink=ParquetTransactionalSink(sink_dir),
        )
    assert excinfo.value.code == "native_route_ledger_invalid"
    assert not sink_dir.exists()
    assert not any(sink_dir.parent.glob("_decoy_stage_*"))
    assert _oracle_spy["n"] == 0


def test_commit_failure_aborts_no_artifact_no_oracle_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _oracle_spy: dict[str, int]
) -> None:
    config, source_path = _config(tmp_path, _multi_batch_table(10))
    sink_dir = tmp_path / "sink"
    sink = ParquetTransactionalSink(sink_dir)

    abort_calls = {"n": 0}
    orig_abort = sink.abort

    def _counting_abort() -> None:
        abort_calls["n"] += 1
        orig_abort()

    def _failing_commit() -> None:
        raise OSError("injected commit failure")

    monkeypatch.setattr(sink, "commit", _failing_commit)
    monkeypatch.setattr(sink, "abort", _counting_abort)

    with pytest.raises(OSError, match="injected commit failure"):
        run_pipeline(
            config,
            {_TABLE: LazySource(path=source_path)},
            engine_version=_ENGINE_VERSION,
            native_route_enabled=True,
            execution_mode="auto",
            sink=sink,
        )
    assert abort_calls["n"] == 1, "commit() failing before it returns must still trigger abort()"
    assert not sink_dir.exists()
    assert _oracle_spy["n"] == 0


def test_schema_drift_batch_aborts_ledger_reflects_partial_state(tmp_path: Path) -> None:
    """Drives `_masked_batches` directly: a second batch whose schema no
    longer matches the admitted first batch raises `native_chunk_schema_
    drift`, increments `rejected_chunks`, and leaves the ledger's `native_
    attempted`/`native_completed` counts equal to only what the FIRST batch
    (successfully masked before the drift was seen) actually did."""
    first = pa.record_batch({"pt": pa.array(["a", "b"], type=pa.utf8())})
    drifted = pa.record_batch({"pt": pa.array([1, 2], type=pa.int64())})  # type changed

    ledger = NativeRouteLedger()
    timing_acc: dict[tuple[str, str], float] = {}
    boundary_ms_box = [0.0]
    out_schema = pa.schema([pa.field("pt", pa.utf8())])
    strategy_cfg: dict[str, tuple[str, dict[str, Any]]] = {"pt": ("passthrough", {})}

    gen = _exec_mod._masked_batches(
        first,
        iter([drifted]),
        table=_TABLE,
        column_order=("pt",),
        strategy_cfg=strategy_cfg,
        out_schema=out_schema,
        ledger=ledger,
        timing_acc=timing_acc,
        boundary_ms_box=boundary_ms_box,
    )

    first_out = next(gen)  # the first batch masks fine
    assert first_out.num_rows == 2
    assert ledger.native_attempted == 1
    assert ledger.native_completed == 1
    assert ledger.rejected_chunks == 0

    with pytest.raises(ExecutionError) as excinfo:
        next(gen)
    assert excinfo.value.code == "native_chunk_schema_drift"
    assert ledger.rejected_chunks == 1
    # The drifted batch was rejected BEFORE any column was attempted on it,
    # so attempted/completed are unchanged from the first batch's state --
    # the ledger never claims work it did not do.
    assert ledger.native_attempted == 1
    assert ledger.native_completed == 1


def test_masked_batches_rejected_chunks_increments_not_overwrites() -> None:
    """`rejected_chunks` is an invocation-scoped counter (see
    `NativeRouteLedger`'s docstring): a drift must add to whatever count the
    ledger already carries, never reset it to 1."""
    first = pa.record_batch({"pt": pa.array(["a"], type=pa.utf8())})
    drifted = pa.record_batch({"pt": pa.array([1], type=pa.int64())})
    ledger = NativeRouteLedger(rejected_chunks=5)
    out_schema = pa.schema([pa.field("pt", pa.utf8())])

    gen = _exec_mod._masked_batches(
        first,
        iter([drifted]),
        table=_TABLE,
        column_order=("pt",),
        strategy_cfg={"pt": ("passthrough", {})},
        out_schema=out_schema,
        ledger=ledger,
        timing_acc={},
        boundary_ms_box=[0.0],
    )
    next(gen)
    with pytest.raises(ExecutionError):
        next(gen)
    assert ledger.rejected_chunks == 6
