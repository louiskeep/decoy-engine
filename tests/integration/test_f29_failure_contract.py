"""F29 acceptance A4: secret, capacity and unknown failures are distinct, truthful and path-free.

Real runs, real sinks, real filesystem faults injected at the writer, the footer close, the commit
rename and the user callback. Each failure is read back through the public
`reason_from_exception`, which returns the record the engine captured at the boundary that saw it.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.keyprovider import MissingMaskSecret
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import CleanupState, Operation, Phase, PublicationState, RetryClass

from ._f29_jobs import ENGINE_VERSION, fk_job

SECRET_PATH = "/very/secret/dir/ZXQ-leak"


def _blob(record: reasons.ReasonRecord) -> str:
    return json.dumps(record.to_payload()) + repr(record)


def _run(tmp_path: Path, sink, **kw):
    cfg, sources = fk_job(tmp_path)
    return run_pipeline(
        cfg, sources, engine_version=ENGINE_VERSION, execution_mode="sequential", sink=sink, **kw
    )


def test_missing_mask_secret_keeps_its_code_and_requires_configuration_repair(
    tmp_path: Path,
) -> None:
    cfg, sources = fk_job(tmp_path, mask_secret_ref="env:F29_NO_SUCH_SECRET_XYZ")
    with pytest.raises(MissingMaskSecret) as info:
        run_pipeline(cfg, sources, engine_version=ENGINE_VERSION)
    record = reason_from_exception(info.value)
    assert (record.namespace, record.code) == ("runtime", "missing_mask_secret")
    assert record.retry_class is RetryClass.CONFIGURATION_REPAIR
    assert record.phase is Phase.RESOLVE
    assert record.outcome is reasons.Outcome.CONFIGURATION
    assert "F29_NO_SUCH_SECRET" not in _blob(record)


@pytest.mark.parametrize("fault", [errno.ENOSPC, errno.EDQUOT])
def test_capacity_exhausted_at_write_is_an_environment_repair_with_truthful_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: int
) -> None:
    def full(*a, **k):
        raise OSError(fault, "No space left on device", f"{SECRET_PATH}/customers.parquet")

    monkeypatch.setattr("decoy_engine.execution._transactional_sink.pq.write_table", full)
    sink = ParquetTransactionalSink(tmp_path / "out")
    with pytest.raises(OSError) as info:
        _run(tmp_path, sink)
    record = reason_from_exception(info.value)
    assert (record.namespace, record.code) == ("decoy", "storage_capacity_exhausted")
    assert record.retry_class is RetryClass.ENVIRONMENT_REPAIR
    assert record.phase is Phase.PUBLISH
    assert record.outcome is reasons.Outcome.PUBLICATION
    ctx = record.context
    assert ctx.operation is Operation.WRITE and ctx.storage_ref == "output"
    assert ctx.publication_state is PublicationState.NOT_PUBLISHED
    assert ctx.cleanup_state is CleanupState.CONFIRMED  # abort ran, and the staging dir is gone
    assert "ZXQ" not in _blob(record) and "/very" not in _blob(record)
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob("_decoy_stage_*"))


def test_enospc_during_footer_close_reports_the_close_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pq.ParquetWriter

    class FullOnClose(real):  # type: ignore[misc, valid-type]
        def close(self):
            super().close()
            raise OSError(errno.ENOSPC, "No space left on device", f"{SECRET_PATH}/footer")

    monkeypatch.setattr("decoy_engine.execution._transactional_sink.pq.ParquetWriter", FullOnClose)
    sink = ParquetTransactionalSink(tmp_path / "out")
    schema = pa.schema([("a", pa.int64())])
    with pytest.raises(OSError) as info:
        sink.write_batches("t", [pa.record_batch([pa.array([1, 2])], schema=schema)], schema=schema)
    record = reason_from_exception(info.value)
    assert record.code == "storage_capacity_exhausted"
    assert record.context.operation is Operation.CLOSE
    assert record.context.publication_state is PublicationState.NOT_PUBLISHED
    assert "ZXQ" not in _blob(record)
    sink.abort()
    assert sink.publication_evidence.cleanup is CleanupState.CONFIRMED


def test_an_unrelated_oserror_is_a_publication_failure_not_a_capacity_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a, **k):
        raise OSError(errno.EIO, "Input/output error", f"{SECRET_PATH}/x")

    monkeypatch.setattr("decoy_engine.execution._transactional_sink.pq.write_table", broken)
    with pytest.raises(OSError) as info:
        _run(tmp_path, ParquetTransactionalSink(tmp_path / "out"))
    record = reason_from_exception(info.value)
    assert record.code == "publication_failed"
    assert record.retry_class is RetryClass.UNKNOWN
    assert "ZXQ" not in _blob(record)


def test_a_failed_commit_rename_publishes_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device", f"{SECRET_PATH}/target")

    monkeypatch.setattr("decoy_engine.execution._transactional_sink.os.replace", refuse)
    with pytest.raises(OSError) as info:
        _run(tmp_path, ParquetTransactionalSink(tmp_path / "out"))
    record = reason_from_exception(info.value)
    assert record.code == "storage_capacity_exhausted"
    assert record.context.operation is Operation.COMMIT
    assert record.context.publication_state is PublicationState.NOT_PUBLISHED
    assert record.context.cleanup_state is CleanupState.CONFIRMED
    assert "ZXQ" not in _blob(record)


def test_an_unknown_callback_exception_leaves_the_publication_state_unknown(tmp_path: Path) -> None:
    calls: list[str] = []

    def callback(table: str, data: pa.Table) -> None:
        calls.append(table)
        if len(calls) == 2:
            raise RuntimeError(f"callback exploded {SECRET_PATH}")

    with pytest.raises(RuntimeError) as info:
        _run(tmp_path, callback)
    record = reason_from_exception(info.value)
    assert (record.namespace, record.code) == ("decoy", "unexpected_error")
    assert record.phase is Phase.PUBLISH
    assert record.retry_class is RetryClass.UNKNOWN
    assert record.context.operation is Operation.WRITE
    # The callable published its first table as it went: the outcome is not knowable, not "clean".
    assert record.context.publication_state is PublicationState.UNKNOWN
    assert record.context.cleanup_state is CleanupState.UNKNOWN
    assert "ZXQ" not in _blob(record)
