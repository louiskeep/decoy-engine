"""Chunked mask execution (capability-gaps WS4, 2026-06-12).

`run_mask_pipeline_chunked` masks ONE table chunk-by-chunk so inputs too
large for memory stream through the engine. The contract is byte parity:
for any chunking of the rows, concatenated output equals the full-frame
`run_pipeline` output exactly.

That contract is only honest for VALUE-KEYED strategies -- where each
output cell is a pure function of (input cell, config, job seed), never
of row position, neighboring rows, or whole-column state. The v1 safe
set is exactly those:

| strategy     | why chunk-safe |
|--------------|----------------|
| hash         | HMAC of the value |
| fpe          | keyed Feistel permutation of the value |
| redact       | constant |
| truncate     | prefix of the value |
| text_redact  | span replacement within the cell |
| date_shift   | offset derived from the value (derive(seed, ns, value)) |
| bucketize    | bin of the value |
| passthrough  | identity |

Two of those rows carry whole-column caveats the per-value story hides:
date_shift's offset is value-keyed, but its date FORMAT is detected from
whole-column samples unless `provider_config.date_format` is explicit,
so byte parity holds only with an explicit format; bucketize falls
through to the original value on null / non-numeric positions, so its
output dtype is chunk-content-dependent unless the source column is
null-free numeric. The auto-chunk planner gates enforce both before
routing (`_planner._whole_column_state_rejections` and the bucketize
runtime source gate); callers of this entrypoint own that judgment.

CONDITIONALLY admitted (deferred follow-up 2, 2026-06-12): faker and
categorical, exactly when their deterministic value-keyed path is the
one that runs and every whole-run input is declared in config rather
than derived from the data:

- faker: `deterministic: true` + `namespace` + an explicit `pool_size`
  (top-level `ColumnConfig.pool_size` or `provider_config.pool_size` --
  DE-11 resolves whichever one is set, rejecting the config at compile
  if both disagree) + `cardinality_mode` absent or `reuse`. The
  deterministic sampler maps each value via
  `derive_index(job_seed, namespace, canonicalize(value), pool_size)`,
  independent of row position or chunk arrival, and the pool build is
  RNG-seeded by its identity, so a pre-built pool equals any rebuild.
  A chunk with more distinct values than `pool_size` changes nothing:
  derive_index maps any value into [0, pool_size) with collisions
  allowed, byte-identical to the full-frame run of the same rows
  (pool_size controls collision rate, not admission).
- categorical: `deterministic: true` + `namespace` + explicit
  `provider_config.categories`, and NOT `from_profile` (profile-derived
  categories would come from the first chunk only).

Rejected at compile time (`check_chunked_compatibility`):

- shuffle (whole-column permutation), composite/nested (bundle state):
  `strategy_not_chunk_safe`;
- faker / categorical with the conditions above unmet:
  `chunked_strategy_conditions_unmet`, naming each unmet condition;
- FK child edges that fail the self-mask gate (see below);
- generate tables (generation is not masking; row_count is whole-run).

FK child self-masking (SC1 port, Option 1, docs/relationships-memory-scaling.md
§2):

An FK edge where the current table is the child is ADMITTED for chunked
execution -- the child self-masks via its own value-keyed strategy under the
shared namespace rather than resolving by parent-map lookup -- ONLY WHEN all
four gate conditions hold (`_chunked_fk.gate_fk_child_edges`). Any failure
raises PlanCompileError (fail closed):

  (a) Parent key strategy is in CHUNK_SAFE_STRATEGIES.
  (b) Child FK column explicitly declares the same value-keyed strategy
      (no 'by-reference' model; the child must own its masking).
  (c) Child FK column explicitly declares the same namespace as the parent
      COLUMN, for namespace-requiring strategies only (hash, fpe,
      date_shift). The parent-column namespace is the authoritative masking
      key: ColumnSeed.namespace = col_entry.get("namespace")
      (_seed_envelope.py:260); the FK namespace auto-binding
      (_namespace.py:225-253) updates the NamespaceRegistry but not the
      ColumnSeed. A config whose edge (relationship) namespace disagrees
      with the parent-column namespace is rejected with
      chunked_fk_parent_namespace_mismatch; a parent column with no
      namespace is rejected with chunked_fk_parent_namespace_missing.
  (d) orphan_policy is 'remap'. REMAP mints orphan values via
      parent_strategy(seed, ns, orphan_key), which is identical to self-
      masking the orphan key under the same strategy and namespace.
      WARN/FAIL/PRESERVE all require the parent key set resident and are
      rejected with chunked_fk_orphan_policy_not_remap.

First cut: single-column FK edges only (composite FK rejected with
chunked_fk_composite_unsupported). Tables that are FK parents but not
children are unaffected by this gate.

Each chunk runs through the SAME compiled plan, one execution adapter
(pandas by default; polars via the `adapter` parameter), and one shared
pre-warmed pool cache, so chunked output is byte-identical to a serial
run on the same substrate by construction rather than by
re-implementation. Cross-substrate parity is value-level, per the v2
rows in tests/parity/SEMANTIC_DIFFERENCES.md.

`passthrough` FK-column ingestion gap (MEDIUM #4, DE-10 rework follow-up,
2026-07-13): `run()`'s ingestion protects a column via
`_fk_keys.to_pandas_fk_safe`/`fk_columns_for_table`, which reads the RUNTIME
`RelationshipGraph` -- but this route always passes an EMPTY graph (`graph`
below) by design (self-masking has no parent-map join), so that protection
covers NOTHING here. Every other admitted chunk-safe strategy re-derives its
output rather than preserving it, so this is silent only for `passthrough`
(identity). Guarded by `_chunked_fk.fk_passthrough_columns_for_table` +
`_chunked_fk.reject_lossy_chunked_fk_passthrough` (extracted there to keep
this module under the LOC cap) -- see that module for the full write-up.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

import pyarrow as pa

from decoy_engine.plan._errors import PlanCompileError

from ._chunked_adapter_gate import chunked_adapter_touches_pandas_ingestion
from ._chunked_fk import (
    CHUNK_SAFE_STRATEGIES,
    fk_passthrough_columns_for_table,
    gate_fk_child_edges,
    reject_lossy_chunked_fk_passthrough,
)

# Admitted only when the column's config pins the deterministic
# value-keyed path (see module docstring for the per-strategy rules).
CHUNK_CONDITIONAL_STRATEGIES: frozenset[str] = frozenset({"faker", "categorical"})


def _conditional_admission_failures(col_entry: dict[str, Any]) -> list[str]:
    """Unmet chunked-admission conditions for a faker/categorical column.

    Returns one human-readable string per unmet condition; an empty
    list means the column is admitted.
    """
    strategy = col_entry.get("strategy")
    cfg = col_entry.get("provider_config") or {}
    failures: list[str] = []
    if not col_entry.get("deterministic"):
        failures.append(
            "requires deterministic: true (the non-deterministic path draws "
            "per-row randomness, which is chunk-variant)"
        )
    if not col_entry.get("namespace"):
        failures.append("requires a namespace (the value-keyed mapping derives from it)")
    if strategy == "faker":
        # DE-11: legal at either location; the compile resolver rejects a
        # disagreeing pair, so this only checks at least one is present.
        if col_entry.get("pool_size") is None and cfg.get("pool_size") is None:
            failures.append(
                "requires an explicit pool_size (top-level or "
                "provider_config.pool_size) as the chunked capacity "
                "declaration (the non-chunked default of 10000 is not "
                "applied silently here)"
            )
        if col_entry.get("cardinality_mode") not in (None, "reuse"):
            failures.append(
                "requires cardinality_mode absent or 'reuse' (source-cardinality "
                "modes describe whole-run state)"
            )
    if strategy == "categorical":
        if cfg.get("from_profile"):
            failures.append(
                "from_profile derives categories from the profile, which chunked "
                "mode builds from the first chunk only; declare categories "
                "explicitly"
            )
        elif not cfg.get("categories"):
            failures.append("requires explicit provider_config.categories")
    return failures


def check_chunked_compatibility(config: dict[str, Any], *, table: str) -> None:
    """Reject configs whose chunked run could not match a full-frame run.

    Raises PlanCompileError with codes:
        chunked_table_unknown: `table` is not in config.tables.
        chunked_generate_unsupported: `table` is a generate-kind table.
        chunked_fk_orphan_policy_not_remap: FK child edge with non-REMAP policy.
        chunked_fk_composite_unsupported: FK child edge with a composite key.
        chunked_fk_parent_strategy_not_safe: parent key strategy not chunk-safe.
        chunked_fk_child_namespace_missing: child column has no explicit namespace.
        chunked_fk_child_namespace_mismatch: child namespace != parent namespace.
        chunked_fk_child_strategy_missing: child column has no explicit strategy.
        chunked_fk_child_strategy_mismatch: child strategy != parent strategy.
        strategy_not_chunk_safe: a non-FK column uses a non-chunk-safe strategy.
        chunked_strategy_conditions_unmet: faker/categorical admission conditions
            are not met; message names each unmet condition.
    """
    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        known = sorted(t.get("name", "?") for t in tables if isinstance(t, dict))
        raise PlanCompileError(
            code="chunked_table_unknown",
            path=f"tables.{table}",
            message=f"table {table!r} is not in the config (tables: {known}).",
        )
    if table_cfg.get("generate_columns"):
        raise PlanCompileError(
            code="chunked_generate_unsupported",
            path=f"tables.{table}",
            message=(
                f"table {table!r} is a generate table; chunked execution masks "
                "existing data and has no generation mode (row_count is whole-run "
                "state)."
            ),
        )
    # Gate FK edges where `table` is the child. Admitted edges self-mask via the
    # child's own value-keyed strategy under the shared namespace. Rejected edges
    # raise PlanCompileError (fail closed). Edges where `table` is a parent only
    # are not constrained here; the parent chunks normally.
    if config.get("relationships"):
        gate_fk_child_edges(config, table=table)
    offending: list[tuple[str, str]] = []
    conditions_unmet: list[tuple[str, str, list[str]]] = []
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict):
            continue
        strategy = col_entry.get("strategy")
        if strategy is None or strategy in CHUNK_SAFE_STRATEGIES:
            continue
        if strategy in CHUNK_CONDITIONAL_STRATEGIES:
            failures = _conditional_admission_failures(col_entry)
            if failures:
                conditions_unmet.append((str(col_entry.get("name", "?")), str(strategy), failures))
            continue
        offending.append((str(col_entry.get("name", "?")), str(strategy)))
    if offending:
        details = ", ".join(f"{name} ({strategy})" for name, strategy in offending)
        raise PlanCompileError(
            code="strategy_not_chunk_safe",
            path=f"tables.{table}.columns",
            message=(
                f"column(s) {details} use strategies that are not value-keyed and "
                f"cannot produce chunk-invariant output. Chunk-safe strategies: "
                f"{', '.join(sorted(CHUNK_SAFE_STRATEGIES))}."
            ),
        )
    if conditions_unmet:
        details = "; ".join(
            f"{name} ({strategy}: {'; '.join(failures)})"
            for name, strategy, failures in conditions_unmet
        )
        raise PlanCompileError(
            code="chunked_strategy_conditions_unmet",
            path=f"tables.{table}.columns",
            message=(
                f"column(s) {details}. faker/categorical run chunked only on "
                "their deterministic value-keyed path with all whole-run inputs "
                "declared in config (see run_mask_pipeline_chunked docs)."
            ),
        )


def _first_chunk_profile(first_chunk: pa.Table, *, table: str, engine_version: str) -> Any:
    """Profile the FIRST chunk so compile_plan can build the seed envelope.

    The envelope iterates `profile.tables` (the table must exist there
    for its columns to mask at all), so a fully-empty --no-profile-style
    Profile silently masks nothing. The first chunk gives real dtypes;
    distinct counts and row_count describe only that chunk, which is
    fine -- admitted strategies consume nothing distribution-dependent.
    Faker pools size from the config-declared pool_size (the admission
    rule requires it explicitly), never from profile distinct counts,
    and the pool-capacity pre-flight lands in checks_skipped under
    no_profile=True, which is correct here: with admission restricted
    to deterministic REUSE, pool capacity is a collision-rate knob, not
    a correctness input. Epoch `profiled_at` keeps the 'not a real
    source profile' sentinel from the --no-profile path."""
    import random

    from decoy_engine.profile import Profile
    from decoy_engine.profile._walk import walk_dataframe

    table_profile = walk_dataframe(
        first_chunk.to_pandas(),
        table_name=table,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )
    return Profile(
        schema_version=1,
        tables=(table_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=engine_version,
        profile_seed=None,
    )


def run_mask_pipeline_chunked(
    config: dict[str, Any],
    chunks: Iterable[pa.Table],
    *,
    table: str,
    engine_version: str,
    registry: Any = None,
    adapter: Any = None,
    vault_writer: Any = None,
    chunk_result_sink: list[Any] | None = None,
) -> Iterator[pa.Table]:
    """Mask `table`'s rows chunk-by-chunk under `config`.

    `config` is the validated pipeline config dump; `chunks` yields
    pyarrow Tables of the table's rows in order. Returns an iterator of
    masked chunks in the same order; concatenating them is byte-identical
    to a full-frame `run_pipeline` of the same rows (the value-keyed
    contract, enforced by `check_chunked_compatibility` up front).

    `adapter` selects the execution substrate; None keeps the pandas
    adapter (the byte-stable default this mode shipped with). Pass a
    `PolarsExecutionAdapter` (e.g. via `select_execution_adapter`) for
    polars-substrate streaming; cross-substrate output is VALUE-equal,
    not Arrow-schema-equal (string widens to large_string etc.; the
    recorded v2 rows in tests/parity/SEMANTIC_DIFFERENCES.md).

    `vault_writer` (a `decoy_engine.vault.VaultWriter`) collects each
    chunk's source->masked pairs for `vault: true` columns as the chunk
    masks; the caller writes the artifact after the stream drains.

    `chunk_result_sink` (optional list) receives each chunk's full
    `ExecutionResult` as it is produced, so callers that need the
    non-output surface (per-chunk warnings, timings, boundary conversion
    figures) can aggregate it without this iterator changing its yielded
    type. The auto-chunk route in `run_pipeline` uses it to keep
    warnings and timings from being dropped on routed jobs.

    Validation and plan compile happen EAGERLY at call time; only the
    per-chunk masking is lazy.
    """
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.generation.pool import PoolCache
    from decoy_engine.plan import compile_plan
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

    check_chunked_compatibility(config, table=table)
    chunk_iter = iter(chunks)
    first = next(chunk_iter, None)
    if first is None:
        return iter(())
    profile = _first_chunk_profile(first, table=table, engine_version=engine_version)
    plan = compile_plan(config, profile, decoy_engine_version=engine_version, no_profile=True)
    resolved_registry = registry if registry is not None else get_default_registry()
    ns_registry = build_namespace_registry(config, profile)
    graph = RelationshipGraph(edges=(), ordering=())
    passthrough_fk_columns = fk_passthrough_columns_for_table(config, table)
    if adapter is None:
        adapter = PandasExecutionAdapter()
    # MEDIUM (DE-10 reland): only pay the guard's cost -- and only reject --
    # when THIS adapter will actually ingest `table` through the pandas
    # round trip the guard protects against. A native-Polars chunked run
    # (see `chunked_adapter_touches_pandas_ingestion`) preserves nullable
    # int64 losslessly and never touches pandas, so applying the guard
    # there is a false-positive fail-closed reject, not a real corruption
    # risk.
    guard_passthrough_fk_columns = (
        passthrough_fk_columns
        if chunked_adapter_touches_pandas_ingestion(adapter, config, table)
        else set()
    )
    # One cache for the whole run: faker pools build ONCE (eagerly, so a
    # provider failure surfaces before any output streams) and every
    # chunk samples from the same pool via the handler's cache consult.
    pool_cache = PoolCache()
    _warm_faker_pools(
        plan,
        table=table,
        registry=resolved_registry,
        pool_cache=pool_cache,
    )

    def _masked() -> Iterator[pa.Table]:
        for chunk in _chain_first(first, chunk_iter):
            if guard_passthrough_fk_columns:
                reject_lossy_chunked_fk_passthrough(
                    chunk, table=table, passthrough_fk_columns=guard_passthrough_fk_columns
                )
            result = adapter.run(
                plan,
                {table: chunk},
                registry=resolved_registry,
                pool_cache=pool_cache,
                relationship_graph=graph,
                namespace_registry=ns_registry,
            )
            if chunk_result_sink is not None:
                chunk_result_sink.append(result)
            # Sprint 2 honesty pack H1 (dennis review 2026-07-04): the
            # chunked/streaming path has no quarantine machinery, so a
            # per-row strategy error (bucketize/date_shift format_error,
            # code_set mask_error -- though code_set is not chunk-admitted)
            # cannot be routed anywhere. Discarding it would silently keep
            # the raw source value in the streamed output (the exact leak the
            # full-frame path closes). Fail CLOSED: raise the moment any chunk
            # reports a row error. This is correct and cheap here; opt-in
            # quarantine is a full-frame-only feature. Applies identically to
            # BOTH callers of this generator (the manual streaming entrypoint
            # and the S3 auto-chunk route in run_pipeline via
            # chunk_result_sink): a routed job is never eligible for
            # row-error quarantine, matching the pre-existing manual-path
            # policy -- `run_pipeline`'s `mask_row_errors` therefore treats
            # any auto-chunked job that reaches that point as error-free by
            # construction (this raise already fired otherwise).
            if result.row_errors:
                from decoy_engine.errors import RowErrorsFailedError

                raise RowErrorsFailedError(result.row_errors)
            masked = result.outputs[table]
            if vault_writer is not None:
                from decoy_engine.vault import collect_vault_entries

                vault_writer.add(collect_vault_entries(config, {table: chunk}, {table: masked}))
            yield masked

    return _masked()


def _warm_faker_pools(
    plan: Any,
    *,
    table: str,
    registry: Any,
    pool_cache: Any,
) -> None:
    """Build each admitted faker column's pool once into `pool_cache`.

    The build parameters mirror FakerStrategyHandler exactly (same
    pool_size/locale/config split), so the handler's identity_for
    lookup hits this cache on every chunk. Caching is byte-safe: the
    build is RNG-seeded by the identity's pool_seed (S5 F2), so any
    rebuild of the same identity is value-identical.

    DE-11: pool_size comes off the COMPILED `ColumnSeed` (already resolved
    by `_seed_envelope.py`), not re-parsed from the raw config dict -- a
    second read site is exactly the bug DE-11 fixes. Admission guarantees
    it is set for every chunk-admitted faker column.
    """
    from decoy_engine.execution._adapter import provider_config_to_dict
    from decoy_engine.generation.pool import PoolBuilder

    table_seed = next((ts for (name, ts) in plan.seed_envelope.per_table if name == table), None)
    if table_seed is None:
        return
    builder = PoolBuilder(registry)
    for col_name, col_seed in table_seed.per_column:
        if col_seed.strategy != "faker" or col_seed.provider is None:
            continue
        if col_seed.pool_size is None:
            # Admission should have rejected this config already.
            raise ValueError(
                f"chunked faker column {table}.{col_name} reached pool pre-warm "
                "with no resolved pool_size; admission should have rejected it."
            )
        pool_size = col_seed.pool_size
        cfg = provider_config_to_dict(col_seed.provider_config)
        locale = cfg.get("locale")
        build_config = {k: v for k, v in cfg.items() if k not in ("pool_size", "locale")}
        identity = builder.identity_for(
            col_seed.provider,
            size=pool_size,
            job_seed=plan.seed_envelope.job_seed,
            locale=locale,
            config=build_config,
            namespace=col_seed.namespace,
        )
        if pool_cache.get(identity) is not None:
            continue
        pool_cache.put(
            builder.build(
                provider=col_seed.provider,
                size=pool_size,
                job_seed=plan.seed_envelope.job_seed,
                locale=locale,
                config=build_config,
                namespace=col_seed.namespace,
            )
        )


def concat_masked_chunks(chunks: list[pa.Table], *, table: str) -> pa.Table:
    """Concatenate masked chunks under the byte-identity contract: equal
    schemas, no type promotion.

    Chunk-identical strategies produce the same column types on every
    chunk, so a schema disagreement here means an eligibility gate
    admitted a chunk-variant job; that must surface loudly (a coded
    error) rather than be papered over by `promote_options="default"`.

    ONE promotion is performed, precisely because full-frame inference
    performs it too: a chunk whose column is entirely null converts back
    from pandas as Arrow `null` type, while the full frame -- which
    contains the other chunks' non-null values -- infers their real
    type. Casting the null-typed column to the single type the non-null
    chunks agree on is lossless and lands exactly where whole-frame
    inference does; when every chunk is null the column stays `null`,
    again matching the full frame.

    Raises:
        ExecutionError: ``code='chunked_schema_mismatch'`` when chunk
            column names differ or two chunks hold different non-null
            types for the same column.
    """
    from decoy_engine.execution._errors import ExecutionError

    names = chunks[0].schema.names
    for chunk in chunks[1:]:
        if chunk.schema.names != names:
            raise ExecutionError(
                code="chunked_schema_mismatch",
                message=(
                    f"masked chunks of table {table!r} disagree on column names "
                    f"({names} vs {chunk.schema.names}); the auto-chunk "
                    "eligibility gates admitted a chunk-variant job."
                ),
            )
    fields = []
    for idx, name in enumerate(names):
        non_null_types: list[pa.DataType] = []
        for chunk in chunks:
            t = chunk.schema.field(idx).type
            if not pa.types.is_null(t) and t not in non_null_types:
                non_null_types.append(t)
        if len(non_null_types) > 1:
            raise ExecutionError(
                code="chunked_schema_mismatch",
                message=(
                    f"masked chunks of table {table!r} disagree on column "
                    f"{name!r} type ({', '.join(str(t) for t in non_null_types)}); "
                    "the auto-chunk eligibility gates admitted a chunk-variant "
                    "job. Re-run with auto_chunk=False and report the config."
                ),
            )
        fields.append(pa.field(name, non_null_types[0] if non_null_types else pa.null()))
    target = pa.schema(fields)
    normalized = [c if c.schema.equals(target) else c.cast(target) for c in chunks]
    return pa.concat_tables(normalized).combine_chunks()


def aggregate_chunk_warnings(chunk_results: list[Any]) -> tuple[Any, ...]:
    """Order-stable union of per-chunk QualityWarnings.

    The same warning re-emitted by every chunk (they all run the same
    plan) collapses to one, exactly what the full-frame run would have
    emitted; genuinely distinct warnings keep first-emission order.
    Equality-based dedup because QualityWarning carries a dict detail
    (unhashable); warning counts are tiny, so O(n^2) is irrelevant.
    """
    seen: list[Any] = []
    for result in chunk_results:
        for warning in result.warnings:
            if warning not in seen:
                seen.append(warning)
    return tuple(seen)


def aggregate_chunk_timings(chunk_results: list[Any]) -> tuple[Any, ...]:
    """Coarse per-(strategy, column) rollup of per-chunk timing records.

    Elapsed times sum (the chunks really ran serially); memory deltas
    take the max, because per-chunk peaks measure the same bounded
    working set repeatedly -- summing them would fabricate a peak no run
    ever had. One record per (strategy, column), the same shape the
    full-frame result carries.
    """
    from decoy_engine.instrumentation.timing import StrategyTimingRecord

    order: list[tuple[str, str]] = []
    elapsed: dict[tuple[str, str], float] = {}
    peak: dict[tuple[str, str], int] = {}
    for result in chunk_results:
        for record in result.timings:
            key = (record.strategy_type, record.column)
            if key not in elapsed:
                order.append(key)
                elapsed[key] = 0.0
                peak[key] = 0
            elapsed[key] += record.elapsed_ms
            peak[key] = max(peak[key], record.peak_memory_delta_kb)
    return tuple(
        StrategyTimingRecord(
            strategy_type=strategy,
            column=column,
            elapsed_ms=elapsed[(strategy, column)],
            peak_memory_delta_kb=peak[(strategy, column)],
        )
        for strategy, column in order
    )


def _chain_first(first: pa.Table, rest: Iterator[pa.Table]) -> Iterator[pa.Table]:
    yield first
    yield from rest
