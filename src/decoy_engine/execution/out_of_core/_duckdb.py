"""DuckDB helper for out-of-core relational operators."""

from __future__ import annotations

import os
from pathlib import Path

from decoy_engine.execution._errors import ExecutionError


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
    # annotation to that surface even though every value here is a str.
    config: dict[str, str | bool | int | float | list[str]] = {"temp_directory": str(temp_dir)}
    if memory_limit is not None:
        config["memory_limit"] = memory_limit
    return duckdb.connect(database=":memory:", config=config)


__all__ = ["connect_duckdb"]
