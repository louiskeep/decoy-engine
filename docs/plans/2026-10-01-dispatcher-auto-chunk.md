# Engine auto-chunk on the chunked dispatcher (Rust engine program B2)

Status: plan (revision 1: first draft, for the Codex plan gate)

Date: 2026-10-01. Program: `docs/plans/2026-09-30-rust-engine-program.md` (rev3), Phase B, item B2 (gap R1 in `docs/records/2026-09-30-rust-coverage-evidence-audit.md`). Branch `feat/dispatcher-auto-chunk`, off `feat/dispatcher-production-contract` at `ad4246a3` (B1, under review). This plan treats B1's pinned contract (`docs/plans/2026-10-01-dispatcher-production-contract.md`, rev4) as its base and rebases onto B1's merged form. B6a depends on this slice.

## Problem

`run_pipeline` already chunks large single-table mask jobs on its own ("auto-chunk", default on). The decision and the lane today:

- **When.** `_pipeline_chunk_route.decide_chunk_route` calls `_planner.classify_job` once (that one call also feeds the `explain_plan` stamp) and routes chunked when `auto_chunk` is true and the mode is `chunked`. The planner requires exactly one mask table, no generate tables, no FK relationships, the pandas substrate, scalar work only, every strategy passing `check_chunked_compatibility`, no `when:` predicate, no fpe join group, date_shift only with an explicit `date_format`, and, on the loaded source: a resident `pa.Table` (a `LazySource` is rejected by `lazy_source_rejection`), at least `auto_chunk_threshold_rows` rows (default `AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT = 100_000`), chunk-stable dtypes (integers only when null-free; string, float, bool, temporal and null types), no extra loaded frames, and the bucketize, group_key, text_mask, code_set and bucket_perturb source gates.
- **Lane.** `_pipeline_generate_mask.run_generate_and_mask_steps` calls `_pipeline_route_exec.run_mask_chunked`, which slices the resident table into zero-copy `chunk_size_rows` slices (default `CHUNK_SIZE_ROWS_DEFAULT = 50_000`), drains the pandas oracle `_chunked.run_mask_pipeline_chunked`, and joins the chunks with `concat_masked_chunks` (strict, no type promotion except null-typed chunks to the agreed type) and `combine_chunks`.

So the lane choice runs backwards. Below the threshold, the default-on unified slice (`_unified_slice`, Task 4.5) runs eligible jobs on native kernels. Above it, `_unified_slice_admission.cheap_admission` declines because `route_chunked` is set, and the job runs chunk by chunk on pandas. The largest jobs take the slowest lane. B1 made `decoy_engine.run_mask_chunked` the production-shaped chunked entry point (benchmark runs R074 and R075 measured the dispatcher against the pandas oracle on a 1M-row table); nothing calls it yet. Auto-chunk also records only `mode: chunked`; nothing says which backend masked each column.

## Guarantee (pinned)

For every `run_pipeline` call:

1. **When is unchanged.** For the same inputs, `route_chunked`, the existing `quality_metrics["auto_chunk"]` keys (`mode`, `chunk_size_rows`, `threshold_rows`, `source_rows`, `chunk_count`, `reason`) and the `explain_plan` stamp are the same as today. B2 changes only the lane that executes a job already routed to chunked.
2. **Lane.** A routed table runs through `run_mask_chunked` (B1), which picks the native or the oracle route for the whole table. Two cases run today's lane (`run_mask_pipeline_chunked`, unchanged) instead: the kill switch `chunked_dispatcher_enabled=False`, and the type-parity gate (Design 3). Both are recorded (guarantee 5).
3. **Output identity.** The routed output table equals the same call with `chunked_dispatcher_enabled=False`: same column names and order, Arrow types, field nullability, values (nulls and row order included) and schema metadata, checked with `Table.equals(other, check_metadata=True)`. This holds whichever lane and route ran, for any valid `native_threads`, and with or without the compiled companion. The routed output keeps equalling the forced full-frame run (`auto_chunk=False`) on values and types, as `tests/unit/execution/test_auto_chunk_routing.py` asserts today.
4. **Side channels.** Vault entries are the same pairs in the same order as today's lane. Row errors fail closed with `RowErrorsFailedError`, nothing quarantined, as today. The warnings tuple and `quality_metrics["code_set_corpora"]` equal today's.
5. **Evidence, never silent.** Every routed result carries `quality_metrics["auto_chunk"]["lane"]` (`dispatcher` or `legacy_oracle`), `["lane_reason"]` (`null` on the dispatcher lane), `["native_threads"]`, and `quality_metrics["chunked_route"]`: `native_admitted`, `reroute_reason`, and per configured column `column`, `strategy`, `planned_backend`, `executed_backend` and `calls`. A column planned for Rust that ran on pandas is visible together with the reason. The payload is JSON-safe and deterministic: two runs with the same inputs and companion state produce equal `quality_metrics`. Elapsed time lives in `ExecutionResult.timings` only.
6. **Threads.** `native_threads` is an `int >= 1` (not `bool`), default 1, validated with the other knobs before profiling (`ExecutionError(code="invalid_execution_knob")`). It reaches the compiled kernels on the dispatcher lane and changes no output byte.
7. **No mid-run fallback.** The lane and the route are fixed before the first chunk is masked. An error after that propagates with its existing type and code; nothing retries on another lane.
8. **Non-routed calls unchanged.** A call that does not route chunked never enters the new code: outputs and `quality_metrics` are byte-identical to today, including the all-default `quality_metrics == {}` golden contract, whatever `native_threads` or `chunked_dispatcher_enabled` is set to.

## Non-goals

Changing when auto-chunk triggers (planner gates, threshold, the `chunk_size_rows` default or its meaning); multi-table dispatch (B7); platform or CLI wiring (B3, B4), admission pricing for native cost and threads (B5), and release or artifact work (B3, B5 and the phase-boundary release); incremental output sinks or lazy batch input (B6a, B6b): the routed output is still fully materialized and concatenated; widening native admission (Phase C), including tables with unconfigured columns, which B1 vetoes to the oracle route; transforms on the chunked route (A8 keeps transform-bearing tables off it); changing `run_mask_chunked`, the oracle `run_mask_pipeline_chunked`, the unified slice, or `classify_job`; adaptive or byte-based chunk sizing; a performance gate (Phase E); showing the lane in `explain_plan`, which stays a static classification.

## Design

### 1. Where it plugs in

`_pipeline_route_exec.py` is at 597 lines against the 600-line cap, so the lane logic goes in a new module `execution/_pipeline_auto_chunk.py`:

```
run_auto_chunk(config, source, *, table, engine_version, registry, adapter, vault_writer,
    chunk_size_rows, key_provider, native_threads, dispatcher_enabled)
    -> tuple[outputs, timings, boundary_conversion_ms, warnings, quality_metrics]
```

`_pipeline_route_exec.run_mask_chunked` keeps its name, its five-part return and its current parameters, gains `native_threads: int = 1` and `dispatcher_enabled: bool = True`, and becomes a delegate to `run_auto_chunk`. Existing imports (`tests/physical/test_characterization_chunked.py`, `physical/drivers/_chunked.ResidentChunkedAggregatorAdapter`) keep working; the adapter forwards the two new parameters, and `tests/physical/test_lossless_forwarding.py` is extended for them.

`run_pipeline` gains two keyword arguments next to the auto-chunk knobs: `native_threads: int = 1` and `chunked_dispatcher_enabled: bool = True`, validated in the existing fail-early block (`require_positive_int`, `require_bool`) and threaded through `run_generate_and_mask_steps` to the routed call. They are runtime resource knobs like `auto_chunk`, never config fields, so they stay out of `pipeline_config_hash`. `_pipeline.py` sits at its 635-line ceiling in `tests/sentry/test_module_size.py`; the builder keeps it there by moving lines out (for example the auto-chunk paragraph of the docstring into `_pipeline_routing`'s module docstring, which it already points to), not by raising the ceiling.

### 2. Lane selection (once per routed table, before any chunk is masked)

In order:

1. `dispatcher_enabled` is false: legacy lane, `lane_reason = "dispatcher_disabled"`.
2. The type-parity gate (section 3) finds columns it cannot reconcile: legacy lane, `lane_reason = "legacy_type_parity:<column>:<type>"` for each such column, sorted and joined with `; `.
3. Otherwise: dispatcher lane.

The gate reads only the config and the source schema. The legacy lane is exactly today's code path (`run_mask_pipeline_chunked` plus `concat_masked_chunks`), so its output is today's by construction.

### 3. Type parity with today's lane

B1's schema rule (`native/_chunked_schema_rule.py`) differs from today's lane in three places, because today every chunk goes through pandas (`to_pandas`, then `Table.from_pandas(..., preserve_index=False)` in `_pandas_adapter.py` ~326):

- Passthrough columns (configured passthrough without `when:`, and unconfigured columns kept under the passthrough policy) are taken from the source Arrow column, with the source field's type, nullability and field metadata. Today they carry the pandas round-trip type, `nullable=True` and no field metadata.
- String-output columns (hash, truncate, redact with a string `redact_with`) are cast from `null` to `string` per chunk. Today a chunk whose output column is all null comes back `null`; `concat_masked_chunks` then casts it to `string`, except when every chunk is null, where the output stays `null`.
- No chunk carries schema metadata. Today the output carries the first chunk's `pandas` metadata, unless the first chunk needed a cast in `concat_masked_chunks` (a null-typed column that another chunk types), in which case it carries none: `c.cast(target)` uses the metadata-free target schema, and `pa.concat_tables` keeps the first table's schema.

A probe on 2026-10-01 (pyarrow 24.0.0, pandas 2.3.3; `Table.from_pandas(t.to_pandas(), preserve_index=False)` on two-row columns) found these round-trip type changes among planner-admitted types: `large_string` to `string`, `date64[ms]` to `date32[day]`, `time32[s]` to `time64[us]`. String, float16/32/64, bool, null-free int8 through uint64, date32, timestamp (`us`, and `ms` with `UTC`), time64, duration and null kept their type. The probe is a pointer, not the specification; acceptance test 0 characterizes today's actual lane.

From that characterization the builder writes one function and one table in `_pipeline_auto_chunk.py`:

- `legacy_chunk_type(column_kind, source_type, output_all_null)`: the Arrow type today's lane gives column `c` in one chunk, where `column_kind` is passthrough, string-output, or natively masked faker. Columns that ran on B1's oracle route under a strategy outside the schema rule already carry the oracle's per-chunk type and need no mapping.
- `LEGACY_PASSTHROUGH_CAST`: source types whose today-type is reached from the source value by `pyarrow.compute.cast(..., safe=True)` for every value. Expected entries: `large_string` to `string`, and `time32` (any unit) to `time64[us]`.

A passthrough source type whose round trip changes the type and is not in `LEGACY_PASSTHROUGH_CAST` sends the table to the legacy lane (section 2, step 2). Expected: `date64`, because pandas truncates sub-day milliseconds while a safe cast would raise. Narrower, never wider: an admitted type the characterization did not cover also goes to the legacy lane.

Reconciliation after the dispatcher lane drains (`run_auto_chunk`, before returning):

1. `concat_masked_chunks` joins the chunks as today.
2. Each column gets its whole-output today-type: `null` when `legacy_chunk_type` is `null` for every chunk, otherwise the single non-null type the chunks agree on (the same rule `concat_masked_chunks` applies). Passthrough casts use `LEGACY_PASSTHROUGH_CAST` with `safe=True`; a failed cast raises `ExecutionError(code="chunked_schema_mismatch")` naming table, column and both types. Casting an all-null `string` column to `null` is lossless.
3. Every field becomes `nullable=True` with no field metadata, as `Table.from_pandas` produces.
4. Schema metadata: if chunk 0 has no column whose `legacy_chunk_type` is `null` while the whole-output type is not, attach `pa.Table.from_pandas(first.to_pandas(), preserve_index=False).schema.metadata`, where `first` is chunk 0 cast to its per-chunk today-types; otherwise attach none. This costs one pandas conversion of one chunk per job.

Acceptance test 3 checks the result against today's lane with `check_metadata=True`. A fixture the reconciliation cannot match moves to the legacy lane through the gate. The test is never weakened.

### 4. Dispatcher lane execution

The lane produces the same slices as today (`source.slice(start, chunk_size_rows)` for `start` in `range(0, num_rows, chunk_size_rows)`) and calls:

```
run_mask_chunked(config, slices, table=table, engine_version=engine_version, registry=registry,
    adapter=adapter, vault_writer=vault_writer, chunk_result_sink=chunk_results,
    key_provider=key_provider, base_row_offset=0, native_threads=native_threads)
```

`adapter` is `run_pipeline`'s pandas adapter, so B1's `adapter_requested` veto does not fire. `run_mask_chunked` runs the oracle's eager preflight first (B1 guarantee 3), so config errors carry today's codes in today's order.

**Per-table fallback.** The native-or-oracle choice is B1's, made once per table before the first chunk is masked: when the native lane refuses the table, the same call runs B1's oracle route, and the refusal is recorded in `reroute_reason`. Refusals that can reach an auto-chunked table: a strategy the chunked native route vetoes (categorical, bucket_perturb, group_key, date_shift, code_set, text_mask and the rest of `CHUNKED_ROUTE_VETOED_STRATEGIES`), a non-native fallback policy, no kernel or pool, `uncovered_columns` (unconfigured columns kept under the passthrough policy), a faker column over a non-string source, `crypto_extension_unavailable` and `index_extension_unavailable`. The `when_predicate_not_native` and `adapter_requested` refusals cannot arise from `run_pipeline` (the planner rejects `when:`, and the adapter is pandas); if one ever does, it is recorded the same way. B7 reuses this per-table choice for each independent table.

Established pattern: other engines with an optional accelerated backend also fall back per unit of work at plan time and report why. Apache Gluten (Velox under Spark) falls back unsupported operators to vanilla Spark and reports a fallback reason per operator. Spark RAPIDS with `spark.rapids.sql.explain=NOT_ON_GPU` logs why each operator stayed on the CPU. Polars' streaming engine runs operations it does not support on the in-memory engine. All three decide before execution and never switch partway through a stream. B2 does the same (guarantee 7). After the first chunk the vault writer and the chunk-result sink already hold that chunk's side effects, so a retry on another lane would duplicate them.

Schema drift on a later chunk (`NativeChunkSchemaDriftError`) cannot happen here, because every chunk is a slice of one resident table. If it ever fires, it propagates as the typed `ExecutionError` B1 defined.

### 5. Results and evidence

- `timings`: `aggregate_chunk_timings(chunk_results)`, as today. Native chunk results carry one record per column per chunk (B1), so the rollup has the same shape.
- `boundary_conversion_ms`: sum over chunks (zero on the native route), plus the one metadata conversion from section 3.4.
- `warnings`: `aggregate_chunk_warnings(chunk_results)`. `quality_metrics["code_set_corpora"]`: `aggregate_chunk_code_set_corpora(chunk_results)`. Both as today.
- `quality_metrics["chunked_route"]`: `aggregate_chunked_route_evidence(chunk_results)` with each column's `elapsed_ms` removed. On the legacy lane the same shape is built from `plan_column_backends` (planned backends) with `executed_backend = pandas_oracle`, `calls` equal to the chunk count, `native_admitted = false` and `reroute_reason = lane_reason`, so both lanes report alike.
- `quality_metrics["auto_chunk"]`: today's six keys plus `lane`, `lane_reason` and `native_threads`, on routed runs only. Non-routed runs stamp exactly what they stamp today.
- One INFO log line per routed job naming the table, lane, `native_admitted` and `reroute_reason`. It contains no values.

### 6. Threads

`native_threads` defaults to 1, matching B1's library default. Production callers pass their admitted reservation (B5 prices it; B3 and B4 pass it). It is forwarded only on the dispatcher lane; the legacy lane and every non-routed route ignore it. The unified slice's own threading is not changed.

### 7. Companion not installed

The engine imports and runs without the companion. B1's preflight probes the crypto kernel only for tables with a hash column, and the index kernel only for tables with a faker column. A missing or ABI-incompatible companion sends that table to B1's oracle route with `crypto_extension_unavailable` or `index_extension_unavailable`, and evidence reports those columns as planned `rust_companion` or `rust_pool_select`, executed `pandas_oracle`. Tables using only redact, truncate and passthrough need no companion and run on the native route (`arrow_python`). Output is the same in every case (guarantee 3).

### 8. Chunk size

Unchanged: a fixed row count, `chunk_size_rows` (default 50,000), caller-set, validated as today and recorded in the stamp. Reasons:

- Values do not depend on chunk boundaries for any admitted strategy; that is the value-keyed contract the planner enforces.
- Keeping today's boundaries keeps the chunk-result count, the vault entry order and the metadata source (chunk 0) the same as today, which the identity guarantee relies on.
- Row-count batches are the established pattern for this kind of engine: pyarrow's dataset scanner defaults to `batch_size = 131_072` rows, and DuckDB stores row groups of 122,880 rows.

A different default, or byte-aware sizing for wide rows, is a performance decision that needs a measured run first. It belongs with B6a and B6b, which bound memory, or to a follow-up after Phase E. B2 does not change it.

### 9. Composition with A8 and lazy sources

B2 does not touch routing, so A8 (`docs/plans/2026-09-30-engine-owned-transforms.md`, rev15) holds unchanged. A transform-bearing table fails `check_chunked_compatibility` with `per_table_transforms_present`; the planner turns that into a full-frame fallback with the coded reason, transforms run before masking on the full-frame route, and the new module is never entered. A `LazySource` is rejected by the planner's runtime gate, so auto-chunk never sees one and B2 never materializes one. Lazy-source admission to full-frame under `auto` (A8's rule) is decided earlier, by `resolve_execution_route`, which B2 does not change. Whichever of A8 and B2 merges second rebases and keeps both slices' acceptance tests green unchanged.

### 10. Bookkeeping

- `tests/sentry/test_physical_seam_disconnection.py`: add `execution/_pipeline_auto_chunk.py` to `GUARDED_MODULES` (it is a routing executor and must not import `execution.physical`), and add it plus each touched module (`_pipeline.py`, `_pipeline_route_exec.py`, `_pipeline_generate_mask.py`, `_pipeline_finalize.py` or `_pipeline_chunk_route.py`, `physical/drivers/_chunked.py`) to the permitted-edit list with a justification. The import-direction and disconnection assertions stay unchanged.
- Docstrings that say auto-chunk routes through `run_mask_pipeline_chunked` (`_planner.py` module docstring, `_pipeline_routing.py`, `_pipeline_route_exec.run_mask_chunked`) are updated.
- CHANGELOG `[Unreleased]`: auto-chunked jobs run on the chunked dispatcher with unchanged output; the two new `run_pipeline` arguments; the new `quality_metrics` keys.
- The compatibility contract's `run_pipeline` signature entry, the README API summary if it lists `run_pipeline` arguments, CODEMAP ownership for the new module, and the roadmap and shipped log (docs-current rule).

## Acceptance tests (written first; behavioral tests record red-before)

0. **Characterization (before any lane code).** Through today's actual lane (`chunked_dispatcher_enabled` absent, current code), for every planner-admitted source type (null-free int8 through uint64, string, large_string, float16/32/64, bool, date32, date64, timestamp in each unit with and without a time zone, time32 and time64 in each unit, duration, null), as a passthrough column and as a column under each string-output strategy and faker, with chunks all-null, mixed and null-free: record each chunk's output type, the final type and the final schema metadata. The recorded table is checked in as a fixture. `legacy_chunk_type` and `LEGACY_PASSTHROUGH_CAST` must reproduce it exactly, and a test asserts that against the live pandas round trip for every type, so a pyarrow or pandas upgrade that changes round-trip behavior fails CI instead of silently changing output.
1. **When unchanged.** For the full `test_auto_chunk_routing.py` fixture matrix: outputs pass the existing assertions unchanged; the six existing `auto_chunk` keys and the `explain_plan` stamp are equal with the dispatcher on and off; every non-routed fixture produces equal `quality_metrics` on and off.
2. **Lane.** A spy shows a routed eligible job calls `run_mask_chunked` exactly once (with the pandas adapter, the registry, the vault writer, the key provider and `native_threads`) and never calls `run_mask_pipeline_chunked` directly; red-before, since today it calls the oracle. With `chunked_dispatcher_enabled=False`, and with a `date64` passthrough column, the reverse holds, and `lane_reason` is `dispatcher_disabled` or `legacy_type_parity:<column>:date64[ms]` respectively.
3. **Output identity.** Cases: every strategy `check_chunked_compatibility` admits for auto-chunk (enumerated from that gate, so a newly admitted strategy joins automatically), crossed with: companion present and absent (loader patched to raise); `native_threads` 1 and 4; an uneven last chunk; chunk 0 all-null in a string-output column that is non-null later; a column all-null across every chunk; nulls throughout; every admitted passthrough type from test 0; a non-nullable source field; source field metadata; a source carrying `pandas` schema metadata; and an unconfigured column under the passthrough policy. For each case the output `equals(..., check_metadata=True)` the same call with `chunked_dispatcher_enabled=False`, and `equals` (values and types) the forced full-frame run, as today.
4. **Native refusals are surfaced.** Each case gives the documented `executed_backend`, `native_admitted` and `reroute_reason`, and output equal to the dispatcher-off run: a categorical column next to a hash column (mixed table); an unconfigured column (`uncovered_columns`); hash with the crypto kernel unavailable (`crypto_extension_unavailable`, hash planned `rust_companion`, executed `pandas_oracle`); faker with the index kernel unavailable; and a redact, truncate and passthrough table with no companion installed (native admitted, `arrow_python`).
5. **Evidence shape.** `quality_metrics["chunked_route"]` passes `json.dumps(..., allow_nan=False)` and round-trips, carries no `elapsed_ms`, and has `calls` equal to the chunk count. Two identical runs give equal `quality_metrics`. `timings` holds one record per (strategy, column), with elapsed times equal to the sum over chunk results.
6. **Vault.** Vault entries, and the vault file written from them, are identical to the dispatcher-off run on both the native and the oracle route; a vault writer keyed differently from the mask key is rejected before any chunk, as today.
7. **Row errors.** A routed job whose oracle-route chunk reports a row error raises `RowErrorsFailedError` exactly as the dispatcher-off run does, with nothing quarantined.
8. **Threads.** `native_threads` 0, -1, `True` and `"2"` raise `invalid_execution_knob` before `profile_source` runs (spy). A spy shows the value reaches the compiled kernels on the dispatcher lane only. A non-routed run with `native_threads=4` gives output and `quality_metrics` equal to the default.
9. **No mid-run fallback.** A native kernel patched to raise on chunk 2 makes the call raise that error with its type and code; the oracle is never called afterwards (spy), and the vault writer holds the entries of chunks 0 and 1 only.
10. **Non-routed untouched.** Below-threshold jobs (unified slice and pandas full frame), FK jobs, multi-table jobs and `auto_chunk=False` never enter `_pipeline_auto_chunk` (spy); the all-default golden `quality_metrics == {}` still holds.
11. **A8 and lazy sources.** A large `LazySource` single-table job is not routed chunked, and the new module never reads it (poisoned reader). If A8 is on the base branch when B2 builds: a large transform-bearing single-table job runs full-frame with reason `per_table_transforms_present`, and the new module is never entered. Otherwise A8's acceptance tests 3 and 5b cover this after rebase, unchanged.
12. **Sentries and bookkeeping.** The seam-disconnection sentry with the updated lists, the module-size sentry with no raised ceiling, lossless forwarding for `ResidentChunkedAggregatorAdapter`, the public `__all__` snapshot (unchanged: B2 adds no public names), and the `run_pipeline` signature in the compatibility contract all pass.

Tests needing the compiled companion carry `@_NEEDS_COMPANION` and run in the companion venv locally and in the native-companion CI job. The companion-absent cases patch the loader, so they run everywhere.

## Known issues (tech debt, not blockers)

- Tables with unconfigured columns kept under the passthrough policy run on B1's oracle route (`uncovered_columns`). That is correct and visible in evidence, but many real configs leave columns unconfigured, so those jobs gain no speed until a later slice admits uncovered passthrough natively.
- Library callers get `native_threads=1` unless they pass a budget; platform and CLI pass reservations from B3, B4 and B5.
- The routed output is still fully materialized and concatenated; peak memory includes the whole output (B6a).
- `date64` passthrough tables, and any type the characterization cannot reconcile, use the legacy lane.
- Today's output schema metadata depends on chunk 0 (it is absent when chunk 0 needed a null cast, and differs from full-frame metadata in that case). B2 reproduces this for identity rather than fixing it. Making it match full-frame is a separate, deliberate output change.
- Metadata reconstruction costs one pandas conversion of one chunk per job.
- A `large_string` chunk holding more than 2 GiB of string data cannot be cast to `string` and raises `chunked_schema_mismatch`. Whether today's lane has the same limit is not characterized; at the default chunk size it needs values averaging over 40 KiB.
- `explain_plan` does not show the lane, because the lane depends on companion availability at run time; the run's own `quality_metrics` does.

## Rollout

- Engine only, size S-M. The kill switch `chunked_dispatcher_enabled=False` restores today's lane without a revert.
- The platform's isolated worker and the CLI call `run_pipeline`, so their large single-table jobs switch lanes only when they pin an engine release that contains B2 (B3, B4). Output is unchanged; `quality_metrics` gains the keys above.
- Ships in the Phase B paired engine and companion release, after B1.
- Gates: Codex plan gate, Sonnet build, dennis, Codex final, under the review-rounds rules. Engine slice: merges under the standing Rust rule after the merge hold lifts.
