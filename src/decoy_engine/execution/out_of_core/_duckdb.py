"""DuckDB helper for out-of-core relational operators."""

from __future__ import annotations

import os
import re
from pathlib import Path

from decoy_engine.execution._errors import ExecutionError

# `_budget.py` (`resolve_budget` / `resolve_ooc_memory_limit`) is the only
# producer of the `memory_limit` string this module consumes, and it always
# emits `f"{mib}MB"` (decimal MiB count, base-10 "MB" suffix DuckDB reads
# literally). Parsing that shape back to bytes here is exact for every
# in-repo caller; an unrecognized shape (a hand-written literal outside that
# contract) degrades to no thread override rather than guessing.
_MEMORY_LIMIT_MB_PATTERN = re.compile(r"^(\d+)MB$")

# Per-thread working-set estimate DuckDB's own threads config is sized
# against: each thread holds its own scan/build buffers on top of the shared
# buffer manager, so an unthrottled thread count (DuckDB's own default is the
# CPU count) can multiply that per-thread overhead past a small memory_limit
# before any operator even spills.
_THREAD_BYTES_PER_THREAD = 2 * 1024 * 1024 * 1024  # 2 GiB


def connect_duckdb(*, temp_dir: Path, memory_limit: str | None = None):
    """Open an in-memory DuckDB connection with a restricted temp directory."""
    try:
        import duckdb
    except ModuleNotFoundError as exc:  # pragma: no cover - covered in no-duckdb envs
        raise ExecutionError(
            code="out_of_core_backend_unavailable",
            message="DuckDB is required for the out-of-core relationship route.",
        ) from exc

    temp_dir.mkdir(parents=True, exist_ok=True)
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
    # (`_stream_join.py` and `_join.py` both `ORDER BY __decoy_row_nr`), and
    # `_relation.py`'s dedup result is consumed only as a hash-join build
    # side (`_stream_join.py`'s `parent_keys` view), never order-sensitively.
    # A future caller of `connect_duckdb` that reads unordered output would
    # violate this invariant, not this setting.
    config["preserve_insertion_order"] = False
    if memory_limit is not None:
        config["memory_limit"] = memory_limit
        threads = _threads_for_memory_limit(memory_limit)
        if threads is not None:
            config["threads"] = threads
    return duckdb.connect(database=":memory:", config=config)


def _threads_for_memory_limit(memory_limit: str | None) -> int | None:
    """DuckDB thread count sized off its own `memory_limit`, ~2 GiB/thread.

    `None` when there is no `memory_limit`, or when it is not the `"<MiB>MB"`
    shape `_budget.py` always emits, leaving DuckDB's own thread default (the
    CPU count) in place rather than mis-deriving one from a missing or
    unparseable string. The `None` guard is defensive: today `connect_duckdb`
    only calls this inside its own `memory_limit is not None` branch, but the
    guard keeps this function correct on its own terms rather than relying on
    that one caller.
    """
    if memory_limit is None:
        return None
    match = _MEMORY_LIMIT_MB_PATTERN.match(memory_limit)
    if match is None:
        return None
    memory_limit_bytes = int(match.group(1)) * 1024 * 1024
    return max(1, min(os.cpu_count() or 1, memory_limit_bytes // _THREAD_BYTES_PER_THREAD))


__all__ = ["connect_duckdb"]
