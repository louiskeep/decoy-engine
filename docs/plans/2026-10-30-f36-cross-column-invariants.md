Status: plan only, revision 3 (Opus remediation 1 of 3 after the Opus hostile review on `review/f36-opus`). This document grants no implementation, release, merge, or publication approval.

Baseline: `decoy-engine` `fdd08a894d5dd4440a1adea0ee62bf0b670808bb`; `decoy-platform` `origin/main` `ea68d8e`; F31 plan `origin/plan/f31-safe-output` `a909118` (itself under revision). Line numbers below are at these commits.

Rules consulted: `/home/cam/dev-rules/` README-selected rulebooks, repository `AGENTS.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and `decoy-platform:handoffs/queue:docs/handoffs/DEFECT-CATALOG.md`.

Risk: **R2**. Public configuration, result fields, a versioned artifact, bounded relational scratch, concurrency, and a Platform schema. Owner approval, RED-first evidence, and adversarial review per slice.

# F36: Cross-column invariants and relational fidelity

## FRAME

### Problem

The engine has point validators and marginal fidelity reports, but no closed, bounded contract for declared row relations or join-cardinality change. Existing validator reports carry names, row positions, prose, and free mappings. F36 needs stable value-free identities, exact or honestly sampled counts, route parity, and a report-only seam for B-9 and future F35. The source/output ordering prerequisite exists at `src/decoy_engine/execution/_runner.py:234-378` with tests at `tests/unit/execution/test_cross_column_ordering.py:83-233`; F36 keeps them and does not reimplement ordering.

### Guarantee as properties

For every valid F36 program within the limits below:

1. The language is a closed tagged union: no expression text, SQL, callback, plugin, literal set, free metadata, or user-defined operation.
2. Every observation identity is `ObservationRef(CheckRef(code, authored_position), scope)`, scope exactly `source`, `output`, or `transition`. Reordering declarations does not change identity. Duplicate identities fail before data read.
3. Source, output, and transition are separate observations with their own status, verdict, counts, and reason. F35 eligibility is only `scope=output`, `status=measured_exact`, `verdict=pass`, and `rows_evaluated>0`.
4. Exact observations report exact counts. Sampled observations report observed and unobserved populations and never claim population failures. A sampled observation with no observed failure is `indeterminate`, never `pass`. Zero evaluated rows is never `pass`.
5. Ordinary comparisons treat null as unknown. Only null checks judge nullity. Non-finite, overflowing, drifting, or non-comparable values are unevaluable.
6. All supported routes feed one semantic collector. A route measures, delegates, or refuses before its first read with a registered reason. No `skip` capability exists.
7. No report, artifact, event, error, log, file, database row, or HTTP body contains a cell, key, row index, configured name, path, DSN, query, exception text, value-derived label, or value-derived number. Every number in the report is a row or parent count.
8. **R-FAIL (measurement never changes the run).** Findings and F36 failures alike do not change output bytes, quarantine, publication, job success, quality grade, or legacy `quality-report/v1`. The binding rule is in "Failure rule R-FAIL" below.

### Non-goals

- Discovery, inference, formulas, regex, Python, SQL, plugins; gating, quarantine policy, output mutation, a new score, legacy validator changes; distributed execution or rows above the limits.
- Containment against malicious code already executing in the caller's process (use `run_pipeline_isolated`).
- A generic aggregate registry, serializer, unseal API, or engine-owned file, database, or HTTP transport.

### Definition of done

- Grammar, side-specific report, codec, collector, R-FAIL guard, and every V1 evaluator are public and tested.
- Route and FK manifests are executable test data. Supported cells are byte-equal to the resident oracle; refused cells perform zero reads; output bytes with invariants on equal bytes with invariants off on every route.
- Every F31 extension point in the table below exists in the built F31 and is used as listed.
- Engine, CLI, and Platform surfaces pass the marker corpus, field walk, exit matrix, barrier matrix, compatibility corpus, and schema snapshots.
- B-9 stores one sibling invariant section per committed attempt without changing score, grade, job outcome, or legacy payloads. The F35 fixture accepts only eligible output observations.

## F31 extension points (BLOCKER 1)

F31 is being revised. F36 assumes only what `origin/plan/f31-safe-output` at `a909118` promises, and names each mechanism. Every F36 slice that consumes a row first runs a reconcile check against the built F31: if the mechanism is absent or changed, the slice stops and asks the owner. F36 never amends F31, widens an analyzer exemption, or adds a directory exclusion.

| ID | F31 mechanism F36 uses (F31 plan line) | F36 use and owning slice |
|---|---|---|
| X1 | Domain family modules under `safe_output/families/` with named typed factories; "no generic public factory" (f31 :243-266, :270) and the documented review path for "a new event family" (f31 :768) | New engine module `safe_output/families/invariants.py` with factories `invariant_summary`, `invariant_not_run`, `invariant_artifact_written`, `invariant_route_refused`. Built in the engine (Slice 4), never in a consumer. CLI and Platform call these public factories; they mint nothing. |
| X2 | `SafeCount(name_enum, value)` with closed names (f31 :293) | Closed names `observations_pass`, `observations_fail`, `observations_indeterminate`, `observations_not_run`. No `SafeMeasurement` use. |
| X3 | F29 `ordinary_output: bool = False` and exact opt-in manifest (f31 :276) | Only the reasons marked "ordinary" in the reason table are opted in, in Slice 4. |
| X4 | Exact data-plane writer manifest with one threat-model entry per writer, no wildcards (f31 :66, :470), per-repository sink manifests (f31 :58, :448) | Four entries, each added in the slice that builds the writer: engine `InvariantSidecarLease.write_bytes` (Slice 8); CLI `publish_invariant_artifact` (Slice 10); Platform `persist_invariant_report` ORM write and `invariant_artifact_response` byte response (Slices 11a, 11b). Each accepts only an exact `SerializedInvariantReportV1`. RED: the shared analyzer flags each writer before its manifest entry exists. |
| X5 | Platform response-model gate: no `response_model`, approved byte adapters only (f31 :460, :671) | The artifact route returns a `Response` built only inside `invariant_artifact_response`, has no response model, and is listed in the Platform route manifest as data-plane. |
| X6 | `SafeEventSink`, `SafeOutputScope`, process-entry install (f31 :270, :356) | Used unchanged for all ordinary F36 events. |
| X7 | F31 changes ordinary serialization of `ExecutionResult` and `IsolatedRunResult` (f31 :228) | F36 field snapshots are taken on top of landed F31 engine Slice 5, never before. |

Not assumed: aggregate registry, generic unseal, `SerializedSafePayload` nesting of the artifact. The invariant artifact is data-plane bytes produced only by the F36 codec; it is value-free by F36's own proof (marker corpus), not by F31's.

## Failure rule R-FAIL (BLOCKER 2)

One rule, enforced at one place: **any F36 internal failure after config validation yields `not_run` plus a registered reason for the affected observations and never changes output bytes, publication, or job outcome.**

- Config time (pre-read) is the only place F36 may fail a run: grammar errors, limits that are static, probe use, direct chunked API use, unsupported OS for relationship checks. These raise the existing config error before any read.
- After config validation, every F36 call from route code goes through `InvariantRunGuard` (in `_collector.py`): `guard.feed(...)` and `guard.finalize(...)`. The guard catches `Exception` raised inside F36 frames only, marks affected observations `not_run` with the matching reason (`invariant.internal_failure` when no narrower code applies), emits `invariant_not_run` through F31, and returns normally. It never wraps route code, and `feed` accepts only materialized Arrow batches (no iterator or lazy frame), so a route read, mask, rewrite, or publish error never surfaces inside the guard and propagates exactly as today.
- At compile time the guard pre-encodes the all-`not_run` fallback report for the program. If that encode fails, config validation fails. Therefore a failure during assembly still returns a valid report and the fallback itself cannot fail at run time.
- `BaseException` that is not `Exception` (cancel, `KeyboardInterrupt`, `SystemExit`) propagates unchanged; F36 leases release in `finally`. The run's existing exit path owns the outcome.
- Failure kinds covered: Arrow kernel error, DuckDB error, scratch identity drift, procfs absence at run time, scratch or memory budget exceeded, 100,000,001 selected rows, drain or deadline timeout, sidecar decode failure, codec cap overflow, collector assembly error.
- Residual: a process OOM kill is not an exception. Bounded accumulators (acceptance 7) and the governor on isolated runs are the mitigation; in-process OOM stays a documented residual.

Proof (acceptance 15): per route, a fault injector raises each failure kind; output bytes, publish commit call, job result, and `quality_metrics` equal the no-F36 run, and the report shows `not_run` with the expected reason. Mutation A: guard re-raises, test goes RED on publication. Mutation B: guard swallows without a reason, RED on reason membership. Mutation C: guard widened around route code, RED on the "route exception still fails the run" cell.

## Binding design

### Closed grammar

```text
program        := {version: 1, checks: [check, 1..256]}
column_ref     := {table: configured name, column: configured name}
compare        := {kind: compare, position: u16, left, op: eq|ne|lt|le|gt|ge,
                   right, coverage: exact|sampled, sample_rows?}
null           := {kind: null, position: u16, column, op: is_null|is_not_null,
                   coverage: exact|sampled, sample_rows?}
null_pair      := {kind: null_pair, position: u16, left,
                   op: same|different|left_implies_right|right_implies_left,
                   right, coverage: exact|sampled, sample_rows?}
sum_equals     := {kind: sum_equals, position: u16, result, terms: [ref, 1..8],
                   absolute_tolerance: decimal, coverage, sample_rows?}
reconciliation := {kind: reconciliation, position: u16, parent ref, child ref,
                   op: sum|count, absolute_tolerance: decimal, coverage: exact}
checksum       := {kind: checksum, position: u16, column,
                   scheme: luhn|npi|iban|vin|isbn13|ean13|gtin, coverage, sample_rows?}
relationship   := {kind: relationship, position: u16,
                   members: [{relationship_position, child_position}, 1..8],
                   coverage: exact}
```

Pydantic models use `extra="forbid"`. `position` is `1..65535`. `sample_rows` is required only for sampled row-local checks. Tolerance is finite `Decimal` in `[0, 10^12]`. Names resolve once at compile and never enter report types. Relationship members must be the complete declared-order first-hit group for one child tuple.

### Hard sizes

| Constant | Value and meaning |
|---|---|
| `MAX_CHECKS` | 256 declarations |
| `MAX_RELATIONSHIP_MEMBERS` | 8 complete ordered members |
| `MAX_SAMPLE_ROWS` | 1,000,000 observed rows per sampled scope |
| `MAX_SELECTED_ROWS_PER_SIDE` | 100,000,000, independently per side; above it R-FAIL `invariant.row_limit` |
| `MAX_EVAL_BATCH_ROWS` | 65,536; larger Arrow batches are sliced before a kernel feed |
| `MAX_OBSERVATIONS` | 1,792: 256 relationship checks times 7 observations |
| `MAX_ARTIFACT_BYTES` | 16,777,216 canonical bytes for file, sidecar, DB, and HTTP alike |
| `MAX_DRAIN_SECONDS` | 300; finalize deadline is `min(MAX_DRAIN_SECONDS, remaining isolated timeout_s)` (`_isolated_run.py:151`) |

A relationship declaration emits source and output observations for `rel.pk.unique`, `rel.fk.coverage`, `rel.fk.orphan`, plus one transition observation `rel.join.cardinality_change`. A row-local or reconciliation declaration emits source, output, and transition. The maximum legal report must encode below `MAX_ARTIFACT_BYTES`, or Slice 0 stops and narrows the schema.

### Counts, status, verdict

```text
ExactRowCounts:   rows_total = rows_evaluated + rows_null_unknown + rows_unevaluable
                  rows_evaluated = rows_passed + rows_failed
SampledRowCounts: population_rows = observed_rows + unobserved_rows
                  observed_rows = observed_passed + observed_failed
                                + observed_null_unknown + observed_unevaluable
```

No sampled type has a population-failure field. Transition types use comparable, introduced, resolved, persistent-fail, persistent-pass, unknown, unevaluable buckets; the sampled variant adds `population_rows`, `observed_rows`, `unobserved_rows`.

Source or output scope: any known failure is `fail`; zero evaluated rows is `indeterminate` with `invariant.no_rows_evaluated`; no failure plus any unevaluable row is `indeterminate`; exact clean is `pass`; sampled clean is `indeterminate` even when `unobserved_rows=0`. Transition scope: introduced failure or changed parent multiplicity is `fail`; zero comparable rows or any unknown alignment is `indeterminate`; exact clean is `pass`; sampled clean is `indeterminate`. `not_run` and `unsupported` always pair with `indeterminate`, `counts=None`, and a registered reason.

### Frozen `/v1` enums and unions (MEDIUM 2)

| Name | Members, in wire order |
|---|---|
| `scope` | `source`, `output`, `transition` |
| `side` | `source`, `output` |
| observation `status` | `measured_exact`, `measured_sampled`, `not_run`, `unsupported` |
| `verdict` | `pass`, `fail`, `indeterminate` |
| report `status` | `complete` (all measured), `partial`, `not_run` (none measured) |
| report `coverage` | `exact`, `sampled`, `mixed`, `none` |
| check `code` | `row.compare`, `row.null`, `row.null_pair`, `row.sum_equals`, `row.checksum`, `rel.reconciliation`, `rel.pk.unique`, `rel.fk.coverage`, `rel.fk.orphan`, `rel.join.cardinality_change` |
| `structures` member | `ColumnStructureRef`, `RelationshipStructureRef` |
| `counts` member | `ExactRowCounts`, `SampledRowCounts`, `ExactTransitionCounts`, `SampledTransitionCounts`, `RelationshipCounts`, `JoinChangeCounts`, `None` |

### Reasons (MEDIUM 3)

New F29 namespace profile `invariant(owner)` in `src/decoy_engine/reasons/_catalog.py` (the file is 170 lines and has no `invariant` profile), declared like `verification` at `_catalog.py:134-146` (kind finding, outcome findings). The registry takes the next minor version after landed F31 (`REGISTRY_VERSION` at `reasons/_registry.py:24`; the added-identity rule at `tests/unit/test_reason_registry.py:70-72` and `:150-157`). All codes are engine-declared, including the two Platform-detected ones (owner `platform`).

| Code | Raised by | Ordinary (X3) |
|---|---|---|
| `invariant.probe_unsupported`, `invariant.chunked_api_unsupported`, `invariant.platform_unsupported` | config time | yes |
| `invariant.internal_failure`, `invariant.row_limit`, `invariant.scratch_budget`, `invariant.deadline`, `invariant.feed_rejected` | R-FAIL | yes |
| `invariant.no_rows_evaluated`, `invariant.no_source_side`, `invariant.alignment_lost` | verdict rules | no (report only) |
| `invariant.report_conflict`, `invariant.report_not_persisted` | Platform (owner `platform`) | yes |

### Collector lifecycle (HIGH 1)

`InvariantCollector` owns one lock, one condition, `inflight_feeds`, `close_pending`, `claimant`, `drain_expired`, bounded accumulators, and this table. Admission increments `inflight_feeds` under the lock; kernel and DuckDB work runs outside it; merge and decrement run under it in `finally`, then notify. Merge applies only when state is `OPEN` or `DRAINING` and `drain_expired` is false; otherwise the result is discarded and still decremented. No callback, query, codec, or sink runs under the lock.

| State | `feed` | `finalize` | `close` | claimant cancelled (`BaseException`) | deadline expires |
|---|---|---|---|---|---|
| `OPEN` | admit iff not `close_pending`; else reject | claim, become `DRAINING` | set `close_pending` (rejects all new feeds); `CLOSED` when inflight is 0 or at deadline | n/a | n/a |
| `DRAINING` | reject | reject (one claimant) | set `close_pending`; claimant returns no report; `CLOSED` at inflight 0 or deadline | `CLOSED`, leases released, exception propagates | set `drain_expired`; enter `FINALIZING`; observations with an unmerged feed become `not_run` `invariant.deadline` |
| `FINALIZING` | reject | reject | set `close_pending`; report is still returned to the claimant, then `CLOSED` | `CLOSED`, no report | relational queries interrupted; affected observations `not_run` `invariant.deadline` |
| `FINALIZED` | reject | reject | release, `CLOSED` | n/a | n/a |
| `CLOSED` | reject | reject | no-op | n/a | n/a |

A rejected feed increments `rejected_feeds`; when rejection happens before snapshot, affected observations are `not_run` `invariant.feed_rejected`. The route matrix asserts `rejected_feeds == 0` on every supported cell. `close` is called only from route exit paths, so a close without a report means the run is not publishing.

### Row and relationship semantics

Comparison and sum kernels use lossless Arrow common types only. Checksums call `decoy_engine.checksums.validate` (`checksums.py:351-369`, one Python call per value). Deterministic sampling selects `m=min(sample_rows,population_rows)` midpoint positions `floor(((2k+1)*population_rows)/(2m))` and aligns source and output on those positions.

Relationship semantics reuse `relationships/_graph.py:134-204` (declared-order first-hit at `:167`, `:177`) and these validator truth definitions, never their Python-set implementations, which grow with rows (LOW 1):

- Null parent tuples are excluded from the parent key set: `_parent_pk_set` (`validators/_fk_validators.py:72-114`).
- An all-null child tuple is a null FK, a partial-null tuple is an ordinary unmatched tuple: `_child_fk_values` (`_fk_validators.py:116-142`, rule at `:140-141`).
- `rel.fk.coverage` follows `validate_fk_intact` (`_fk_validators.py:150`, null FK ignored); `rel.fk.orphan` follows `validate_no_orphan_children` (`_fk_validators.py:235`, null FK is an orphan).
- `rel.reconciliation` follows `validate_reconciliation_holds` (`validators/_relationship_checks.py:199-215`) grouping, but uses absolute `Decimal` tolerance, not that validator's `isclose`-style default `1e-6`. The divergence is documented in the module docstring.

Duplicate non-null parents stay visible and make only join change indeterminate. Parent ordinals are `(member_position, row_position)` and never leave the evaluator. Quarantine alignment happens before side and transition collection.

### Streamed source feed (HIGH 3)

Each route feeds the source side at the read it already performs; F36 adds no source read.

| Route | Source feed point | Per-row transition carrier |
|---|---|---|
| Resident (full-frame, mixed, multi-table, unified) | resident source table after load | in-memory aligned Arrow verdict arrays, bounded by `MAX_EVAL_BATCH_ROWS` slices |
| Sequential and auto-chunk streamed | the raw batch in hand before masking, same iteration as its output batch | per-batch verdict array, aligned by in-batch ordinal, then quarantine-aligned |
| OOC reorder | phase 1 raw batch, the only raw read (`out_of_core/_stream_driver.py:11-23`) | 2-bit verdict per row per row-local check, spilled as Arrow IPC under `InvariantScratchLease`, keyed by the same `row_nr` that phase 3 asserts (`_stream_driver.py:32-37`) |
| OOC resident and batch-join | the batch where the raw child is read | same spilled carrier |

Relationship keys for both sides are staged into DuckDB scratch at the same reads. Spill size counts against the scratch budget; exceeding it is R-FAIL `invariant.scratch_budget`. Acceptance 8 counts source reads per table and requires the count with invariants on to equal the count with invariants off.

### Scratch capability

`InvariantScratchLease` retains root and child descriptors, creates fixed names relative to a dirfd with exclusive non-following opens, checks type, owner, device, inode, and link count, and gives Arrow duplicated file objects. DuckDB receives only a proven live `/proc/self/fd/<fd>` bridge plus configured `memory_limit`, `temp_directory`, and `max_temp_directory_size`. Today `out_of_core/_duckdb.py:42` sets only `temp_directory` and `memory_limit`, so the temp-size cap is new configuration, not reuse. It streams aggregates, never keys. The Slice 3a spike must also prove that `DuckDBPyConnection.interrupt()` stops a running query (deadline column above); if not, Slice 3a stops for the owner.

**Platform matrix (MEDIUM 7).** Row-local checks run on every OS. Relationship and reconciliation checks are Linux-only in V1; elsewhere they are `unsupported` with `invariant.platform_unsupported` decided at config time, before staging. Relationship cells of the parity matrix are Linux-only and labelled so; non-Linux CI, where present, asserts the `unsupported` cells.

### Artifact and isolation ownership

`encode_invariant_report_v1` and `decode_invariant_report_v1` are schema-specific engine functions: exact types, closed fields, canonical order, duplicate-key rejection, bounded integers, F29 membership, UTF-8, byte cap, byte-for-byte re-encode equality. `SerializedInvariantReportV1` is an opaque exact type with no mapping protocol or generic unseal. `measure_invariants` is a normal in-process API over typed Arrow inputs; callers with a hostile-code threat model use `run_pipeline_isolated`. F36 builds only the engine sidecar lease; the CLI owns its file publish, Platform its DB write and HTTP response, F31 every ordinary event.

### Platform persistence contract (HIGH 2)

Table `job_invariant_reports`, one row per attempt (V1 produces exactly one report per run, so there is no section position):

| Column | Contract |
|---|---|
| `attempt_id` | PK; FK to `job_attempts.id` `ON DELETE CASCADE`, the same pattern as `job_outputs.attempt_id` (`api/models.py:1187-1190`) |
| `schema_version` | Fixed `decoy-invariant-report/v1` |
| `artifact_bytes` | Non-null binary, `1..MAX_ARTIFACT_BYTES`, exact canonical bytes |

- Fencing: attempts are fenced generations (`JobAttempt`, `uq_job_attempts_job_generation` and one non-terminal attempt, `api/models.py:971-995`). A retried generation is a new attempt and a new key, so a changed source never conflicts with an older report. The write happens only while the writing attempt still holds the fence, checked in the same transaction by the existing fence check that publication uses; Slice 11a cites that function before any code or stops for the owner.
- Call site and transaction: a fourth best-effort hook in `_run_post_success_report_hooks` (`api/jobs/runner.py:289-335`), called from `run_job` and from the worker (`api/jobs/dispatch_worker_main.py:505`). It follows the quality hook pattern exactly: own session commit (`api/quality/runner_hook.py:273`), every `Exception` caught and logged through F31 without text (`runner_hook.py:141-153`), never raised into job completion.
- Same-attempt second write with identical bytes is a no-op; with different bytes it keeps the first row and records `invariant.report_conflict` as an F31 event. Neither changes job outcome (R-FAIL).
- Read: the report of the attempt whose outputs are committed. No row returns the fixed `invariant.report_not_persisted` body. Owner-or-admin authorization applies before read. Downgrade drops only this table.

## Defect-class closure table

| # | Class | Closing structure | Non-vacuous RED-first proof | Required mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | F36 codec plus F31 extension points X1-X5; no value-derived numbers (no measurement field) | Marker corpus incl. numeric markers through every surface; F31 analyzer over the four X4 writers | Re-add a deviation measurement; drop one X4 manifest entry |
| 2 | Cleanup on every exit | Scratch lease, sidecar lease, CLI file owner, Platform hook session; R-FAIL guard | Owner by exit matrix plus R-FAIL fault matrix per route | Remove one `finally`, or make the guard re-raise |
| 3 | State and concurrency | Lock, condition, inflight leases, `close_pending`, claimant, deadline; attempt fence | Barrier matrix generated from the six-column table incl. starvation, claimant cancel, deadline | Let `OPEN` admit while `close_pending`; drop claimant cancel transition |
| 4 | Route parity and silent fallback | Executable capability manifest; R-FAIL reasons; Linux matrix | Route matrix asserts telemetry, nonempty observations, bytes, zero-read refusal, `rejected_feeds==0` | Delete one route hook or one R-FAIL reason |
| 5 | Referential integrity | FK-shape by route disposition table | Relational oracle per shape per route with expected status and route | Drop one key component; omit a later batch |
| 6 | Untrusted producer | Exact frozen types, bounded codec, fixed errors, closed enums | Field walk and hostile artifact corpus | Replace an exact type check with `isinstance` |
| 7 | TOCTOU (check then use) | Descriptor-anchored scratch capability and identity checks | Symlink, rename, hard link, fd reuse, absent procfs, bridge drift | Use one bare path; remove post-query check |
| 8 | Weak evidence | Non-vacuity counters; zero-evaluated is indeterminate; same-lock RED logs | All-quarantined output is not `pass`; reviewer reruns baseline | Remove the zero-evaluated rule |
| 9 | Refactor drift | Characterization first; census ratchet-down only | Placement mutations for full-frame, unified, OOC; read counter | Move full-frame hook above `_pipeline_full_frame.py:194` |
| 10 | Frozen drift | Enum table, field ledger, dump-shape rule, schema snapshots | Snapshot and old-reader fixtures; `config_digest` unchanged | Emit `"invariants": None` in the default dump |

## Reuse inventory

| Existing capability | Binding decision |
|---|---|
| F29 registry (`src/decoy_engine/reasons/`) | New `invariant` profile and minor bump only (Reasons section). |
| F31 extension points X1-X7 | Exactly as tabled; nothing else from F31. |
| Strict `PipelineConfig` (`config/_pipeline.py:45-99`) | One optional strict block; dump shape unchanged when omitted (MEDIUM 6 list). |
| Relationship config and graph (`config/_relationships.py:24-49`, `relationships/_graph.py:134-204`) | Resolve the existing graph; no second graph, no change to first-hit or cycle rules. |
| Validator and checksum truth definitions | Semantics as cited in Row and relationship semantics; never value-bearing result shapes or Python-set implementations. |
| `OutputPublish` and transactional sinks | F36 runs before the existing commit and adds no output-data sink. |
| Platform quality hook pattern (`api/quality/runner_hook.py:119-153`, `:273`) | Copied for the invariant hook. |
| `tests/sentry/test_module_size.py`, `test_public_import_boundary.py`, `test_physical_seam_disconnection.py` (narrow role only) | Unchanged; census rule in Slices. |
| `tests/sentry/test_validate_no_mutation.py`, `test_methodology_citations.py`, `test_capability_matrix.py` (LOW 3) | `measure_invariants` must pass all three; new evaluator modules carry methodology citations (Great Expectations result formats, PostgreSQL null comparison, Deequ analyzers, OpenLineage assertion identity, Python `dir_fd`, DuckDB configuration, `proc_pid_fd(5)`). |

## Code-filled enumerations

### Execution routes and hook placement (HIGH 5)

Dispatch: `execution/_pipeline.py:313-320` sends to `_pipeline_route_dispatch.py:27` (sequential), `:55` (out-of-core), `:80` (mixed), else `_pipeline_full_frame.run_full_frame_route` (`_pipeline_full_frame.py:86`).

| Cells | Grounding and binding placement |
|---|---|
| Full-frame resident, pure mask, pure generate, generate then mask | `_pipeline_full_frame.py:86-244`. Collect after `finalize_validators_and_quarantine` (`:194-200`) and before `publish.commit()` (`:243`). Never inside `run_generate_and_mask_steps` (`:123`), which runs before quarantine. |
| Unified slice (early return at `_pipeline_full_frame.py:111`) | `_unified_slice.py:275-350`; collect after `finalize_validators_and_quarantine` at `:314` |
| Sequential route executor | `_pipeline_route_exec.py:114` (`run_sequential_route`) into `_sequential.py:531-638`; final rewritten, quarantine-aligned batches before commit |
| Out-of-core route executor | `_pipeline_route_exec.py:195` (`run_out_of_core_route`) into `out_of_core/_runner.py:125-341,450-531`; output after FK rewrite, before commit; source per HIGH 3 table |
| Mixed and runtime decline | `_pipeline_route_exec.py:595` and `_pipeline_mixed_route.py:148-282`; the same collector carries through decline |
| Independent multi-table | `_pipeline_multi_table.py:260-402`; one collector, one finalize |
| Auto-chunk dispatcher and legacy | `_pipeline_auto_chunk.py:218-350,395-450`; one run collector |
| Isolated false/true | `_isolated_run.py:141-243,409-622`; in-process parity or sidecar |
| Governor | `_governor.py:243-436`; only the final completed `IsolatedRunResult` carries a report |
| Probe | `_probe.py:222-352`; config-time refusal `invariant.probe_unsupported` before spawn |
| Direct public chunk iterators | `native/_chunked_entry.py:601` (`run_mask_chunked`, file is 681 lines), `_chunked.py:369-449`; refuse before first pull |
| Direct `PandasExecutionAdapter.run` | `_pandas_adapter.py:105`, `run` at `:163`; config-unaware; callers use `measure_invariants` |

### Owned state and exit paths

| Owner | Every required exit |
|---|---|
| `InvariantCollector` and `InvariantRunGuard` | every cell of the six-column table; each R-FAIL kind; repeated close |
| `InvariantScratchLease` | capability refusal, open failure, bridge mismatch, Arrow or DuckDB failure, budget, interrupt, success, cancel, cleanup failure, repeated close |
| `InvariantSidecarLease` | create, write, fsync, close, decode failure, child crash, timeout, malformed envelope, parent discard and commit, cleanup failure, repeated dispose |
| Existing `OutputPublish` and sinks (`_sequential.py:23-63`) | unchanged; R-FAIL proves commit is called exactly as without F36 |
| Route owners | resident and sink success, decline, validation failure, stream error, exception before and after commit |
| Governor and probe | failed rung, later success, exhaustion, callback failure; probe zero spawn |
| CLI file owner | refusal, stage, write, fsync, rename failures, success, cancel, cleanup failure |
| Platform invariant hook | fence lost, same-bytes retry, conflict, DB error, rollback, cascade, migration up and down |

### FK shapes by route (MEDIUM 5, LOW 2)

R = resident routes (full-frame, unified, mixed, multi-table), S = sequential and auto-chunk, O = out-of-core. Default disposition is `measured_exact` on the route the run actually took, with route telemetry asserting that route.

| Shape | R | S | O |
|---|---|---|---|
| Single-column cross-table; composite all-present, partial-null, all-null | measured | measured | measured |
| One parent many children, child in a later batch | measured | measured | measured |
| Same parent table, different child tuples; multi-parent first-hit group | measured | measured | measured |
| Self-reference, parent in a later batch | measured | measured | OOC rejects (`out_of_core/_compat.py:478-492`); run falls back to full-frame; report measured, telemetry `full_frame` |
| Multi-hop chain, diamond fan-in, junction with two parents | measured | measured | measured |
| Zero, one, many multiplicity; duplicate and null parents; unmatched; empty child | measured (duplicates make join change `indeterminate`) | same | same |
| Generate-parent to mask-child | source scope `not_run` `invariant.no_source_side` for the parent; output measured; transition `not_run` | same | same |
| Generate-child to mask-parent; cycle | existing config refusal, zero reads | same | same |
| Direct public chunked API | n/a | config refusal `invariant.chunked_api_unsupported`, zero pulls | n/a |

### Public result fields and defaults (HIGH 4)

Frozen, slotted, exact dataclasses, no inheritance, fields in order:

| Type | Fields |
|---|---|
| `CheckRef` | `code`, `position` |
| `ObservationRef` | `check_ref`, `scope` |
| `ColumnStructureRef` | `side`, `table_position`, `column_positions` |
| `RelationshipMemberRef` | `relationship_position`, `child_position` |
| `RelationshipStructureRef` | `side`, `members` |
| `ExactRowCounts` | `rows_total`, `rows_evaluated`, `rows_passed`, `rows_failed`, `rows_null_unknown`, `rows_unevaluable` |
| `SampledRowCounts` | `population_rows`, `observed_rows`, `observed_passed`, `observed_failed`, `observed_null_unknown`, `observed_unevaluable`, `unobserved_rows` |
| `RelationshipCounts` | `parent_rows`, `child_rows`, `null_parent_key_rows`, `duplicate_parent_key_groups`, `duplicate_parent_key_rows`, `null_child_key_rows`, `matched_child_rows`, `unmatched_child_rows`, `parent_zero_match_rows`, `parent_one_match_rows`, `parent_many_match_rows` |
| `ExactTransitionCounts` | `comparable_rows`, `introduced_failures`, `resolved_failures`, `persistent_failures`, `persistent_passes`, `unknown_transitions`, `unevaluable_transitions` |
| `SampledTransitionCounts` | `population_rows`, `observed_rows`, `introduced_failures`, `resolved_failures`, `persistent_failures`, `persistent_passes`, `unknown_transitions`, `unevaluable_transitions`, `unobserved_rows` |
| `JoinChangeCounts` | `comparable_parents`, `changed_parents`, `increased_parents`, `decreased_parents`, `lost_all_children`, `gained_from_zero`, `unchanged_parents` |
| `InvariantObservation` | `observation_ref`, `structures`, `status`, `verdict`, `reason`, `counts` |
| `InvariantReportV1` | `schema_version`, `status`, `coverage`, `observations` |

V1 has no measurement field: every candidate (maximum deviation, residual sum) is value-derived and is dropped. Every integer is a row or parent count bounded by `MAX_SELECTED_ROWS_PER_SIDE`. Enum and union members are the frozen table above.

`ExecutionResult` (`_adapter.py:61-71`) gains trailing `invariant_report=None`. `IsolatedRunResult` (`_isolated_common.py:362-388`) gains trailing `invariant_report=None`. Unchanged: `GovernorTripRecord` and `GovernorResult` (`_governor.py:170-211`), `ProbePoint` and `ProbeResult` (`_probe.py:133-172`). `SerializedInvariantReportV1` is opaque with no public fields.

**Default config dump consumers (MEDIUM 6).** Producers: `config/_pipeline.py:50`, `config/_override.py:65`. Consumers that require the default-inclusive dump: `generation/_reproducibility.py:204` (hashes the whole dump into `config_digest`), `execution/capacity.py:219`, `execution/_job_estimate.py:82`, `execution/_isolated_run.py:158`, `execution/_pipeline.py:12`, `verification/postmask/runner.py:53`, `verification/postmask/residual_pii.py:139`, `profile/_source.py:57`, `profile/_fixed_width_reader.py:148`, `plan/_checks_dp.py:66`. Rule: when the block is omitted, `model_dump()` omits the `invariants` key, so every dump and every `config_digest` is byte-identical to today. A characterization test pins the digest of every golden config before Slice 0 code.

### Output surfaces

| Surface | F36 disposition |
|---|---|
| Direct API, object `repr` | Exact typed report only |
| Canonical bytes, isolated sidecar | F36 codec, one 16 MiB cap, X4 writer |
| CLI stdout, stderr, JSON, quiet, verbose, help, parse errors, root callbacks | X1 factories via X6; quiet is silent; findings and R-FAIL exit 0 |
| CLI requested file | X4 `publish_invariant_artifact`; path never printed |
| Engine logs, warnings, progress, errors | X1 events only |
| Governor, probe | Existing fields unchanged; failed rungs carry no report |
| Platform DB row, artifact HTTP body | X4 writers; X5 route without response model |
| FastAPI 404, 422, framework exceptions, OpenAPI | F31 process boundary; hostile path, query, body tests |
| Platform logs, job events, audit, evidence, telemetry, notifications, support bundles | F31 events only; no F36 payload; B-9 gets one sibling section |

## Frozen contracts touched

| Contract | Classification and proof |
|---|---|
| Pipeline V1 config | Additive optional block; omitted block leaves dump and `config_digest` byte-identical |
| `run_pipeline`, route choice, output bytes | Unchanged with or without invariants (R-FAIL and on/off byte equality) |
| `ExecutionResult`, `IsolatedRunResult` | Additive trailing defaulted field; snapshot on top of F31 (X7) |
| Governor and probe records | Unchanged; snapshots |
| Direct chunked APIs | Config refusal before first pull; shapes unchanged |
| `decoy_engine.__all__`, F29 registry | Exact additive names; new profile; next minor |
| F31 `decoy-safe-output/v1` | One new family and closed names via X1-X3; artifact never nested |
| `quality-report/v1`, grade, legacy validators | Unchanged; no invariant `quality_metrics` key |
| `decoy-invariant-report/v1` | New; enum table, field order, limits, canonical bytes frozen; change needs `/v2` |
| CLI commands and exits | Additive; findings and F36 failures exit 0 |
| Platform tables and endpoints | Additive table keyed by attempt; owner-scoped endpoint; legacy routes unchanged |

## Acceptance tests, all RED first

1. **Grammar and identity:** valid block rejected today; implementation rejects extra keys, expression shapes, duplicates, partial groups, 257 checks; reorder and renames preserve refs.
2. **Scoped verdicts:** source fail/output pass and inverse, transitions, pure generate, alignment loss give independent verdicts.
3. **Balance:** every bucket nonzero across fixtures; equations hold. `sample_rows < population` gives `unobserved_rows>0`; `sample_rows >= population` gives `unobserved_rows==0` and clean is still `indeterminate`.
4. **Truth tables:** every operator, tolerance, overflow, null, non-finite, type, and checksum scheme; a spy proves `checksums.validate` reuse.
5. **Collector races:** barrier matrix generated from all six table columns, plus feed/feed.
6. **Relationships:** every FK shape against an independent oracle, including later batches and changed multiplicity at constant row totals.
7. **Scratch:** every TOCTOU evasion refuses before staging; low limits spill within budget; no Python collection grows with rows; `interrupt()` honoured.
8. **Route product:** every capability cell at batch sizes 1, 7, 65,536; canonical bytes, route telemetry, source-read count equal to invariants-off, output bytes equal to invariants-off, `rejected_feeds==0`; refusals record zero reads.
9. **Process ownership:** isolated parity, child crash, timeout, malformed envelope, oversize sidecar, governor failures then success, exhaustion, probe zero spawn, direct chunk zero pull.
10. **Artifact corpus:** maximum legal report under 16 MiB; every schema, order, bound, UTF-8, duplicate-key, trailing-byte, reason, and size mutation rejected with no partial report.
11. **Selected scope:** subset and quarantine differ from a deliberately wrong full-source oracle.
12. **Platform:** fence-lost write refused, same-bytes no-op, conflict recorded and job still succeeds, rollback, cascade, authorization, no-report read, migration round trip, exact bytes, 404, 422, OpenAPI, response-model bypass.
13. **B-9 and F35:** one section per committed attempt; score, grade, outcome, legacy payload fixed; only eligible output observations accepted.
14. **Scale:** 100,000,000 narrow rows within budgets; 100,000,001 on either side yields R-FAIL `invariant.row_limit` before staging; checksum throughput at 10M rows recorded as evidence (LOW 5).
15. **R-FAIL fault matrix:** see the R-FAIL section.

Generated tests (field walk, exit matrix, barrier matrix, marker corpus with raw cell, key, name, path, DSN, credential, exception, class-name, Unicode, short, numeric, UUID-shaped, SQL-shaped, and control-character markers, route by FK product, balance, artifact corpus, R-FAIL matrix) each first assert the marker is in input and a positive visit or emission count.

## Hostile corpus: ten gate breaks, each with a RED-first test

| # | Break (review section 5) | RED-first test | Slice |
|---|---|---|---|
| 1 | CLI prints a summary with no F31 factory | CLI test calls only `families.invariants`; analyzer RED on any CLI-local event construction | 4, 10 |
| 2 | Sidecar, CLI file, or Platform byte writer is an unregistered sink | Analyzer run before each X4 entry exists is RED, green after | 8, 10, 11a, 11b |
| 3 | DuckDB error drops publication or is swallowed | R-FAIL matrix: DuckDB fault keeps publication and records `invariant.internal_failure` | 1, 5-7 |
| 4 | Close starvation, stranded `DRAINING` | Barrier rows: feed after `close_pending` rejected; claimant cancel reaches `CLOSED`; deadline row | 1 |
| 5 | Retried attempt conflicts and fails the job | Two generations with different source bytes store two rows; same-attempt conflict records a reason and job succeeds | 11a |
| 6 | OOC source scope re-reads source | Source-read counter equals invariants-off | 7 |
| 7 | Numeric marker surfaces as a measurement | Numeric cell marker larger than every fixture count absent from report, DB, HTTP; mutation re-adding deviation turns RED | 0, 2 |
| 8 | Full-frame hook before quarantine | Quarantine fixture where pre-quarantine counts differ; mutation moves hook into `run_generate_and_mask_steps` | 5 |
| 9 | All-quarantined output reports exact pass | Zero evaluated rows gives `indeterminate` and F35 fixture refuses it | 2, 11b |
| 10 | Self-reference on OOC claims OOC measured it | FK-by-route table cell asserts telemetry `full_frame` and `measured_exact` | 7 |

## Implementation slices

New modules have a 450-line plan ceiling and need no census entry. Census rule (MEDIUM 1): existing `tests/sentry/test_module_size.py` entries may only ratchet down to the exact new LOC; no new entries and no upward bumps. Hook sites already on the census: `_sequential.py` 696 (`:120`), `out_of_core/_runner.py` 681 (`:124`), `out_of_core/_stream_driver.py` 609 (`:126`), `native/_chunked_entry.py` 681 (`:100`), `_chunked.py` 627 (`:96`), `_isolated_run.py` 622 (`:68`). `_pipeline_route_exec.py` is at 600 and must stay at or below 600. Each hook is a single guard call; extract first where needed. Every slice writes its RED tests first, implements, reruns prior parity, mutates its mechanism once, then gets adversarial review.

- **Slice 0** (engine): models, limits, enum table, codec, dump-shape rule and digest characterization, import sentry. RED: acceptance 1, 10, field walk, break 7 schema half.
- **Slice 1** (engine): collector table and `InvariantRunGuard` with fake accumulators and the precomputed fallback. RED: acceptance 5, breaks 3 and 4 (unit level).
- **Slice 2** (engine): row compiler, kernels, sampling, count types, zero-evaluated rule. RED: acceptance 2-4, breaks 7 and 9.
- **Slice 3a** (engine): dirfd, procfs, DuckDB bridge and `interrupt()` spike, scratch lease, cleanup matrix. RED: acceptance 7. Stop if the spike fails.
- **Slice 3b** (engine): relationship and reconciliation evaluator, first-hit groups, aligned ordinals. RED: acceptance 6.
- **Slice 4** (engine; needs F31 engine gated): X1-X3 family module, reasons opt-in, reconcile check. RED: break 1 at engine level.
- **Slice 5** (engine): full-frame, unified, mixed, multi-table hooks. RED: route cells, break 8, R-FAIL cells.
- **Slice 6** (engine): sequential and auto-chunk hooks, direct chunk refusal. RED: route cells, R-FAIL cells.
- **Slice 7** (engine): out-of-core source carrier and hooks, scale. RED: breaks 6 and 10, acceptance 14.
- **Slice 8** (engine): sidecar lease (X4), governor, probe. RED: acceptance 9, break 2 engine writer.
- **Slice 9** (engine): compatibility, snapshots after F31 (X7), docs, release identity from current lineage.
- **Slice 10** (`decoy` CLI): thin `report invariants`, X4 file publisher, exact pin. RED: breaks 1 and 2 CLI cells.
- **Slice 11a** (`decoy-platform`): migration, ORM, fenced hook, X4 DB writer. RED: acceptance 12 persistence half, breaks 2 and 5.
- **Slice 11b** (`decoy-platform`): artifact endpoint (X5), B-9 section, F35 fixture, framework surfaces. RED: acceptance 12 API half, 13, break 9.

## Verification and worker evidence rules

Per slice record repository and lock identities, the RED assertion, green commands, changed files with before and after line counts, non-vacuity counters, the mutation killed, checks not run, and review findings. `scripts/dev/bootstrap.sh` and `gates.sh` do not exist in `decoy-engine` at baseline (LOW 4); until they land, baselines come from `uv sync --locked` then the named `pytest`, `ruff`, and `mypy` commands in the branch's own environment, and the evidence says so.

- Name the defect class of every finding and fix the class structure, not one instance.
- Write generated tests first and record the behavioral RED, not an import error.
- Attack your own diff: mutate each new mechanism once and show the agreed test turns RED.
- Run baselines with `uv.lock` in the same environment as the branch. State omissions.
- Never widen a threshold, add a census entry, skip a test, reduce a budget, or weaken an assertion.
- If a defect of an already catalogued class reappears, stop and report it. Do not patch a third time.
- No worker merges, publishes, enables F35, or exposes a raw diagnostic without owner approval.

## Pre-mortem

The class table and the ten gate breaks above are the pre-mortem; the three known third-round risks are the Slice 3a spike passing on one host and failing on the gate host (stop, do not patch), F31 changing an X row during its own revision (reconcile check stops the slice), and the Platform fence function not being reachable from the post-success hook (Slice 11a stops for the owner).

## Open questions for the owner

1. Linux-only relationship and reconciliation checks in V1 (MEDIUM 7): accept, or require a non-procfs bridge first?
2. Flag-off `run_job` runs without a `JobAttempt`: persist nothing (`invariant.report_not_persisted`, proposed) or key by job?
3. Checksum exact coverage above 10M rows: keep exact with recorded throughput (proposed), or cap and require sampled?
4. Engine-declared ownership of the two Platform-detected reason codes (proposed) versus a Platform-side registry.
