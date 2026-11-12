"""DuckDB helper for out-of-core relational operators."""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._scratch import private_dir
from decoy_engine.execution.pipeline import detect_usable_cpus

# `_budget.py` (`resolve_budget` / `resolve_ooc_memory_limit`) is the only
# producer of the `memory_limit` string this module consumes, and it always
# emits `f"{mib}MB"` (decimal MiB count, base-10 "MB" suffix DuckDB reads
# literally). Parsing that shape back to bytes here is exact for every
# in-repo caller; an unrecognized shape (a hand-written literal outside that
# contract) degrades to no thread override rather than guessing.
# COPY options for a scratch Parquet file `precreate_private` made 0600: DuckDB writes that file
# in place instead of a sibling temp file renamed over it with an umask-derived mode.
PRIVATE_COPY = "FORMAT PARQUET, USE_TMP_FILE false"
_MEMORY_LIMIT_MB_PATTERN = re.compile(r"^(\d+)MB\Z")

# Per-thread working-set estimate DuckDB's own threads config is sized
# against: each thread holds its own scan/build buffers on top of the shared
# buffer manager, so an unthrottled thread count (DuckDB's own default is the
# CPU count) can multiply that per-thread overhead past a small memory_limit
# before any operator even spills.
_THREAD_BYTES_PER_THREAD = 2 * 1024 * 1024 * 1024  # 2 GiB


@dataclass(frozen=True, order=True)
class AppliedConnection:
    """What one connection was opened with; a None field was left to DuckDB's own default."""

    memory_limit_bytes: int | None
    threads: int | None


class DuckDBRunRecorder:
    """The thread cap bound for one run and the settings every connection of it applied."""

    def __init__(self, thread_cap: int | None) -> None:
        self.thread_cap = thread_cap
        self._applied: set[AppliedConnection] = set()
        self._lock = threading.Lock()

    def record(self, applied: AppliedConnection) -> None:
        with self._lock:
            self._applied.add(applied)

    def applied(self) -> list[AppliedConnection]:
        with self._lock:
            return sorted(self._applied, key=lambda a: (a.memory_limit_bytes or 0, a.threads or 0))


# Run-scoped, not process-global: `connect_duckdb` has six nested callers that never see the
# run's arguments, and two runs in one process must not read each other's cap.
_RUN: contextvars.ContextVar[DuckDBRunRecorder | None] = contextvars.ContextVar(
    "decoy_duckdb_run", default=None
)


@contextlib.contextmanager
def bind_duckdb_run(thread_cap: int | None) -> Iterator[DuckDBRunRecorder]:
    """Bind `thread_cap` for every `connect_duckdb` call made inside the block."""
    recorder = DuckDBRunRecorder(thread_cap)
    token = _RUN.set(recorder)
    try:
        yield recorder
    finally:
        _RUN.reset(token)


def connect_duckdb(*, temp_dir: Path, memory_limit: str | None = None):
    """Open an in-memory DuckDB connection with a restricted temp directory."""
    try:
        from decoy_engine.fileaccess.adapters import duckdb as duckdb_adapter
    except ModuleNotFoundError as exc:  # pragma: no cover - covered in no-duckdb envs
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_BACKEND_UNAVAILABLE,
            message="DuckDB is required for the out-of-core relationship route.",
        ) from exc

    private_dir(temp_dir)
    os.chmod(temp_dir, 0o700)
    # duckdb.connect's config maps str keys to a union value type; widen the
    # annotation to that surface even though every value here is a str/bool/int.
    config: dict[str, str | bool | int | float | list[str]] = {"temp_directory": str(temp_dir)}
    # preserve_insertion_order=False lets DuckDB's window/sort/aggregate
    # operators spill their state instead of pinning it all in memory to
    # preserve row order it does not need to guarantee on its own -- SAFE
    # here only because of a route-wide invariant this connection's callers
    # all honor: any DuckDB result whose row order reaches this route's
    # OUTPUT carries an explicit `ORDER BY` at the point it is read back
    # (`_batch_join.py` and `_join.py` both `ORDER BY __decoy_row_nr`), and
    # `_relation.py`'s dedup result is consumed only as a hash-join build
    # side (`_batch_join.py`'s `parent_keys` view), never order-sensitively.
    # A future caller of `connect_duckdb` that reads unordered output would
    # violate this invariant, not this setting.
    config["preserve_insertion_order"] = False
    run = _RUN.get()
    if memory_limit is not None:
        config["memory_limit"] = memory_limit
    threads = _threads_for_memory_limit(memory_limit, thread_cap=run.thread_cap if run else None)
    if threads is not None:
        config["threads"] = threads
    conn = duckdb_adapter.connect_internal(config)
    if run is not None:
        run.record(AppliedConnection(_memory_limit_bytes(memory_limit), threads))
    return conn


def _memory_limit_bytes(memory_limit: str | None) -> int | None:
    match = None if memory_limit is None else _MEMORY_LIMIT_MB_PATTERN.match(memory_limit)
    return None if match is None else int(match.group(1)) * 1024 * 1024


def _threads_for_memory_limit(
    memory_limit: str | None, *, thread_cap: int | None = None
) -> int | None:
    """DuckDB thread count: ``max(1, min(cap or usable CPUs, memory_limit // 2 GiB))``.

    The memory term is ~2 GiB per thread of the `"<MiB>MB"` limit `_budget.py` always emits. A
    `memory_limit` that is missing or not that shape has no memory term: with a cap the result is
    ``max(1, min(cap, usable CPUs))``, and without one it is None, leaving DuckDB's own default
    (the CPU count) in place rather than mis-deriving a figure.

    When a cap is given and CPU detection raises, the cap stands in for the CPU term, so the
    documented `out_of_core_threads` workaround for an unreadable cgroup works. Without a cap the
    detection error is not swallowed.
    """
    memory_limit_bytes = _memory_limit_bytes(memory_limit)
    if memory_limit_bytes is None and thread_cap is None:
        return None
    ceilings: list[int] = []
    try:
        ceilings.append(detect_usable_cpus())
    except ExecutionError:
        if thread_cap is None:
            raise
    if thread_cap is not None:
        ceilings.append(thread_cap)
    if memory_limit_bytes is not None:
        ceilings.append(memory_limit_bytes // _THREAD_BYTES_PER_THREAD)
    return max(1, min(ceilings))


__all__ = ["AppliedConnection", "DuckDBRunRecorder", "bind_duckdb_run", "connect_duckdb"]
