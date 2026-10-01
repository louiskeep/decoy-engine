"""`run_mask_chunked`: the production-shaped chunked masking entry point.

Masks one table chunk-by-chunk on the compiled native kernels when the whole
table admits, and on the pandas oracle otherwise, behind one contract:

- Every validation the oracle runs (`check_chunked_compatibility` and the eager
  checks of `_oracle_preflight`) runs first on every call, so a config is
  accepted or rejected identically on both routes, before any chunk is yielded.
- Values equal the oracle's value for value. Output types follow the schema rule
  in `_chunked_schema_rule` on both routes, and no chunk carries pandas metadata.
- `vault_writer`, `chunk_result_sink` and `base_row_offset` behave the same on
  both routes. The sink receives one `ExecutionResult` per chunk whose
  `quality_metrics["chunked_route"]` records each column's planned and executed
  backend (see `_chunked_evidence`).

The public oracle `run_mask_pipeline_chunked` is unchanged. Wiring callers to
this function is a separate step.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator
from typing import Any

import pyarrow as pa

from decoy_engine.execution import _chunked_dgrn as dgrn
from decoy_engine.execution import _chunked_oracle
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._chunked import _chain_first
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._chunk_masking import _mask_chunk_native, _resolve_faker_pools
from decoy_engine.execution.native._chunk_schema import _check_chunk_schema_drift
from decoy_engine.execution.native._chunked_evidence import (
    ColumnPlan,
    aggregate_chunked_route_evidence,
    chunk_route_evidence,
    plan_column_backends,
)
from decoy_engine.execution.native._chunked_schema_rule import (
    SchemaRule,
    build_schema_rule,
    normalize_chunk,
)
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    _oracle_evidence,
    plan_native_route,
)
from decoy_engine.generation.pool import PoolCache
from decoy_engine.instrumentation.timing import StrategyTimingRecord


def _validate_native_threads(native_threads: Any) -> None:
    if (
        isinstance(native_threads, bool)
        or not isinstance(native_threads, int)
        or native_threads < 1
    ):
        raise ExecutionError(
            code="invalid_native_threads",
            message=f"native_threads must be an int >= 1; got {native_threads!r}.",
        )


def _oracle_route(
    state: Any,
    *,
    config: dict[str, Any],
    table: str,
    vault_writer: Any,
    chunk_result_sink: list[Any] | None,
    base_row_offset: int,
    rule: SchemaRule,
    columns: tuple[ColumnPlan, ...],
    reroute_reason: str | None,
) -> Iterator[pa.Table]:
    produced = 0

    def on_chunk(result: Any, chunk: pa.Table) -> pa.Table:
        nonlocal produced
        out = normalize_chunk(rule, result.outputs[table], chunk, table=table, chunk_index=produced)
        produced += 1
        if chunk_result_sink is not None:
            elapsed: dict[str, float] = {}
            for rec in result.timings:
                elapsed[rec.column] = elapsed.get(rec.column, 0.0) + rec.elapsed_ms
            evidence = chunk_route_evidence(
                table=table,
                native_admitted=False,
                reroute_reason=reroute_reason,
                columns=columns,
                elapsed_ms=elapsed,
            )
            chunk_result_sink.append(
                dataclasses.replace(
                    result,
                    outputs={**result.outputs, table: out},
                    quality_metrics={**result.quality_metrics, "chunked_route": evidence},
                )
            )
        return out

    return _chunked_oracle._oracle_masked(
        state,
        config=config,
        table=table,
        vault_writer=vault_writer,
        chunk_result_sink=chunk_result_sink,
        base_row_offset=base_row_offset,
        on_chunk=on_chunk,
    )


def _native_route(
    state: Any,
    *,
    config: dict[str, Any],
    table: str,
    decision: NativeRouteEvidence,
    index_kernel: Any,
    vault_writer: Any,
    chunk_result_sink: list[Any] | None,
    base_row_offset: int,
    native_threads: int,
    rule: SchemaRule,
    columns: tuple[ColumnPlan, ...],
) -> Iterator[pa.Table]:
    from decoy_engine.keyprovider import require_mask_key

    first = state.first
    plan = state.plan
    mask_key = require_mask_key(plan, state.key_provider)
    table_seed = next((ts for (name, ts) in plan.seed_envelope.per_table if name == table), None)
    if table_seed is None:  # pragma: no cover - admission implies a seed envelope
        raise AssertionError(
            f"native route admitted {table!r} but the compiled plan has no seed envelope for it."
        )
    col_seed_by_name = dict(table_seed.per_column)
    pool_by_column = _resolve_faker_pools(
        col_seed_by_name,
        job_seed=plan.seed_envelope.job_seed,
        pool_cache=state.pool_cache,
        registry=state.registry,
    )

    def _masked() -> Iterator[pa.Table]:
        row_offset = base_row_offset
        for i, chunk in enumerate(_chain_first(first, state.chunk_iter)):
            _check_chunk_schema_drift(first.schema, chunk, table=table, chunk_index=i)
            dgrn.validate_chunk_row_offset_range(row_offset, chunk.num_rows)
            elapsed_s: dict[str, float] = {}
            masked = _mask_chunk_native(
                chunk,
                col_seed_by_name=col_seed_by_name,
                mask_key=mask_key,
                evidence=decision,
                pool_by_column=pool_by_column,
                native_threads=native_threads,
                index_kernel=index_kernel,
                column_elapsed_s=elapsed_s,
            )
            out = normalize_chunk(rule, masked, chunk, table=table, chunk_index=i)
            if chunk_result_sink is not None:
                elapsed_ms = {col: s * 1000.0 for col, s in elapsed_s.items()}
                chunk_result_sink.append(
                    ExecutionResult(
                        outputs={table: out},
                        timings=tuple(
                            StrategyTimingRecord(
                                strategy_type=col_seed_by_name[col].strategy,
                                column=col,
                                elapsed_ms=ms,
                                peak_memory_delta_kb=0,
                            )
                            for col, ms in elapsed_ms.items()
                        ),
                        boundary_conversion_ms=0.0,
                        warnings=(),
                        quality_metrics={
                            "chunked_route": chunk_route_evidence(
                                table=table,
                                native_admitted=True,
                                reroute_reason=None,
                                columns=columns,
                                elapsed_ms=elapsed_ms,
                            )
                        },
                        row_errors=(),
                    )
                )
            row_offset = dgrn.advance_row_offset(row_offset, chunk)
            if vault_writer is not None:
                from decoy_engine.vault import collect_vault_entries

                vault_writer.add(collect_vault_entries(config, {table: chunk}, {table: out}))
            yield out

    return _masked()


def run_mask_chunked(
    config: dict[str, Any],
    chunks: Iterable[pa.Table],
    *,
    table: str,
    engine_version: str,
    registry: Any = None,
    adapter: Any = None,
    vault_writer: Any = None,
    chunk_result_sink: list[Any] | None = None,
    key_provider: Any = None,
    base_row_offset: int = 0,
    native_threads: int = 1,
    route_evidence_sink: list[NativeRouteEvidence] | None = None,
    pool_cache: PoolCache | None = None,
) -> Iterator[pa.Table]:
    """Mask `table` chunk-by-chunk, natively when the whole table admits.

    Accepts everything `run_mask_pipeline_chunked` accepts, plus `native_threads`
    (an int >= 1, the thread budget of the compiled kernels; output bytes do not
    depend on it), `route_evidence_sink` (receives one `NativeRouteEvidence`) and
    `pool_cache` (one `PoolCache` shared by both routes and across calls).

    The whole table runs on the oracle when any masked column is not natively
    capable, any column carries a nonblank `when:` predicate, `adapter` is
    neither `None` nor the pandas adapter, a companion is missing, or the source
    has columns the config does not cover. `chunk_result_sink` receives one
    `ExecutionResult` per chunk on either route, with per-column `timings` and
    `quality_metrics["chunked_route"]` (see `aggregate_chunked_route_evidence`).

    Validation and route choice happen eagerly at call time; only the per-chunk
    masking is lazy. A chunk whose output cannot be made schema-stable without
    losing data raises `ExecutionError(code="chunked_schema_mismatch")`; schema
    drift on a later chunk of the native route raises
    `NativeChunkSchemaDriftError` (an `ExecutionError`).
    """
    _validate_native_threads(native_threads)
    state = _chunked_oracle._oracle_preflight(
        config,
        chunks,
        table=table,
        engine_version=engine_version,
        registry=registry,
        adapter=adapter,
        vault_writer=vault_writer,
        key_provider=key_provider,
        base_row_offset=base_row_offset,
        pool_cache=pool_cache,
    )
    if state.first is None:
        if route_evidence_sink is not None:
            route_evidence_sink.append(_oracle_evidence(table, "empty_input"))
        return iter(())

    preflight = plan_native_route(
        config,
        state.profile,
        table=table,
        engine_version=engine_version,
        first_schema=state.first.schema,
        adapter=adapter,
    )
    decision = preflight.evidence
    if route_evidence_sink is not None:
        route_evidence_sink.append(decision)
    columns = plan_column_backends(
        config, state.profile, table=table, engine_version=engine_version
    )
    rule = build_schema_rule(config, table=table, first=state.first)
    if not decision.native_admitted:
        return _oracle_route(
            state,
            config=config,
            table=table,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_result_sink,
            base_row_offset=base_row_offset,
            rule=rule,
            columns=columns,
            reroute_reason=decision.reroute_reason,
        )
    return _native_route(
        state,
        config=config,
        table=table,
        decision=decision,
        index_kernel=preflight.index_kernel,
        vault_writer=vault_writer,
        chunk_result_sink=chunk_result_sink,
        base_row_offset=base_row_offset,
        native_threads=native_threads,
        rule=rule,
        columns=columns,
    )


__all__ = ["aggregate_chunked_route_evidence", "run_mask_chunked"]
