"""Acceptance test 6 of plan 2026-10-02-b6b-lazy-batch-input (rev 2.1): failures and cleanup.

A changed or missing source, a read error, a short read, a masking error, a sink error and
`KeyboardInterrupt` each leave nothing published, abort the session once, and close the
source handle.

These tests are written before the implementation. Do not delete one, add a skip or xfail,
or relax a comparison without a new plan gate.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6b_support as b6b

ROWS = 100
SINKS = ["recording", "parquet"]


def _source(rows: int = ROWS, nulls: bool = False) -> pa.Table:
    return pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(rows)]),
            "x": pa.array([None if nulls and i == 3 else i for i in range(rows)], pa.int64()),
        }
    )


class Job:
    """A native-route job whose source file the test can replace after routing.

    The config profiles a private copy, so replacing or removing `source` never disturbs
    `profile_source`."""

    def __init__(self, tmp_path: Path, kind: str, **write: Any) -> None:
        self.tmp_path = tmp_path
        self.source = tmp_path / "source.parquet"
        table = _source()
        b6b.write(table, self.source, row_group_size=30, **write)
        shutil.copy(self.source, tmp_path / "profile.parquet")
        self.cfg = support.make_cfg(
            [support.redact_col("r"), support.pass_col("x")],
            path=str(tmp_path / "profile.parquet"),
        )
        if kind == "recording":
            self.sink: Any = b6b.b6a.RecordingSink()
            self.target: Path | None = None
        else:
            self.sink, self.target = b6b.b6a.real_sink(tmp_path, "out")

    def run(self, **extra: Any) -> Any:
        return b6b.run(
            self.cfg, {b6b.TABLE: b6b.lazy(self.source)}, self.sink, chunk_size_rows=16, **extra
        )

    def assert_clean(self) -> None:
        assert self.sink.count("abort") == 1 and self.sink.count("commit") == 0
        if self.target is not None:
            assert not self.target.exists()
        assert b6b.b6a.leftovers(self.tmp_path) == []


class Probes:
    """The close spy, the ParquetFile spy and a chunk counter, installed together."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from decoy_engine.execution.native import _chunked_entry
        from decoy_engine.profile._readers import OpenedLazyBatches

        self.files = b6b.ParquetFileSpy(monkeypatch)
        self.closes: list[Any] = []
        real_close = OpenedLazyBatches.close
        monkeypatch.setattr(
            OpenedLazyBatches,
            "close",
            lambda owner: (self.closes.append(owner), real_close(owner))[1],
        )
        self.pulled: list[int] = []
        real_mask = _chunked_entry.run_mask_chunked

        def counted(config: Any, chunks: Any, **kwargs: Any) -> Any:
            def tap() -> Any:
                for chunk in chunks:
                    self.pulled.append(chunk.num_rows)
                    yield chunk

            return real_mask(config, tap(), **kwargs)

        monkeypatch.setattr(_chunked_entry, "run_mask_chunked", counted)

    def readers(self) -> list[Any]:
        return [
            pf
            for pf, o in zip(self.files.instances, self.files.opened, strict=True)
            if o.get("pre_buffer") is False
        ]

    def assert_closed(self, path: Path) -> None:
        assert self.closes, "the close spy saw no call"
        readers = self.readers()
        assert readers and all(pf in self.files.closed for pf in readers)
        assert b6b.open_fds_on(path) == []


def _hook_open_input(monkeypatch: pytest.MonkeyPatch, before: Callable[[], None]) -> None:
    mod = b6b.chunked_input()
    real = mod.open_input

    def hooked(source: Any, **kwargs: Any) -> Any:
        before()
        return real(source, **kwargs)

    monkeypatch.setattr(mod, "open_input", hooked)


def _replace(job: Job, table: pa.Table, **write: Any) -> Callable[[], None]:
    return lambda: b6b.write(table, job.source, **write)


@pytest.mark.parametrize("kind", SINKS)
@pytest.mark.parametrize("variant", ["row_count", "schema", "null_count", "row_group_layout"])
def test_a_source_changed_after_routing_raises_before_any_chunk_and_closes_the_handle(
    variant: str, kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    probes = Probes(monkeypatch)
    if variant == "row_count":
        replacement = _replace(job, _source(90), row_group_size=30)
    elif variant == "schema":
        changed = _source().rename_columns(["r", "y"])
        replacement = _replace(job, changed, row_group_size=30)
    elif variant == "null_count":
        replacement = _replace(job, _source(nulls=True), row_group_size=30)
    else:
        replacement = _replace(job, _source(), row_group_size=45)
    _hook_open_input(monkeypatch, replacement)
    with pytest.raises(ExecutionError) as err:
        job.run()
    assert err.value.code == "lazy_source_changed"
    assert probes.pulled == []
    probes.assert_closed(job.source)
    job.assert_clean()


@pytest.mark.parametrize("kind", SINKS)
def test_a_source_missing_at_routing_raises_with_zero_sink_calls(kind: str, tmp_path: Path) -> None:
    job = Job(tmp_path, kind)
    job.source.unlink()
    with pytest.raises(FileNotFoundError):
        job.run()
    assert job.sink.calls == []
    if job.target is not None:
        assert not job.target.exists()
    assert b6b.b6a.leftovers(tmp_path) == []


@pytest.mark.parametrize("kind", SINKS)
def test_a_source_deleted_after_routing_raises_with_one_abort(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    _hook_open_input(monkeypatch, job.source.unlink)
    with pytest.raises(FileNotFoundError):
        job.run()
    job.assert_clean()


@pytest.mark.parametrize("kind", SINKS)
def test_a_masking_error_on_chunk_two_closes_the_handle(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    probes = Probes(monkeypatch)
    b6b.b6a.ChunkSpy(monkeypatch, fail_at=2)
    with pytest.raises(RuntimeError, match="injected kernel failure"):
        job.run()
    probes.assert_closed(job.source)
    job.assert_clean()


@pytest.mark.parametrize("kind", SINKS)
def test_a_sink_write_error_closes_the_handle(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    probes = Probes(monkeypatch)
    seen = {"n": 0}

    def explode(_table: str, _batch: pa.RecordBatch) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            raise OSError("injected sink failure")

    job.sink.on_batch = explode
    from decoy_engine.execution import _chunked_output_sink as out

    monkeypatch.setattr(out, "ROW_GROUP_ROWS", 16)
    with pytest.raises(OSError, match="injected sink failure"):
        job.run()
    probes.assert_closed(job.source)
    job.assert_clean()


@pytest.mark.parametrize("kind", SINKS)
def test_a_successful_run_closes_the_handle(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    probes = Probes(monkeypatch)
    result = job.run()
    assert result.quality_metrics["auto_chunk"]["input"]["mode"] == "lazy"
    assert job.sink.count("commit") == 1
    probes.assert_closed(job.source)
    assert probes.pulled == [16] * 6 + [4]


def _corrupt_second_row_group(path: Path) -> None:
    meta = pq.ParquetFile(path).metadata
    column = meta.row_group(1).column(0)
    start = column.dictionary_page_offset or column.data_page_offset
    raw = bytearray(path.read_bytes())
    raw[start : start + 16] = b"\xff" * 16
    path.write_bytes(bytes(raw))


@pytest.mark.parametrize("kind", SINKS)
def test_a_corrupt_data_page_in_the_second_row_group_fails_at_that_row_group(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    _corrupt_second_row_group(job.source)
    probes = Probes(monkeypatch)
    with pytest.raises((OSError, pa.ArrowException)):
        job.run()
    assert probes.pulled and sum(probes.pulled) <= 30, probes.pulled
    probes.assert_closed(job.source)
    job.assert_clean()


@pytest.mark.parametrize("kind", SINKS)
def test_a_reader_that_yields_one_row_fewer_raises_a_row_count_mismatch(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    probes = Probes(monkeypatch)
    real = pq.ParquetFile.iter_batches

    def short(self: Any, *args: Any, **kwargs: Any) -> Any:
        cut = False
        for batch in real(self, *args, **kwargs):
            if not cut and batch.num_rows > 1:
                cut = True
                yield batch.slice(0, batch.num_rows - 1)
            else:
                yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", short)
    with pytest.raises(ExecutionError) as err:
        job.run()
    assert err.value.code == "lazy_source_row_count_mismatch"
    probes.assert_closed(job.source)
    job.assert_clean()


@pytest.mark.parametrize("kind", SINKS)
def test_a_keyboard_interrupt_from_the_reader_closes_the_handle(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = Job(tmp_path, kind)
    probes = Probes(monkeypatch)
    real = pq.ParquetFile.iter_batches

    def interrupted(self: Any, *args: Any, **kwargs: Any) -> Any:
        for index, batch in enumerate(real(self, *args, **kwargs)):
            if index == 2:
                raise KeyboardInterrupt
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", interrupted)
    with pytest.raises(KeyboardInterrupt):
        job.run()
    probes.assert_closed(job.source)
    job.assert_clean()
