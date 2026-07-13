"""Adapter-aware gating for the chunked passthrough-FK guard (DE-10 reland).

Extracted from `_chunked_fk.py` to keep that module under the orchestration
LOC cap (`tests/sentry/test_module_size.py`); this is a single, self-contained
predicate with no state shared with the rest of `_chunked_fk.py`.
"""

from __future__ import annotations

from typing import Any


def chunked_adapter_touches_pandas_ingestion(
    adapter: Any, config: dict[str, Any], table: str
) -> bool:
    """Whether `adapter.run` for `table` will ingest through
    `PandasExecutionAdapter`'s pandas round trip -- and so is exposed to
    `_chunked_fk.reject_lossy_chunked_fk_passthrough`'s float64-on-null risk
    -- for THIS table's declared strategies (MEDIUM, DE-10 reland: the
    pandas guard was firing unconditionally, over-rejecting a native-Polars
    chunked adapter that preserves nullable `int64` losslessly and never
    touches pandas).

    `PandasExecutionAdapter` (the default / explicit `adapter=None` case):
    always True -- it always goes through `to_pandas_fk_safe`'s unprotected
    (empty-graph) ingestion on this route.

    `PolarsExecutionAdapter`: only True when it will NOT take its pure-polars
    loop for this table, i.e. some declared column strategy is not in
    `POLARS_SCALAR_HANDLERS` (mirrors `PolarsExecutionAdapter.
    _is_fully_polars_native`'s scalar-native-work check; the chunked route's
    relationship graph is always empty here, so the edges half of that check
    is vacuously satisfied). A NOT-fully-native table falls back to
    `_run_via_pandas_oracle`, which calls the exact same
    `PandasExecutionAdapter.run` -- the same unprotected ingestion -- so the
    guard must still fire there.

    Any other adapter (a future/custom substrate): True (fail closed). Its
    ingestion path is not provably lossless here, so this defaults to
    keeping the guard on rather than assuming safety.
    """
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    if isinstance(adapter, PandasExecutionAdapter):
        return True
    try:
        from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter
        from decoy_engine.execution.polars._strategies import POLARS_SCALAR_HANDLERS
    except ImportError:
        return True
    if not isinstance(adapter, PolarsExecutionAdapter):
        return True
    native = frozenset(POLARS_SCALAR_HANDLERS)
    table_strategies = {
        col.get("strategy")
        for tbl in config.get("tables") or []
        if isinstance(tbl, dict) and tbl.get("name") == table
        for col in tbl.get("columns") or []
        if isinstance(col, dict) and col.get("strategy")
    }
    return not table_strategies.issubset(native)


__all__ = ["chunked_adapter_touches_pandas_ingestion"]
