# POSTSCAN-STREAM: post-scan integrity streams on every route, with output key checks

Status: plan (2026-11-12, not built). Engine only. Roadmap row 60 (CLI `docs/ROADMAP.md`).

Base: engine `origin/main` `a001f4d7` (#510). Plan branch `plan/postscan-stream` from that head.

Risk: R2. It changes routing for `post_scan.integrity: true` jobs, adds a check that can block
a run before its output commits, and touches the commit choke point every route uses. No public
API removal, no schema major, no lock change.

Rules consulted: `~/dev-rules/README.md` (routing table), `00-universal.md`,
`risk-and-exceptions.md` (R2 gates), `development-loop.md` (PLAN: guarantee, design notes, choke
point, placement, established solutions), `testing.md` (Test depth, full-suite escalation),
`cross-cutting-concerns.md` (choke point plus a guard that walks the real artifact, seeded
bypass), `catalogs/recurring-defects.md` (classes 1, 3, 4, 5, 6, 8, 9, 11 bind this plan).

Also read: CLI `docs/ROADMAP.md` (section 2 "Integrity checks are standard", 5.2 item 8
publication chain, rows 31, 58, 59, 61, 76); engine `docs/plans/2026-10-30-f22-publication-replan.md`
(rev 11, slices c1a, c1b, c2); `plan/f25-4c-consumer-capabilities` (ScratchArena I1, I2, T05,
G5); the SCAN-ML plan (M4 `PostScanBlockedError`, M5 route guard, PR #508);
`docs/relationships.md`; `docs/reasons.md`; `docs/compatibility-contract.md` section 3.1;
`CODEMAP.md`; `.importlinter`.

## 1. Guarantee and non-goals

The change promises:

1. One post-scan implementation. It reads each table as bounded batches and never builds a whole
   table in memory beyond what the route already holds. It runs on every route `run_pipeline`
   can take: `full_frame`, `sequential`, `chunked`, `out_of_core`, `mixed`, `unified_slice`
   (the `declared_casting/_eligibility.py:26` `ROUTES` tuple), streamed generation, and the
   isolated worker.
2. When relationships are declared, the output is checked before it commits: every declared
   primary key is unique and non-null, and every child key (MATCH SIMPLE) is present in the
   parent's output keys. A breach that masking introduced blocks the run: nothing commits.
3. Results hold counts, table names, column names and reason codes only. No key value, row
   value or DuckDB message text reaches the report, an exception, a log line or a reason record.
4. The verdict reaches the run result under the existing check id `post_scan_integrity`
   (`run_result/_model.py:182`) and as one blocking exception, so F22's publish and the CLI
   delivery row consume one seam. This plan builds the seam, not delivery.

Non-goals (V2-LOG unless said otherwise): model (NER) post-scan on streamed routes (M5's guard
stays and model asks keep the resident route); post-scan on direct primitives (`run_sequential`,
`run_fk_out_of_core`, the chunked entries); exact per-row "introduced" attribution; a
`tables[].primary_key` config field (Owner question Q3); streaming the A1 post-validation suite;
the composite-key bug in `validation/post/_checks/_pk_uniqueness.py`; moving
`validators/_fk_validators.py` onto the key core.

## 2. Measured today

All on `origin/main` `a001f4d7`.

| Route | What post-scan does today | Where |
|---|---|---|
| full_frame (incl. resident auto-chunk lane) | Whole-table `to_pandas()` of every mask source and output, then `run_post_mask` in pandas. Generate tables skipped. Report only, never blocks. No PK check. FK check is `set(parent.tolist())` plus `isin` in Python | `execution/_pipeline_finalize.py:470-503` `compute_post_scan_integrity`, called at `execution/_pipeline_full_frame.py:308` before `publish.commit()` at 316; `verification/postmask/fk_preservation.py` |
| sequential | Declined: integrity makes the job ineligible, so it runs full-frame, or is refused before read (`FK_FULL_FRAME_OOM_RISK_REJECTED`) when too big; explicit sequential is rejected | `execution/_pipeline_routing.py:87-143` `_sequential_eligible`, code at 140-142 |
| chunked (output lane) | Output forced resident | `execution/_chunked_output_sink.py:105-131` `decide_output_mode`, 124-130 |
| out_of_core | Declined; explicit `out_of_core` raises `out_of_core_ineligible` | `execution/_pipeline_routing.py` `decide_execution_route` |
| mixed | Declined | `execution/_pipeline_routing.py:328-329` |
| unified_slice | Declined, falls to full-frame | `execution/_unified_slice_admission.py:252-257` |
| stream mode | Refused `runtime.streaming_unavailable` because output stays resident | `execution/pipeline/_stream.py:187-188` |
| streamed generation | No post-scan (the suite covers mask tables only) | `generation/_streamed.py:524` commits with no check |
| isolated | Same as the child's route (the child runs `run_pipeline`) | `execution/_isolated_worker.py` |
| direct primitives | No post-scan; M5 (PR #508) refuses model asks only | `verification/postmask/_route_guard.py` on `feat/scanml-m5` |

Routing also prices the resident route for integrity jobs at `execution/physical/_compiler.py:437`,
`execution/physical/_shadow_generation.py:422`, `execution/capacity.py:364,426`,
`execution/_job_estimate.py:161`, `execution/_transforms_admission.py:101` and
`execution/_pipeline_routing_signals.py:579`.

A streamed key-integrity core already exists, for pre-scan: `profiling/_scan_integrity.py`
(368 lines). `_check` (line 304) loads parent and child key columns as Arrow batches
(`_BATCH_ROWS` 100,000) into DuckDB (`MEMORY_LIMIT` 512MB, spill under
`fileaccess.protected_scratch_dir`). It counts parent null-key rows, duplicate excess (complete
rows minus `DISTINCT`), and per child the null-key rows and `NOT EXISTS` orphans
(`_child_facts`, line 358). It reads only local file descriptors (`_open_keys`, line 201).

"Declared" keys today (CONFIG-EXPLICIT): there is no `primary_key` field. A key is declared only
through `relationships:` (`config/_relationships.py`: `RelationshipEnd(table, columns)`,
`RelationshipConfig(parent, children, orphan_policy, namespace)`). `profile/_source.py`
`_derive_pk_cols` marks every column in a `parent.columns` list as `declared_pk`.

## 3. Established solutions

Searches run 2026-11-12: DuckDB larger-than-memory joins and aggregates; dbt `unique`,
`not_null`, `relationships`; Great Expectations compound uniqueness; Polars streaming anti joins.

- DuckDB spills hash joins and hash aggregates to `temp_directory` once intermediates pass
  `memory_limit` ([Memory Management in DuckDB](https://duckdb.org/2024/07/09/memory-management),
  [tuning guide](https://duckdb.org/docs/current/guides/performance/how_to_tune_workloads),
  [Saving Private Hash Join](https://duckdb.org/library/saving-private-hash-join-vldb/),
  [external hash aggregation](https://duckdb.org/library/robust-external-hash-aggregation/)).
  The engine already relies on this (`out_of_core/_duckdb.py`). Taken: the key checks stay in
  DuckDB, through the existing `connect_duckdb`, with a capped `memory_limit`.
- dbt's generic tests compile to queries that return failing rows: `not_null`, `unique`
  (group and count), `relationships` (child keys absent from the parent)
  ([dbt data tests](https://docs.getdbt.com/docs/build/data-tests)). The existing `_check`
  already has these shapes; nulls are counted by the null check, not by uniqueness.
- Great Expectations checks composite keys as one tuple with
  `expect_compound_columns_to_be_unique`
  ([GX expectation](https://greatexpectations.io/expectations/expect_compound_columns_to_be_unique),
  [GX uniqueness guide](https://docs.greatexpectations.io/docs/reference/learn/data_quality_use_cases/uniqueness)).
  Taken: a composite key is one tuple, never checked per column.
- Polars 2.0 streams by default, but sources disagree on whether its joins and group-bys spill
  ([Polars 2 announcement](https://pola.rs/posts/announcing-polars-2/),
  [runtimewire summary](https://runtimewire.com/article/polars-2-ritchie-vink-streaming-out-of-core)).
  Rejected: no proven out-of-core anti join, and the engine keeps Polars off the mask path.
- Sampling detectors: the engine's pre-scan samples 10,000 rows with a fixed seed
  (`profiling/scan_set.py::scan_sources`). Taken for the detector part only.

Custom code left: batch readers over the engine's own input kinds, the source-versus-output
verdict, and the streamed column facts. No library covers "compare a masked output with its
input" for these engine types.

## 4. Behavior after POSTSCAN-STREAM

### Two parts, one gate

The gate runs two parts over the same batch inputs:

- **Key checks.** Run whenever the config declares at least one relationship (Q1). Exact.
- **Column suite** (residual PII, policy validation). Runs only under `post_scan.integrity: true`,
  as today.

### Inputs: bounded batch readers

`verification/postmask/_stream_input.py` gives each table a `TableBatches` (schema, row count,
`batches(columns, rows)` callable, `bounded` flag), built from one of:

1. A resident `pa.Table` (full-frame outputs, caller sources): `to_batches(max_chunksize=)`.
2. A `LazySource` (Parquet or CSV): `iter_batches`. Its file identity check raises
   `runtime.lazy_source_changed` when the file changed after the route read it (reused code).
3. A staged engine output: the Parquet file a `ParquetTransactionalSink` staged, opened through
   the sink's stage handle (`txn.open_file`, the handle F22 measures by), read by row group.
4. A platform `source_loader` callable: one table at a time, the same bound the sequential
   route accepts today; `bounded=False` is recorded.

Key checks read only key columns. The column suite reads one configured column pair (source,
output) at a time from Parquet, and all configured columns per batch from CSV (CSV has no column
projection). Memory is batch rows times columns read, independent of table length.

### Key checks

Declared primary keys: each distinct `(parent.table, set(parent.columns))` across
`relationships:`. Declared foreign keys: each `(child.table, child.columns)` of each relationship.

For every declared key the gate computes the same facts twice, over the run's input sources and
over the output, with one shared function (section 10, "One key core"):

- PK: rows, null-key rows (any part null, as SQL requires for a primary key), duplicate excess
  (complete rows minus distinct complete tuples).
- FK: rows, null-key rows, orphan rows (a complete child tuple with no matching parent tuple;
  MATCH SIMPLE, so a child tuple with any null part is not checked), orphan distinct keys.

Verdict, per key:

| Fact | Blocks when | Code |
|---|---|---|
| PK duplicate excess | output > source | `post_scan.primary_key_duplicates_introduced` |
| PK null-key rows | output > source | `post_scan.primary_key_nulls_introduced` |
| FK orphan rows | output > source | `post_scan.foreign_key_orphans_introduced` |
| FK null-key rows | never; reported as a count | none |
| key column absent from output | always | `post_scan.key_column_missing` |
| parent and child output key types not comparable (`_comparable`) | always | `post_scan.key_type_mismatch` |
| DuckDB or read error during key checks | always | `post_scan.check_error` (reused) |

The source comparison keeps valid runs valid: `orphan_policy: preserve` and `warn` keep source
orphans, `remap` gives them fresh values that are still orphans, and a source that already has
duplicate parent keys is not the run's fault. A table with no source (a generate table, or a
mask child whose parent is generated) has baseline 0. `orphan_policy: fail` aborted earlier, so
the gate never sees those orphans.

V1 key shapes:

| Shape | Behavior |
|---|---|
| Single column | one label `k0` |
| Composite | one tuple `k0..kn`, never per column |
| Nullable FK | a tuple with a null part passes (not an orphan) and is counted in null-key rows |
| Self-reference | the table is read twice: parent columns, then child columns |
| Cross-table cycle | each edge is checked alone, so order does not matter; cycles already run full-frame |

### Column suite

`verification/postmask/_column_facts.py` streams, per configured column, one pass over aligned
source and output batches: row counts, comparable rows, identical rows (positional, exact),
`bytes_identical` (exact: every batch equal under the existing object comparison), and a
"constant source" flag (exact; holds at most one value in memory, never reported). When the row
counts differ, the identity facts are `None` and the existing row-count finding is emitted, as
today.

Detectors and distinct counts run on a deterministic positional sample: up to 10,000 rows per
table, positions drawn with the pre-scan seed from the known row count (Parquet footer, CSV
facts pass, resident length). The same positions are read on every route, so every route gives
the same report. A table of 10,000 rows or fewer is read whole and the result is exact. The
report names the sampled tables (`sample: {rows, of_rows}` per table, additive).

`residual_pii.py` and `policy_validation.py` keep their decision tables and take these facts
instead of frames. `run_post_mask(source_frames, output_frames, config=)` stays as a thin public
adapter that wraps frames as `TableBatches`, so its callers and tests keep working. The
`fk_preservation` report field is filled from the key facts; its pandas checker is deleted.

### Where the gate runs (the seam)

1. `execute_route` (`execution/_pipeline_route_dispatch.py:35`) binds one gate per run for the
   route's duration (context variable; precedent `bind_duckdb_run`).
2. Readable sink (`ParquetTransactionalSink`, or F22's staged writer): `observed_commit`
   (`execution/_publication_observer.py:99`) runs the bound gate over the staged tables before
   `sink.commit()`. Every route's commit already goes through it (`_sequential.py:603`,
   `out_of_core/_runner.py:306`, `_chunked_output_sink.py:164` for full-frame, mixed and
   streamed generation).
3. Full-frame route: the call at `_pipeline_full_frame.py:308` becomes `gate.run_resident(outputs)`
   over the resident tables, and `compute_post_scan_integrity` is deleted. The commit hook then
   sees that the gate has run.
4. No sink: `execute_route` runs the gate over `result.outputs` after the route returns.
5. Exactly once: the gate records that it ran; a second call is a no-op. After the route returns,
   `execute_route` raises `post_scan.check_error` if a required gate did not run (fail closed).
6. A foreign sink (`_CallableSinkAdapter`, a third-party `TransactionalSink`) cannot be read
   back. With the column suite asked, routing keeps the resident route (today's behavior,
   existing code `route.post_scan_integrity`). Key checks alone on a streamed route record status
   `skipped_unreadable_output` with `warning.post_scan_output_unreadable` and do not block.
7. A blocking verdict raises `PostScanBlockedError` (SCAN-ML M4's class, with the new codes)
   before `sink.commit()`. It carries the value-free report. Each route's existing failure path
   aborts the sink (`out_of_core/_runner.py:315`; the sequential and `OutputPublish` paths the
   same way), so nothing commits. With no sink, the caller receives no outputs.
8. Non-blocking results land in `quality_metrics["post_scan_integrity"]` with a new `keys`
   section and `verdict: {blocking, codes}`. The RunResult builder (RESULT row 62) maps that to
   `Check(id="post_scan_integrity", status, blocking, counts, codes)`; no new `CheckId`.

F22 seam: `publish_unit` binds the unit inside commit, after the gate. CLI delivery (row 61) reads
the verdict from the result or catches `PostScanBlockedError`. Isolated route: after F22 c2 the
parent is the only publisher, so the parent's commit runs the gate over the child's staged files.

### Routing after the lift

`config/_post_scan.py` gains `post_scan_needs_resident(config)`: true only when a model detector
is asked (whatever SCAN-ML treats as a model in `post_scan.detectors`). The routing call sites in
section 2 switch from `post_scan_integrity_requested` to it, and their keyword is renamed
`post_scan_models`. Code `route.post_scan_integrity_requested` retires with
`replaced_by=("route", "post_scan_models_resident")`, because its meaning narrows and codes never
change meaning (`docs/reasons.md`). `route.post_scan_integrity` keeps its meaning.

Memory: the gate's DuckDB `memory_limit` is the smaller of `MEMORY_LIMIT` (512MB) and the run's
own DuckDB share (`resolve_ooc_memory_limit(ctx.out_of_core_budget_bytes)`); no cap rises. Spill
goes to the run's scratch: the `ScratchArena` when F25 S2a has merged (I2), otherwise
`fileaccess.protected_scratch_dir` as pre-scan does today, which F25's census then moves.

## 5. Failure modes

| Failure | Result | Code (one owner) |
|---|---|---|
| Output PK duplicates above source | block, nothing commits | `post_scan.primary_key_duplicates_introduced` (new) |
| Output PK nulls above source | block | `post_scan.primary_key_nulls_introduced` (new) |
| Output FK orphans above source | block | `post_scan.foreign_key_orphans_introduced` (new) |
| Declared key column missing from output | block | `post_scan.key_column_missing` (new) |
| Parent and child output key types differ | block | `post_scan.key_type_mismatch` (new) |
| DuckDB error, spill disk full, read error in key checks | block; message names the exception type only | `post_scan.check_error` (reused) |
| Column-suite check raises | finding with severity error, run continues (today's best-effort rule) | `post_scan.check_error` (reused) |
| Source file changed before the gate re-read it | refuse | `runtime.lazy_source_changed` (reused) |
| Foreign sink, key checks on a streamed route | skipped, warning in report | `warning.post_scan_output_unreadable` (new) |
| Gate required but did not run | refuse after the route | `post_scan.check_error` (reused) |
| Model detectors asked | resident route kept | `route.post_scan_models_resident` (new; retires `route.post_scan_integrity_requested`) |

New codes go in `reasons/post_scan/_part1.py` (17 lines today), `reasons/warning/` and
`reasons/route/_added.py`; the retirement in `reasons/route/_retired.py`. Each code has one
owner module. The builder takes the next free registry minor with
`scripts/dev/registry_renumber.py` at merge and adds the reason-manifest fixture.

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard (walks the real artifact) | Mutation that must turn it red |
|---|---|---|---|
| No commit without the gate | `observed_commit` | AST sentry over `src/decoy_engine/execution/` and `generation/`: every sink `.commit()` call sits inside `observed_commit` or F22's `publish_unit`, aliases resolved | Call `sink.commit()` directly in `_sequential.py` |
| Gate ran on every route | `execute_route` post-condition | route-coverage sentry (AT-17), keyed on the imported `ROUTES` tuple | Return early from the gate when the route is `mixed` |
| One key core | `profiling.key_facts` | AST sentry: no `NOT EXISTS` key SQL outside `profiling/_scan_integrity.py` | Copy `_child_facts` into `_keys.py` |
| Value-free results | facts types hold ints, names and `ReasonRef` only | AT-14 canary | Put a duplicate key value in a finding |
| No whole-table read in the gate | `TableBatches` | AT-15 spies | Read a staged file with `pq.read_table` |

The coverage sentry derives its route list from `ROUTES`; the two entries outside that tuple
(`generate_streamed`, `isolated`) are named exemptions with reasons, as `cross-cutting-concerns.md`
asks. Each sentry is shown red on its seeded mutation in the slice record.

## 7. Acceptance tests

Red first: each slice's first commit adds its tests on the base, red; the record keeps the output.

| AT | Behavior | Failure guarded | Red mutation |
|---|---|---|---|
| AT-1 | `key_facts` over Arrow batches gives the same counts as pre-scan `_check` on the same files | drift in the shared core | change `_complete` to `OR` |
| AT-2 | `TableBatches` from resident, Parquet `LazySource`, CSV `LazySource`, staged Parquet and loader give identical batches and row counts | an input kind reads differently | drop the last partial batch |
| AT-3 | Pre-scan scan-integrity tests pass unchanged after PS-1 | pre-scan behavior change | swap null-key rows and duplicate excess |
| AT-4 | Masking that collapses two parent keys into one blocks with `primary_key_duplicates_introduced`; source duplicates alone do not block | false pass, false block | compare output with 0 instead of source |
| AT-5 | A nulled PK part blocks with `primary_key_nulls_introduced`; composite `(a, b)` with `b` null counts as null | per-column null logic | check only `k0` |
| AT-6 | Child keys remapped outside the parent block with `foreign_key_orphans_introduced`; source orphans under `preserve`, `warn` and `remap` do not block | baseline ignored | drop the source baseline |
| AT-7 | Composite FK: child `(1, 2)` against parents `(1, 3)` and `(4, 2)` is an orphan | per-column membership | join on `k0` only |
| AT-8 | Nullable FK: a child with any null part is neither an orphan nor blocking; the null count is reported | MATCH FULL by mistake | remove the completeness filter |
| AT-9 | Self-reference `employees.manager_id -> employees.id` reads both ends | one read reused for both ends | load parent columns as the child |
| AT-10 | Cycle A->B->A: both edges checked, same verdict for either config order | order dependence | stop after the first edge per table |
| AT-11 | Missing output key column blocks with `key_column_missing`; parent `int64` with child `string` blocks with `key_type_mismatch` | silent skip | return `checked` on a mismatch |
| AT-12 | DuckDB failure (injected at `_before_fetch`) blocks with `check_error`; the message has the type name only; `__cause__` and `__context__` are `None` | DuckDB text with values leaks | raise inside the `except` block |
| AT-13 | Column suite: `bytes_identical`, identity counts and the row-count finding equal the old pandas runner on tables under 10,000 rows; above that the report names the sample, and two routes give the same report | sampling changes small tables, or routes differ | seed from the wall clock |
| AT-14 | Canary values (key `CANARY-7f3a` duplicated, orphaned, and inside a forced DuckDB error) appear in no report, exception `str`, `ReasonRecord`, captured log or `quality_metrics` dump | value leak | add an example value to a finding |
| AT-15 | Bounded memory, one cell per route (sequential, chunked, out_of_core, mixed, unified_slice, streamed generation, isolated; full_frame asserts no second copy): the gate calls no `pq.read_table`, `ParquetFile.read`, `materialize_source` or whole-table `to_pandas`; the Arrow peak during the gate (`pa.total_allocated_bytes`, sampled per batch) stays under 4 times the batch bytes; the DuckDB `memory_limit` recorded by `DuckDBRunRecorder` is at most 512MB; with the limit forced to 8MB, spill files appear at `_before_fetch` | a route reads whole tables | read staged output with `read_table` |
| AT-16 | A blocking verdict commits nothing: the output dir holds no table, the sink evidence state is aborted, OOC and sequential temp files are gone | partial publish | run the gate after `sink.commit()` |
| AT-17 | Route-coverage sentry: a `GATE_TESTS` map keyed on every `ROUTES` entry plus `generate_streamed` and `isolated` names a test that proves the gate ran and stamped `keys`; a missing key or a missing test node fails | a new route without the gate | delete the `mixed` entry |
| AT-18 | `post_scan.integrity: true` with regex detectors takes sequential and out_of_core when eligible; model detectors keep the resident route with `post_scan_models_resident`; stream mode no longer refuses for regex | the lift misses a call site | leave `_unified_slice_admission.py:252` on the old predicate |
| AT-19 | Foreign sink: key checks report `skipped_unreadable_output` and warn; with the column suite asked the route stays resident with `route.post_scan_integrity` | a foreign sink read as empty passes | treat unreadable as zero rows |
| AT-20 | The gate runs exactly once (full-frame with sink, OOC with sink, no sink); a required gate that never ran raises `check_error` | double counting, silent skip | reset the `ran` flag in `observed_commit` |
| AT-21 | The verdict sits at `quality_metrics["post_scan_integrity"]["verdict"]` as `{blocking, codes}`; the tag stays `post-scan-integrity/v2`; the `run_post_mask` adapter equals the old runner on existing fixtures | frozen tag drift | bump the tag |
| AT-22 | A source file edited between the route read and the gate read refuses with `lazy_source_changed` | stale baseline | skip the identity check |

## 8. Slices

| Slice | Content | Size (hand-written) | Tier | Depends on | Build |
|---|---|---|---|---|---|
| PS-1 | Public `KeyBatches` and `key_facts(parent, children, *, scratch, memory_limit)` extracted from `_check` in `profiling/_scan_integrity.py`, re-exported from `profiling/__init__.py`; pre-scan calls it; `verification/postmask/_stream_input.py` `TableBatches` for the five input kinds; AT-1 to AT-3 | src about 170, tests about 230 | R1 (behavior-preserving split plus a new reader) | none | today |
| PS-2 | `verification/postmask/_keys.py`: declared PK and FK sets, source and output facts, verdict, report section; five `post_scan` codes and the warning code; AT-4 to AT-12, AT-14 (keys part) | src about 190, tests about 260 | R2 | PS-1 | today |
| PS-3 | `verification/postmask/_column_facts.py` (streamed facts, seeded sample); `residual_pii.py`, `policy_validation.py`, `runner.py` take facts; `run_post_mask` adapter; `fk_preservation` field from PS-2 facts; AT-13, AT-14 (suite part), AT-21 | src about 230 (plus about 120 deleted), tests about 200 | R2 (report numbers change above 10,000 rows) | PS-2, SCAN-ML M4 merged (both edit `runner.py`) | after M4 |
| PS-4 | `execution/pipeline/_post_scan.py`: the gate, per-run binding, exactly-once, fail-closed post-condition; `execute_route` binds it; the full-frame call replaces `compute_post_scan_integrity`; no-sink path; `PostScanBlockedError` raised with the new codes; AT-16 (full-frame), AT-20, AT-14 (end to end) | src about 170 (plus about 35 deleted), tests about 220 | R2 | PS-3, M4 | after M4 |
| PS-5 | Commit-time gate in `observed_commit` over staged outputs through F22's staged handle; foreign-sink skip; AT-15 for sequential, out_of_core, chunked and streamed generation; AT-16 streamed; AT-19; AT-22 | src about 110, tests about 280 | R2 | PS-4, F22 c1b | waits for F22 c1b |
| PS-6 | Routing lift: `post_scan_needs_resident`, the routing call sites and keyword rename, route code retire and add, stream-mode refusal lifted; AT-15 for mixed, unified_slice and isolated; AT-17; AT-18; commit AST sentry; docs (`docs/relationships.md`, `docs/reasons.md`, CODEMAP rows) | src about 120 (mostly mechanical), tests about 260 | R2 | PS-5, F22 c2 (isolated cell) | waits for F22 c2 |

Each slice merges alone and leaves main correct. After PS-4, full-frame runs (the only route that
runs the suite today) get key checks and blocking, and the column suite streams. After PS-5, key
checks run on every streamed route. After PS-6, `post_scan.integrity: true` stops forcing the
resident route.

PS-4 scratch: if F25 S2a has merged, the gate takes the run's `arena` (I2); otherwise it uses
`fileaccess.protected_scratch_dir` like pre-scan, and F25's G5 sentry flags it for the census.

Commits per slice: (1) red tests; (2) code; (3) docs and record. Conventional Commits, at most 72
characters, final line `cam`, no other trailers.

## 9. Test scope and full-suite triggers

```text
Tiers:        T0, T1 [, T3 because reason registry (frozen contract) on PS-2 to PS-6]
Named tests:  tests/unit/detection_verification/test_postscan_*.py,
              tests/unit/execution/test_postscan_gate.py, test_postscan_bounded_memory.py,
              tests/sentry/test_post_scan_route_coverage.py, the pre-scan scan-integrity tests,
              tests/unit/test_reason_registry.py, tests/unit/test_reason_release_pin.py
Selection:    scripts/dev/gates.sh --baseline (impact selection) on each commit
Guards:       route-coverage sentry, commit AST sentry, key-SQL sentry, import-linter
Triggers:     PS-2 to PS-6 add reason codes; PS-6 changes routing for every integrity job
Mutations:    the red mutations in sections 6 and 7, on the final head
Full suite:   once per merge head from PS-2 on, against the saved main baseline (decoy-vm-t3)
```

Run on the Mac first, a VM through `~/bin/vm-run` if it is busy; never on the devbox.

## 10. Design notes

Principles applied: one choke point for a rule with many sites (the commit hook, not a helper
each route calls); one place per fact (the key SQL lives only in `_scan_integrity.py`; the route
list comes from `ROUTES`); typed context objects (`TableBatches`, the gate bound per run) instead
of long parameter lists. No flag arguments: the gate's two parts are chosen by config facts read
once.

**One key core.** The pre-scan module already holds the right SQL. `key_facts` takes
`KeyBatches` (schema plus a batch callable), so the pre-scan file opener and the post-scan readers
feed one function. The DuckDB import stays inside `_scan_integrity.py`, covered by the one
existing import-linter ignore (`profiling._scan_integrity -> execution.out_of_core._duckdb`); no
ignore is added. `verification` (tier 2) may import `profiling` (tier 4).

**Why compare with the run's sources.** A blind "output keys must be unique" rule would block runs
whose source already has duplicate or orphan keys, which `preserve` and `warn` exist to allow.
One more key-column pass over the input flags only what masking changed.

**Count rule limit.** Counts can hide an introduced orphan when quarantine drops another orphan
row from the same table. Exact attribution needs row alignment, which quarantine breaks. V2-LOG.

**Why sample only the detectors.** Identity and byte equality are exact in one pass, and they
decide leak escalation. Detector match rates only decide whether a column looks like PII, at
thresholds of 0.4 and 0.5; a 10,000-row sample estimates that within about one point. Sampling on
every route keeps route parity. Exact streaming would need per-batch detector state that
`detection/_scoring.py` does not expose.

**Why `observed_commit`.** It is the one place every route's output passes while staged and not
yet visible. With the gate there, F22's `publish_unit` and CLI delivery never see output that
failed. The full-frame call stays before `cast_run.finish`, where it is today.

**Model post-scan stays resident.** NER over streamed batches needs M4's model path to take
batches. V2-LOG; M5's guard summary is reworded then.

## 11. Placement

| New file | Directory | CODEMAP "Where Things Go" row | Sits beside |
|---|---|---|---|
| `_stream_input.py` | `src/decoy_engine/verification/postmask/` | `detection/`, `profiling/`, `verification/` (line 106) | `runner.py` |
| `_keys.py` | same | same | `fk_preservation.py` (deleted in PS-3) |
| `_column_facts.py` | same | same | `residual_pii.py` |
| `_post_scan.py` | `src/decoy_engine/execution/pipeline/` | execution pipeline cluster | `_stream.py`, `_route_summary.py` |
| `test_postscan_keys.py`, `test_postscan_stream_input.py`, `test_postscan_column_facts.py` | `tests/unit/detection_verification/` | Unit tests | `test_bf4_fk_preservation.py` |
| `test_postscan_gate.py`, `test_postscan_bounded_memory.py` | `tests/unit/execution/` | Unit tests | the routing tests |
| `test_post_scan_route_coverage.py` | `tests/sentry/` | Sentry test | `test_f06_route_matrix.py` (same `PARITY_TESTS` pattern) |

`execution/` stays at 110 top-level modules (the new module goes under `pipeline/`).
`verification/postmask/` goes from 6 modules (7 with M5) to 9, under the 25 limit.

Sizes after: `_scan_integrity.py` about 400 (from 368); `runner.py` about 200 (from 190);
`residual_pii.py` and `policy_validation.py` shrink; `_pipeline_finalize.py` about 470 (from 503);
`_publication_observer.py` about 175 (from 158); `_pipeline_route_dispatch.py` about 170 (from
151). New modules stay under 300 lines each. `_pipeline_context.py` (594) and
`_pipeline_routing_signals.py` (613) do not grow: the gate reads existing context fields and the
routing edit is a rename. No size pin, ceiling, ratchet or allowlist rises.

## 12. Recurring-defect coverage

- 1 (anchored validation): reason identities compare as `ReasonRef` equality, never by substring
  or regex.
- 3 (no exception context): every DuckDB or reader error in the gate is saved as its type name
  and the safe error is raised after the `except` block closes; AT-12 asserts `__cause__` and
  `__context__` are `None`.
- 4 (versioned read models): the report tag stays `post-scan-integrity/v2` and fields are
  additive; `status` values are a closed `Literal`.
- 5 (normalized guard scanners): the commit AST sentry covers `sink.commit()`, an aliased bound
  method (`c = sink.commit; c()`), and `getattr(sink, "commit")()`; other spellings are V2-LOG.
- 6 (no private modules): `KeyBatches` and `key_facts` are exported from `profiling/__init__.py`;
  `verification` imports the package.
- 8 (one trust class): staged files and re-read sources are read as untrusted input; schema and
  row count come from the file, never from the route's claim.
- 9 (mutations on the final head): every mutation in sections 6 and 7 reruns on the merge head,
  which the record names.
- 11 (no raised caps): `MEMORY_LIMIT` and `_BATCH_ROWS` are unchanged; no size pin or allowlist
  grows.

## 13. Pre-mortem

- "Valid runs started failing." The source baseline covers inherited breaches; AT-6 covers each
  orphan policy. A generate parent with repeated keys now blocks; GEN-UNIQUE made generated keys
  unique, so that is a defect if seen.
- "Runs got slower." Key checks read key columns only (columnar on Parquet); CSV sources pay a
  re-read. PS-5's record measures the 10M laptop demo with and without the gate.
- "F22 rebased over PS-5." PS-5 waits for F22 c1b by design.
- "M4 and PS-3 both edited `runner.py`." PS-3 starts from M4's merged head.
- "Report numbers moved." Above 10,000 rows the detector rates are sampled; the report says so
  (AT-13).

## 14. Owner questions

Each changes what a customer gets from a run.

**Q1. When do key checks run?**
- A. Whenever relationships are declared, with or without `post_scan.integrity` (recommended:
  the roadmap says integrity checks are standard, and the cost is one key-column pass).
- B. Only under `post_scan.integrity: true`.
- C. A, plus an opt-out `post_scan.keys: false`.

**Q2. Do introduced key breaches block?**
- A. Yes, before commit, as specified (recommended: a broken foreign key in a test database is a
  function break under the V1 bar).
- B. Report only in V1, block in V2.

**Q3. Declare primary keys for tables that no relationship references?**
- A. No in V1; a key is declared only through `relationships:` (recommended: V2-LOG, no new
  config surface before release).
- B. Add `tables[].primary_key` now.

## 15. Follow-ups

V2-LOG: model post-scan on streamed routes; post-scan on direct primitives; exact per-row
introduced orphans; `tables[].primary_key` (if Q3 is A); streaming A1 post-validation; the
composite-PK bug in `validation/post/_checks/_pk_uniqueness.py`; `validators/_fk_validators.py`
onto `key_facts`; the dead `orphan_policy == "skip"` branch goes with `fk_preservation.py` in PS-3.
CLI rows: `src/decoy/_native_gate.py:236-250` still says post-scan integrity disqualifies native;
SCAN-ML-4 (`run --chunked` skips regex post-scan, `run.py:856`).

## 16. Plan contract

### Class table

| # | Class | Applies? | Closing structure | Proof and mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | Yes: key and column values pass through the gate | facts and verdict types hold ints, names and `ReasonRef` only; DuckDB errors kept as type names | AT-12, AT-14; mutation: add an example value |
| 2 | Cleanup on an exit path | Yes: DuckDB connection, spill dir, staged files on block | `try/finally` close inside `key_facts`; scratch through the arena or `protected_scratch_dir`; route abort paths unchanged | AT-16; mutation: skip `conn.close()` and assert the spill dir is gone |
| 3 | State machine and concurrency | Yes: gate state `bound`, `ran` per run | context variable per run; exactly-once flag; post-condition | AT-20; mutation: reset the flag |
| 4 | Route parity and silent fallback | Yes: the reason for this row | gate in `observed_commit` plus the `execute_route` post-condition; coverage sentry keyed on `ROUTES` | AT-15, AT-17, AT-18; mutation: drop a route entry |
| 5 | Referential integrity | Yes | `key_facts` PK and MATCH SIMPLE FK over output, baseline over source | AT-4 to AT-11 |
| 6 | Untrusted producer | Yes: staged files, foreign sinks | schema and counts read from the file; a foreign sink is skipped and the report says so | AT-19; mutation: trust the route's row count |
| 7 | Check then use (TOCTOU) | Yes: sources re-read after masking; staged files read, then committed | `LazySource` identity check; the gate reads the staged handle that commit renames | AT-22; mutation: skip the identity check |
| 8 | Weak tests and overclaimed evidence | Yes: memory tests can pass on tiny data | AT-15 forces an 8MB DuckDB limit and asserts spill files exist | mutation: a whole read turns AT-15 red |
| 9 | Behavior-preserving refactor drift | Yes: `_check` split, runner adapter | AT-1, AT-3, AT-21 against the old outputs | mutation in AT-1 |
| 10 | Frozen contract drift | Yes: report tag, reason registry | additive fields; one code retired with `replaced_by`; manifest fixture | AT-21; registry tests |

### Enumerations

#### Exit paths

- Gate passes: report stamped, commit continues (`execution/_publication_observer.py:103`).
- Gate blocks: `PostScanBlockedError` before commit; the route aborts the sink
  (`execution/out_of_core/_runner.py:315`).
- Gate errors: `post_scan.check_error`, blocks (`reasons/post_scan/_part1.py:15`).
- Gate skipped for a foreign sink: `warning.post_scan_output_unreadable`, run continues.
- Gate never ran: `post_scan.check_error` raised after the route
  (`execution/_pipeline_route_dispatch.py:35`).
- Source changed: `runtime.lazy_source_changed` (`reasons/runtime/_part1.py:216`).

#### Routes

- `full_frame`, `sequential`, `chunked`, `out_of_core`, `mixed`, `unified_slice`
  (`declared_casting/_eligibility.py:26`).
- Streamed generation (`generation/_streamed.py:524`).
- Isolated worker (`execution/_isolated_worker.py:113`, `_stage_outputs`).
- Stream mode (`execution/pipeline/_stream.py:187`).
- Direct primitives: out of scope (section 1).

#### FK shapes

- Single column.
- Composite tuple (`profiling/_scan_integrity.py:300`, `_complete`).
- Nullable FK (MATCH SIMPLE).
- Self-reference.
- Cross-table cycle (each edge alone).
- Generated parent with a mask child: baseline 0.

#### Fields

- `keys[]`: `table`, `columns`, `kind` (`primary`, `foreign`), `parent` (foreign only), `bounded`,
  `codes`.
- `keys[].status`: `checked`, `key_type_mismatch`, `key_column_missing`,
  `skipped_unreadable_output` (extends `Status` at `profiling/_scan_integrity.py:40`).
- `keys[].source` and `keys[].output`: `rows`, `null_key_rows`, `duplicate_excess` or
  `orphan_rows`, `orphan_distinct_keys`.
- `sample` per table: `rows`, `of_rows`.
- `verdict`: `blocking`, `codes`.

#### Output surfaces

- `quality_metrics["post_scan_integrity"]` (stamped today at `execution/_pipeline_finalize.py:503`).
- `PostScanBlockedError` message and attributes.
- `ReasonRecord`s.
- Publication evidence state (`execution/_publication_observer.py:110`).
- RunResult `Check` (`run_result/_model.py:182`, through RESULT row 62).
- Log lines: none added that carry data.

### Reuse inventory

- `profiling/_scan_integrity.py` `_check`, `_child_facts`, `_comparable`, `_labels`,
  `_before_fetch` (they become `key_facts`).
- `execution/out_of_core/_duckdb.py` `connect_duckdb`, `DuckDBRunRecorder`;
  `out_of_core/_budget.py` `resolve_ooc_memory_limit`.
- `fileaccess.protected_scratch_dir` now, F25 `ScratchArena` later.
- `profile/_readers.py` `LazySource.iter_batches` and its file identity check; `CsvLazySource`.
- `execution/_publication_observer.py` `observed_commit`; the `_transactional_sink.py` stage handle.
- `verification/postmask/residual_pii.py` and `policy_validation.py` decision tables.
- SCAN-ML M4 `PostScanBlockedError`; M5 route guard unchanged.
- `tests/sentry/test_f06_route_matrix.py` pattern for the coverage sentry.
- `scripts/dev/registry_renumber.py` for registry minors.

### Frozen contracts

`post-scan-integrity/v2` (additive fields only, `docs/compatibility-contract.md` section 3.1);
the reason registry (append-only, one retirement with `replaced_by`, a new manifest fixture);
the `run_post_mask` signature; `CheckId`.
