"""Auto-chunk lane executor for `run_pipeline` (Rust engine program slice B2).

`_pipeline_chunk_route.decide_chunk_route` decides WHEN a job runs chunked; this
module owns the lane that executes a job already routed there. The dispatcher
lane (default) masks the resident table in `chunk_size_rows`-row slices through
B1's `run_mask_chunked`, which picks the native or the oracle route once for the
whole table, and joins the chunks with `join_dispatcher_chunks`. The kill switch
`dispatcher_enabled=False` runs the previous lane unchanged
(`run_mask_pipeline_chunked` plus `concat_masked_chunks`).

Output contract of the dispatcher lane (plan 2026-10-01-dispatcher-auto-chunk,
guarantee 3): masked columns keep their values, string-output columns (hash,
truncate, string redact) are always `string`, passthrough columns are the source
column exactly (type, values, field nullability, field metadata), and the output
carries no schema metadata. The lane is fixed before the first chunk is masked
(no mid-run fallback), and every routed result says which lane ran, why, and
which backend masked each column (`quality_metrics["chunked_route"]`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._substrate import require_bool, require_positive_int

if TYPE_CHECKING:
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.providers_v2 import ProviderRegistry

__all__ = [
    "LANE_DISPATCHER",
    "LANE_LEGACY",
    "REASON_DISPATCHER_DISABLED",
    "join_dispatcher_chunks",
    "merge_lane_stamp",
    "require_lane_knobs",
    "run_auto_chunk",
    "select_lane",
]

_LOG = logging.getLogger(__name__)

LANE_DISPATCHER = "dispatcher"
LANE_LEGACY = "legacy_oracle"
REASON_DISPATCHER_DISABLED = "dispatcher_disabled"


def require_lane_knobs(native_threads: Any, chunked_dispatcher_enabled: Any) -> None:
    """Fail-early validation of the two `run_pipeline` lane knobs, before profiling.

    `native_threads` is an int from 1 to B1's `MAX_NATIVE_THREADS`, not a bool;
    `require_positive_int` alone accepts any larger int, so the upper bound is
    checked here against B1's constant (never a second literal)."""
    from decoy_engine.execution.native._chunked_entry import MAX_NATIVE_THREADS

    require_positive_int("native_threads", native_threads)
    if native_threads > MAX_NATIVE_THREADS:
        raise ExecutionError(
            code="invalid_execution_knob",
            message=(
                f"native_threads must be at most {MAX_NATIVE_THREADS}; got {native_threads!r}."
            ),
        )
    require_bool("chunked_dispatcher_enabled", chunked_dispatcher_enabled)


def select_lane(dispatcher_enabled: bool) -> tuple[str, str | None]:
    """`(lane, lane_reason)`, decided once per routed table before any chunk is masked.

    The kill switch is the only legacy case: B1 already refuses, per table and
    with a recorded reason, whatever the native route cannot run."""
    if dispatcher_enabled:
        return LANE_DISPATCHER, None
    return LANE_LEGACY, REASON_DISPATCHER_DISABLED


def merge_lane_stamp(
    reproducibility_stamp: dict[str, Any], routed_quality_metrics: Mapping[str, Any]
) -> dict[str, Any]:
    """The six reproducibility keys of `quality_metrics["auto_chunk"]`, then the lane's own
    keys (`lane`, `lane_reason`, `native_threads`) from the routed result, which win a tie."""
    return {**reproducibility_stamp, **routed_quality_metrics.get("auto_chunk", {})}


def join_dispatcher_chunks(chunks: list[pa.Table], *, table: str) -> pa.Table:
    """Join the dispatcher lane's chunks into one table, keeping what B1 produced.

    `concat_masked_chunks` rebuilds every field as `pa.field(name, type)`, which turns
    a non-nullable passthrough field nullable, so the dispatcher lane has its own rule:

    1. column names must be equal across chunks;
    2. per column, a field every chunk agrees on (`check_metadata=True`) is the target;
       otherwise the single non-null type the chunks agree on (`null` when every chunk
       is `null`) with the first chunk's field of that type, and two different non-null
       types raise `chunked_schema_mismatch`;
    3. chunks that differ from the target schema are cast to it, and the result is
       one contiguous table with no schema metadata.

    Precondition: `chunks` is non-empty (the resident route always yields at least one
    chunk); an empty list is a caller bug and raises `chunked_schema_mismatch` naming
    the table instead of an `IndexError`.
    """
    if not chunks:
        raise _mismatch(table, "no chunks to join")
    names = chunks[0].column_names
    for chunk in chunks[1:]:
        if chunk.column_names != names:
            raise _mismatch(
                table, f"column names differ across chunks: {names} vs {chunk.column_names}"
            )
    fields = [_target_field(chunks, index, table) for index in range(len(names))]
    schema = pa.schema(fields)
    parts = [c if c.schema.equals(schema, check_metadata=True) else c.cast(schema) for c in chunks]
    return pa.concat_tables(parts).combine_chunks().replace_schema_metadata(None)


def _mismatch(table: str, detail: str) -> ExecutionError:
    return ExecutionError(
        code="chunked_schema_mismatch",
        message=f"cannot join the chunks of table {table!r}: {detail}.",
    )


def _target_field(chunks: list[pa.Table], index: int, table: str) -> pa.Field:
    per_chunk = [c.schema.field(index) for c in chunks]
    first = per_chunk[0]
    if all(f.equals(first, check_metadata=True) for f in per_chunk):
        return first
    typed = [f for f in per_chunk if not pa.types.is_null(f.type)]
    if not typed:
        return first
    if any(f.type != typed[0].type for f in typed):
        types = sorted({str(f.type) for f in typed})
        raise _mismatch(table, f"column {first.name!r} has disagreeing types {types}")
    return typed[0]


def _slices(source: pa.Table, chunk_size_rows: int) -> Iterator[pa.Table]:
    for start in range(0, source.num_rows, chunk_size_rows):
        yield source.slice(start, chunk_size_rows)


def _without_elapsed(evidence: dict[str, Any]) -> dict[str, Any]:
    """Elapsed time lives in `ExecutionResult.timings` only, so the evidence in
    `quality_metrics` stays deterministic."""
    columns = [{k: v for k, v in col.items() if k != "elapsed_ms"} for col in evidence["columns"]]
    return {**evidence, "columns": columns}


def _legacy_route_evidence(
    config: dict[str, Any],
    source: pa.Table,
    *,
    table: str,
    engine_version: str,
    chunk_size_rows: int,
    chunk_count: int,
    lane_reason: str | None,
    registry: ProviderRegistry,
) -> dict[str, Any]:
    """The `chunked_route` payload for the legacy lane, in the dispatcher lane's shape.

    Today's lane masks every column on pandas and converts every passthrough column, so
    each configured column is reported as executed on `pandas_oracle` next to its
    planned backend, and `pandas_read_passthrough` lists every passthrough column."""
    from decoy_engine.execution._chunked_carry import passthrough_columns
    from decoy_engine.execution._chunked_profile import first_chunk_profile
    from decoy_engine.execution.native._chunked_evidence import (
        chunk_route_evidence,
        plan_column_backends,
    )

    # Profile the real first slice, as the dispatcher lane's preflight does, so a
    # type-dependent admission decision plans the same backend on both lanes.
    profile = first_chunk_profile(
        source.slice(0, chunk_size_rows), table=table, engine_version=engine_version
    )
    columns = plan_column_backends(
        config, profile, table=table, engine_version=engine_version, registry=registry
    )
    evidence = chunk_route_evidence(
        table=table,
        native_admitted=False,
        reroute_reason=lane_reason,
        columns=columns,
        elapsed_ms={},
        pandas_read_passthrough=passthrough_columns(
            config,
            table=table,
            names=source.column_names,
            registry=registry,
        ),
    )
    evidence = _without_elapsed(evidence)
    for col in evidence["columns"]:
        col["calls"] = chunk_count
    return evidence


def _run_legacy(
    config: dict[str, Any],
    source: pa.Table,
    *,
    table: str,
    engine_version: str,
    registry: ProviderRegistry,
    adapter: Any,
    vault_writer: Any,
    chunk_size_rows: int,
    key_provider: KeyProvider | None,
    chunk_results: list[ExecutionResult],
) -> pa.Table:
    from decoy_engine.execution import _chunked

    masked_chunks = list(
        _chunked.run_mask_pipeline_chunked(
            config,
            _slices(source, chunk_size_rows),
            table=table,
            engine_version=engine_version,
            registry=registry,
            adapter=adapter,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_results,
            key_provider=key_provider,
        )
    )
    return _chunked.concat_masked_chunks(masked_chunks, table=table)


def _run_dispatcher(
    config: dict[str, Any],
    source: pa.Table,
    *,
    table: str,
    engine_version: str,
    registry: ProviderRegistry,
    adapter: Any,
    vault_writer: Any,
    chunk_size_rows: int,
    key_provider: KeyProvider | None,
    native_threads: int,
    chunk_results: list[ExecutionResult],
) -> pa.Table:
    # Looked up through the module so a spy on the public entry sees the call.
    from decoy_engine.execution.native import _chunked_entry

    chunks = list(
        _chunked_entry.run_mask_chunked(
            config,
            _slices(source, chunk_size_rows),
            table=table,
            engine_version=engine_version,
            registry=registry,
            adapter=adapter,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_results,
            key_provider=key_provider,
            base_row_offset=0,
            native_threads=native_threads,
        )
    )
    return join_dispatcher_chunks(chunks, table=table)


def run_auto_chunk(
    config: dict[str, Any],
    source: pa.Table,
    *,
    table: str,
    engine_version: str,
    registry: ProviderRegistry,
    adapter: Any,
    vault_writer: Any,
    chunk_size_rows: int,
    key_provider: KeyProvider | None,
    native_threads: int,
    dispatcher_enabled: bool,
) -> tuple[dict[str, pa.Table], tuple[Any, ...], float, tuple[Any, ...], dict[str, Any]]:
    """Mask one eligible table in `chunk_size_rows`-row slices on the selected lane.

    Returns `(outputs, timings, boundary_conversion_ms, warnings, quality_metrics)`.
    Warnings are the order-stable union of per-chunk warnings, timings a per-(strategy,
    column) rollup, conversion the per-chunk sum. `quality_metrics` carries
    `code_set_corpora` (when a chunk masked a code_set column), `chunked_route` (the
    backend evidence) and a partial `auto_chunk` block (`lane`, `lane_reason`,
    `native_threads`) that `stamp_execution_metrics` merges into the six existing keys.
    Row errors are not part of the return: both lanes fail closed with
    `RowErrorsFailedError` the moment a chunk reports one. An error after the first
    chunk propagates with its own type and code; nothing retries on the other lane.
    """
    from decoy_engine.execution._chunked import aggregate_chunk_timings, aggregate_chunk_warnings
    from decoy_engine.execution._chunked_code_set import aggregate_chunk_code_set_corpora
    from decoy_engine.execution.native._chunked_evidence import aggregate_chunked_route_evidence

    lane, lane_reason = select_lane(dispatcher_enabled)
    chunk_results: list[ExecutionResult] = []
    common: dict[str, Any] = {
        "table": table,
        "engine_version": engine_version,
        "registry": registry,
        "adapter": adapter,
        "vault_writer": vault_writer,
        "chunk_size_rows": chunk_size_rows,
        "key_provider": key_provider,
        "chunk_results": chunk_results,
    }
    if lane == LANE_DISPATCHER:
        masked = _run_dispatcher(config, source, native_threads=native_threads, **common)
        evidence = _without_elapsed(aggregate_chunked_route_evidence(chunk_results))
    else:
        masked = _run_legacy(config, source, **common)
        evidence = _legacy_route_evidence(
            config,
            source,
            table=table,
            engine_version=engine_version,
            chunk_size_rows=chunk_size_rows,
            chunk_count=len(chunk_results),
            lane_reason=lane_reason,
            registry=registry,
        )
    _LOG.info(
        "auto-chunk table=%s lane=%s native_admitted=%s reroute_reason=%s",
        table,
        lane,
        evidence["native_admitted"],
        evidence["reroute_reason"],
    )
    quality_metrics: dict[str, Any] = dict(aggregate_chunk_code_set_corpora(chunk_results))
    quality_metrics["chunked_route"] = evidence
    quality_metrics["auto_chunk"] = {
        "lane": lane,
        "lane_reason": lane_reason,
        "native_threads": native_threads,
    }
    return (
        {table: masked},
        aggregate_chunk_timings(chunk_results),
        sum(r.boundary_conversion_ms for r in chunk_results),
        aggregate_chunk_warnings(chunk_results),
        quality_metrics,
    )
