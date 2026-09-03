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
| text_mask    | per-span HMAC-keyed dispatch (fpe/faker/date_shift/redact) |

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

DGRN-admitted (Phase 4 slice 1): `windowed_date`, position-keyed on the durable
global row number via `CHUNK_DGRN_STRATEGIES` (kept SEPARATE from CHUNK_SAFE so
the FK gate still rejects it). See `_chunked_dgrn.py` for the full design.

Sibling-keyed (Phase 4 slice 2): `group_key`, keyed on a SIBLING `group_by`
column's value via `CHUNK_SIBLING_KEYED_STRATEGIES` (also SEPARATE from
CHUNK_SAFE for the FK reason). See `_chunked_group_key.py`.

Own-value-keyed (Phase 4 slice 3): `text_mask` joins `CHUNK_SAFE_STRATEGIES`
directly (each span masked by its OWN text under `ctx.mask_key`; FK-self-mask
eligible). Its only new gate is the `when:` rejection in `_chunked_text_mask.py`.

Conditionally admitted, non-key (Phase 4 slice 4): `code_set` mask mode
(no `chapter_preserve`, chunk-stable string source, no `when:`, and no
participation as an FK key column in either orientation). Stays OUT of
CHUNK_SAFE_STRATEGIES -- it is a CONDITIONAL strategy like faker/categorical,
gated on config shape rather than admitted unconditionally -- and one corpus
record is resolved per column BEFORE any chunk streams and pinned into every
chunk's context so masking cannot drift across a mid-run corpus file swap.
See `_chunked_code_set.py`.

Conditionally admitted, non-key (Phase 4 slice 5): `bucket_perturb` with an
explicit `date_format` (chunk-stable string source, no `when:`, and no
participation as an FK key column in either orientation). Same CONDITIONAL
treatment as code_set -- autodetect is a whole-column reduction, so it stays
OUT of CHUNK_SAFE_STRATEGIES -- but simpler: no corpus, no evidence, so
there is no pinning/aggregation counterpart. See `_chunked_bucket_perturb.py`.

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
shared namespace rather than resolving by parent-map lookup -- ONLY WHEN
every gate condition holds (`_chunked_fk.gate_fk_child_edges`). Any failure
raises PlanCompileError (fail closed):

  (a) Parent key strategy is EXACTLY `hash` (2026-09-02 cascade-safety fix:
      every other CHUNK_SAFE_STRATEGIES member is a proven strategy x
      representation x substrate divergence hole for FK self-masking
      specifically, even though each is safe as an ordinary chunked
      strategy).
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
  (8-12) The parent key node is not itself an FK child endpoint elsewhere
      (a same-key cascade); neither FK key column has an effective `when`;
      neither FK key column declares a `provider`; and the FK key dtype is
      in the exact cross-adapter-safe set hash is proven byte-identical
      for, validated at BOTH compile time (the declared dtype) and per
      chunk (the real dtype) -- see `_chunked_fk.gate_fk_child_edges`'s
      docstring for the full rationale and `_chunked_fk_dtype_safety.py`
      for the exact dtype set.

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
from typing import Any

import pyarrow as pa

from decoy_engine.plan._errors import PlanCompileError

from . import _chunked_bucket_perturb as bucket_perturb_gate
from . import _chunked_code_set as code_set_gate
from . import _chunked_dgrn as dgrn
from . import _chunked_group_key as group_key
from . import _chunked_text_mask as text_mask_gate
from ._chunked_adapter_gate import chunked_adapter_touches_pandas_ingestion
from ._chunked_fk import (
    CHUNK_SAFE_STRATEGIES,
    fk_hash_strategy_columns_for_table,
    fk_passthrough_columns_for_table,
    gate_fk_child_edges,
    reject_lossy_chunked_fk_passthrough,
)
from ._chunked_fk_dtype import (
    fk_declared_dtypes_for_table,
    reject_mismatched_chunked_fk_declared_dtype,
)

# Admitted only when the column's config pins the deterministic
# value-keyed path (see module docstring for the per-strategy rules).
CHUNK_CONDITIONAL_STRATEGIES: frozenset[str] = frozenset(
    {"faker", "categorical", "code_set", "bucket_perturb"}
)

# Strategies admitted onto the chunked route: value-keyed (CHUNK_SAFE), the
# position-keyed DGRN set (`_chunked_dgrn.py`), and the sibling-keyed set
# (`_chunked_group_key.py`).
_CHUNK_ADMITTED_STRATEGIES: frozenset[str] = (
    CHUNK_SAFE_STRATEGIES | dgrn.CHUNK_DGRN_STRATEGIES | group_key.CHUNK_SIBLING_KEYED_STRATEGIES
)


def _conditional_admission_failures(col_entry: dict[str, Any]) -> list[str]:
    """Unmet chunked-admission conditions for a faker/categorical column.

    Returns one human-readable string per unmet condition; an empty
    list means the column is admitted.
    """
    strategy = col_entry.get("strategy")
    if strategy == "code_set":
        # code_set's admission conditions (mask mode, no chapter_preserve) do
        # not involve `deterministic`/`namespace` at all -- unlike faker/
        # categorical below, mask mode is deterministic by construction and
        # namespace is optional (defaults to "code_set"). See _chunked_code_set.py.
        return code_set_gate.code_set_conditional_failures(col_entry)
    if strategy == "bucket_perturb":
        # bucket_perturb's only admission condition is an explicit date_format;
        # deterministic/namespace do not apply (namespace is REQUIRED but
        # already fails closed identically on both routes -- see
        # _chunked_bucket_perturb.py).
        return bucket_perturb_gate.bucket_perturb_conditional_failures(col_entry)
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
        chunked_fk_parent_strategy_not_self_mask_safe: parent key strategy is not exactly `hash`.
        chunked_fk_child_namespace_missing: child column has no explicit namespace.
        chunked_fk_child_namespace_mismatch: child namespace != parent namespace.
        chunked_fk_child_strategy_missing: child column has no explicit strategy.
        chunked_fk_child_strategy_mismatch: child strategy != parent strategy.
        strategy_not_chunk_safe: a non-FK column uses a non-chunk-safe strategy.
        chunked_strategy_conditions_unmet: faker/categorical admission conditions
            are not met; message names each unmet condition.
        chunked_windowed_date_when_not_supported: `windowed_date` + `when:`.
        chunked_text_mask_when_not_supported: `text_mask` + `when:`.
        chunked_code_set_when_not_supported: `code_set` + `when:`.
        chunked_code_set_fk_key_unsupported: `code_set` used as an FK key
            column, as parent or child.
        chunked_bucket_perturb_when_not_supported: `bucket_perturb` + `when:`.
        chunked_bucket_perturb_fk_key_unsupported: `bucket_perturb` used as
            an FK key column, as parent or child.
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
        # code_set as an FK key (either orientation) is rejected here because
        # gate_fk_child_edges above only inspects CHILD-side edges (see
        # _chunked_code_set.py point 4).
        code_set_gate.reject_code_set_fk_keys(config, table=table)
        # Same both-orientation gap for bucket_perturb (see
        # _chunked_bucket_perturb.py point 3).
        bucket_perturb_gate.reject_bucket_perturb_fk_keys(config, table=table)
    # `windowed_date` + `when:` inadmissible here (public entry; see `_chunked_dgrn.py`).
    dgrn.reject_windowed_date_when(table_cfg, table=table)
    # `group_key` + `when:` inadmissible here too (see `_chunked_group_key.py`).
    group_key.reject_group_key_when(table_cfg, table=table)
    # `text_mask` + `when:` inadmissible here too (see `_chunked_text_mask.py`).
    text_mask_gate.reject_text_mask_when(table_cfg, table=table)
    # `code_set` + `when:` inadmissible here too (see `_chunked_code_set.py`).
    code_set_gate.reject_code_set_when(table_cfg, table=table)
    # `bucket_perturb` + `when:` inadmissible here too (see `_chunked_bucket_perturb.py`).
    bucket_perturb_gate.reject_bucket_perturb_when(table_cfg, table=table)
    offending: list[tuple[str, str]] = []
    conditions_unmet: list[tuple[str, str, list[str]]] = []
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict):
            continue
        strategy = col_entry.get("strategy")
        if strategy is None or strategy in _CHUNK_ADMITTED_STRATEGIES:
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
    key_provider: Any = None,
    base_row_offset: int = 0,
) -> Iterator[pa.Table]:
    """Mask `table`'s rows chunk-by-chunk under `config`.

    `config` is the validated pipeline config dump; `chunks` yields
    pyarrow Tables of the table's rows in order. Returns an iterator of
    masked chunks in the same order; concatenating them is byte-identical
    to a full-frame `run_pipeline` of the same rows. `check_chunked_
    compatibility` enforces the CONFIG-level value-keyed contract up front,
    but it does not see the data: for some strategies runtime dtype stability
    is the caller's precondition. A string-converting strategy on an
    int-with-nulls source diverges by chunk boundary (a null-free chunk stays
    int64 -> "1"; a null-bearing one widens to float64 -> "1.0"). `text_mask`
    now enforces a chunk-stable-string-source gate at admission below (both
    this entry and the auto route); the OTHER string-converting strategies
    (`text_redact` / `hash`) still rely on this caller precondition at this
    low-level entry (the auto route gates them via
    `_planner._runtime_source_rejections`). Carry-forward: extend the
    text_mask gate to that whole class.

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

    `base_row_offset` is the durable global row number the position-keyed
    `windowed_date` strategy consumes (Phase 4 slice 1); inert for every
    value-keyed strategy. See `_chunked_dgrn.py` for the domain contract.

    Validation and plan compile happen EAGERLY at call time; only the
    per-chunk masking is lazy.
    """
    dgrn.validate_base_row_offset(base_row_offset)
    from decoy_engine.execution._chunked_profile import empty_input_profile, first_chunk_profile
    from decoy_engine.execution._output_projection import resolve_unconfigured_column_policy
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.generation.pool import PoolCache
    from decoy_engine.plan import compile_plan
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

    check_chunked_compatibility(config, table=table)
    chunk_iter = iter(chunks)
    first = next(chunk_iter, None)
    # Codex-found: the gate below used to run AFTER this point returned early
    # for a zero-chunk source, so a keyed job with zero rows/batches and a
    # missing/invalid mask secret slipped through the GA fail-closed gate
    # (empty output, no error). The profile/plan/gate sequence now always
    # runs -- from a real first-chunk profile, or from `empty_input_profile`
    # when there is none -- and the empty-input return moves to AFTER the
    # gate so it only ever short-circuits a job the gate has already cleared.
    if first is None:
        profile = empty_input_profile(config, table=table, engine_version=engine_version)
    else:
        profile = first_chunk_profile(first, table=table, engine_version=engine_version)
    plan = compile_plan(config, profile, decoy_engine_version=engine_version, no_profile=True)
    # DE-02 (Codex BLOCKER 4): this is a PUBLIC entry point. Resolve the config's
    # `mask_secret_ref` when no programmatic provider was passed, then run the
    # fail-closed gate up front (fail fast) -- the per-chunk adapter.run() re-gates
    # via require_mask_key, so a keyed chunked job cannot run off job_seed at GA.
    if key_provider is None:
        _ref = (config.get("global_settings") or {}).get("mask_secret_ref")
        if _ref:
            from decoy_engine.keyprovider import key_provider_from_ref

            key_provider = key_provider_from_ref(_ref)
    from decoy_engine.keyprovider import require_mask_key

    _resolved_mask_key = require_mask_key(plan, key_provider)
    # DE-02 (Codex item 6a): this public entry point also collects vault entries,
    # so it must run the SAME vault-key guard as run_pipeline -- the vault holds
    # reversible plaintext PII and cannot be written under a key that differs from
    # the resolved mask key.
    if vault_writer is not None:
        from decoy_engine.vault import assert_vault_writer_keyed

        assert_vault_writer_keyed(vault_writer, _resolved_mask_key)
    resolved_registry = registry if registry is not None else get_default_registry()
    graph = RelationshipGraph(edges=(), ordering=())
    # Corpus pinning (Phase 4 slice 4): resolved BEFORE the empty-input return
    # below, so a zero-row job with an invalid/version-mismatched corpus fails
    # closed like the oracle instead of "succeeding" with no code_set column
    # ever dispatched. See _chunked_code_set.py.
    code_set_records = code_set_gate.resolve_pinned_code_set_records(
        plan, resolved_registry, graph, table=table
    )
    # bucket_perturb's namespace requirement is data-independent (the handler
    # raises before touching data), so validate it BEFORE the empty-input return
    # -- a namespace-less config with a zero-chunk input must fail closed like
    # the oracle, not return empty.
    bucket_perturb_gate.reject_bucket_perturb_missing_namespace(
        plan, resolved_registry, graph, table=table
    )
    if first is None:
        # Gate cleared (or the plan is unkeyed / pre-GA); there is genuinely
        # nothing to mask, so skip pool warming and adapter setup below --
        # unchanged from the original empty-input short-circuit, just moved
        # to run after the gate instead of before it.
        return iter(())
    # DE-03: resolve the projection policy once; each per-chunk adapter.run()
    # enforces it (a chunk carries the same column set as the whole table, so
    # per-chunk enforcement IS whole-table enforcement). Single mask table, no
    # generate echo on this route, so no table is exempted.
    projection_policy = resolve_unconfigured_column_policy(config)
    ns_registry = build_namespace_registry(config, profile)
    # Trap E group_by effective-type guard: needs the plan + source schema
    # (absent at the config-only check_chunked_compatibility above); once, pre-stream.
    group_key.reject_unsafe_group_key_group_by_dtype(
        plan, first.schema, table=table, registry=resolved_registry, relationship_graph=graph
    )
    # text_mask requires a chunk-stable string source (Trap: a non-string int+null
    # source widens by chunk boundary under the handler's str()-conversion). This
    # entry takes an arbitrary chunk iterable whose dtype could DRIFT across
    # chunks, so resolve the text_mask columns once and validate the first chunk
    # here + EVERY chunk in the masking loop below. The config-only compat gate
    # cannot see the data.
    text_mask_cols = text_mask_gate.text_mask_source_columns(
        plan, resolved_registry, graph, table=table
    )
    text_mask_gate.reject_unsafe_text_mask_chunk_schema(first.schema, text_mask_cols, table=table)
    # code_set has the identical chunk-stable-string-source requirement (same
    # str()-conversion hazard); same once-resolved / per-chunk validation split.
    code_set_cols = code_set_gate.code_set_source_columns(
        plan, resolved_registry, graph, table=table
    )
    code_set_gate.reject_unsafe_code_set_chunk_schema(first.schema, code_set_cols, table=table)
    # bucket_perturb has the identical chunk-stable-string-source requirement
    # (same date-parsing hazard on a non-string source); same once-resolved /
    # per-chunk validation split.
    bucket_perturb_cols = bucket_perturb_gate.bucket_perturb_source_columns(
        plan, resolved_registry, graph, table=table
    )
    bucket_perturb_gate.reject_unsafe_bucket_perturb_chunk_schema(
        first.schema, bucket_perturb_cols, table=table
    )
    passthrough_fk_columns = fk_passthrough_columns_for_table(config, table)
    # DE-10 residual: the compile-time FK gate trusts the operator-DECLARED FK
    # key dtype (it never sees the data). Read those declarations so the per-chunk
    # guard validates them against the real Arrow dtype and fails closed on a
    # misdeclaration (which would else silently void RI). Substrate-independent,
    # so unlike the passthrough magnitude guard it is not adapter-gated.
    declared_fk_dtypes = fk_declared_dtypes_for_table(config, table)
    # Predicate 12's REAL stage (cascade-safety fix) is scoped to hash-
    # strategy FK columns specifically, not every chunk-safe strategy the
    # family guard above covers -- see `reject_mismatched_chunked_fk_declared_
    # dtype`'s docstring.
    hash_fk_key_columns = fk_hash_strategy_columns_for_table(config, table)
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
        row_offset = base_row_offset  # DGRN counter; inert for value-keyed strategies.
        for chunk in _chain_first(first, chunk_iter):
            if guard_passthrough_fk_columns:
                reject_lossy_chunked_fk_passthrough(
                    chunk, table=table, passthrough_fk_columns=guard_passthrough_fk_columns
                )
            if declared_fk_dtypes:
                reject_mismatched_chunked_fk_declared_dtype(
                    chunk,
                    table=table,
                    declared_fk_dtypes=declared_fk_dtypes,
                    hash_fk_key_columns=hash_fk_key_columns,
                )
            # text_mask source must stay a chunk-stable string on EVERY chunk, not
            # just the first (the iterable's dtype can drift); see `_chunked_text_mask.py`.
            if text_mask_cols:
                text_mask_gate.reject_unsafe_text_mask_chunk_schema(
                    chunk.schema, text_mask_cols, table=table
                )
            # Same per-chunk check for code_set (see `_chunked_code_set.py`).
            if code_set_cols:
                code_set_gate.reject_unsafe_code_set_chunk_schema(
                    chunk.schema, code_set_cols, table=table
                )
            # Same per-chunk check for bucket_perturb (see `_chunked_bucket_perturb.py`).
            if bucket_perturb_cols:
                bucket_perturb_gate.reject_unsafe_bucket_perturb_chunk_schema(
                    chunk.schema, bucket_perturb_cols, table=table
                )
            # Per-chunk DGRN domain guard (no whole-stream row count); see `_chunked_dgrn.py`.
            dgrn.validate_chunk_row_offset_range(row_offset, chunk.num_rows)
            result = adapter.run(
                plan,
                {table: chunk},
                registry=resolved_registry,
                pool_cache=pool_cache,
                relationship_graph=graph,
                namespace_registry=ns_registry,
                unconfigured_column_policy=projection_policy,
                key_provider=key_provider,
                row_offset=row_offset,
                code_set_records=code_set_records,
            )
            if chunk_result_sink is not None:
                chunk_result_sink.append(result)
            # Sprint 2 honesty pack H1 (dennis review 2026-07-04): the
            # chunked/streaming path has no quarantine machinery, so a
            # per-row strategy error (bucketize/date_shift format_error,
            # code_set mask_error -- unreachable for an admitted code_set
            # column here, since its per-value errors are chapter_preserve-
            # only and chapter_preserve is excluded from admission)
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
            row_offset = dgrn.advance_row_offset(row_offset, chunk)
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
    from decoy_engine.generation.pool._identity import resolve_faker_pool_identity

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
        # Task 3.1 HIGH 1: the pool_size/locale/build_config split lives in ONE
        # place shared with the oracle handler and the native route, so this
        # pre-warm cannot drift onto a different cache identity than they use.
        cfg = provider_config_to_dict(col_seed.provider_config)
        pool_size, locale, build_config, identity = resolve_faker_pool_identity(
            builder=builder,
            provider=col_seed.provider,
            plan_pool_size=col_seed.pool_size,
            namespace=col_seed.namespace,
            job_seed=plan.seed_envelope.job_seed,
            cfg=cfg,
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
