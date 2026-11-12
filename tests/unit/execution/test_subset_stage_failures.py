"""The staging writer names only a full scratch disk as a capacity failure (SUB-RUN E2 review).

A source read error, a scratch permission error and a failure after a yielded batch keep their
own types, and the opened source is closed before any of them escapes.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons, run_pipeline
from decoy_engine.declared_casting import _stream
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.profile._readers import LazySource, OpenedLazyBatches
from tests.unit.execution import _subset_run_support as sup

CAPACITY = reasons.runtime.OUT_OF_CORE_TEMP_DISK_EXCEEDED


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sup.VAR, "ab" * 32)


def _run(tmp_path: Path) -> Any:
    cfg, _, lazy = sup.job(tmp_path)
    return run_pipeline(cfg, lazy, engine_version="t")


def _raising(exc: BaseException) -> Any:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    return boom


@pytest.mark.parametrize("exc", [PermissionError(errno.EACCES, "x"), OSError(errno.EIO, "x")])
def test_a_source_read_error_is_not_a_capacity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: OSError
) -> None:
    monkeypatch.setattr(LazySource, "open_batches", _raising(exc))
    with pytest.raises(OSError) as info:
        _run(tmp_path)
    assert info.value.errno == exc.errno
    assert getattr(info.value, "code", None) != CAPACITY


@pytest.mark.parametrize("number", [errno.ENOSPC, errno.EDQUOT])
def test_a_full_scratch_disk_is_the_capacity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    monkeypatch.setattr(pq.ParquetWriter, "write_table", _raising(OSError(number, "no room")))
    with pytest.raises(ExecutionError) as info:
        _run(tmp_path)
    assert info.value.code == CAPACITY
    text = str(info.value)
    assert str(tmp_path) not in text and "no room" not in text and "customers" not in text


def test_a_scratch_permission_error_stays_a_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.out_of_core import _scratch

    monkeypatch.setattr(_scratch, "open_private", _raising(PermissionError(errno.EACCES, "x")))
    with pytest.raises(PermissionError):
        _run(tmp_path)


def test_the_opened_source_is_closed_before_a_failure_after_a_batch_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.out_of_core import _budget

    closed: list[bool] = []
    real_close = OpenedLazyBatches.close

    def close(self: OpenedLazyBatches) -> None:
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(OpenedLazyBatches, "close", close)
    monkeypatch.setattr(_budget, "check_temp_disk_budget", _raising(RuntimeError("stop")))
    with pytest.raises(RuntimeError):
        _run(tmp_path)
        pytest.fail("the staging failure was swallowed")
    # Read while the exception (and the frames it holds) is still alive.
    assert closed, "the producer was left to generator finalization"


def test_declared_casting_staging_maps_a_full_disk_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "src.parquet"
    pq.write_table(pa.table({"v": ["a", "b"]}), path)
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(pq.ParquetWriter, "write_table", _raising(OSError(errno.ENOSPC, "x")))
    with pytest.raises(ExecutionError) as info:
        _stream.stage_typed_sources(
            ["t"],
            {"t": LazySource(path)},
            batch_fn=lambda _t, b, _o: pa.Table.from_batches([b]),
            empty_fn=lambda _t, s: s.schema.empty_table(),
            resident_fn=lambda _t, v: v,
            root=root,
            batch_rows=10,
        )
    assert info.value.code == CAPACITY
