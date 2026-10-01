# Chunked dispatcher production contract (Rust engine program B1)

Status: plan (revision 1)

Date: 2026-10-01. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase B, item B1 (gap R1 in `docs/records/2026-09-30-rust-coverage-evidence-audit.md`). Branch `feat/dispatcher-production-contract`, off `rust-program/integration` (engine main `8dc559e5` plus the approved A7, A6 and A5a slices; merges are on hold, so Phase B builds on that integration branch).

## Problem

The compiled chunked dispatcher `execution/native/_dispatch.run_native_or_oracle_chunked` masks a 1M-row hash, redact, truncate and passthrough table 5.9x faster at 1 thread and 8.4x faster at 4 threads than the pandas chunked oracle, with byte-identical values (R074, R075). No production code calls it. Its signature lacks what production callers pass to the oracle `execution/_chunked.run_mask_pipeline_chunked` (`registry`, `adapter`, `vault_writer`, `chunk_result_sink`, `base_row_offset`); its oracle branch drops those and `pool_cache`/`native_threads`; its native branch skips `check_chunked_compatibility`, emits no `ExecutionResult` (no timings, warnings or per-column evidence), carries no vault support, yields chunks without the oracle's pandas schema metadata, and in two degenerate shapes yields different column types than the oracle (an all-null string-output column: oracle `null`, native `string`; a zero-row chunk: oracle `float64`, native `string`). Schema drift on a later chunk raises `NativeChunkSchemaDriftError`, a `DecoyError` with no `.message` that callers cannot map. The thread budget is an untyped optional.

B1 makes the dispatcher a complete, public, production-shaped entry point. Wiring callers to it is B2 (engine auto-chunk), B7 (multi-table), B3 (platform Phase 1) and B4 (CLI).

## Guarantee (pinned) and non-goals

Guarantee: `decoy_engine.run_mask_chunked(...)` accepts everything the oracle `run_mask_pipeline_chunked` accepts, plus `native_threads` and an evidence sink, and for the same config and chunks:
1. Values: the concatenated output equals the oracle's concatenated output value for value (nulls and row order included), on whichever route it takes.
2. Schema: for columns masked with hash, redact or truncate (string-output strategies) or passed through, every yielded chunk of one call has the same type for that column, independent of route and of chunk contents: `string` for the string-output strategies, the source column's type for passthrough. No yielded chunk carries pandas schema metadata.
3. Validation: the same config errors are raised, with the same codes, on both routes, before any chunk is yielded (`check_chunked_compatibility` and the oracle's eager checks run first on every call).
4. Side channels: `vault_writer` receives the same source-to-masked pairs on both routes; `chunk_result_sink` receives one `ExecutionResult` per chunk on both routes, with per-column `timings`; `base_row_offset` is validated and advanced identically on both routes.
5. Evidence: per column, the route records `planned_backend` and `executed_backend` from {`rust_companion`, `rust_pool_select`, `arrow_python`, `pandas_oracle`}, a call count and elapsed time; a call that was planned native and ran the oracle anywhere is visible as such.
6. Threads: `native_threads` is an `int >= 1`, default 1; output bytes do not depend on it.
7. Errors: schema drift on a later chunk raises an `ExecutionError` with code `native_chunk_schema_drift` and a message naming the table, chunk index and detail.

Non-goals: cross-chunk type stability for columns masked by strategies that only run on the oracle route (categorical, bucket_perturb, group_key, date_shift and the rest keep the oracle's per-chunk types, as today; Phase C pins each one when it becomes native); wiring any caller (B2, B7, B3, B4); widening native admission (categorical, bucket_perturb, group_key, date_shift, FK stay on the oracle route; Phase C and D widen it); incremental output sinks or lazy input (B6); quarantine (the oracle chunked route has none; native emits no row errors); changing the oracle `run_mask_pipeline_chunked`'s own behavior or signature; the 100M and timing benchmarks (Phase E).

## Design

New module `execution/native/_chunked_entry.py` (the dispatcher module is at 535 lines; the size cap is 600), exported as `decoy_engine.run_mask_chunked` and from `decoy_engine.execution`, and added to the compatibility contract's public surface:

```
run_mask_chunked(config, chunks, *, table, engine_version, registry=None, adapter=None,
    vault_writer=None, chunk_result_sink=None, key_provider=None, base_row_offset=0,
    native_threads=1, route_evidence_sink=None, pool_cache=None) -> Iterator[pa.Table]
```

1. **Eager preflight, both routes.** Validate `native_threads` (`int`, not `bool`, `>= 1`; otherwise `ExecutionError(code="invalid_native_threads")`) and `base_row_offset` (existing `validate_base_row_offset`). Run `check_chunked_compatibility(config, table=table)`. Pull the first chunk. Run the oracle's eager checks that do not depend on the route (profile, compile, key resolution, `assert_vault_writer_keyed` when a vault writer is given). Then the existing `plan_native_route` decides the route.
2. **Oracle route.** Delegate to `run_mask_pipeline_chunked` with every parameter forwarded (`registry`, `adapter`, `vault_writer`, `chunk_result_sink`, `key_provider`, `base_row_offset`), then post-process each yielded chunk with the schema rule (step 5).
3. **Native route.** The existing `_mask_native` path, extended: `registry` is the one used for Faker pools; `adapter` is not used (documented: it selects the substrate only when the oracle route runs); `vault_writer` receives `collect_vault_entries(config, {table: source_chunk}, {table: masked_chunk})` per chunk, exactly as the oracle does; per chunk, an `ExecutionResult` with `outputs`, per-column `timings` (one `timed_strategy(strategy, column)` scope per column, around its kernel call), `warnings=()`, `row_errors=()`, `boundary_conversion_ms=0.0` and the evidence (step 6) in `quality_metrics` is appended to `chunk_result_sink`; `base_row_offset` is advanced by the chunk's row count (inert for admitted strategies, which are value-keyed, but the domain check runs).
4. **Both routes** reuse one `PoolCache` (the given one or a fresh one) and pass `native_threads` to the compiled kernels on the native route.
5. **Schema rule.** Before the first yield, compute the declared type for each hash, redact, truncate and passthrough column from the plan and the first chunk's source schema (`string` for the first three, the source field's type for passthrough). On both routes, each yielded chunk's column of those kinds is cast to its declared type when it is null-typed or is a zero-row `float64` (the two degenerate oracle artifacts); any other mismatch for those columns raises `ExecutionError(code="chunked_schema_mismatch")`, the oracle's existing code. Columns of other strategies are yielded as the oracle produces them. Schema metadata is dropped from every yielded chunk.
6. **Evidence.** `route_evidence_sink` keeps receiving one `NativeRouteEvidence` (existing behavior). New per-column records, `ChunkedColumnEvidence(column, strategy, planned_backend, executed_backend, calls, elapsed_ms)`, go into each chunk's `ExecutionResult.quality_metrics["chunked_route"]` together with the table-level `native_admitted` and `reroute_reason`; an aggregate helper `aggregate_chunked_route_evidence(results)` sums calls and time across chunks. Backends: hash on the native route is `rust_companion`; Faker pool selection is `rust_pool_select`; redact, truncate and passthrough on the native route are `arrow_python`; everything on the oracle route is `pandas_oracle`.
7. **Errors.** `NativeChunkSchemaDriftError` becomes a subclass of `ExecutionError` with `code="native_chunk_schema_drift"` and a `.message` naming table, chunk index and detail; its `.table`, `.chunk_index` and `.detail` attributes stay. Existing callers that catch `DecoyError` still catch it.
8. **Physical adapter.** `physical/drivers/_chunked.NativeOrOracleChunkedAdapter.run` forwards the new parameters unchanged (lossless-forwarding test extended). The seam stays disconnected from production routes (the disconnection sentry is unchanged).
9. `run_native_or_oracle_chunked` stays as the internal implementation (or a thin wrapper over the new module); its existing tests keep passing.

## Acceptance tests (written first; behavioral tests record red-before)

1. **Parameters accepted and forwarded**: every oracle parameter is accepted; on the oracle route each reaches `run_mask_pipeline_chunked` (spy); on the native route `registry` reaches pool resolution and `adapter` is unused (documented).
2. **Values**: across the native kernel set (hash, redact, truncate, passthrough, deterministic-REUSE Faker) and a vetoed strategy (categorical, oracle route), multi-chunk inputs (including uneven last chunk) concatenate to values equal to the oracle's concatenation and to full-frame `run_pipeline`.
3. **Schema**: for hash, redact, truncate and passthrough columns, one type for all chunks of a call, equal across routes, for: an all-null string column; a zero-row chunk first, middle and last; a chunk where a passthrough column is all null; no pandas metadata on any yielded chunk. A categorical column on the oracle route keeps the oracle's per-chunk types (pinned characterization).
4. **Validation parity**: each `check_chunked_compatibility` rejection and each eager oracle check raises the same code on a config the native route would otherwise admit, before any chunk is consumed (poisoned chunk iterator after the first).
5. **Vault**: with `vault: true` columns on the native route, the vault writer receives the same entries as the oracle route; a vault writer keyed differently from the mask key is rejected on both routes.
6. **Chunk results**: one `ExecutionResult` per chunk on both routes; native per-column timings have one record per configured column per chunk, `elapsed_ms >= 0`; the aggregate equals the sum.
7. **Row offset**: an out-of-domain `base_row_offset` raises `chunked_row_offset_out_of_domain` on both routes; the offset advances by each chunk's rows (spy on the domain check).
8. **Evidence**: native-route columns report the documented planned and executed backends with call counts equal to the chunk count; an oracle-route call reports `pandas_oracle` for every column and the reroute reason.
9. **Threads**: `native_threads` 0, -1, `True` and `"2"` raise `invalid_native_threads`; 1 and 4 give byte-identical output.
10. **Drift error**: a later chunk with a changed type or a missing column raises `ExecutionError` (`isinstance`) with code `native_chunk_schema_drift`, a message naming the chunk index, and the old attributes; still a `DecoyError`.
11. **Public surface**: `from decoy_engine import run_mask_chunked` works; it is in `__all__`, `decoy_engine.execution.__all__` and the compatibility contract.
12. **Oracle unchanged**: `run_mask_pipeline_chunked`'s existing tests pass unchanged; `test_phase2_gate.py` and the native parity suites pass, with the two degenerate allowlist entries now unnecessary for the new entry point (a new gate test asserts exact type equality through `run_mask_chunked`).

Tests needing the compiled companion carry `@_NEEDS_COMPANION` and run in the companion venv locally and the native-companion CI job.

## Gates

Codex plan-gate, Sonnet build, dennis, Codex final, under the review-rounds rules. Engine slice: merges under the standing Rust rule after the merge hold lifts.
