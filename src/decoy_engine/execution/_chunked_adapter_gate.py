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
    `_chunked_fk.reject_lossy_chunked_fk_passthrough`'s float64-on-null risk.

    Pandas is the only masking substrate, so the chunked route always ingests
    through `PandasExecutionAdapter`'s unprotected (empty-graph) pandas round
    trip: unconditionally True. The `adapter` / `config` / `table` parameters
    are retained (callers pass them, and a future non-pandas substrate would
    restore the per-adapter branch here as fail-closed defence).
    """
    return True


__all__ = ["chunked_adapter_touches_pandas_ingestion"]
