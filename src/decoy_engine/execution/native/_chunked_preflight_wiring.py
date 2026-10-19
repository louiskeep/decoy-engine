"""Non-empty-source preflight wiring for `_run_chunked`, split out for module headroom.

Runs after the empty-source guard: rejects non-string pools, installs the per-chunk
schema-drift validator, and derives the C5c-ii admission context. A pure move out of
`_chunked_entry.py`; the route choice itself stays there."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution.native._chunk_masking import (
    reject_nonstring_deterministic_pools,
    reject_nonstring_positional_pools,
)
from decoy_engine.execution.native._chunk_schema import validated_rest
from decoy_engine.execution.native._faker_deterministic_admission import C5cIiAdmissionContext


def wire_nonempty_preflight(
    state: Any,
    *,
    config: dict[str, Any],
    table: str,
    captured_schema: pa.Schema | None,
) -> C5cIiAdmissionContext | None:
    """Reject non-string pools, wrap `state.chunk_iter` in the drift validator, and return
    the C5c-ii admission context (or None when the producer's schema is not guaranteed)."""
    reject_nonstring_positional_pools(state, table=table)
    reject_nonstring_deterministic_pools(state, config=config, table=table)
    # One drift contract for both routes: each chunk is validated against the
    # first chunk's schema and passed on unchanged. Validation never casts, so the
    # oracle route's adapter runs its own ingest guards on the chunk exactly as the
    # source produced it (and so does the public oracle); the native route runs
    # the same guards itself, then casts null-typed columns for its kernels.
    state.chunk_iter = validated_rest(state.first, state.chunk_iter, table=table)
    # The guarantee holds only if the producer's schema matches the actual first chunk
    # metadata-inclusive; a failed match cannot admit C5c-ii (never inferred from `state.first`).
    if captured_schema is not None and state.first.schema.equals(
        captured_schema, check_metadata=True
    ):
        return C5cIiAdmissionContext(source_schema=captured_schema)
    return None
