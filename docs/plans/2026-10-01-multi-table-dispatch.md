# Independent multi-table dispatch in run_pipeline (Rust engine program B7)

Status: plan (revision 1)

Date: 2026-10-01. Program: `docs/plans/2026-09-30-rust-engine-program.md` (rev3), Phase B, item B7 (gap R1 in `docs/records/2026-09-30-rust-coverage-evidence-audit.md`, which notes that engine auto-chunk is single-table only). Branch `feat/multi-table-dispatch`, created at the B1 head `746256cc`. B7 composes with B2 (`docs/plans/2026-10-01-dispatcher-auto-chunk.md`, rev3.1 at `d0e3c14a` on `feat/dispatcher-auto-chunk`) and reuses its lane function, knobs, gates and benchmark method. B2 is a plan, not yet built. B7's build starts only after B2's build has passed its gates; the B7 branch then rebases onto B2's reviewed head, and this plan's references to B2 code mean that head.

## Problem

`run_pipeline` sends a mask job with more than one mask table to one full-frame pandas adapter call, whatever the tables' sizes:

- The planner's chunked gate refuses it: "chunked execution masks one table per run" (`execution/_planner.py` ~291-296).
- The unified slice refuses it: `len(table_kinds) != 1` (`execution/_unified_slice_admission.py:276`).
- So `_pipeline_generate_mask.run_generate_and_mask_steps` takes its `else` branch (~149) and calls `adapter.run(plan, merged_sources, ...)` once. The pandas adapter converts every source table to pandas before masking any of them (`execution/_pandas_adapter.py` ~210), masks every node on pandas, and converts every table back.

A 1M-row table masks on the dispatcher when it is the only table in the job (B2). The same table, submitted next to an unrelated 10-row lookup table, masks on pandas with every table's pandas frame resident at once. Only the platform's own Phase 1 streaming route handles several large tables, and it calls the chunked oracle per table outside `run_pipeline`.

B7 makes `run_pipeline` treat each independent table the way it would treat that table alone: a table that would auto-chunk as a single-table job runs through B2's dispatcher lane, one table at a time; the rest run in one full-frame adapter call, exactly as today.

## What "independent" means

Two mask tables in one job are independent when masking one never reads the other's data. In the engine today, the only cross-table data read during masking is the FK machinery:

- FK parent columns are snapshotted before masking and children are resolved against the parent's source-to-masked map (`_pandas_adapter.py` ~255-266, `_dispatch_mask_node` via `relationship_graph.parents_of`); `order_work` adds dependency edges only from FK edges and composite-to-FK-child edges (`execution/_runner.py:209`).
- Everything else is keyed by table: every work node carries its own `table` and reads `frames[node.table]`; the per-job sinks on `StrategyContext` (code-set evidence, pinned corpus records, date-shift anchors) are keyed by `(table, column)` (`execution/_adapter.py:85` and the field comments there); the vault writer holds a set and sorts at write (`vault.py:213, 265`), so entry order does not matter.
- Namespaces do not carry data between tables. `build_namespace_registry` is a pure function of config and FK relationships (`relationships/_namespace.py:167`), and every chunk-admitted strategy derives its output from the namespace seed and the value itself (the value-keyed contract the chunked gate enforces, `execution/_chunked.py` ~175-185). Two tables sharing a namespace therefore mask the same source value to the same output in any order and in any process.
- Randomness is per call, not a shared stream: non-deterministic strategies draw from a fresh `default_rng()` per handler call (`_strategies/_categorical.py:215`, `_strategies/_shuffle.py:52-54`), so removing one table from an adapter call cannot shift another table's draws.

Definition used by B7: a job's mask tables are independent when the job has no FK edges at all, that is `RelationshipGraph.edges` is empty and the config has no `relationships` block. A job with any FK edge keeps today's routing for every table, including tables outside the FK component (Known issues). Acceptance test 1 pins the definition, including a namespace shared across a dispatched and a non-dispatched table.

## Guarantee (pinned)

For every `run_pipeline` call:

1. **Nothing changes unless a table is dispatched.** The split engages only when every job gate in Design 2 holds and at least one table passes the table gates. A call where the split does not engage never enters the new module: its outputs, `quality_metrics`, warnings, timings and errors are byte-identical to the same call with `multi_table_dispatch_enabled=False`, for any valid knob values.
2. **Per-table "when" equals the single-table rule.** A table is dispatched exactly when the same table, submitted alone as a single-table job with the same knobs and its own source, would route chunked (same planner gate, same threshold), and B2's lane gates find nothing for its source (Design 3). The reason recorded for a table that is not dispatched is the reason that single-table job would record.
3. **Output identity.** For a call where the split engages, compared with the same call with `multi_table_dispatch_enabled=False`: `outputs` has the same keys in the same order, and every table `equals(..., check_metadata=True)` its counterpart (column names and order, Arrow types, field nullability, values with nulls and row order, schema metadata). This holds for any valid `native_threads`, with or without the compiled companion. The vault writer ends with the same entry set.
4. **Side channels, as permutations.** `warnings` is a permutation of today's tuple, `timings` carries the same multiset of `(strategy_type, column)` keys, and `quality_metrics["code_set_corpora"]` the same entries; their order follows execution order (Design 6). Validators, quarantine findings, `fidelity_report` and `post_validation` see the same outputs and give the same results, because finalize is unchanged and runs after every table has masked.
5. **Deterministic order and error precedence.** Dispatched tables run one at a time in `tables:` config order, then the full-frame group runs once. The first unit that raises ends the call with that error, and no later unit starts. Declared difference from today, the same one single-table auto-chunk already has (`_pipeline_routing.py` "Row-error honesty"): a dispatched table that reports a row error raises `RowErrorsFailedError` at once, carrying that table's failing-chunk records, before validators run. Today the job collects row errors from every table and raises from finalize, where a failing validator raises `ValidatorFailedError` first. Both behaviors fail closed and return no output; jobs with quarantine enabled are not split (job gate 6), so no row a job would quarantine today is turned into a failure.
6. **Threads.** Dispatched tables run sequentially, each with the job's whole `native_threads` budget (B2's knob, 1 to 1024). Peak native thread use is `native_threads`, never a multiple of it. Output bytes do not depend on it.
7. **Memory.** No more than one dispatched table's B2 working set is alive at a time, and the full-frame group holds pandas frames only for its own tables. Design 7 states the bound against today.
8. **Evidence, never silent.** Every split call records, per mask table, whether it was dispatched and why or why not, and per dispatched table B2's lane fields and per-column planned and executed backends (Design 8). The payload is JSON-safe and deterministic.

## Non-goals

FK-linked tables, and jobs where any FK edge exists (sequential and out-of-core routes, phase D); mixed generate and mask jobs (F2); native admission for unconfigured passthrough columns (B8: such tables are dispatched but B1 reroutes them to its oracle route with `uncovered_columns`); releases and platform or CLI wiring (B3, B4, B5); changing B1's `run_mask_chunked`, B2's lane selection and gates, the planner's single-table classification, the unified slice, or the pandas adapter; dispatching tables below `auto_chunk_threshold_rows` (they stay in the full-frame group, Known issues); running tables concurrently; incremental output sinks or lazy batch input (B6a, B6b); the platform's Phase 1 streaming route; the Phase E 100M-row gate (B7 carries its own bounded merge benchmark, section 11).

## Design

### 1. Where it plugs in

New module `execution/_pipeline_multi_table.py` (at most 600 lines), with two entry points:

```
decide_multi_table_split(config, *, plan, registry, graph, substrate, caller_sources,
    table_kinds, auto_chunk, auto_chunk_threshold_rows, dispatcher_enabled,
    split_enabled) -> MultiTableSplit | None

run_multi_table_split(split, config, *, resident_sources, engine_version, registry,
    adapter, vault_writer, chunk_size_rows, key_provider, native_threads, plan, graph,
    namespace_registry, unconfigured_column_policy, generate_output_tables)
    -> tuple[outputs, timings, boundary_conversion_ms, warnings, quality_metrics, row_errors]
```

`MultiTableSplit` is a frozen dataclass: `dispatched: tuple[str, ...]` in config order, `full_frame: tuple[str, ...]` (the remaining mask tables, config order) and `reasons: Mapping[str, str]` (one entry per mask table). `decide_multi_table_split` returns `None` when a job gate fails or no table passes the table gates.

`run_generate_and_mask_steps` gains three parameters (`caller_sources`, `native_threads`, `multi_table_dispatch_enabled`). Before its existing `if route_chunked:` branch it calls `decide_multi_table_split`; on a non-`None` result it calls `run_multi_table_split` instead of `adapter.run`, then continues with the same vault, `stamp_execution_metrics` and fidelity code. The single-table `route_chunked` branch and the plain `adapter.run` branch are untouched.

`run_pipeline` gains one keyword argument next to B2's two: `multi_table_dispatch_enabled: bool = True`, validated with `require_bool` in the existing fail-early block, never a config field, so it stays out of `pipeline_config_hash`. `_pipeline.py` adds no net lines: the builder keeps the module-size ratchet exactly as B2 does (moving docstring lines into `_pipeline_routing`'s module docstring), never by raising or loosening it.

### 2. Job gates (all must hold, else `None`)

1. `auto_chunk` is true, B2's `chunked_dispatcher_enabled` is true, and `multi_table_dispatch_enabled` is true. Either kill switch restores today's multi-table behavior; with the dispatcher off there is no speed to gain, so B7 does not move tables onto the chunked pandas lane.
2. The call reached the full-frame continuation: layer-1 routing chose `full_frame` (explicit or `auto`) and the unified slice did not run. Like single-table auto-chunk, an explicit `execution_mode="full_frame"` does not prevent dispatch.
3. At least two mask tables and no generate tables.
4. Independence: `graph.edges` is empty and `config.get("relationships")` is empty.
5. The resolved substrate is `pandas`.
6. Quarantine is not enabled (`(config.get("quarantine") or {}).get("enabled")` is falsy, the same read as `_pipeline_finalize.py:400`). Chunk-admitted strategies can emit row errors (date_shift, code_set, bucketize), and the chunked route fails closed on them, so a quarantine-enabled job stays whole.

### 3. Table gates (per mask table, in config order)

A table is dispatched when both hold; otherwise it joins the full-frame group with the first failing gate's reason:

1. **The single-table chunked rule.** `_planner._chunked_rejection(config, mask_tables=[t], generate_tables=[], work=..., ordered_work=..., substrate=..., source_tables={t: caller_sources[t]} if t in caller_sources else {}, auto_chunk_threshold_rows=...)` returns `None`, with `work` and `ordered_work` built once from the job's plan exactly as `classify_job` builds them. Passing only `t`'s own source makes the "extra loaded frames" check pass for siblings while every other runtime gate applies unchanged: a missing source or a `LazySource` (`lazy_source_rejection`), the size threshold, chunk-stable dtypes, and the bucketize, group_key, text_mask, code_set and bucket_perturb source gates. The reason recorded is the joined rejection string, identical to that single-table job's `rejections["chunked"]`.
2. **B2's lane gates find nothing.** B2's metadata-parity and type-parity gates (B2 Design 2 step 2 and Design 3), run on `t`'s source schema, return no finding. A table with a finding stays in the full-frame group with reason `multi_table_lane_gate: <B2 lane_reason>`. B2 would run such a table on its legacy chunked lane; B7 keeps it on today's full-frame lane instead, because B7's metadata rule (section 5) is characterized only for sources B2 admits.

Gates read only config, the plan and source schemas and Arrow null counts; no values. Decision order and reasons are deterministic.

### 4. Execution

1. **Dispatched tables, one at a time, in config order.** For each, call B2's `run_auto_chunk(config, resident_sources[t], table=t, engine_version=..., registry=..., adapter=..., vault_writer=..., chunk_size_rows=..., key_provider=..., native_threads=..., dispatcher_enabled=True)`. B2 runs B1's `run_mask_chunked`, which picks its native or oracle route for the whole table before the first chunk (B2 Design 4, "per-table fallback"). B7 keeps the returned output table and the small per-table results; it holds no reference to the table's chunk list after the call returns.
2. **Then the full-frame group, once.** If the group has tables, or the caller loaded extra sources that are not mask tables, call `adapter.run(plan, group_sources, ...)` with today's arguments, where `group_sources` is `resident_sources` minus the dispatched tables, in their original order. Extra loaded frames therefore keep being echoed in `outputs` as today. Vault entries for the group are collected exactly as today, from `group_sources` and the group's outputs (`collect_vault_entries` skips absent tables, `vault.py:170`). The group's row errors flow to finalize as today.
3. **Assemble.** `outputs` is rebuilt in the key order today's single adapter call produces (the order of `resident_sources`, which the adapter echoes), taking each table from its unit.

Established pattern: per-unit plan-time fallback is how Apache Gluten (operators that Velox cannot run fall back to vanilla Spark with a recorded reason) and Spark RAPIDS (`spark.rapids.sql.explain=NOT_ON_GPU`) handle a partly accelerated plan; B2 already cites both and applies the rule per table. B7 applies the same unit, a table, to jobs with several of them.

### 5. Output identity and the metadata rule

A dispatched table's output on B2's dispatcher lane equals B2's legacy chunked lane with `check_metadata=True` (B2 guarantee 3), and the legacy chunked lane already equals the full-frame route on values and Arrow types (`tests/unit/execution/test_auto_chunk_routing.py`, "BYTE IDENTITY"). Schema metadata is the one place they differ: the chunked lanes derive pandas metadata from chunk 0, the full-frame route from the whole table. A scratch probe on 2026-10-01 (pyarrow 25.0.1, pandas 2.3.3; `b7_meta_probe.py` and `b7_sample_meta_probe.py` in the session scratchpad) confirmed both halves: a two-table full-frame run and a single-table auto-chunk run of the same table were equal on values and types, and equal with metadata when every column's null pattern in chunk 0 matched the whole column; with a string column all null in chunk 0, the chunked output carried no metadata while the full-frame output did; and with a bool column whose nulls start after chunk 0, chunk-0 metadata recorded `bool` where the whole-table metadata recorded `object`.

Rule: after B2 returns a dispatched table's output `out`, B7 replaces its schema metadata with `full_frame_metadata(out)`:

- For each column, take the index of its first valid value (when it has one) and of its first null (when it has one), using `null_count` to skip the scan when a column has no nulls or only nulls, and `pyarrow.compute.is_null` / `is_valid` with `index` otherwise.
- `sample = out.take(sorted(set(indices)))`, at most two rows per column.
- Return `pa.Table.from_pandas(sample.to_pandas(), preserve_index=False).schema.metadata`.

Why it is exact: with `preserve_index=False` the pandas metadata records no row count or index (`index_columns: []`), only each column's dtype, and `to_pandas()` picks a column's dtype from its Arrow type and its null class (none, some, all), which the sample reproduces for every column at once. The probe above matched the whole-table metadata on bool, string, null-typed, int64, uint64, float32 and float64, time-zone-aware and naive timestamps, date32, duration and large_string columns in every null class; acceptance test 4a pins the rule against the live pandas round trip for every B2-characterized type and null class, so an upgrade that breaks it fails CI. Every chunk-admitted strategy preserves nulls (`vault.py` `collect_vault_entries` docstring relies on the same property), and the rule reads the output itself, so it does not depend on that property anyway.

Full-frame group tables run today's code on a subset of frames. With no FK edges, nothing in the adapter reads another table's frame (see "What "independent" means"), so their outputs are today's.

### 6. Results and ordering

- `timings`: dispatched tables' B2 timings in config order, then the group's. `boundary_conversion_ms`: the sum, including each dispatched table's metadata sample conversion.
- `warnings`: each dispatched table's B2 warnings in config order, then the group's; no de-duplication across units (today's adapter keeps a warning emitted by two tables twice, and so does B7).
- `quality_metrics["code_set_corpora"]`: the concatenation of the units' entries, in the same order.
- `row_errors`: the group's only (dispatched tables raise instead, guarantee 5).

### 7. Threads and memory

**Threads: sequential tables, whole budget each.** Considered: running dispatched tables concurrently with the budget split between them. Rejected because it raises peak memory to several tables' working sets at once, makes error precedence depend on timing, and gives no gain when the companion kernels already spread one column's batch over `native_threads`. Established pattern: morsel-driven query engines (Leis et al., SIGMOD 2014, the design DuckDB follows) give all worker threads to one pipeline's morsels at a time rather than partitioning threads across independent pipelines, and Spark's default FIFO scheduler gives the first job priority on all executors while its stages have tasks. The admitted thread reservation B5 prices therefore stays `native_threads` for any number of tables.

**Memory bound.** Today a split-eligible job holds, at its peak: every resident source, the pandas frames of every mask table at once (`_pandas_adapter.py` ~210), and every output. With B7 the peak is at most: every resident source (unchanged; the caller loaded them), the finished outputs of tables already masked (they are returned, as today), plus the larger of (a) one dispatched table's B2 working set (its chunk list and the concatenated output, B2 Known issues) and (b) the full-frame group's adapter working set, which covers only the group's tables. B7 never holds two dispatched tables' working sets at once. Section 11 checks this as a measured bar, not a claim.

### 8. Evidence

- `quality_metrics["auto_chunk"]` on a split call keeps its six existing keys: `mode` is `"chunked"` (at least one table was dispatched), `chunk_size_rows` and `threshold_rows` as today, `source_rows` and `chunk_count` `null` (as today for multi-table jobs), `reason` is `"multi_table_split: <k> of <n> mask tables dispatched (<names>)"`. It adds `tables`: one entry per mask table in config order with `dispatched` (bool), `source_rows`, `chunk_count` (null when not dispatched), `reason` (table gate reason, or the single-table chunked reason on dispatch), and for dispatched tables B2's `lane`, `lane_reason` and `native_threads`.
- `quality_metrics["chunked_route_by_table"]`: per dispatched table, B2's `chunked_route` payload (`native_admitted`, `reroute_reason`, per configured column `column`, `strategy`, `planned_backend`, `executed_backend`, `calls`). B2's top-level `chunked_route` key is single-table only and is not emitted on split calls.
- One INFO log line per dispatched table (B2's), plus one naming the split and the full-frame group. No values.
- `quality_metrics["execution"]` keeps `route: full_frame` (B7 is not a layer-1 route). `explain_plan` stays B2's static classification and does not show the split (Known issues).

### 9. Companion not installed

The new module imports nothing from the companion. Per dispatched table, B1's preflight probes only the kernels that table's columns need; a missing or ABI-incompatible companion sends that table to B1's oracle route (`crypto_extension_unavailable`, `index_extension_unavailable`), recorded per column as planned `rust_companion` or `rust_pool_select`, executed `pandas_oracle`. Tables with only redact, truncate and passthrough run natively without it (`arrow_python`). Output is identical either way (guarantee 3).

### 10. Composition with B2, A8 and lazy sources

- **B2.** B7 calls `run_auto_chunk` unchanged and reuses `native_threads`, `chunked_dispatcher_enabled`, B2's lane gates, evidence payload and benchmark method. B2's guarantee 1 ("when is unchanged") is a single-table statement and still holds for single-table jobs; B7 changes "when" only for multi-table jobs, behind its own kill switch. Two existing assertions are superseded by this plan and change only as stated in acceptance test 11: the multi-table case in `test_auto_chunk_routing.py::test_multi_mask_table_stays_full_frame`, and the "multi-table jobs" clause of B2 acceptance test 10.
- **A8** (`docs/plans/2026-09-30-engine-owned-transforms.md`, rev15). A transform-bearing table fails `check_chunked_compatibility` with `per_table_transforms_present`, so table gate 1 keeps it in the full-frame group, where A8's prepared-table state and `resolve_resident_sources` apply its transforms exactly once, as today. A8's transforms are per table (filter, sort, limit, dedupe, drop_column, derive), so they add no cross-table dependency. Dispatched tables never carry transforms. Whichever of A8 and B7 merges second rebases and keeps both slices' acceptance tests green unchanged.
- **Lazy sources.** A `LazySource` table, or one supplied only through `source_loader`, fails table gate 1 exactly as it fails single-table auto-chunk, so it stays in the full-frame group and is materialized where today's code materializes it. B7 never reads a lazy source itself.

### 11. Merge evidence: performance and memory

The standing Rust merge rule needs "faster, within its declared memory ceiling". B2's run measures single-table jobs; B7 adds its own bounded local benchmark, using B2's section 11 method unchanged except where stated. It is merge evidence, not the Phase E gate.

- **Harness.** `scripts/bench-multi-table`, next to `scripts/bench-auto-chunk`, calling `run_pipeline` with default knobs (threshold 100,000 rows, chunk size 50,000).
- **Workload.** Three independent tables, each built with B2's generator and column layout (`email` hash, `secret` redact, `zip` truncate length 3, `ref` passthrough; Arrow tables built from Python strings, no schema metadata): `t1` 1,000,000 rows from `default_rng(20261001)`, namespace `bench_ns_1`; `t2` 1,000,000 rows from `default_rng(20261002)`, namespace `bench_ns_2`; `t3` 50,000 rows from `default_rng(20261003)`, namespace `bench_ns_3` (below the threshold, so it stays in the full-frame group). Global seed 42. The record states each source's `Table.nbytes`.
- **Configurations.** (a) `multi_table_dispatch_enabled=False` (today's single full-frame call); (b) split on, `native_threads=1`; (c) split on, `native_threads=4`; (d) `t1` and `t2` each with one extra unconfigured column `extra` = `f"x{k:09d}"`, so B1 reroutes both to its oracle route (`uncovered_columns`), split on and off. (d) measures what B7 costs the tables it dispatches but cannot accelerate (the chunked pandas lane instead of today's full-frame pandas).
- **Method.** As B2 section 11: one fresh subprocess per trial under the `/home/cam/bin/pytest-one` lock, thread environment pinned, one discarded warmup per configuration, seven measured rounds with a seeded shuffled order (seed and order recorded), p50 and max-of-seven wall time of the `run_pipeline` call, whole-process `ru_maxrss` captured as soon as `run_pipeline` returns, outputs (Arrow IPC) and evidence written for the parent. Reference outputs come from separate unmeasured subprocesses, and every comparison happens in the parent.
- **Correctness.** Every measured trial's tables `equals(..., check_metadata=True)` the configuration's split-off reference; in (b) and (c), `t1` and `t2` report `native_admitted = true` with zero oracle fallbacks in `chunked_route_by_table`, and `t3` is in the full-frame group. A trial that fails either check fails the run.
- **Declared memory ceiling (before measuring).** 4.0 GiB peak RSS (4 x 1024^3 = 4,294,967,296 bytes) for every measured trial process, whole-process. An absolute safety ceiling for this workload only, not a scaling claim; working default, Cam may adjust.
- **Bars.** (b) and (c) have lower p50 and lower max-of-seven wall time than (a). (b)'s p50 peak RSS is at most (a)'s p50 peak RSS (the memory bound in Design 7). Every trial stays under the ceiling. For (d), split-on p50 is at most 1.10 times split-off p50; working default, Cam may adjust. Missing a bar stops for Cam.
- **Recording.** The next benchmark run ID and a row in the sprint and testing ledger. Docs and CHANGELOG quote a B7 speed or memory figure only as "measured" with that run ID. This plan states none.

### 12. Bookkeeping

- `tests/sentry/test_physical_seam_disconnection.py`: add `execution/_pipeline_multi_table.py` to `GUARDED_MODULES` and add it plus each touched module (`_pipeline.py`, `_pipeline_generate_mask.py`) to the permitted-edit list with a justification; import-direction and disconnection assertions unchanged.
- Module docstrings that say multi-table jobs always run full-frame (`_planner.py` mode 1, `_pipeline_routing.py` layer 2, `_pipeline_generate_mask.py`) are updated.
- CHANGELOG `[Unreleased]`: independent multi-table jobs dispatch qualifying tables per table with unchanged output; the new `run_pipeline` argument; the new `quality_metrics` keys; the declared row-error precedence difference.
- The compatibility contract's `run_pipeline` signature entry, CODEMAP ownership for the new module, the roadmap and shipped log (docs-current rule).

## Acceptance tests (written first; behavioral tests record red-before)

**Test protection.**

- The tests below are committed before any implementation commit, and the build record cites that commit's SHA.
- Tests 3 and 4 key their strategy fixtures by the live chunk-admission set (`_chunked._CHUNK_ADMITTED_STRATEGIES | CHUNK_CONDITIONAL_STRATEGIES`), asserted equal by a test, as in B2 test 3.
- Without a new plan gate, nobody deletes an acceptance test, adds a skip or xfail outside `@_NEEDS_COMPANION`, reduces a cross-product, replaces `check_metadata=True` with a weaker comparison, or removes a dispatch, reason, lane or backend-evidence assertion. A failing acceptance test is a defect in the code or a finding for the plan, never a reason to edit the test. The only existing assertions this plan changes are the two named in test 11.
- Coverage and mutation, recorded in the build record: branch coverage for `_pipeline_multi_table.py` and each changed unit; a mutmut run over the job gates, table gates, `full_frame_metadata`, output assembly order, result merging and evidence stamping, with that logic in free functions. A surviving mutant that can change output identity, which tables are dispatched, a reason, the kill switch, execution order or evidence is build work: a new test kills it, or the build record argues it equivalent and dennis accepts.

1. **Independence.** A job with one FK edge between two of three tables never enters the new module (spy), on every route it takes today. A job with a `relationships` block and no profiled edge does the same. A two-table job whose hash columns share one namespace, one table dispatched and one in the full-frame group (below threshold), masks the same source value to the same output in both tables, equal to the split-off run.
2. **Job gates and the no-dispatch golden.** For each failing job gate (one mask table; a generate table present; an FK edge; substrate forced through the existing knob path; quarantine enabled; `auto_chunk=False`; `chunked_dispatcher_enabled=False`; `multi_table_dispatch_enabled=False`), and for a multi-table job where no table passes the table gates (all below threshold): the new module's executor is never entered (spy), and outputs, `quality_metrics`, warnings and timings keys equal the split-off run byte for byte. `multi_table_dispatch_enabled` values `0`, `None` and `"yes"` raise `invalid_execution_knob` before `profile_source` runs (spy).
3. **Per-table "when" equals the single-table rule.** For a matrix of tables (each strategy in the live admission set; a non-chunk-safe strategy such as non-deterministic categorical and shuffle; a `when:` column; a composite bundle; an fpe join group; an integer column with nulls; bucketize over a null-bearing source; below and at the threshold; a `LazySource`; a table supplied only through `source_loader`; a pandas-metadata source with a `StringDtype` column; a `date64` column; a `time64[ns]` column), each table is dispatched exactly when the same table alone, as a single-table `run_pipeline` call with the same knobs, routes chunked and B2's lane gates find nothing; the recorded reason equals that single-table call's `auto_chunk.reason` (or `rejections["chunked"]`), or `multi_table_lane_gate: <B2 lane_reason>` for the last three. Red-before: today no multi-table table is dispatched.
4. **Output identity.** Jobs of two to four tables combining dispatched and full-frame tables from test 3's matrix, crossed with: companion present and absent (loader patched); `native_threads` 1 and 4; an uneven last chunk; chunk 0 all null in a string-output column that is non-null later; a bool passthrough column whose nulls start after chunk 0; a column all null in every chunk; every B2-characterized passthrough type; an unconfigured column under the passthrough policy (B1 `uncovered_columns`); a caller-loaded extra source that is not a mask table; and sources ordered differently from `tables:`. Each table `equals(..., check_metadata=True)` the split-off run, `outputs` keys and order are equal, and the case asserts which tables it expects to be dispatched, so a case cannot pass by quietly not dispatching.
4a. **Metadata rule.** `full_frame_metadata(t)` equals `pa.Table.from_pandas(t.to_pandas(), preserve_index=False).schema.metadata` against the live pandas and pyarrow for every type in B2's test 0 characterization, in each null class (none, leading nulls, trailing nulls, nulls only after the first 50,000 rows, all null), singly and all together in one table; a test fails CI if an upgrade breaks it.
5. **Side channels.** Vault entries equal as a set and the decrypted vault file contents are equal; a vault writer keyed differently is rejected before any table masks, as today. Warnings are a permutation of the split-off run's (including one warning emitted by two tables, kept twice), timings carry the same multiset of `(strategy_type, column)` keys, `code_set_corpora` the same entries. With validators (passing and failing), `fidelity_report=True` and `post_validation=True`, results equal the split-off run's.
6. **Evidence.** The `auto_chunk` block and `chunked_route_by_table` have the documented shape for dispatched, full-frame and lane-gated tables; they pass `json.dumps(..., allow_nan=False)` and round-trip; two identical runs give equal `quality_metrics`; no top-level `chunked_route` key on split calls; a companion-absent run reports hash planned `rust_companion`, executed `pandas_oracle`, reason `crypto_extension_unavailable`.
7. **Order and errors.** Spies show dispatched tables run in config order whatever the source order, never overlapping, then the group once. A native kernel patched to raise on the second dispatched table propagates that error with its type and code; the third dispatched table and the group never start. With two tables patched to fail, the first in config order wins. A dispatched date_shift table with an unparseable value under an explicit `date_format` raises `RowErrorsFailedError` with that table's records (the split-off run also raises `RowErrorsFailedError`); with a failing validator added, the split run raises `RowErrorsFailedError` and the split-off run `ValidatorFailedError`, pinning the declared difference in guarantee 5.
8. **Threads.** A spy shows each dispatched table's kernels receive the same `native_threads` value; outputs for 1 and 4 are byte-identical; a call with no dispatched table ignores the value.
9. **Memory structure.** A spy on `run_auto_chunk` shows calls never overlap, and after each returns the B7 executor holds only that table's output (a weak reference to the B2 chunk list, or an equivalent probe on the returned objects, is dead before the next table starts).
10. **A8 and lazy sources.** If A8 is on the base: a large transform-bearing table next to a dispatched table runs full-frame with reason containing `per_table_transforms_present`, its transforms applied once (A8's spy), its output equal to the split-off run. Otherwise A8's tests cover it after rebase, unchanged. A large `LazySource` table next to a dispatched table is never read by the new module (poisoned reader until today's materialization point).
11. **Superseded assertions.** `test_multi_mask_table_stays_full_frame` keeps its current assertions verbatim under `multi_table_dispatch_enabled=False`, and gains a split-on variant asserting both tables dispatched and identity with the split-off run. B2 test 10's multi-table clause becomes: multi-table jobs never enter `_pipeline_auto_chunk` when any B7 job gate fails. No other existing test changes.
12. **Sentries and bookkeeping.** The seam-disconnection sentry with the updated lists; the module-size sentry with `_pipeline.py` at its base's exact ratchet and `_pipeline_multi_table.py` at 600 lines or fewer; the public `__all__` snapshot unchanged (B7 adds no public names); the `run_pipeline` signature in the compatibility contract.
13. **Merge benchmark.** Section 11's run passes its correctness checks and bars; its run ID is in the build record.

Tests needing the compiled companion carry `@_NEEDS_COMPANION` and run in the companion venv locally and in the native-companion CI job; companion-absent cases patch the loader and run everywhere.

## Known issues (tech debt, not blockers)

- Tables below `auto_chunk_threshold_rows` in a multi-table job stay on full-frame pandas: the unified slice, which runs small single-table jobs natively, is single-table by design. A per-table unified slice, or one-chunk dispatch of small tables, is a follow-up that needs its own measured run.
- Any FK edge keeps the whole job on today's routes, including tables outside the FK component. Splitting by connected component belongs with the FK work in phase D.
- Quarantine-enabled multi-table jobs are not split.
- Row-error precedence differs from today for dispatched tables (guarantee 5), as it already does for single-table auto-chunk.
- On a split call `auto_chunk.mode` is `"chunked"` even when some tables ran full-frame; the platform's route label reads that key (`decoy-platform api/jobs/v2_node_runs.py:193`) and will say `legacy_chunked`. B3 reads `auto_chunk.tables` instead.
- `explain_plan` does not show the per-table split; the run's `quality_metrics` does.
- Tables whose source has uncharacterized pandas metadata, or a `date64` or `time64[ns]` column, stay on full-frame pandas rather than B2's legacy chunked lane.
- Tables with unconfigured columns are dispatched but run on B1's oracle route until B8.
- Outputs are fully materialized and concatenated per table (B6a); the metadata rule adds one small pandas conversion per dispatched table.
- Library callers get `native_threads=1` unless they pass a budget; platform and CLI pass reservations from B3, B4 and B5.

## Gates

Codex plan-gate, then Sonnet build (after B2's build has passed dennis and Codex final, rebased onto that head), dennis, Codex final, under the review-rounds rules. Engine slice: merges under the standing Rust rule after the merge hold lifts, with section 11's run as its speed and memory evidence. Ships in the Phase B paired engine and companion release. The platform's isolated worker and the CLI call `run_pipeline`, so their multi-table jobs change lanes only when they pin a release containing B7; the kill switch `multi_table_dispatch_enabled=False` restores today's behavior without a revert.
