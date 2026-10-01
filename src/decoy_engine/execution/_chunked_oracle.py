"""The pandas-oracle chunked masker, split into a preflight and a masking loop.

`run_mask_pipeline_chunked` and `native._chunked_entry.run_mask_chunked` both
run `_oracle_preflight` before choosing a route, so a config the oracle rejects
is rejected identically (same error, same order, before any chunk beyond the
first is read) whichever route the call would have taken. `_oracle_masked` is
the per-chunk loop; `on_chunk` lets the new entry point normalize each chunk's
output and record evidence without a second copy of the loop.

Preflight order is part of the contract: offset domain, chunked compatibility,
first-chunk pull, profile, compile, key resolution, vault key guard, registry
default, corpus pinning, bucket namespace, then the zero-chunk return. Checks
that need a first chunk run after it, and per-chunk checks stay in the loop.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from . import _chunked as _chunked_mod
from . import _chunked_bucket_perturb as bucket_perturb_gate
from . import _chunked_code_set as code_set_gate
from . import _chunked_dgrn as dgrn
from . import _chunked_group_key as group_key
from . import _chunked_text_mask as text_mask_gate
from ._chunked_adapter_gate import chunked_adapter_touches_pandas_ingestion
from ._chunked_fk import (
    fk_hash_strategy_columns_for_table,
    fk_passthrough_columns_for_table,
    reject_lossy_chunked_fk_passthrough,
)
from ._chunked_fk_dtype import (
    fk_declared_dtypes_for_table,
    reject_mismatched_chunked_fk_declared_dtype,
)


@dataclass
class OraclePreflightState:
    """Everything the masking loop needs, resolved once before any chunk runs.

    `first` is None for a zero-chunk call that cleared every gate; the callers
    then return an empty iterator. The non-empty-only fields are unset in that
    case.
    """

    first: pa.Table | None
    chunk_iter: Iterator[pa.Table]
    profile: Any
    plan: Any
    registry: Any
    key_provider: Any
    code_set_records: Any = None
    graph: Any = None
    adapter: Any = None
    pool_cache: Any = None
    projection_policy: Any = None
    ns_registry: Any = None
    text_mask_cols: Any = None
    code_set_cols: Any = None
    bucket_perturb_cols: Any = None
    guard_passthrough_fk_columns: Any = None
    declared_fk_dtypes: Any = None
    hash_fk_key_columns: Any = None


def _oracle_preflight(
    config: dict[str, Any],
    chunks: Iterable[pa.Table],
    *,
    table: str,
    engine_version: str,
    registry: Any = None,
    adapter: Any = None,
    vault_writer: Any = None,
    key_provider: Any = None,
    base_row_offset: int = 0,
    pool_cache: Any = None,
    warm_pools: bool = True,
) -> OraclePreflightState:
    """The eager half of the oracle: every check that runs before a chunk masks.

    `warm_pools=False` leaves faker pool warming to the caller: the native route
    builds its pools itself, and warming here too would look the cache up twice.
    """
    dgrn.validate_base_row_offset(base_row_offset)
    from decoy_engine.execution._chunked_profile import empty_input_profile, first_chunk_profile
    from decoy_engine.execution._output_projection import resolve_unconfigured_column_policy
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.generation.pool import PoolCache
    from decoy_engine.plan import compile_plan
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

    _chunked_mod.check_chunked_compatibility(config, table=table)
    chunk_iter = iter(chunks)
    first = next(chunk_iter, None)
    # A keyed job with zero rows and a missing or invalid mask secret must still
    # fail the fail-closed gate, so the profile/plan/gate sequence runs for an
    # empty source too (from `empty_input_profile`) and the empty-input return
    # comes after the gate.
    if first is None:
        profile = empty_input_profile(config, table=table, engine_version=engine_version)
    else:
        profile = first_chunk_profile(first, table=table, engine_version=engine_version)
    plan = compile_plan(config, profile, decoy_engine_version=engine_version, no_profile=True)
    # Public entry point: resolve the config's `mask_secret_ref` when no
    # programmatic provider was passed, then run the fail-closed gate up front.
    # The per-chunk adapter.run() re-gates via require_mask_key, so a keyed
    # chunked job cannot run off job_seed at GA.
    if key_provider is None:
        _ref = (config.get("global_settings") or {}).get("mask_secret_ref")
        if _ref:
            from decoy_engine.keyprovider import key_provider_from_ref

            key_provider = key_provider_from_ref(_ref)
    from decoy_engine.keyprovider import require_mask_key

    _resolved_mask_key = require_mask_key(plan, key_provider)
    # The vault holds reversible plaintext PII and cannot be written under a key
    # that differs from the resolved mask key, so this entry runs the same
    # vault-key guard as run_pipeline.
    if vault_writer is not None:
        from decoy_engine.vault import assert_vault_writer_keyed

        assert_vault_writer_keyed(vault_writer, _resolved_mask_key)
    resolved_registry = registry if registry is not None else get_default_registry()
    graph = RelationshipGraph(edges=(), ordering=())
    # Corpus pinning is resolved before the empty-input return, so a zero-row job
    # with an invalid or version-mismatched corpus fails closed like the oracle
    # instead of succeeding with no code_set column ever dispatched.
    code_set_records = code_set_gate.resolve_pinned_code_set_records(
        plan, resolved_registry, graph, table=table
    )
    # bucket_perturb's namespace requirement is data-independent (the handler
    # raises before touching data), so it is validated before the empty-input
    # return as well.
    bucket_perturb_gate.reject_bucket_perturb_missing_namespace(
        plan, resolved_registry, graph, table=table
    )
    if first is None:
        return OraclePreflightState(
            first=None,
            chunk_iter=chunk_iter,
            profile=profile,
            plan=plan,
            registry=resolved_registry,
            key_provider=key_provider,
            code_set_records=code_set_records,
            graph=graph,
        )
    # Resolve the projection policy once; each per-chunk adapter.run() enforces
    # it (a chunk carries the same column set as the whole table). Single mask
    # table, no generate echo on this route, so no table is exempted.
    projection_policy = resolve_unconfigured_column_policy(config)
    ns_registry = build_namespace_registry(config, profile)
    # group_by effective-type guard: needs the plan and source schema, absent at
    # the config-only compatibility check; runs once, pre-stream.
    group_key.reject_unsafe_group_key_group_by_dtype(
        plan, first.schema, table=table, registry=resolved_registry, relationship_graph=graph
    )
    # text_mask, code_set and bucket_perturb each need a chunk-stable string
    # source (a non-string source widens by chunk boundary under the handlers'
    # str() conversion or date parsing). The iterable's dtype can drift across
    # chunks, so the columns are resolved once, the first chunk is validated
    # here, and every later chunk is validated in the masking loop.
    text_mask_cols = text_mask_gate.text_mask_source_columns(
        plan, resolved_registry, graph, table=table
    )
    text_mask_gate.reject_unsafe_text_mask_chunk_schema(first.schema, text_mask_cols, table=table)
    code_set_cols = code_set_gate.code_set_source_columns(
        plan, resolved_registry, graph, table=table
    )
    code_set_gate.reject_unsafe_code_set_chunk_schema(first.schema, code_set_cols, table=table)
    bucket_perturb_cols = bucket_perturb_gate.bucket_perturb_source_columns(
        plan, resolved_registry, graph, table=table
    )
    bucket_perturb_gate.reject_unsafe_bucket_perturb_chunk_schema(
        first.schema, bucket_perturb_cols, table=table
    )
    passthrough_fk_columns = fk_passthrough_columns_for_table(config, table)
    # The compile-time FK gate trusts the operator-declared FK key dtype (it never
    # sees the data). The per-chunk guard validates those declarations against the
    # real Arrow dtype and fails closed on a misdeclaration, which would else
    # silently void RI. Substrate-independent, so it is not adapter-gated.
    declared_fk_dtypes = fk_declared_dtypes_for_table(config, table)
    # Predicate 12's real stage is scoped to hash-strategy FK columns, not every
    # chunk-safe strategy the family guard covers.
    hash_fk_key_columns = fk_hash_strategy_columns_for_table(config, table)
    if adapter is None:
        adapter = PandasExecutionAdapter()
    # The guard only applies when the adapter ingests `table` through the pandas
    # round trip it protects against; the seam stays for a future non-pandas
    # substrate (see `chunked_adapter_touches_pandas_ingestion`).
    guard_passthrough_fk_columns = (
        passthrough_fk_columns
        if chunked_adapter_touches_pandas_ingestion(adapter, config, table)
        else set()
    )
    # One cache for the whole run: faker pools build once (eagerly, so a provider
    # failure surfaces before any output streams) and every chunk samples from
    # the same pool via the handler's cache consult. A caller-supplied cache is
    # shared across calls and across routes.
    if pool_cache is None:
        pool_cache = PoolCache()
    if warm_pools:
        _chunked_mod._warm_faker_pools(
            plan, table=table, registry=resolved_registry, pool_cache=pool_cache
        )
    return OraclePreflightState(
        first=first,
        chunk_iter=chunk_iter,
        profile=profile,
        plan=plan,
        registry=resolved_registry,
        key_provider=key_provider,
        code_set_records=code_set_records,
        graph=graph,
        adapter=adapter,
        pool_cache=pool_cache,
        projection_policy=projection_policy,
        ns_registry=ns_registry,
        text_mask_cols=text_mask_cols,
        code_set_cols=code_set_cols,
        bucket_perturb_cols=bucket_perturb_cols,
        guard_passthrough_fk_columns=guard_passthrough_fk_columns,
        declared_fk_dtypes=declared_fk_dtypes,
        hash_fk_key_columns=hash_fk_key_columns,
    )


def _oracle_masked(
    state: OraclePreflightState,
    *,
    config: dict[str, Any],
    table: str,
    vault_writer: Any = None,
    chunk_result_sink: list[Any] | None = None,
    base_row_offset: int = 0,
    on_chunk: Callable[[Any, pa.Table], pa.Table] | None = None,
    ingest_guarded: bool = False,
) -> Iterator[pa.Table]:
    """The lazy per-chunk loop over a non-empty preflight state.

    Per chunk, in this order: adapter run; the raw result goes to
    `chunk_result_sink` (always when `on_chunk` is None, and for a failing chunk
    even when it is set, so a row-error chunk is reported unnormalized); fail
    closed on row errors; `on_chunk(result, source_chunk)` returns the table to
    yield and owns any enriched result for the sink; advance the row offset; add
    vault entries; yield. `ingest_guarded` says the caller already ran the ingest
    guards on each chunk as the source produced it, so the adapter skips them.
    """
    from contextlib import nullcontext

    from decoy_engine.errors import RowErrorsFailedError
    from decoy_engine.execution._guards import ingest_guards_already_run

    first = state.first
    if first is None:  # pragma: no cover - callers return early for a zero-chunk state
        raise AssertionError("_oracle_masked called with a zero-chunk preflight state")

    def _masked() -> Iterator[pa.Table]:
        row_offset = base_row_offset  # DGRN counter; inert for value-keyed strategies.
        for chunk in _chunked_mod._chain_first(first, state.chunk_iter):
            if state.guard_passthrough_fk_columns:
                reject_lossy_chunked_fk_passthrough(
                    chunk, table=table, passthrough_fk_columns=state.guard_passthrough_fk_columns
                )
            # `hash_fk_key_columns` triggers the guard independently of
            # `declared_fk_dtypes`: `dtype` is optional in config, so a hash FK key
            # with no declared dtype leaves `declared_fk_dtypes` empty yet still
            # needs predicate 12's real-type check (else an unsafe real
            # date64/decimal256 reaches the kernel unchecked).
            if state.declared_fk_dtypes or state.hash_fk_key_columns:
                reject_mismatched_chunked_fk_declared_dtype(
                    chunk,
                    table=table,
                    declared_fk_dtypes=state.declared_fk_dtypes,
                    hash_fk_key_columns=state.hash_fk_key_columns,
                )
            # These source columns must stay a chunk-stable string on every chunk,
            # not just the first (the iterable's dtype can drift).
            if state.text_mask_cols:
                text_mask_gate.reject_unsafe_text_mask_chunk_schema(
                    chunk.schema, state.text_mask_cols, table=table
                )
            if state.code_set_cols:
                code_set_gate.reject_unsafe_code_set_chunk_schema(
                    chunk.schema, state.code_set_cols, table=table
                )
            if state.bucket_perturb_cols:
                bucket_perturb_gate.reject_unsafe_bucket_perturb_chunk_schema(
                    chunk.schema, state.bucket_perturb_cols, table=table
                )
            # Per-chunk DGRN domain guard (no whole-stream row count); see `_chunked_dgrn.py`.
            dgrn.validate_chunk_row_offset_range(row_offset, chunk.num_rows)
            with ingest_guards_already_run() if ingest_guarded else nullcontext():
                result = state.adapter.run(
                    state.plan,
                    {table: chunk},
                    registry=state.registry,
                    pool_cache=state.pool_cache,
                    relationship_graph=state.graph,
                    namespace_registry=state.ns_registry,
                    unconfigured_column_policy=state.projection_policy,
                    key_provider=state.key_provider,
                    row_offset=row_offset,
                    code_set_records=state.code_set_records,
                )
            if chunk_result_sink is not None and (on_chunk is None or result.row_errors):
                chunk_result_sink.append(result)
            # The chunked path has no quarantine machinery, so a per-row strategy
            # error (bucketize/date_shift format_error, code_set mask_error)
            # cannot be routed anywhere. Discarding it would silently keep the raw
            # source value in the streamed output, the leak the full-frame path
            # closes. Fail closed the moment any chunk reports a row error; this
            # applies to every caller, including a routed job in run_pipeline.
            if result.row_errors:
                raise RowErrorsFailedError(result.row_errors)
            masked = result.outputs[table] if on_chunk is None else on_chunk(result, chunk)
            row_offset = dgrn.advance_row_offset(row_offset, chunk)
            if vault_writer is not None:
                from decoy_engine.vault import collect_vault_entries

                vault_writer.add(collect_vault_entries(config, {table: chunk}, {table: masked}))
            yield masked

    return _masked()
