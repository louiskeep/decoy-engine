"""Shared helpers for the B6a incremental-output-sink acceptance tests.

Plan: docs/plans/2026-10-02-b6a-incremental-output-sink.md (revision 2.1). The tests
need the same few things: a recording sink that also forwards to a real
`ParquetTransactionalSink`, the resident reference run (`stream_chunked_output=False`),
a spy on B1's chunk iterator, and failure injection that does not depend on the
implementation under test.
"""

from __future__ import annotations

import contextlib
import gc
import importlib
import inspect
import weakref
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from tests.unit.execution import _auto_chunk_support as support

SINK_MODULE = "decoy_engine.execution._chunked_output_sink"


def knob_supported() -> bool:
    return "stream_chunked_output" in inspect.signature(run_pipeline).parameters


def resident_kw() -> dict[str, Any]:
    """Today's B2 / B7 behavior: the resident run. Before B6a exists the kwarg is
    omitted so the reference still runs (and the streamed side fails on its own)."""
    return {"stream_chunked_output": False} if knob_supported() else {}


def sink_module() -> Any:
    return importlib.import_module(SINK_MODULE)


class RecordingSink:
    """The four `TransactionalSink` methods, recording every call in order.

    `inner` forwards to a real sink (batches are passed on one at a time, so the
    interleaving with masking is preserved); `on_batch(table, batch)` runs for each
    received batch; `abort_raises` makes `abort()` raise after recording."""

    def __init__(
        self,
        inner: ParquetTransactionalSink | None = None,
        *,
        on_batch: Callable[[str, pa.RecordBatch], None] | None = None,
        abort_raises: bool = False,
    ) -> None:
        self.inner = inner
        self.on_batch = on_batch
        self.abort_raises = abort_raises
        self.calls: list[tuple[str, ...]] = []
        self.batches: dict[str, list[pa.RecordBatch]] = {}
        self.schemas: dict[str, pa.Schema] = {}

    def write(self, table: str, data: pa.Table) -> None:
        self.calls.append(("write", table))
        if self.inner is not None:
            self.inner.write(table, data)

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        self.calls.append(("write_batches", table))
        self.schemas[table] = schema
        got = self.batches.setdefault(table, [])

        def tap() -> Iterator[pa.RecordBatch]:
            for batch in batches:
                got.append(batch)
                if self.on_batch is not None:
                    self.on_batch(table, batch)
                yield batch

        if self.inner is not None:
            self.inner.write_batches(table, tap(), schema=schema)
        else:
            for _ in tap():
                pass

    def commit(self) -> None:
        self.calls.append(("commit",))
        if self.inner is not None:
            self.inner.commit()

    def abort(self) -> None:
        self.calls.append(("abort",))
        if self.inner is not None:
            self.inner.abort()
        if self.abort_raises:
            raise RuntimeError("abort failed")

    def table(self, name: str) -> pa.Table:
        return pa.Table.from_batches(self.batches[name], schema=self.schemas[name])

    def count(self, call: str) -> int:
        return sum(1 for c in self.calls if c[0] == call)


def run_streamed(cfg: dict[str, Any], src: pa.Table, sink: Any, **extra: Any) -> Any:
    return run_pipeline(cfg, sources={support.TABLE: src}, sink=sink, **support.run_kwargs(**extra))


def reference(cfg: dict[str, Any], src: pa.Table, **extra: Any) -> dict[str, pa.Table]:
    """The resident outputs of the same call with streaming off."""
    result = run_pipeline(
        cfg, sources={support.TABLE: src}, **support.run_kwargs(**resident_kw(), **extra)
    )
    return dict(result.outputs)


def real_sink(tmp_path: Path, name: str = "out") -> tuple[RecordingSink, Path]:
    target = tmp_path / name
    return RecordingSink(ParquetTransactionalSink(target)), target


def parquet_bytes(table: pa.Table, path: Path, **kwargs: Any) -> bytes:
    pq.write_table(table, path, **kwargs)
    return path.read_bytes()


def assert_streamed_equals(sink: RecordingSink, expected: pa.Table, table: str = "t") -> None:
    """Guarantee 1 (a): the received batches equal the resident table exactly."""
    assert sink.schemas[table].metadata is None
    got = sink.table(table)
    assert got.equals(expected, check_metadata=True), table
    assert got.schema.equals(expected.schema, check_metadata=True)


def leftovers(parent: Path) -> list[str]:
    """Staging and spill directories under `parent` (the sink's parent or the patched tempdir)."""
    return sorted(
        p.name for p in parent.iterdir() if p.name.startswith(("_decoy_stage_", "_decoy_hold_"))
    )


@contextlib.contextmanager
def patched_tempdir(monkeypatch: pytest.MonkeyPatch, path: Path) -> Iterator[Path]:
    import tempfile

    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    yield path


class ChunkSpy:
    """Wraps B1's `run_mask_chunked` (looked up through its module by the lane) and keeps a
    weak reference to every chunk it yields, plus the `chunk_result_sink` each call got.

    `fail_at` (a 0-based pull index within one table's iterator) raises `exc` instead of
    yielding that chunk; `before_pull(table, index)` runs before each pull."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail_at: int | None = None,
        exc: BaseException | None = None,
        before_pull: Callable[[str, int], None] | None = None,
    ) -> None:
        from decoy_engine.execution.native import _chunked_entry

        self.refs: dict[str, list[weakref.ref[pa.Table]]] = {}
        self.result_lists: dict[str, list[Any]] = {}
        self.nbytes: list[int] = []
        self.fail_at = fail_at
        self.exc = exc if exc is not None else RuntimeError("injected kernel failure")
        self.before_pull = before_pull
        real = _chunked_entry.run_mask_chunked

        def spy(config: Any, chunks: Any, *, table: str, **kwargs: Any) -> Iterator[pa.Table]:
            self.result_lists[table] = kwargs.get("chunk_result_sink")
            inner = real(config, chunks, table=table, **kwargs)
            return self._wrap(table, inner)

        monkeypatch.setattr(_chunked_entry, "run_mask_chunked", spy)

    def _wrap(self, table: str, inner: Iterator[pa.Table]) -> Iterator[pa.Table]:
        index = 0
        while True:
            if self.before_pull is not None:
                self.before_pull(table, index)
            if self.fail_at is not None and index == self.fail_at:
                raise self.exc
            try:
                chunk = next(inner)
            except StopIteration:
                return
            self.refs.setdefault(table, []).append(weakref.ref(chunk))
            self.nbytes.append(chunk.nbytes)
            index += 1
            yield chunk
            del chunk

    def alive(self, table: str | None = None) -> int:
        gc.collect()
        tables = [table] if table is not None else list(self.refs)
        return sum(1 for t in tables for r in self.refs.get(t, []) if r() is not None)
