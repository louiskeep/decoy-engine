"""Shared helpers for the B6b LazySource batch-input acceptance tests.

Plan: docs/plans/2026-10-02-b6b-lazy-batch-input.md (revision 2.1). The tests compare a
call that passes `LazySource(path)` with its resident twin (the same call with
`pq.read_table(path)`), spy on what the lazy handle reads, and inject failures without
depending on the implementation under test. The new modules are imported lazily so a
missing implementation fails the test that needs it, not the whole file at collection.
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import json
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a
from tests.unit.execution import _multi_table_support as mt

TABLE = support.TABLE
INPUT_MODULE = "decoy_engine.execution._chunked_input"


def chunked_input() -> Any:
    return importlib.import_module(INPUT_MODULE)


def write(table: pa.Table, path: Path, row_group_size: int | None = None, **kwargs: Any) -> str:
    """Write `table` and return the path; `row_group_size=None` is pyarrow's default."""
    if row_group_size is None:
        pq.write_table(table, str(path), **kwargs)
    else:
        pq.write_table(table, str(path), row_group_size=row_group_size, **kwargs)
    return str(path)


def lazy(path: str | Path) -> LazySource:
    return LazySource(Path(path))


def twin_sources(sources: Mapping[str, Any]) -> dict[str, pa.Table]:
    """The resident twin of a sources mapping: every `LazySource` read with `pq.read_table`."""
    return {
        name: pq.read_table(src.path) if isinstance(src, LazySource) else src
        for name, src in sources.items()
    }


def run(cfg: dict[str, Any], sources: Mapping[str, Any], sink: Any = None, **extra: Any) -> Any:
    kwargs: dict[str, Any] = {} if sink is None else {"sink": sink}
    return run_pipeline(cfg, sources=sources, **kwargs, **support.run_kwargs(**extra))


def run_both(
    cfg: dict[str, Any],
    sources: Mapping[str, Any],
    tmp_path: Path,
    *,
    real: bool = False,
    **extra: Any,
) -> tuple[Any, Any, Any, Any, Path | None, Path | None]:
    """`(lazy_result, twin_result, lazy_sink, twin_sink, lazy_target, twin_target)`."""
    if real:
        lsink, ltarget = b6a.real_sink(tmp_path, "lazy_out")
        tsink, ttarget = b6a.real_sink(tmp_path, "twin_out")
    else:
        lsink, tsink = b6a.RecordingSink(), b6a.RecordingSink()
        ltarget = ttarget = None
    got = run(cfg, sources, lsink, **extra)
    want = run(cfg, twin_sources(sources), tsink, **extra)
    return got, want, lsink, tsink, ltarget, ttarget


def strip_input_leaves(quality_metrics: Mapping[str, Any]) -> dict[str, Any]:
    """`quality_metrics` without the two leaves guarantee 5 adds, and without wall-clock fields."""
    qm = copy.deepcopy(dict(quality_metrics))
    block = qm.get("auto_chunk")
    if isinstance(block, dict):
        block.pop("input", None)
        for entry in block.get("tables", ()):
            entry.pop("input", None)
    execution = qm.get("execution")
    if isinstance(execution, dict):
        execution.pop("loaded_fully_in_memory", None)
    stripped: dict[str, Any] = mt.strip_elapsed(qm)
    return stripped


def assert_lazy_equals_twin(got: Any, want: Any, lsink: Any, tsink: Any) -> None:
    """Guarantee 1: same sink calls, batches, result fields and metrics (minus the two leaves)."""
    assert lsink.calls == tsink.calls
    assert lsink.schemas.keys() == tsink.schemas.keys()
    for name in tsink.batches:
        assert lsink.schemas[name].equals(tsink.schemas[name], check_metadata=True), name
        assert len(lsink.batches[name]) == len(tsink.batches[name]), name
        for a, b in zip(lsink.batches[name], tsink.batches[name], strict=True):
            assert a.equals(b), name
    assert list(got.outputs) == list(want.outputs)
    for name in want.outputs:
        assert got.outputs[name].equals(want.outputs[name], check_metadata=True), name
    assert strip_input_leaves(got.quality_metrics) == strip_input_leaves(want.quality_metrics)
    assert got.warnings == want.warnings
    assert mt.timing_keys(got) == mt.timing_keys(want)
    assert got.table_kinds == want.table_kinds
    assert got.row_errors == want.row_errors
    assert (got.boundary_conversion_ms == 0.0) == (want.boundary_conversion_ms == 0.0)
    assert got.boundary_conversion_ms >= 0.0


def input_block(result: Any, table: str | None = None) -> dict[str, Any]:
    block = result.quality_metrics["auto_chunk"]
    if table is None:
        return dict(block["input"])
    entry = next(t for t in block["tables"] if t["table"] == table)
    return dict(entry["input"])


def json_safe(value: Any) -> str:
    return json.dumps(value, allow_nan=False, sort_keys=True)


class ReadSpies:
    """Counts the whole-file readers a lazy run must never call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.to_table: list[Path] = []
        self.read_table: list[Any] = []
        self.parquet_file_read: list[Any] = []
        real_to_table = LazySource.to_table
        real_read_table = pq.read_table
        real_pf_read = pq.ParquetFile.read

        def to_table(src: LazySource) -> pa.Table:
            self.to_table.append(src.path)
            return real_to_table(src)

        def read_table(*args: Any, **kwargs: Any) -> pa.Table:
            self.read_table.append(args)
            return real_read_table(*args, **kwargs)

        def pf_read(pf: Any, *args: Any, **kwargs: Any) -> pa.Table:
            self.parquet_file_read.append(args)
            return real_pf_read(pf, *args, **kwargs)

        monkeypatch.setattr(LazySource, "to_table", to_table)
        monkeypatch.setattr(pq, "read_table", read_table)
        monkeypatch.setattr(pq.ParquetFile, "read", pf_read)

    def none_called(self) -> bool:
        return not (self.to_table or self.read_table or self.parquet_file_read)


class ParquetFileSpy:
    """Records every `pq.ParquetFile` construction (kwargs) and `iter_batches` call (kwargs),
    and whether `close()` was called on it. Subclasses the real class so behavior is unchanged."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[dict[str, Any]] = []
        self.iter_calls: list[dict[str, Any]] = []
        self.instances: list[Any] = []
        self.closed: list[Any] = []
        spy = self
        real = pq.ParquetFile

        class Spy(real):  # type: ignore[valid-type, misc]
            def __init__(self, source: Any, *args: Any, **kwargs: Any) -> None:
                spy.opened.append({"source": source, "args": args, **kwargs})
                spy.instances.append(self)
                super().__init__(source, *args, **kwargs)

            def iter_batches(self, *args: Any, **kwargs: Any) -> Any:
                spy.iter_calls.append({"args": args, **kwargs})
                return super().iter_batches(*args, **kwargs)

            def close(self) -> None:
                spy.closed.append(self)
                super().close()

        monkeypatch.setattr(pq, "ParquetFile", Spy)


def open_fds_on(path: str | Path) -> list[str]:
    """`/proc/self/fd` entries that point at `path` (Linux; empty elsewhere)."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        return []
    found: list[str] = []
    target = str(Path(path).resolve())
    for entry in fd_dir.iterdir():
        with contextlib.suppress(OSError):
            if str(entry.resolve()) == target:
                found.append(entry.name)
    return found


def spy_on(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, log: list[Any]) -> None:
    real = getattr(owner, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        log.append((name, args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, spy)


@contextlib.contextmanager
def companion_state(monkeypatch: pytest.MonkeyPatch, state: str) -> Iterator[None]:
    if state == "absent":
        support.remove_companion(monkeypatch)
    yield


def fails_with(exc_type: type[BaseException], fn: Callable[[], Any]) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def stream_job(
    tmp_path: Path, rows: int, row_group_size: int | None = None
) -> tuple[dict[str, Any], str]:
    """A native-route job (redact, truncate, passthrough) over `rows` rows, written in a
    bounded loop so no full table is resident, and its config."""
    path = tmp_path / "stream.parquet"
    schema = pa.schema([("r", pa.string()), ("z", pa.string()), ("p", pa.string())])
    step = row_group_size or 20_000
    with pq.ParquetWriter(str(path), schema) as writer:
        for start in range(0, rows, step):
            stop = min(start + step, rows)
            writer.write_table(
                pa.table(
                    {
                        "r": pa.array([f"s{i}" for i in range(start, stop)]),
                        "z": pa.array([f"{i:07d}" for i in range(start, stop)]),
                        "p": pa.array([f"keep-{i}" for i in range(start, stop)]),
                    },
                    schema=schema,
                ),
                row_group_size=step,
            )
    cfg = support.make_cfg(
        [support.redact_col("r"), support.truncate_col("z"), support.pass_col("p")],
        path=str(path),
    )
    return cfg, str(path)


class CountingSink:
    """A streaming sink that keeps nothing: batches are consumed and dropped, so a memory
    measurement sees the engine's working set and not a test sink's copy of the output."""

    def __init__(self, on_batch: Callable[[str, pa.RecordBatch], None] | None = None) -> None:
        self.on_batch = on_batch
        self.calls: list[tuple[str, ...]] = []
        self.rows = 0

    def write(self, table: str, data: pa.Table) -> None:
        self.calls.append(("write", table))

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.calls.append(("write_batches", table))
        for batch in batches:
            self.rows += batch.num_rows
            if self.on_batch is not None:
                self.on_batch(table, batch)

    def commit(self) -> None:
        self.calls.append(("commit",))

    def abort(self) -> None:
        self.calls.append(("abort",))


def is_streaming_run(result: Any) -> bool:
    return bool(result.quality_metrics["execution"]["outputs_streamed"])


__all__ = [
    "INPUT_MODULE",
    "TABLE",
    "CountingSink",
    "ParquetFileSpy",
    "ReadSpies",
    "assert_lazy_equals_twin",
    "b6a",
    "chunked_input",
    "companion_state",
    "fails_with",
    "input_block",
    "is_streaming_run",
    "json_safe",
    "lazy",
    "mt",
    "open_fds_on",
    "run",
    "run_both",
    "spy_on",
    "stream_job",
    "strip_input_leaves",
    "support",
    "twin_sources",
    "write",
]
