"""`run_mask_chunked`: the production-shaped chunked masking entry point.

Masks one table chunk-by-chunk on the compiled native kernels when the whole
table admits, and on the pandas oracle otherwise, behind one contract:

- Every validation the oracle runs (`check_chunked_compatibility` and the eager
  checks of `_oracle_preflight`) runs first on every call, so a config is
  accepted or rejected identically on both routes, before any chunk is yielded.
- Values equal the oracle's value for value, with one stated exception: on the
  stock-adapter path a carried passthrough column (one no `when:` predicate or
  sibling-reading strategy reads, see `_chunked_carry`) is the source column itself
  and never goes through pandas, so a value the oracle's round trip alters or
  refuses (a nullable integer above 2^53, `time64[ns]` that is not whole
  microseconds, out-of-range dates, `-2^63` timestamps, nested columns) comes back
  exact. Output types follow the schema rule in `_chunked_schema_rule` on both
  routes, and no chunk carries pandas metadata.
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

from decoy_engine.execution import _chunked, _chunked_oracle
from decoy_engine.execution import _chunked_dgrn as dgrn
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._chunked import _chain_first
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._guards import run_chunk_ingest_guards
from decoy_engine.execution._output_projection import (
    enforce_output_projection,
    known_output_columns,
)
from decoy_engine.execution.native._chunk_masking import (
    _mask_chunk_native,
    _resolve_faker_pools,
    pool_values_are_strings,
)
from decoy_engine.execution.native._chunk_schema import cast_null_columns, validated_rest
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
    _downgrade_to_oracle,
    _oracle_evidence,
    plan_native_route,
)
from decoy_engine.generation.pool import PoolCache, ValuePool
from decoy_engine.instrumentation.timing import StrategyTimingRecord

# Matches `MAX_NATIVE_THREADS` in decoy-engine-native/src/threads.rs: the compiled
# kernels refuse a larger request, so the entry point refuses it before any work.
MAX_NATIVE_THREADS = 1024


def _validate_native_threads(native_threads: Any) -> None:
    if (
        isinstance(native_threads, bool)
        or not isinstance(native_threads, int)
        or not 1 <= native_threads <= MAX_NATIVE_THREADS
    ):
        raise ExecutionError(
            code="invalid_native_threads",
            message=(
                f"native_threads must be an int in 1..={MAX_NATIVE_THREADS}; "
                f"got {native_threads!r}."
            ),
        )


def _oracle_route(
    state: Any,
    *,
    config: dict[str, Any],
    table: str,
    vault_writer: Any,
    chunk_result_sink: list[Any] | None,
    base_row_offset: int,
    rule: SchemaRule | None,
    columns: tuple[ColumnPlan, ...],
    reroute_reason: str | None,
    read_passthrough: tuple[str, ...],
) -> Iterator[pa.Table]:
    # Eager, like the oracle's own preflight: a provider failure surfaces now.
    _chunked._warm_faker_pools(
        state.plan, table=table, registry=state.registry, pool_cache=state.pool_cache
    )
    if rule is None:
        return _chunked_oracle._oracle_masked(
            state,
            config=config,
            table=table,
            vault_writer=vault_writer,
            chunk_result_sink=chunk_result_sink,
            base_row_offset=base_row_offset,
        )
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
                pandas_read_passthrough=read_passthrough,
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
    pool_by_column: dict[str, ValuePool],
    vault_writer: Any,
    chunk_result_sink: list[Any] | None,
    base_row_offset: int,
    native_threads: int | None,
    rule: SchemaRule | None,
    columns: tuple[ColumnPlan, ...],
    read_passthrough: tuple[str, ...],
    unconfigured: tuple[str, ...] = (),
) -> Iterator[pa.Table]:
    """The one native chunk loop. Per chunk: row-offset domain check, mask, schema rule
    (when `rule` is given), the unconfigured-column warning, result for the sink (when
    one is given), offset advance, vault entries, yield. Source drift was already refused
    by `_run_chunked`; the ingest guards run here on the chunk as the source produced it,
    and only then are null-typed columns cast. `unconfigured` names the source columns
    the plan does not cover; they are carried unchanged."""
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
    unconfigured_set = frozenset(unconfigured)

    def _guard(raw: pa.Table) -> pa.Table:
        run_chunk_ingest_guards(plan, {table: raw}, state.registry, state.graph)
        return cast_null_columns(first.schema, raw)

    def _masked() -> Iterator[pa.Table]:
        # Every chunk, the first included, is guarded here so a refusal surfaces at
        # the first `next()` exactly as it does on the oracle route.
        row_offset = base_row_offset
        guarded = (_guard(c) for c in _chain_first(first, state.chunk_iter))
        for i, chunk in enumerate(guarded):
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
                unconfigured=unconfigured_set,
            )
            out = (
                masked
                if rule is None
                else normalize_chunk(rule, masked, chunk, table=table, chunk_index=i)
            )
            # The one enforcement point: the same call the stock adapter makes, so the
            # warning (and, if a table were ever admitted under `error`, the refusal)
            # cannot drift from the oracle route's.
            warnings = tuple(
                enforce_output_projection(table, out.column_names, plan, state.projection_policy)
            )
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
                        warnings=warnings,
                        quality_metrics={
                            "chunked_route": chunk_route_evidence(
                                table=table,
                                native_admitted=True,
                                reroute_reason=None,
                                columns=columns,
                                elapsed_ms=elapsed_ms,
                                pandas_read_passthrough=read_passthrough,
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


def _resolve_admitted_pools(
    state: Any, *, table: str, decision: NativeRouteEvidence
) -> tuple[NativeRouteEvidence, dict[str, ValuePool]]:
    """Resolve every faker column's pool once, from the caller's registry, before
    the route is committed; the native loop then uses these same pools.

    A provider whose non-null output is not string-compatible cannot go through
    the string pool the native sampler gathers from, so the table reroutes to the
    oracle with a coded reason instead of failing after admission. The pools stay
    in `state.pool_cache`, so the oracle route's own warm-up is a cache hit.
    """
    plan = state.plan
    table_seed = next((ts for (name, ts) in plan.seed_envelope.per_table if name == table), None)
    if table_seed is None:  # pragma: no cover - admission implies a seed envelope
        raise AssertionError(
            f"native route admitted {table!r} but the compiled plan has no seed envelope for it."
        )
    col_seed_by_name = dict(table_seed.per_column)
    pools = _resolve_faker_pools(
        col_seed_by_name,
        job_seed=plan.seed_envelope.job_seed,
        pool_cache=state.pool_cache,
        registry=state.registry,
    )
    for column, pool in pools.items():
        if not pool_values_are_strings(pool):
            provider = col_seed_by_name[column].provider
            return (
                _downgrade_to_oracle(
                    decision, f"faker_provider_output_not_string:{column}:{provider}"
                ),
                {},
            )
    return decision, pools


def _run_chunked(
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
    native_threads: int | None = 1,
    route_evidence_sink: list[NativeRouteEvidence] | None = None,
    pool_cache: PoolCache | None = None,
    enforce_schema_rule: bool = True,
) -> Iterator[pa.Table]:
    """Preflight, route choice and the two chunk loops behind both public entry
    points. `enforce_schema_rule=False` is the legacy `run_native_or_oracle_chunked`
    contract: each route yields what it produces, with no type normalization."""
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
        warm_pools=False,
        carry_passthrough=enforce_schema_rule,
    )
    if state.first is None:
        if route_evidence_sink is not None:
            route_evidence_sink.append(_oracle_evidence(table, "empty_input"))
        return iter(())

    # One drift contract for both routes: each chunk is validated against the
    # first chunk's schema and passed on unchanged. Validation never casts, so the
    # oracle route's adapter runs its own ingest guards on the chunk exactly as the
    # source produced it (and so does the public oracle); the native route runs
    # the same guards itself, then casts null-typed columns for its kernels.
    state.chunk_iter = validated_rest(state.first, state.chunk_iter, table=table)
    preflight = plan_native_route(
        config,
        state.profile,
        table=table,
        engine_version=engine_version,
        first_schema=state.first.schema,
        adapter=adapter,
        unconfigured_policy=state.projection_policy if enforce_schema_rule else None,
    )
    decision = preflight.evidence
    if decision.native_admitted and enforce_schema_rule:
        # The native masker passes through the columns it has no plan node for, while the
        # warning (and, under `error`, the refusal) comes from `known_output_columns`. If
        # the two ever disagree, a column could pass through unmasked with no warning.
        oracle_set = set(state.first.schema.names) - known_output_columns(state.plan, table)
        if oracle_set != set(preflight.unconfigured_passthrough):
            decision = _downgrade_to_oracle(
                decision,
                f"unconfigured_set_mismatch:{sorted(oracle_set)}:"
                f"{sorted(preflight.unconfigured_passthrough)}",
            )
    pools: dict[str, ValuePool] = {}
    if decision.native_admitted and any(n.strategy == "faker" for n in decision.node_routes):
        decision, pools = _resolve_admitted_pools(state, table=table, decision=decision)
    if route_evidence_sink is not None:
        route_evidence_sink.append(decision)
    columns = (
        plan_column_backends(config, state.profile, table=table, engine_version=engine_version)
        if chunk_result_sink is not None
        else ()
    )
    rule = (
        build_schema_rule(config, table=table, first=state.first) if enforce_schema_rule else None
    )
    read_passthrough = state.carry.read if state.carry is not None else ()
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
            read_passthrough=read_passthrough,
        )
    return _native_route(
        state,
        config=config,
        table=table,
        decision=decision,
        index_kernel=preflight.index_kernel,
        pool_by_column=pools,
        vault_writer=vault_writer,
        chunk_result_sink=chunk_result_sink,
        base_row_offset=base_row_offset,
        native_threads=native_threads,
        rule=rule,
        columns=columns,
        read_passthrough=read_passthrough,
        unconfigured=preflight.unconfigured_passthrough,
    )


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
    (an int in 1..=1024, the thread budget of the compiled kernels; output bytes do not
    depend on it), `route_evidence_sink` (receives one `NativeRouteEvidence`) and
    `pool_cache` (one `PoolCache` shared by both routes and across calls).

    The whole table runs on the oracle when any masked column is not natively
    capable, any column carries a nonblank `when:` predicate, `adapter` is
    neither `None` nor the pandas adapter, a companion is missing, or, under the
    `error` unconfigured-column policy, the source has columns the config does not
    cover. Under `warn` (the pre-GA default) such columns are carried unchanged on the
    native route and each chunk's `ExecutionResult.warnings` holds the same
    `undeclared_output_columns` warning the oracle route emits. `chunk_result_sink` receives one
    `ExecutionResult` per chunk on either route, with per-column `timings` and
    `quality_metrics["chunked_route"]` (see `aggregate_chunked_route_evidence`).

    Passthrough columns: with `adapter` `None` or exactly `PandasExecutionAdapter`,
    a passthrough column that no `when:` predicate and no sibling-reading strategy
    reads is returned as the source holds it and never converted to pandas, on both
    routes. One that is read still goes through pandas, and a value pandas refuses in
    it raises `ExecutionError(code="chunked_passthrough_value_unrepresentable")` with
    the oracle's exception as its cause. With any other adapter every passthrough
    column behaves as on the public oracle, raw exceptions included.
    `quality_metrics["chunked_route"]["pandas_read_passthrough"]` lists the columns
    that still go through pandas.

    Admission uses the first chunk's real Arrow types and each Faker provider's
    output type, and both routes run the same per-chunk ingest guards, so an
    input the oracle masks or refuses is masked or refused the same way.

    Validation and route choice happen eagerly at call time; only the per-chunk
    masking is lazy. A chunk whose output cannot be made schema-stable without
    losing data raises `ExecutionError(code="chunked_schema_mismatch")`. On both
    routes, a source column whose Arrow type changes after the first chunk
    raises `NativeChunkSchemaDriftError` (an `ExecutionError`), except an
    all-null `null`-typed chunk, which the ingest guards see as the source
    produced it before it is brought to the first chunk's type. A column that is `null`-typed in the first
    chunk and typed later raises `ExecutionError(code="chunked_leading_null_type")`.
    """
    _validate_native_threads(native_threads)
    return _run_chunked(
        config,
        chunks,
        table=table,
        engine_version=engine_version,
        registry=registry,
        adapter=adapter,
        vault_writer=vault_writer,
        chunk_result_sink=chunk_result_sink,
        key_provider=key_provider,
        base_row_offset=base_row_offset,
        native_threads=native_threads,
        route_evidence_sink=route_evidence_sink,
        pool_cache=pool_cache,
    )


__all__ = ["aggregate_chunked_route_evidence", "run_mask_chunked"]
