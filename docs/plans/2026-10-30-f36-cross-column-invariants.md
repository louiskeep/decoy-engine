Status: plan only, revision 5 (folds the Dennis plan review under the V1 bar: GO-WITH-FIXES, 1 V1 finding, 2 MEDIUM, 3 LOW; Q2 persistence decided by the orchestrator under owner delegation). Status: handoff. This document grants no implementation, release, merge, or publication approval.

Baseline: `decoy-engine` plan branch `plan/f36-cross-column-invariants` with `origin/main` `0173c054` merged in (merge `92f4b83`); `decoy-platform` `origin/main` `d8908f4`. Engine line numbers below are still the revision 4 reads at `fdd08a89` unless marked "(at 0173c05)"; the merge changed many cited engine files (listed in "Revision 5 baseline drift"), so every slice's reconcile check re-reads its cited lines at its own head before RED and stops on a mismatch it cannot place. Platform persistence lines (the "Platform persistence contract") are re-read at `d8908f4`. Module sizes in "Implementation slices" are re-measured at the merged head with `wc -l`.

Plan contract check: `plan-lint plan <file>` was **not run**: no `plan-lint` binary exists in this environment, `/home/cam/dev-rules/` is absent, and the PyPI package named `plan-lint` (0.0.3, `cirbuk/plan-lint`) is an unrelated JSON agent-plan linter, so it was downloaded only to inspect its metadata and not executed. The done-criterion "plan-lint OK" is therefore unmet and the next worker who has the real tool must run it and record the output. Checked by hand instead: status line, baseline identities, risk class, FRAME, V1 rules with named limits, binding design, one persistence design, defect-class table, named acceptance tests with seeded violation, fail-before location and killing mutation, every slice listed with an estimated production-file count and changed-line count within the per-slice bound, worker evidence rules, pre-mortem, owner questions with defaults, V2-LOG.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, scope-discipline, reuse-first, data-and-migrations, and the testing rulebook (named as the task requires; `/home/cam/dev-rules/` is absent here, so their text was not re-read in this revision), plus repository `CLAUDE.md`, `CODEMAP.md`, `CONTRIBUTING.md`.

Risk: **R2** (plan only). Public configuration, result fields, a versioned artifact, bounded relational scratch, concurrency, and one Platform migration. Owner approval, RED-first evidence, and adversarial review per slice.

# F36: Cross-column invariants and relational fidelity

## FRAME

### Problem

The engine has point validators and marginal fidelity reports, but no closed, bounded contract for declared row relations or join-cardinality change. Existing validator reports carry names, row positions, prose, and free mappings. F36 needs stable value-free identities, exact or honestly sampled counts, route parity, and a report-only seam for B-9 and future F35. The source/output ordering prerequisite exists at `src/decoy_engine/execution/_runner.py:234-378` with tests at `tests/unit/execution/test_cross_column_ordering.py:83-233`; F36 keeps them and does not reimplement ordering.

### Guarantee: V1 rules 1 to 3, a ratchet plus named limits

The guarantee is a ratchet plus named limits. The ratchet: every named test in "Acceptance tests" only tightens, census entries only shrink, and no threshold or allowlist widens. The named limits are listed under each rule. Nothing outside the rules and their limits is claimed.

**V1 rule 1, product function.** F36 never changes what a run does. Findings and F36 internal failures alike leave output bytes, quarantine, publication, job success, quality grade, and legacy `quality-report/v1` unchanged; any F36 failure after config validation becomes `not_run` plus a registered reason ("Failure rule R-FAIL"). The only place F36 may stop a run is config time, before the first read. Output with invariants on is byte-equal to output with invariants off on every supported route (A8a). Every observation is either measured with exact or honestly sampled counts, or `not_run`/`unsupported` with a reason; zero evaluated rows and sampled-clean are never `pass` (A3c, B9). Named limits: RF-OOM (a process OOM kill is not an exception) and RF-T (feed time counts against an isolated run's `timeout_s`; only finalize is bounded, "Finalize deadline").

**V1 rule 2, data.** F36 adds no output-data writer and runs before the existing commit. The isolated sidecar is written temp-then-rename-then-envelope, so a kill never leaves a partial report (A16d). Platform persists one append-only row per run hook in one table, `job_invariant_reports`, keyed by a non-unique `job_id` FK with `ON DELETE CASCADE`. The write is fenced by an attempt ordinal under a row lock, and the reader returns only the current run's latest row, so a late hook from run N can never be read as run N+1's report (A12j, A12k). Named limit: a job relabelled from `success` to `failed` after its hook (the flag-on F4 path, `api/jobs/runner.py:157-174` at `d8908f4`) keeps its row, but the reader hides it.

**V1 rule 3, values.** Check cell values never appear in reports: a report holds counts, closed enums, and positions only, and the grammar has no value literals (no constants, literal sets, expressions, SQL, or regex; `absolute_tolerance` and `sample_rows` are configuration bounds that are never compared against as cell values and never enter a report), so there is no value for a report to echo. Every integer in a report is a row or parent count bounded by `MAX_SELECTED_ROWS_PER_SIDE`, and V1 has no measurement field (no deviation, residual, or min/max). The non-admin surfaces covered are the typed report and its `repr`, canonical bytes and the isolated sidecar, CLI stdout, stderr, JSON and the requested file, the Platform DB row, the artifact HTTP body, the B-9 section, user-visible `Job.log`, job events, and notifications. None of them carries a cell, key, row index, configured name, path, DSN, query, `str(exc)` text, or value-derived number (marker corpus and field walk). Named limits: server-admin logs, crash output, traceback locals and `__cause__` chains are V2 ("V2-LOG").

Design properties that serve the rules (unchanged from revision 4): the language is a closed tagged union; every identity is `ObservationRef(CheckRef(code, authored_position), scope)` with scope `source`, `output` or `transition`, stable under reordering, and duplicates fail before the first read; scopes are separate observations; F35 eligibility is only `scope=output`, `status=measured_exact`, `verdict=pass`, `rows_evaluated>0`; null is unknown except to null checks; non-finite, overflowing or non-comparable values are unevaluable; every route measures, delegates, or refuses before its first read with a registered reason, and no `skip` exists.

### Non-goals

- Discovery, inference, formulas, regex, Python, SQL, plugins, literals of any kind in the grammar.
- Gating, quarantine policy, output mutation, a new score, legacy validator changes (including the legacy lookup defect C8).
- Distributed execution, or rows above `MAX_SELECTED_ROWS_PER_SIDE`.
- Containment against malicious code already in the caller's process (use `run_pipeline_isolated`).
- A generic aggregate registry, serializer, unseal API, or engine-owned file, database, or HTTP transport.
- Persisting reports for runs that never reach the Platform post-success hook, report history UI, or a per-attempt key (Q2 is closed: K-JOB).
- Value-free server-admin logs, and census machinery that proves completeness over every entry point (V2-LOG).
- Lease lifetime across in-flight feeds at close (V2-LOG, with probe evidence below).

### Definition of done

- Grammar, side-specific report, codec, collector, R-FAIL guard, and every V1 evaluator are public and tested.
- Route and FK manifests are executable test data. Supported cells are byte-equal to the resident oracle; refused cells perform zero reads; output bytes with invariants on equal bytes with invariants off on every route.
- Every F31 extension point in the table below exists in the built F31 and is used as listed.
- Engine, CLI, and Platform surfaces pass the marker corpus over the V1 rule 3 surfaces, the field walk, the named exit tests, the barrier tests, the compatibility corpus, and schema snapshots.
- B-9 reads one sibling invariant section per committed run from `job_invariant_reports` (latest row of the current attempt ordinal), without changing score, grade, job outcome, or legacy payloads. The F35 fixture accepts only eligible output observations.

## Revision 5 finding map

Each finding is fixed at its class; each fix adds acceptance rows or removes design, never scope.

| Review finding | Defect class | Resolved in | Acceptance |
|---|---|---|---|
| V1: two persistence designs; K-ATTEMPT stores nothing on every normal run (F-P1 to F-P3), so B-9 and F35 never get a report | 4 silent fallback (the feature silently persists nothing; V1 rule 1 product function); 5 referential integrity (no parent row to key on) | "Platform persistence contract": K-ATTEMPT, its read path, and the second migration design deleted; one table, one migration, one writer, one reader | A12e, A12g, A12h, A12j, A12k, A12l |
| V1 (same finding): K-JOB "first row wins, record conflict" keeps run N's verdict next to run N+1's output on re-run or retry promotion | 3 state machine and concurrency (stale writer), 5 referential integrity (report to wrong run) | Append-only rows plus attempt ordinal `jobs.invariant_run_ordinal` checked at write under `FOR UPDATE` and at read; conflict code, `invariant.report_conflict`, A12f and B5 deleted | A12j (fail-before: stub writer with status-only predicate), A12k, B11 |
| MEDIUM: no per-slice bound; slice 8 and others unbounded | 8 overclaimed evidence (slices too large to review) | Per-slice bound (at most 10 production files and 800 changed lines including tests); slices 0, 1, 2, 3a, 3b, 3c, 5, 6, 7, 8 re-costed and split | slice table |
| MEDIUM: resource lifetime across feeds at `close` | 3 state and concurrency, 2 cleanup on exit | V2 per the V1 bar; probe found no native fault (command in "V2-LOG"); one line kept in "Collector lifecycle" | none (V2-LOG) |
| LOW: contract-check wording claimed "slices with bounds" | 8 overclaimed evidence | Header rewritten; bounds now exist; `plan-lint` stated as not run | none |
| LOW: guarantee not phrased as V1 rules | 8 overclaimed evidence (admin logs inside property 7) | "Guarantee: V1 rules 1 to 3"; admin-log surfaces moved to V2-LOG | marker corpus scope |
| LOW: non-goals incomplete | none | "Non-goals" | none |

Repeat-class note: the Platform persistence defect (classes 3 and 5) has now appeared in revisions 2, 3 and 4. Revision 5 does not patch it by assumption: the orchestrator decided Q2 (K-JOB), and the ordering rule is pinned by a characterization test of the one run-start owner (A12l). If A12l is false at Slice 11a-0 (some run does not pass through `api/jobs/v2_orchestrator.py:132-134`), the slice stops and reports instead of patching a fourth time.

### Revision 5 baseline drift

Merging `origin/main` `0173c054` moved lines in these cited engine modules, among others: `execution/out_of_core/_stream_driver.py` (+358/-401, now 566 lines, off the census), `execution/out_of_core/_runner.py` (+192/-229, now 644), `execution/native/_chunked_entry.py` (+59/-75, now 665), `execution/_chunked.py` (+19/-60, now 586, off the census), `execution/_isolated_run.py` (+31/-2, now 651), `execution/_sequential.py` (+3, now 699, one line under the 700 hard max), `execution/_pipeline_context.py` (+28/-14), `execution/out_of_core/_stream_join_cursors.py` (+34/-26), `config/_pipeline.py` (+6/-10), `reasons/_catalog.py` (+26), `reasons/_registry.py` (+5/-1), `validators/_fk_validators.py` (+7/-5), `validators/_relationship_checks.py` (+2/-1), `execution/_governor.py`, `execution/_pipeline.py`, `execution/_pipeline_full_frame.py`, `execution/_pipeline_mixed_route.py`, `execution/_adapter.py`, `execution/out_of_core/_compat.py`, `execution/out_of_core/_duckdb.py`. Line numbers in those files are stale until the owning slice re-reads them; the semantics cited were not re-verified in this revision.

### Revision 4 finding map (history, unchanged)

Revision 4 folded the argon triage of revision 3 (findings 1 to 8: acceptance matrix, reconciliation binding, `MAX_DRAIN_SECONDS` deadline, `measure_invariants`, Slice 11a split, OOC verdict join, decoder bounds, owner questions). Its acceptance rows are kept below, except A12f and B5, which revision 5 deletes with the conflict design.

### Revision 4 corrections (found while re-reading the cited code)

- **C1, first-hit order (class 9, comment drift).** Multi-parent precedence is `RelationshipGraph.edges` order, which is sorted by `(parent_table, parent_columns, child_table, child_columns)` (`relationships/_graph.py:252-262`, returned at `:311`) and consumed through `parents_of` (`:113-121`) at `execution/_pandas_adapter.py:380` and on OOC (`out_of_core/_multi_parent.py:5`, `:58-59`). The prose at `_graph.py:166-167`, `:177` and `_pandas_adapter.py:490-493` says "declared order"; the code does not do that. F36 evaluates first-hit in graph edge order, the order every route executes, and never in config order. Test A6d.
- **C2, deadline source (class 3).** `_isolated_run.py:151` is the `timeout_s` parameter default, not the enforcement. Enforcement is `proc.communicate(timeout=timeout_s)` at `:450` and `proc.kill()` at `:454`. The child payload (`:385-392`) carries no timeout, so a child cannot compute "remaining isolated timeout_s". `_run_in_process` (`:258-319`) ignores `timeout_s` entirely. Fixed in "Finalize deadline".
- **C3, grammar fail-before.** A config carrying `invariants` is rejected today by `extra="forbid"` at `config/_pipeline.py:58`; `:45` is the class line.
- **C4, result field order (class 10).** `IsolatedRunResult.memory_evidence` carries the invariant "Stays the last field" (`execution/_isolated_common.py:386-388`). Revision 3's "trailing `invariant_report`" contradicted it. `invariant_report` is inserted immediately before `memory_evidence`. Test A19.
- **C5, vacuous sentries (class 8).** `tests/sentry/test_validate_no_mutation.py` selects functions by `_TARGET_NAME = re.compile(r"^(validate|check)([_A-Z]|$)")` (`:55`), which `measure_invariants` does not match, and `tests/sentry/test_capability_matrix.py` renders only the registries in `scripts/gen_capability_matrix.py:160-214`, none of which is F36. Revision 3's "`measure_invariants` must pass all three" was vacuous. Fixed in "Standalone entry `measure_invariants`".
- **C6, Platform fence (class 3).** The only publication fence is `verify_fence(connection, FenceContext)` (`decoy-platform api/scheduler/reconcile_apply.py:100-124`, `FenceContext` at `:90`), a scheduler-leader fence; neither post-success hook call site holds a `FenceContext`. Fixed in "Platform persistence contract".
- **C7, OOC quarantine.** The out-of-core route has no quarantine channel (`out_of_core/_compat.py:70-83`), and `execute_out_of_core_route` (`execution/_pipeline_route_dispatch.py:55-77`) passes no `quarantine_config`, so the accounted-quarantine set on OOC is empty by construction at baseline. Test A17e pins this.
- **C8, legacy reconciliation lookup (class 4).** `validate_reconciliation_holds` resolves its edge by table pair (`_edge_for`, `validators/_relationship_checks.py:31-56`, refusal at `:50`) and then takes the first child entry whose table matches (`:258-262`), silently picking one when a relationship lists the same child table twice with different columns. F36 copies neither lookup. Test A1d.

## F31 extension points (BLOCKER 1)

F31 is being revised. F36 assumes only what `origin/plan/f31-safe-output` at `a909118` promises, and names each mechanism. Every F36 slice that consumes a row first runs a reconcile check against the built F31: if the mechanism is absent or changed, the slice stops and asks the owner. F36 never amends F31, widens an analyzer exemption, or adds a directory exclusion.

| ID | F31 mechanism F36 uses (F31 plan line) | F36 use and owning slice |
|---|---|---|
| X1 | Domain family modules under `safe_output/families/` with named typed factories; "no generic public factory" (f31 :243-266, :270) and the documented review path for "a new event family" (f31 :768) | New engine module `safe_output/families/invariants.py` with factories `invariant_summary`, `invariant_not_run`, `invariant_artifact_written`, `invariant_route_refused`. Built in the engine (Slice 4), never in a consumer. CLI and Platform call these public factories; they mint nothing. |
| X2 | `SafeCount(name_enum, value)` with closed names (f31 :293) | Closed names `observations_pass`, `observations_fail`, `observations_indeterminate`, `observations_not_run`. No `SafeMeasurement` use. |
| X3 | F29 `ordinary_output: bool = False` and exact opt-in manifest (f31 :276) | Only the reasons marked "ordinary" in the reason table are opted in, in Slice 4. |
| X4 | Exact data-plane writer manifest with one threat-model entry per writer, no wildcards (f31 :66, :470), per-repository sink manifests (f31 :58, :448) | Four entries, each added in the slice that builds the writer: engine `InvariantSidecarLease.write_bytes` (Slice 8c); CLI `publish_invariant_artifact` (Slice 10); Platform `persist_invariant_report` ORM write and `invariant_artifact_response` byte response (Slices 11a-1, 11b). Each accepts only an exact `SerializedInvariantReportV1`. RED: the shared analyzer flags each writer before its manifest entry exists. |
| X5 | Platform response-model gate: no `response_model`, approved byte adapters only (f31 :460, :671) | The artifact route returns a `Response` built only inside `invariant_artifact_response`, has no response model, and is listed in the Platform route manifest as data-plane. |
| X6 | `SafeEventSink`, `SafeOutputScope`, process-entry install (f31 :270, :356) | Used unchanged for all ordinary F36 events. |
| X7 | F31 changes ordinary serialization of `ExecutionResult` and `IsolatedRunResult` (f31 :228) | F36 field snapshots are taken on top of landed F31 engine Slice 5 (F31's numbering), never before. |

Not assumed: aggregate registry, generic unseal, `SerializedSafePayload` nesting of the artifact. The invariant artifact is data-plane bytes produced only by the F36 codec; it is value-free by F36's own proof (marker corpus), not by F31's.

## Failure rule R-FAIL (BLOCKER 2)

One rule, enforced at one place: **any F36 internal failure after config validation yields `not_run` plus a registered reason for the affected observations and never changes output bytes, publication, or job outcome.**

- Config time (pre-read) is the only place F36 may fail a run: grammar errors, limits that are static, probe use, direct chunked API use, unsupported OS for relationship checks. These raise the existing config error before any read.
- After config validation, every F36 call from route code goes through `InvariantRunGuard` (in `_collector.py`): `guard.feed(...)` and `guard.finalize(...)`. The guard catches `Exception` raised inside F36 frames only, marks affected observations `not_run` with the matching reason (`invariant.internal_failure` when no narrower code applies), emits `invariant_not_run` through F31, and returns normally. It never wraps route code, and `feed` accepts only materialized Arrow batches (no iterator or lazy frame), so a route read, mask, rewrite, or publish error never surfaces inside the guard and propagates exactly as today.
- At compile time the guard pre-encodes the all-`not_run` fallback report for the program. If that encode fails, config validation fails. Therefore a failure during assembly still returns a valid report and the fallback itself cannot fail at run time.
- `BaseException` that is not `Exception` (cancel, `KeyboardInterrupt`, `SystemExit`) propagates unchanged; F36 leases release in `finally`. The run's existing exit path owns the outcome.
- Failure kinds covered: Arrow kernel error, DuckDB error, scratch identity drift, procfs absence at run time, scratch or memory budget exceeded, 100,000,001 selected rows, drain or deadline timeout, sidecar decode failure, codec cap overflow, collector assembly error.
- Residual RF-OOM: a process OOM kill is not an exception. Bounded accumulators (A7c) and the governor on isolated runs are the mitigation; in-process OOM stays a documented residual.
- Residual RF-T: F36 feed time is part of an isolated run's wall clock; the D-ISO partition bounds only finalize ("Finalize deadline"). Evidence A14d.

Proof (A15a, A15b): per route, a fault injector raises each failure kind; output bytes, publish commit call, job result, and `quality_metrics` equal the no-F36 run, and the report shows `not_run` with the expected reason. Mutation A: guard re-raises, test goes RED on publication. Mutation B: guard swallows without a reason, RED on reason membership. Mutation C: guard widened around route code, RED on the "route exception still fails the run" cell.

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
reconciliation := {kind: reconciliation, position: u16,
                   member: {relationship_position, child_position},
                   parent_value: configured name, child_value?: configured name,
                   op: sum|count, absolute_tolerance: decimal, coverage: exact}
checksum       := {kind: checksum, position: u16, column,
                   scheme: luhn|npi|iban|vin|isbn13|ean13|gtin, coverage, sample_rows?}
relationship   := {kind: relationship, position: u16,
                   members: [{relationship_position, child_position}, 1..8],
                   coverage: exact}
```

Pydantic models use `extra="forbid"`. `position` is `1..65535`. `sample_rows` is required only for sampled row-local checks. Tolerance is finite `Decimal` in `[0, 10^12]`. Names resolve once at compile and never enter report types. `relationship_position` is the 1-based index into `PipelineConfig.relationships` (`config/_pipeline.py:71`) and `child_position` the 1-based index into that entry's `children` (`config/_relationships.py:47`). A `relationship` check's members must be the complete first-hit group for one child tuple (every declared member whose child tuple equals it, as a set); evaluation order inside the group is graph edge order (C1). A `reconciliation` names exactly one member and carries no table names: both tables come from the member, so a parent or child table mismatch cannot be written. `child_value` is required for `op: sum` and forbidden for `op: count`.

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
| `MAX_DRAIN_SECONDS` | 300; the upper bound on one finalize wait in any process. The applied deadline depends on the execution context, defined in "Finalize deadline" (C2) |
| `FINALIZE_RESERVE_FRACTION`, `FINALIZE_RESERVE_MIN_S`, `FINALIZE_RESERVE_MAX_S` | 0.10, 1, 60; isolated runs only: `reserve_s = min(60, max(1, 0.10 * timeout_s))` of `timeout_s` is withheld from F36 for sidecar write, envelope write, exit, and child start skew |

A relationship declaration emits source and output observations for `rel.pk.unique`, `rel.fk.coverage`, `rel.fk.orphan`, plus one transition observation `rel.join.cardinality_change`. A row-local or reconciliation declaration emits source, output, and transition. The maximum legal report must encode below `MAX_ARTIFACT_BYTES`, or Slice 0d stops and narrows the schema.

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

New F29 namespace profile `invariant(owner)` in `src/decoy_engine/reasons/_catalog.py` (the file is 170 lines and has no `invariant` profile), declared like `verification` at `_catalog.py:134-146` (kind finding, outcome findings). The registry takes the next minor version after landed F31 (`REGISTRY_VERSION` at `reasons/_registry.py:24`; the added-identity rule at `tests/unit/test_reason_registry.py:70-72` and `:150-157`). All codes are engine-declared, including the one Platform-detected code (owner `platform`).

| Code | Raised by | Ordinary (X3) |
|---|---|---|
| `invariant.probe_unsupported`, `invariant.chunked_api_unsupported`, `invariant.platform_unsupported` | config time | yes |
| `invariant.member_unknown`, `invariant.member_duplicate`, `invariant.column_unknown` | config time, reconciliation and relationship member binding (finding 2) | yes |
| `invariant.input_shape` | call time, `measure_invariants` input validation before any read (finding 4) | yes |
| `invariant.internal_failure`, `invariant.row_limit`, `invariant.scratch_budget`, `invariant.deadline`, `invariant.feed_rejected` | R-FAIL | yes |
| `invariant.no_rows_evaluated`, `invariant.no_source_side`, `invariant.alignment_lost` | verdict rules; `alignment_lost` also from the OOC verdict join (finding 6) | no (report only) |
| `invariant.report_not_persisted` | Platform reader (owner `platform`): no row for the job's current attempt ordinal, or the write was refused by the ordinal fence | yes |

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

Lease lifetime across feeds (V2-LOG, classes 2 and 3): `close` releases the scratch leases at the deadline even when `inflight_feeds > 0`, so a feed still running in DuckDB or Arrow outside the lock can lose its scratch under it. This is logged, not built: no V1 function or data-loss path was found, and the probe in "V2-LOG" shows the in-flight query fails with a Python exception (caught by the guard, so `not_run`), never a native fault. No barrier test is added for it.

### Finalize deadline (finding 3)

Facts (C2): the parent enforces `timeout_s` only by `proc.communicate(timeout=timeout_s)` and `proc.kill()` (`_isolated_run.py:450-454`); the child payload (`:385-392`) holds no timeout; `_run_in_process` (`:258-319`) and direct `run_pipeline` have no timeout at all; `timeout_s=None` is legal (`:151`, `:181-186`). Revision 3's `min(MAX_DRAIN_SECONDS, remaining isolated timeout_s)` was therefore undefined in-process and about 0 s for an isolated run that used `timeout_s - epsilon`, so finalize would abort and race the parent kill during the sidecar write.

`InvariantDeadline` (new `invariants/_deadline.py`) is built once per run, in the process that owns the collector, from `time.monotonic()`. `claim_s` is the monotonic instant `finalize` moves the collector to `DRAINING`.

| Context | Where it applies | Finalize deadline |
|---|---|---|
| D-IN | direct `run_pipeline`; `run_pipeline_isolated(isolate=False)` (`_run_in_process`, `_isolated_run.py:258`); the re-entrant degrade (`:209-230`); governor flag-off (`_governor.py:526`); CLI | `claim_s + MAX_DRAIN_SECONDS`. No external kill exists, so F36 does not invent one; a caller-supplied `timeout_s` is ignored here exactly as `_run_in_process` ignores it today |
| D-ISO-NONE | `isolate=True`, `timeout_s=None` | same as D-IN |
| D-ISO | `isolate=True`, `timeout_s` a number | partitioned, below |

D-ISO partition. The parent computes `reserve_s = min(FINALIZE_RESERVE_MAX_S, max(FINALIZE_RESERVE_MIN_S, FINALIZE_RESERVE_FRACTION * timeout_s))` and `exec_budget_s = timeout_s - reserve_s`, and writes two payload keys: `invariant_exec_budget_s` and `invariant_spawn_wall_ns` (`time.time_ns()` read immediately before `Popen`, `_isolated_run.py:433`). The child reads both at worker entry (`_isolated_worker.main`, `_isolated_worker.py:241`) with `entry_s = monotonic()`, computes `skew_s = clamp((time.time_ns() - spawn_wall_ns) / 1e9, 0, reserve_s)`, and sets `cutoff_s = entry_s + exec_budget_s - skew_s`. The deadline is `min(claim_s + MAX_DRAIN_SECONDS, cutoff_s)`. If `cutoff_s <= claim_s`, finalize performs no merge or query and returns the compile-time pre-encoded all-`not_run` report with `invariant.deadline` (R-FAIL), whose cost is a copy of at most `MAX_ARTIFACT_BYTES`. A wall-clock step backward makes `skew_s` 0 (the reserve still covers startup); a forward step only shortens F36's budget. Neither clock change can lengthen the run.

Child write order (class 2). The sidecar lease writes `invariant.sidecar.tmp` relative to the work-root dirfd, fsyncs, renames to `invariant.sidecar`, fsyncs the directory, and only then the worker writes the envelope (`_isolated_worker.py:266`) carrying `invariant_sidecar: {present, size}`. The parent opens the sidecar only on the completed-envelope path (`_isolated_run.py:484-497`) and checks the size before decode. A kill at any instant yields one of: no envelope, so the existing timeout or abnormal classification (`:453-470`, `:499-531`) with no report; or a complete envelope, with the sidecar present at the stated size or the report replaced by the all-`not_run` `invariant.internal_failure` report. The two fixed names are unlinked by the parent in a `finally` when the caller owns `staging_dir`, and removed with the work root otherwise (`:363-365`).

LOC. `_isolated_run.py` is 651 lines at the merged head and on the census (`tests/sentry/test_module_size.py:71`); it may only shrink. Slice 8a first extracts `_write_payload` (`:368-406` at `fdd08a89`, re-read at 8a) into a new `_isolated_payload.py` and ratchets the census entry down to the new exact LOC; Slice 8b then adds the two keys there and one call at the spawn site.

Residual RF-T. Feeds run during execution and are not deadline-bounded, and the reserve limits only F36's finalize, not the run. An isolated run whose invariants-off wall time lies within the F36 feed overhead of `timeout_s` can be killed with invariants on and not with them off. This is the second named R-FAIL residual beside RF-OOM; A14d records the feed overhead ratio as evidence, from `uv.lock` in the branch's environment, and the docs state the residual.

### Row and relationship semantics

Comparison and sum kernels use lossless Arrow common types only. Checksums call `decoy_engine.checksums.validate` (`checksums.py:351-369`, one Python call per value). Deterministic sampling selects `m=min(sample_rows,population_rows)` midpoint positions `floor(((2k+1)*population_rows)/(2m))` and aligns source and output on those positions.

Relationship semantics reuse `relationships/_graph.py:134-311` (first-hit in graph edge order, C1; the "declared order" prose at `:166-167`, `:177` does not describe the sort at `:252-262`) and these validator truth definitions, never their Python-set implementations, which grow with rows (LOW 1):

- Null parent tuples are excluded from the parent key set: `_parent_pk_set` (`validators/_fk_validators.py:72-114`).
- An all-null child tuple is a null FK, a partial-null tuple is an ordinary unmatched tuple: `_child_fk_values` (`_fk_validators.py:116-142`, rule at `:140-141`).
- `rel.fk.coverage` follows `validate_fk_intact` (`_fk_validators.py:150`, null FK ignored); `rel.fk.orphan` follows `validate_no_orphan_children` (`_fk_validators.py:235`, null FK is an orphan).
- `rel.reconciliation` follows `validate_reconciliation_holds` (`validators/_relationship_checks.py:199-215`) grouping, but uses absolute `Decimal` tolerance, not that validator's `isclose`-style default `1e-6`. The divergence is documented in the module docstring.

Duplicate non-null parents stay visible and make only join change indeterminate. Parent ordinals are `(member_position, row_position)` and never leave the evaluator. Quarantine alignment happens before side and transition collection.

### Reconciliation binding and observation mapping (finding 2)

Binding, at compile time, before any read. Each failure raises the existing config error with the named F29 code and positions only; zero reads (A1d).

1. `member` must index an existing relationship and child, else `invariant.member_unknown`.
2. The member's edge key is `k = (parent.table, tuple(parent.columns), child.table, tuple(child.columns))`. Exactly one `RelationshipEdge` in the resolved `RelationshipGraph` has key `k`, because the graph deduplicates identical declarations (`relationships/_graph.py:244-250`). If any other declared member `(r', c') != (r, c)` has the same `k`, two authored identities would name one edge, so compile refuses with `invariant.member_duplicate`. The same rule applies to every `relationship` check member.
3. Several relationships between the same two tables with different column tuples are different edges with different `k`; the member selects exactly one. No table-pair lookup is ever made (contrast `_edge_for`, C8). A composite member uses the full parent and child column tuples with the null rules of `_parent_pk_set` and `_child_fk_values`.
4. Multi-parent child tuple (`graph.parents_of(child_table, child_columns)` returns more than one edge): a child row contributes to this reconciliation only when this edge is its first hit in graph edge order (C1). Child rows whose key hits an earlier edge, all-null child keys, and unmatched child keys contribute to no group.
5. `parent_value` must name a column of the member's parent table and `child_value` one of its child table, else `invariant.column_unknown`.

Counting unit: parent rows, as in the legacy validator, whose findings are parent rows (`validators/_relationship_checks.py:307-327`). For each selected parent row on a side: null key tuple or null `parent_value` is `rows_null_unknown` (F36 treats null as unknown, a documented divergence from the legacy validator, which fails it at `:319-321`); a non-numeric, non-finite, or overflowing `parent_value`, or a group containing such a child value, is `rows_unevaluable`; otherwise the row is evaluated, and passes when `|parent_value - aggregate| <= absolute_tolerance` in `Decimal`. A parent with no attributed child compares against an aggregate of 0, as the legacy validator does (`:322`). Duplicate parent keys each compare against the same group aggregate.

Observation mapping for `rel.reconciliation`:

| Scope | `structures` (in this order) | `counts` union member |
|---|---|---|
| `source` | `RelationshipStructureRef(side=source, members=(RelationshipMemberRef(r, c),))`; `ColumnStructureRef(side=source, parent table, (parent_value,))`; for `op: sum` also `ColumnStructureRef(side=source, child table, (child_value,))` | `ExactRowCounts` over source parent rows |
| `output` | the same three or two refs with `side=output` | `ExactRowCounts` over output parent rows |
| `transition` | the source refs, then the output refs | `ExactTransitionCounts` over parent rows aligned by quarantine-aligned parent position |

`not_run` and `unsupported` carry `counts=None`. Coverage is always exact, so the sampled members never appear for this code; the decoder rejects them (A10).

### Observation shape table

The decoder enforces this table per code and scope; any other combination is rejected (A10).

| Code | `source` and `output` counts | `transition` counts | `structures` |
|---|---|---|---|
| `row.compare`, `row.null`, `row.null_pair`, `row.sum_equals`, `row.checksum`, exact | `ExactRowCounts` | `ExactTransitionCounts` | one `ColumnStructureRef` per referenced table on that side; transition lists source then output |
| the same codes, sampled | `SampledRowCounts` | `SampledTransitionCounts` | as above |
| `rel.reconciliation` | `ExactRowCounts` | `ExactTransitionCounts` | per the mapping above |
| `rel.pk.unique`, `rel.fk.coverage`, `rel.fk.orphan` | `RelationshipCounts` | no transition observation | one `RelationshipStructureRef` with all group members |
| `rel.join.cardinality_change` | no side observation | `JoinChangeCounts` | two `RelationshipStructureRef`, source then output |

### Streamed source feed (HIGH 3)

Each route feeds the source side at the read it already performs; F36 adds no source read.

| Route | Source feed point | Per-row transition carrier |
|---|---|---|
| Resident (full-frame, mixed, multi-table, unified) | resident source table after load | in-memory aligned Arrow verdict arrays, bounded by `MAX_EVAL_BATCH_ROWS` slices |
| Sequential and auto-chunk streamed | the raw batch in hand before masking, same iteration as its output batch | per-batch verdict array, aligned by in-batch ordinal, then quarantine-aligned |
| OOC reorder | phase 1 raw batch, the only raw read (`out_of_core/_stream_driver.py:11-23`) | 2-bit verdict per row per row-local check, spilled as Arrow IPC under `InvariantScratchLease`, keyed by the same `row_nr` that phase 3 asserts (`_stream_driver.py:32-37`) |
| OOC resident and batch-join | the batch where the raw child is read | same spilled carrier |

Relationship keys for both sides are staged into DuckDB scratch at the same reads. Spill size counts against the scratch budget; exceeding it is R-FAIL `invariant.scratch_budget`. A8b counts source reads per table and requires the count with invariants on to equal the count with invariants off.

### OOC verdict join check (finding 6)

Carrier. In phase 1, the only raw read (`out_of_core/_stream_driver.py:317-346`), each row-local check appends `(row_nr int64, verdict uint8)` Arrow IPC batches under `InvariantScratchLease`, where `row_nr` is the same `__decoy_row_nr` the FK join numbers by (`:281`) and phase 3 asserts (`_stream_join_cursors.py:235-240`, `:256-268`). The verdict code uses 2 bits: pass, fail, null-unknown, unevaluable. Output verdicts are computed on each emitted phase-3 batch (`_stream_driver.py:416-443`) and numbered `row_nr_start + i`.

Join, inside the F36 frame only (it never wraps the route's own cursors, whose `_row_alignment_error` fails the run before F36 is reached, exactly as today). Streaming merge in `row_nr` order, residency bounded by `MAX_EVAL_BATCH_ROWS`, steps in this order:

1. Quarantine exclusion first. Rows in the route's accounted quarantine set are removed from the source verdict stream before any alignment check, so an accounted row is never reported as missing. On OOC the set is empty by construction today (C7); the step is still executed and asserted (A17e), so a future OOC quarantine channel must feed it rather than bypass it.
2. Alignment. The next output `row_nr` must equal the next remaining source `row_nr`. A source `row_nr` with no output partner that is not accounted (missing), a repeated `row_nr` on either side (duplicate), a decreasing `row_nr` (out of order), or one stream ending first is alignment loss: that check's `transition` observation is `not_run`, `indeterminate`, `counts=None`, reason `invariant.alignment_lost`. Its `source` and `output` observations, which need no alignment, stay measured. This is a verdict rule, not an R-FAIL, and the run is untouched.
3. Read failure. An Arrow IPC read error, truncated or schema-mismatched spill, or lease identity drift on the spill file is an F36 internal failure: the guard marks every observation that depends on that spill `not_run` with `invariant.internal_failure` (R-FAIL) and output bytes, commit (`out_of_core/_runner.py:313-315`), and job result are unchanged.

Joining by position instead of `row_nr`, checking alignment before quarantine exclusion, or classifying a read failure as `alignment_lost` are the three mutations A17 must kill.

### Scratch capability

`InvariantScratchLease` retains root and child descriptors, creates fixed names relative to a dirfd with exclusive non-following opens, checks type, owner, device, inode, and link count, and gives Arrow duplicated file objects. DuckDB receives only a proven live `/proc/self/fd/<fd>` bridge plus configured `memory_limit`, `temp_directory`, and `max_temp_directory_size`. Today `out_of_core/_duckdb.py:42` sets only `temp_directory` and `memory_limit`, so the temp-size cap is new configuration, not reuse. It streams aggregates, never keys. The Slice 3a-2 spike must also prove that `DuckDBPyConnection.interrupt()` stops a running query (deadline column above); if not, Slice 3a-2 stops for the owner.

**Platform matrix (MEDIUM 7).** Row-local checks run on every OS. Relationship and reconciliation checks are Linux-only in V1; elsewhere they are `unsupported` with `invariant.platform_unsupported` decided at config time, before staging. Relationship cells of the parity matrix are Linux-only and labelled so; non-Linux CI, where present, asserts the `unsupported` cells.

### Artifact and isolation ownership

`encode_invariant_report_v1` and `decode_invariant_report_v1` are schema-specific engine functions: exact types, closed fields, canonical order, duplicate-key rejection, bounded integers, F29 membership, UTF-8, byte cap, byte-for-byte re-encode equality. `SerializedInvariantReportV1` is an opaque exact type with no mapping protocol or generic unseal. `measure_invariants` is a normal in-process API over typed Arrow inputs (next section); callers with a hostile-code threat model use `run_pipeline_isolated`. F36 builds only the engine sidecar lease; the CLI owns its file publish, Platform its DB write and HTTP response, F31 every ordinary event.

Decoder count and balance rules (finding 7, class 6). `decode_invariant_report_v1` rejects, with a fixed `InvariantDecodeError(code)` and no partial report, any payload that breaks one of these; the encoder asserts the same rules, so a producer bug cannot write what the decoder would refuse.

- Every count field of every counts type is an exact `int` (never `bool`) with `0 <= n <= MAX_SELECTED_ROWS_PER_SIDE`.
- `ExactRowCounts`: `rows_total == rows_evaluated + rows_null_unknown + rows_unevaluable` and `rows_evaluated == rows_passed + rows_failed`.
- `SampledRowCounts`: `population_rows == observed_rows + unobserved_rows`; `observed_rows == observed_passed + observed_failed + observed_null_unknown + observed_unevaluable`; `observed_rows <= MAX_SAMPLE_ROWS`.
- `ExactTransitionCounts`: `comparable_rows == introduced_failures + resolved_failures + persistent_failures + persistent_passes`; `comparable_rows + unknown_transitions + unevaluable_transitions <= MAX_SELECTED_ROWS_PER_SIDE`.
- `SampledTransitionCounts`: `population_rows == observed_rows + unobserved_rows`; `observed_rows == introduced_failures + resolved_failures + persistent_failures + persistent_passes + unknown_transitions + unevaluable_transitions`; `observed_rows <= MAX_SAMPLE_ROWS`.
- `RelationshipCounts`: `child_rows == null_child_key_rows + matched_child_rows + unmatched_child_rows`; `parent_zero_match_rows + parent_one_match_rows + parent_many_match_rows == parent_rows - null_parent_key_rows`; `null_parent_key_rows + duplicate_parent_key_rows <= parent_rows`; `2 * duplicate_parent_key_groups <= duplicate_parent_key_rows`, and the two are zero together.
- `JoinChangeCounts`: `comparable_parents == changed_parents + unchanged_parents`; `changed_parents == increased_parents + decreased_parents`; `lost_all_children <= decreased_parents`; `gained_from_zero <= increased_parents`.
- Pairing: `measured_*` carries exactly the counts member the observation shape table allows for its code and scope; `not_run` and `unsupported` carry `counts=None`, `verdict=indeterminate`, and a registered reason. The decoder recomputes the verdict from the counts by the verdict rules and rejects a mismatch, so a balanced payload with a forged `pass` is refused.
- Report: at most `MAX_OBSERVATIONS` observations, unique and in canonical `ObservationRef` order; report `status` and `coverage` equal the values recomputed from the observations.

### Standalone entry `measure_invariants` (finding 4)

```text
def measure_invariants(
    config: Mapping[str, Any],
    *,
    output: Mapping[str, pa.Table],
    source: Mapping[str, pa.Table] | None,
    quarantined_source_rows: Mapping[str, pa.Array] | None = None,
) -> InvariantReportV1
```

- `config` is the validated dump, the same contract as `run_pipeline` (`config/_pipeline.py:47-55`); it must carry an `invariants` block. The program is compiled by the same `compile_invariant_program` the routes use, so every config-time refusal (grammar, member binding, Linux gate, static limits) is identical and happens before any read.
- Accepted inputs: values whose type is exactly `pyarrow.Table` (exact type check, not `isinstance`); `quarantined_source_rows` values are exactly `pyarrow.Array` of `int64`, strictly increasing, within `[0, num_rows)` of that source table. Every table the program references must be present on `output`, and on `source` when it is not `None`; unreferenced tables are never read. No `RecordBatchReader`, `LazySource`, pandas object, path, or connector. Any violation raises the config error with `invariant.input_shape` and positions only, before any read.
- `source=None` makes every `source` and `transition` observation `not_run` with `invariant.no_source_side`.
- Relationship graph: resolved by the same chain `run_pipeline` uses at `execution/_pipeline_context.py:318-326` (`build_namespace_registry`, `check_orphan_fk_policy_completeness`, `build_relationship_graph`, or the empty graph when there are no relationships). Slice 3c-1 extracts that block into one shared function both call, so the two cannot diverge; A18c asserts equal `graph.edges` for every FK-shape fixture.
- Return: the typed `InvariantReportV1`, never bytes; callers encode with `encode_invariant_report_v1`. After input validation it never raises for an F36 failure: the same `InvariantRunGuard` turns it into `not_run` observations (R-FAIL). It builds its own collector and scratch lease and closes both in `finally`, including on `BaseException`.
- It does not mutate any input mapping, table, array, or the config, and it records no telemetry into the caller's objects.
- Module `invariants/_measure.py`, within the 450-line new-module ceiling. Implementation slice: Slice 3c-1, after the row (Slices 2a-2c) and relationship (Slices 3b-1, 3b-2) evaluators it composes.

Sentry hardening (C5), in Slice 3c-2, tightening only:

- `tests/sentry/test_validate_no_mutation.py`: an explicit target set `_EXTRA_TARGETS = frozenset({"measure_invariants", "compile_invariant_program"})` is OR'ed with `_TARGET_NAME` (`:55`), plus a non-vacuity test that each extra name is found exactly once under `src/` and visited. The allowlist is not touched.
- `scripts/gen_capability_matrix.py` gains an "Invariant checks" section rendered from the closed check-code enum and the executable route capability manifest; `docs/capability-matrix.md` is regenerated, so `tests/sentry/test_capability_matrix.py` goes RED when a code or route cell changes without a doc refresh.
- `tests/sentry/test_methodology_citations.py` scope is unchanged; `_measure.py` carries the citations listed in "Reuse inventory".

### Platform persistence contract (decided: K-JOB, append-only, attempt ordinal)

Decision (orchestrator, owner delegation "go with recommended"; Q2 is closed and is not reopened here): persist as K-JOB shaped like the existing `job_quality_reports`. K-ATTEMPT, the conflict rule, `invariant.report_conflict`, the conflict tests (old A12f, B5) and the second migration design are deleted. Pre-release: hard delete, no compatibility shim.

Facts at `decoy-platform` `d8908f4` that the design rests on:

- **F-P1 to F-P6** (revision 4, at `ea68d8e`): attempts are minted only for `sealed_attestation` jobs; neither the flag-off `run_job` nor the flag-on worker has an attempt id or a `FenceContext`; the hooks run after the job row is `success`; the engine result does not reach the hooks today. Pinned by A12a-A12c, re-read at the Slice 11a-0 reconcile check.
- **F-P7.** `JobQualityReport` (`api/models.py:1553-1573`): integer `id` PK, non-unique indexed `job_id` FK `jobs.id` `ON DELETE CASCADE`. Writer appends (`api/quality/service.py:77`); reader `load_latest_quality_report` takes the highest `id` for the job (`service.py:98-125`, order at `:114-119`). The shape is copied; the reader's unrestricted "highest id" is not, because of F-P8.
- **F-P8, re-runs under one `job_id`.** A job re-enters `pending` with the same id from `running` by the reaper (`api/jobs/reaper.py:189-200`) and sequential re-admission (`api/jobs/sequential_readmission.py:244-256`), and from `retry_wait` by retry promotion (`api/scheduler/retry_promotion.py:174-178`). Retry promotion does not clear `started_at`, and the run start sets it only when it is `None` (`api/jobs/v2_orchestrator.py:132-134`). So `started_at` is not a per-run identity, and no universal per-run ordinal exists today: `host_job_leases.generation` covers only the priced claim path, `infra_retry_count` only retry promotion, and `committed_attempt_id` only sealed attestation.
- **F-P9, one run-start owner.** `run_v2_pipeline_job` (`api/jobs/v2_orchestrator.py:86`) is called only from `api/jobs/v2_submission.py:140`, and its start block (`:132-134`, comment "the START owner for every executing pipeline run") is reached by every executing run, including sequential re-admission attempts. A12l pins this on both the flag-off and flag-on paths.

**Ordering rule: attempt ordinal, not written-at order.** Written-at order alone (highest `id`, as `load_latest_quality_report` does) is wrong under F-P8: a slow or zombie hook from run N can insert after run N+1's hook and become "latest". So:

1. One new column `jobs.invariant_run_ordinal` (`Integer`, not null, server default 0).
2. At the run-start block (F-P9), in its own short session that commits immediately, so no row lock is held across the run: `UPDATE jobs SET invariant_run_ordinal = invariant_run_ordinal + 1 WHERE id = :job_id RETURNING invariant_run_ordinal`. The returned value is the run's ordinal and travels with the report carrier. If this statement fails, the carrier ordinal is `None`, the hook persists nothing, and the job is unaffected (V1 rule 1).
3. Writer, one transaction: `SELECT 1 FROM jobs WHERE id = :job_id AND status = 'success' AND invariant_run_ordinal = :ordinal FOR UPDATE`. No row: write nothing, emit `invariant_not_run` with `invariant.report_not_persisted` through F31, and return. Otherwise insert, then commit. A later run's start increments the ordinal under the same row lock, so after run N+1 starts, run N's write predicate can never hold again.
4. Reader: `SELECT r.artifact_bytes FROM job_invariant_reports r JOIN jobs j ON j.id = r.job_id WHERE r.job_id = :job_id AND j.status = 'success' AND r.run_ordinal = j.invariant_run_ordinal ORDER BY r.id DESC LIMIT 1`. Inside one ordinal, written-at order (`id`) breaks ties between duplicate deliveries of the same run's hook. No row returns the fixed `invariant.report_not_persisted` body.

Table `job_invariant_reports` (one migration, which also adds the column in step 1): `id` integer PK; `job_id` FK `jobs.id` `ON DELETE CASCADE`, not null, indexed, non-unique; `run_ordinal` integer not null; `schema_version` `String(64)` fixed to `decoy-invariant-report/v1`; `artifact_bytes` `LargeBinary` not null, length `1..MAX_ARTIFACT_BYTES`, exact canonical bytes from `encode_invariant_report_v1`; `created_at` timestamptz default now. Index `(job_id, run_ordinal, id)`. Append-only: no update or delete path exists besides the cascade. Downgrade drops the table and the column, nothing else.

One writer `persist_invariant_report(session, *, job_id, run_ordinal, report: SerializedInvariantReportV1)` (new `api/invariants/service.py`). One reader `load_latest_invariant_report(session, *, job_id) -> SerializedInvariantReportV1 | None` in the same module. The hook is a fourth best-effort hook in `_run_post_success_report_hooks` (`api/jobs/runner.py:287`) following the quality hook pattern: its own session and commit; every `Exception` caught and logged through F31 without text (`api/quality/runner_hook.py:156`); never raised into job completion. Owner-or-admin authorization applies before every read.

Slices:

- **Slice 11a-0** (no migration): characterization tests A12a-A12c and A12l; the carrier `SerializedInvariantReportV1 | None` from `_run_v2_pipeline_in_worker` through `run_v2_pipeline_job` and `run_v2_pipeline_from_config` to both hook call sites (`runner.py:576`, `dispatch_worker_main.py:505`), accepted and not persisted (A12d). The carrier never raises into job completion. If changing those return types breaks a frozen Platform contract, the slice stops for the owner.
- **Slice 11a-1**: the migration, ORM model, the ordinal bump at the run start, writer, reader, hook, X4 DB writer entry. RED: A12e, A12g, A12h, A12j, A12k, B2 DB cell, B11.

## Defect-class closure table

| # | Class | Closing structure | Non-vacuous RED-first proof | Required mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | F36 codec plus F31 extension points X1-X5; no value-derived numbers (no measurement field) | Marker corpus incl. numeric markers through every surface; F31 analyzer over the four X4 writers | Re-add a deviation measurement; drop one X4 manifest entry |
| 2 | Cleanup on every exit | Scratch lease, sidecar lease, CLI file owner, Platform hook session; R-FAIL guard | Owner by exit matrix plus R-FAIL fault matrix per route | Remove one `finally`, or make the guard re-raise |
| 3 | State and concurrency | Lock, condition, inflight leases, `close_pending`, claimant; per-context finalize deadline with the D-ISO partition; Platform attempt-ordinal write predicate under `FOR UPDATE` and ordinal-filtered reader | A5a-A5d, A16a-A16e, A12e, A12j, A12k | Let `OPEN` admit while `close_pending`; drop claimant cancel transition; drop the reserve; drop the ordinal clause from the write predicate or the reader |
| 4 | Route parity and silent fallback | Executable capability manifest; R-FAIL reasons; Linux matrix | Route matrix asserts telemetry, nonempty observations, bytes, zero-read refusal, `rejected_feeds==0` | Delete one route hook or one R-FAIL reason |
| 5 | Referential integrity | FK-shape by route disposition table; member binding by exact edge key; graph-order first-hit; `row_nr` verdict join with quarantine exclusion first; Platform row keyed by `job_id` (a parent row always exists) and tied to its run by `run_ordinal` | A6a-A6f, A1d, A17a-A17f, A12a-A12c, A12h, A12l | Drop one key component; resolve by table pair; join by position |
| 6 | Untrusted producer | Exact frozen types, bounded codec with count bounds, balance equations, shape table and verdict recomputation, fixed errors, closed enums | Field walk, A10a-A10d | Replace an exact type check with `isinstance`; remove one balance equation |
| 7 | TOCTOU (check then use) | Descriptor-anchored scratch capability and identity checks | Symlink, rename, hard link, fd reuse, absent procfs, bridge drift | Use one bare path; remove post-query check |
| 8 | Weak evidence | Named test matrix with seeded violation and fail-before line; non-vacuity counters; sentry target set for `measure_invariants`; zero-evaluated is indeterminate; residuals RF-OOM and RF-T stated | A1-A19, B1-B10, A18g, A18h, B9 | Remove the zero-evaluated rule; remove a name from `_EXTRA_TARGETS` |
| 9 | Refactor drift | Characterization first; census ratchet-down only | Placement mutations for full-frame, unified, OOC; read counter | Move full-frame hook above `_pipeline_full_frame.py:194` |
| 10 | Frozen drift | Enum table, field ledger, dump-shape rule, schema snapshots, `memory_evidence` stays last | Snapshot and old-reader fixtures; A1e; A19 | Emit `"invariants": None` in the default dump; append after `memory_evidence` |

## Reuse inventory

| Existing capability | Binding decision |
|---|---|
| F29 registry (`src/decoy_engine/reasons/`) | New `invariant` profile and minor bump only (Reasons section). |
| F31 extension points X1-X7 | Exactly as tabled; nothing else from F31. |
| Strict `PipelineConfig` (`config/_pipeline.py:45-99`) | One optional strict block; dump shape unchanged when omitted (MEDIUM 6 list). |
| Relationship config and graph (`config/_relationships.py:24-49`, `relationships/_graph.py:134-311`) | Resolve the existing graph through the shared helper extracted from `execution/_pipeline_context.py:318-326`; no second graph, no change to first-hit (graph edge order, C1) or cycle rules. |
| Validator and checksum truth definitions | Semantics as cited in Row and relationship semantics; never value-bearing result shapes or Python-set implementations. |
| `OutputPublish` and transactional sinks | F36 runs before the existing commit and adds no output-data sink. |
| Platform quality hook pattern (`api/quality/runner_hook.py:119-153`, `:273`) | Copied for the invariant hook. |
| `tests/sentry/test_module_size.py`, `test_public_import_boundary.py`, `test_physical_seam_disconnection.py` (narrow role only) | Unchanged; census rule in Slices. |
| `tests/sentry/test_validate_no_mutation.py`, `test_methodology_citations.py`, `test_capability_matrix.py` (LOW 3) | The first two did not cover `measure_invariants` and the third covers no F36 registry (C5); Slices 3c-1 and 3c-2 tighten them as listed in "Standalone entry `measure_invariants`". New evaluator modules carry methodology citations (Great Expectations result formats, PostgreSQL null comparison, Deequ analyzers, OpenLineage assertion identity, Python `dir_fd`, DuckDB configuration, `proc_pid_fd(5)`). |

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
| Isolated false/true | `_isolated_run.py:141-243` (entry), `:258-319` (in-process, D-IN), `:409-531` (spawn, kill, envelope, D-ISO); in-process parity or sidecar |
| Governor | `_governor.py:243-436`; only the final completed `IsolatedRunResult` carries a report |
| Probe | `_probe.py:222-352`; config-time refusal `invariant.probe_unsupported` before spawn |
| Direct public chunk iterators | `native/_chunked_entry.py:601` (`run_mask_chunked`, file is 681 lines), `_chunked.py:369-449`; refuse before first pull |
| Direct `PandasExecutionAdapter.run` | `_pandas_adapter.py:105`, `run` at `:163`; config-unaware; callers use `measure_invariants` |

### Owned state and exit paths

| Owner | Every required exit |
|---|---|
| `InvariantCollector` and `InvariantRunGuard` | every cell of the six-column table; each R-FAIL kind; repeated close |
| `InvariantScratchLease` | capability refusal, open failure, bridge mismatch, Arrow or DuckDB failure, budget, interrupt, success, cancel, cleanup failure, repeated close |
| `InvariantSidecarLease` | create, temp write, fsync, rename, directory fsync, envelope written after rename, kill before rename, kill after rename before envelope, size mismatch, decode failure, child crash, timeout, malformed envelope, parent discard and commit, caller-owned `staging_dir` unlink, cleanup failure, repeated dispose |
| Existing `OutputPublish` and sinks (`_sequential.py:23-63`) | unchanged; R-FAIL proves commit is called exactly as without F36 |
| Route owners | resident and sink success, decline, validation failure, stream error, exception before and after commit |
| Governor and probe | failed rung, later success, exhaustion, callback failure; probe zero spawn |
| CLI file owner | refusal, stage, write, fsync, rename failures, success, cancel, cleanup failure |
| Platform invariant carrier and hook | carrier `None`, carrier ordinal `None` (bump failed), carrier present, write predicate lost (status or ordinal), late hook from an earlier run, duplicate delivery in one run, DB error, rollback, cascade, migration up and down |

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

`ExecutionResult` (`_adapter.py:61-71`) gains trailing `invariant_report=None`. `IsolatedRunResult` (`_isolated_common.py:362-388`) gains `invariant_report=None` immediately before `memory_evidence`, which must stay the last field (`:386-388`, C4); construction is keyword-only at every call site, pinned by A19. Unchanged: `GovernorTripRecord` and `GovernorResult` (`_governor.py:170-211`), `ProbePoint` and `ProbeResult` (`_probe.py:133-172`). `SerializedInvariantReportV1` is opaque with no public fields.

**Default config dump consumers (MEDIUM 6).** Producers: `config/_pipeline.py:50`, `config/_override.py:65`. Consumers that require the default-inclusive dump: `generation/_reproducibility.py:204` (hashes the whole dump into `config_digest`), `execution/capacity.py:219`, `execution/_job_estimate.py:82`, `execution/_isolated_run.py:158`, `execution/_pipeline.py:12`, `verification/postmask/runner.py:53`, `verification/postmask/residual_pii.py:139`, `profile/_source.py:57`, `profile/_fixed_width_reader.py:148`, `plan/_checks_dp.py:66`. Rule: when the block is omitted, `model_dump()` omits the `invariants` key, so every dump and every `config_digest` is byte-identical to today. A characterization test pins the digest of every golden config before Slice 0a code.

### Output surfaces

| Surface | F36 disposition |
|---|---|
| Direct API, object `repr` | Exact typed report only |
| Canonical bytes, isolated sidecar | F36 codec, one 16 MiB cap, X4 writer |
| CLI stdout, stderr, JSON, quiet, verbose, help, parse errors, root callbacks | X1 factories via X6; quiet is silent; findings and R-FAIL exit 0 |
| CLI requested file | X4 `publish_invariant_artifact`; path never printed |
| Engine user-visible warnings and progress | X1 events only |
| Engine server-admin logs, crash output, traceback locals | V2-LOG (V1 bar); X1 events are still the only F36 emitter |
| Governor, probe | Existing fields unchanged; failed rungs carry no report |
| Platform DB row, artifact HTTP body | X4 writers; X5 route without response model |
| FastAPI 404, 422, framework exceptions, OpenAPI | F31 process boundary; hostile path, query, body tests |
| Platform user-visible `Job.log`, job events, notifications, evidence | F31 events only; no F36 payload; B-9 gets one sibling section |
| Platform server logs, audit, telemetry, support bundles | V2-LOG (V1 bar) |

## Frozen contracts touched

| Contract | Classification and proof |
|---|---|
| Pipeline V1 config | Additive optional block; omitted block leaves dump and `config_digest` byte-identical |
| `run_pipeline`, route choice, output bytes | Unchanged with or without invariants (R-FAIL and on/off byte equality) |
| `ExecutionResult`, `IsolatedRunResult` | Additive defaulted field (trailing on `ExecutionResult`; before the last field `memory_evidence` on `IsolatedRunResult`, C4); snapshot on top of F31 (X7) |
| Governor and probe records | Unchanged; snapshots |
| Direct chunked APIs | Config refusal before first pull; shapes unchanged |
| `decoy_engine.__all__`, F29 registry | Exact additive names; new profile; next minor |
| F31 `decoy-safe-output/v1` | One new family and closed names via X1-X3; artifact never nested |
| `quality-report/v1`, grade, legacy validators | Unchanged; no invariant `quality_metrics` key |
| `decoy-invariant-report/v1` | New; enum table, field order, limits, canonical bytes frozen; change needs `/v2` |
| CLI commands and exits | Additive; findings and F36 failures exit 0 |
| Platform tables and endpoints | Additive table `job_invariant_reports` (K-JOB, append-only) and additive column `jobs.invariant_run_ordinal`, one migration; owner-scoped endpoint; legacy routes unchanged |

## Acceptance tests, all RED first

RED convention. "Fail-before" names the baseline line (engine `fdd08a89`, re-read at each slice head per "Revision 5 baseline drift"; Platform `ea68d8e` for A12a-A12d and `d8908f4` for A12e-A12l) where the behavior is absent or wrong. When the test drives an entry that exists at baseline (`PipelineConfig.model_validate`, `run_pipeline`, `run_pipeline_isolated`, the Platform hooks), its RED is behavioral at that line. When the mechanism has no baseline entry (collector, codec, kernels, lease), the RED is recorded against the slice's first commit, which holds an inert stub with the stated stub behavior; an `ImportError` is never accepted as RED. Rows marked "characterization" are green at baseline by design and must stay green; their RED evidence is the named mutation. Every generated test first asserts its marker is in the input and that its visit or emission count is positive. The killing mutation is applied to the future F36 code once, in the slice that builds it, and the evidence records the RED.

All `tests/unit/invariants/` and `tests/integration/invariants/` files are new. Platform test files are under `decoy-platform/tests/`. CLI rows name the `decoy` CLI repository, which is not attached to this session and was not read; their fail-before is stated as unverified.

| ID | Test (file::function) | Seeded violation | Fail-before at baseline | Mutation that must turn it RED | Slice |
|---|---|---|---|---|---|
| A1a | `tests/unit/invariants/test_grammar.py::test_valid_block_accepted` | minimal valid `invariants` block with one check of each kind | `config/_pipeline.py:58` `extra="forbid"` rejects the `invariants` key (C3) | drop the field from `PipelineConfig` | 0a |
| A1b | `tests/unit/invariants/test_grammar.py::test_rejects_closed_shape_violations[case]`, cases `extra_key`, `expression_text`, `sql_text`, `literal_set`, `duplicate_identity`, `partial_first_hit_group`, `checks_257`, `tolerance_nan`, `tolerance_above_1e12`, `sample_rows_on_exact`, `child_value_on_count` | each case is one hostile block | stub models use `extra="allow"` and no validators | set any model to `extra="allow"`; remove the group-completeness check | 0a |
| A1c | `tests/unit/invariants/test_grammar.py::test_identity_stable_under_reorder_and_rename` | same program with checks permuted and tables renamed | stub derives identity from list index | derive `CheckRef` from list index | 0a |
| A1d | `tests/unit/invariants/test_grammar.py::test_member_binding[case]`, cases `member_unknown`, `member_duplicate_edge_key`, `two_relationships_same_tables_selects_exact_edge`, `composite_member`, `column_unknown`, `zero_reads_on_refusal` | config with two relationships between the same parent and child tables on different columns, plus a duplicate declaration | `validators/_relationship_checks.py:50` refuses by table pair and `:258-262` takes the first child of that table (C8) | resolve the member by table pair; skip the duplicate-key check | 0b |
| A1e | `tests/unit/invariants/test_dump_shape.py::test_omitted_block_dump_and_digest_unchanged[golden]` (characterization) | every golden config, digest pinned before Slice 0a code | green at baseline: `generation/_reproducibility.py:204` hashes the dump | emit `"invariants": None` in the default dump | 0a |
| A2 | `tests/unit/invariants/test_verdicts.py::test_scopes_are_independent[case]`, cases `source_fail_output_pass`, `source_pass_output_fail`, `introduced`, `resolved`, `pure_generate_no_source`, `alignment_lost` | fixtures built so each pair of scopes disagrees | stub copies one verdict to all three scopes | derive the transition verdict from the output verdict | 2a |
| A3a | `tests/unit/invariants/test_counts.py::test_every_bucket_nonzero_and_balanced[counts_type]` | fixture family that makes every bucket of every counts type nonzero | stub returns all-zero counts | drop `rows_unevaluable` from the total | 2a |
| A3b | `tests/unit/invariants/test_counts.py::test_sampled_partial_has_unobserved_rows` | `sample_rows < population_rows` | stub sets `unobserved_rows=0` | compute `m` as `sample_rows` without the `min` | 2a |
| A3c | `tests/unit/invariants/test_counts.py::test_sampled_clean_full_coverage_is_indeterminate` | `sample_rows >= population_rows`, no failure | stub reports `pass` | return `pass` when `unobserved_rows == 0` | 2a |
| A4a | `tests/unit/invariants/test_kernels.py::test_compare_truth_table[op-left-right]` | every op times {value, null, NaN, inf, overflow, non-comparable type} | stub treats null as false | compare null as a value | 2b |
| A4b | `tests/unit/invariants/test_kernels.py::test_null_and_null_pair_truth_tables[op]` | every null op times null patterns | stub | swap `left_implies_right` operands | 2b |
| A4c | `tests/unit/invariants/test_kernels.py::test_sum_equals_decimal_tolerance[case]` | values at tolerance, tolerance plus one ulp, overflow, non-finite | stub uses float `isclose` | use float arithmetic | 2b |
| A4d | `tests/unit/invariants/test_kernels.py::test_checksum_reuses_validate[scheme]` | each scheme, with a spy on `decoy_engine.checksums.validate` (`checksums.py:351-369`) | stub reimplements Luhn | reimplement any scheme (spy count 0) | 2c |
| A5a | `tests/unit/invariants/test_collector_barrier.py::test_lifecycle_cell[state-event]` | generated: 5 states times the 5 table columns, each driven by barriers | stub collector without lock or state | let `OPEN` admit while `close_pending` | 1a |
| A5b | `tests/unit/invariants/test_collector_barrier.py::test_feed_feed_merge_is_order_independent` | two concurrent feeds released in both orders | stub merges without the lock | merge outside the lock | 1a |
| A5c | `tests/unit/invariants/test_collector_barrier.py::test_claimant_cancel_reaches_closed` | `KeyboardInterrupt` raised in the claimant while `DRAINING` | stub leaves `DRAINING` | drop the claimant-cancel transition | 1a |
| A5d | `tests/unit/invariants/test_collector_barrier.py::test_deadline_marks_unmerged_not_run` | a feed held past the fake-clock deadline | stub waits forever | merge a result after `drain_expired` | 1a |
| A6a | `tests/unit/invariants/test_relationship_eval.py::test_fk_shape_against_oracle[shape]` | generated from the FK-shape table, against an independent Python oracle | stub reports zero counts | drop one composite key component | 3b-1 |
| A6b | `tests/unit/invariants/test_relationship_eval.py::test_child_in_later_batch_is_matched` | child rows split so the matching child arrives in a later batch | stub evaluates per batch | evaluate per batch instead of over staged keys | 3b-1 |
| A6c | `tests/unit/invariants/test_relationship_eval.py::test_multiplicity_change_at_constant_totals` | output moves one child between two parents, row totals equal | stub compares totals only | compare totals instead of per-parent multiplicity | 3b-1 |
| A6d | `tests/unit/invariants/test_relationship_eval.py::test_first_hit_uses_graph_edge_order` | two parents whose config order is the reverse of their sorted order, one key present in both | `_graph.py:166-167` prose says declared order; the sort is `:252-262` and the route consumes it at `_pandas_adapter.py:380` (C1) | evaluate the group in config order | 3b-1 |
| A6e | `tests/unit/invariants/test_relationship_eval.py::test_reconciliation_counts_parent_rows[case]`, cases `null_parent_value_is_unknown`, `non_numeric_child_is_unevaluable`, `childless_parent_compares_zero`, `duplicate_parent_keys` | parent and child fixtures per case | legacy fails null parents (`_relationship_checks.py:319-321`) | count null `parent_value` as failed | 3b-2 |
| A6f | `tests/unit/invariants/test_relationship_eval.py::test_reconciliation_multi_parent_first_hit_only` | child key present in an earlier parent of the same group | no baseline counterpart (legacy ignores multi-parent) | attribute every matching child row to this edge | 3b-2 |
| A7a | `tests/unit/invariants/test_scratch_lease.py::test_toctou_refused[evasion]`, evasions `symlink`, `rename`, `hard_link`, `fd_reuse`, `absent_procfs`, `bridge_drift` | each evasion staged between check and use | stub opens by path | open one path without the dirfd; remove the post-query identity check | 3a-1 |
| A7b | `tests/unit/invariants/test_scratch_lease.py::test_temp_size_cap_configured` | low `max_temp_directory_size` | `out_of_core/_duckdb.py:42` sets only `temp_directory` and `memory_limit` | omit `max_temp_directory_size` | 3a-2 |
| A7c | `tests/unit/invariants/test_scratch_lease.py::test_spill_within_budget_and_no_python_growth` | 1e5 and 1e6 rows under a low limit, `tracemalloc` peak compared | stub collects keys in a Python set | collect keys in a Python set | 3a-2 |
| A7d | `tests/unit/invariants/test_scratch_lease.py::test_duckdb_interrupt_honoured` | long query interrupted at the deadline | spike result; stop if it fails | ignore `interrupt()` | 3a-2 |
| A8a | `tests/integration/invariants/test_route_matrix.py::test_route_cell[route-shape-batch_rows]` | generated from the executable capability manifest times batch rows 1, 7, 65,536; asserts canonical bytes equal the resident oracle, route telemetry, output bytes equal to invariants-off, `rejected_feeds == 0`, nonempty observations | no hook: full-frame `_pipeline_full_frame.py:194-200` then `:243`; unified `_unified_slice.py:314`; sequential `_sequential.py:590` (`observed_commit`); OOC `out_of_core/_runner.py:313-315`; mixed `_pipeline_mixed_route.py:268`; multi-table `_pipeline_multi_table.py:260-402`; auto-chunk `_pipeline_auto_chunk.py:218-350` | delete one route's hook (that cell has no observations) | 5a, 5b, 6a, 6b, 7b |
| A8b | `tests/integration/invariants/test_route_matrix.py::test_source_read_count_equals_off[route]` | read counter wrapped around each source loader | characterization of today's read counts | add one source read in any hook | 5a, 5b, 6a, 6b, 7b |
| A8c | `tests/integration/invariants/test_route_matrix.py::test_refused_cell_zero_reads[cell]` | every refused manifest cell | no refusal exists | refuse after the first read | 5a, 5b, 6a, 6b, 7b |
| A9a | `tests/integration/invariants/test_isolated_invariants.py::test_isolated_report_equals_in_process` | same program with `isolate=True` and `False` | `_isolated_run.py:484-497` returns no report | drop the sidecar decode | 8c |
| A9b | `tests/integration/invariants/test_isolated_invariants.py::test_no_report_on_crash_timeout_or_malformed[case]`, cases `child_crash`, `timeout`, `malformed_envelope`, `oversize_sidecar`, `size_mismatch` | fault injected per case | `_isolated_run.py:453-470`, `:499-531` | attach a report on an abnormal outcome | 8c |
| A9c | `tests/integration/invariants/test_governor_probe.py::test_only_final_completed_rung_has_report` | two failing rungs then success; then exhaustion | `_governor.py:243-436` | attach a report to a failed rung | 8d |
| A9d | `tests/integration/invariants/test_governor_probe.py::test_probe_refused_zero_spawn` | program present on a probed run | `_probe.py:277` spawns | refuse after spawn | 8d |
| A9e | `tests/unit/invariants/test_chunked_refusal.py::test_direct_chunk_api_zero_pull[entry]` | counting iterator passed to `run_mask_chunked` and `_chunked.py` entries | `native/_chunked_entry.py:601` pulls | refuse after the first pull | 6b |
| A10a | `tests/unit/invariants/test_codec.py::test_max_legal_report_under_cap` | 1,792 observations with maximal counts | stub has no cap | raise `MAX_ARTIFACT_BYTES` | 0d |
| A10b | `tests/unit/invariants/test_codec_hostile.py::test_rejects[case]`, structural cases `schema_version`, `field_order`, `duplicate_key`, `trailing_bytes`, `invalid_utf8`, `unknown_reason`, `size_cap`, `unknown_enum_member` | one mutation of a valid canonical payload per case | stub decoder accepts any JSON | replace an exact type check with `isinstance` | 0d |
| A10c | `tests/unit/invariants/test_codec_hostile.py::test_rejects[case]`, count cases `count_negative`, `count_above_max`, `count_bool`, `exact_row_total_unbalanced`, `exact_row_evaluated_unbalanced`, `sampled_population_unbalanced`, `sampled_observed_above_max_sample`, `exact_transition_unbalanced`, `sampled_transition_unbalanced`, `relationship_child_unbalanced`, `relationship_parent_buckets_unbalanced`, `relationship_duplicate_groups_without_rows`, `join_change_unbalanced` | one count changed by one so a single equation breaks | stub decoder checks no count | remove one balance equation or the upper bound | 0e |
| A10d | `tests/unit/invariants/test_codec_hostile.py::test_rejects[case]`, shape cases `verdict_disagrees_with_counts`, `not_run_with_counts`, `counts_member_wrong_for_code`, `sampled_member_on_reconciliation`, `report_status_disagrees`, `observations_unsorted` | balanced payload with a forged field | stub | skip the verdict recomputation | 0e |
| A11 | `tests/integration/invariants/test_selected_scope.py::test_subset_and_quarantine_differ_from_full_source_oracle` | subset and quarantine fixture where a full-source oracle gives different counts | full-frame quarantine at `_pipeline_full_frame.py:194-200` | feed the unsubsetted, unquarantined source | 5a |
| A12a | `tests/test_invariant_persistence_facts.py::test_run_job_and_worker_create_no_job_attempt` (characterization, Platform) | run one flag-off and one flag-on job | green at baseline (F-P2, F-P3) | not applicable; pins why K-JOB was chosen | 11a-0 |
| A12b | `tests/test_invariant_persistence_facts.py::test_hooks_receive_no_attempt_or_fence` (characterization) | inspect the hook signature and call sites | `runner.py:289-291`, `:589`; `dispatch_worker_main.py:505` | not applicable | 11a-0 |
| A12c | `tests/test_invariant_persistence_facts.py::test_attempts_minted_only_for_sealed_attestation` (characterization) | non-sealed job through admission | `admission.py:268`, `:281`, `:351-356` | not applicable | 11a-0 |
| A12d | `tests/test_invariant_persistence.py::test_report_reaches_hook_and_never_fails_job` | spy on the hook; carrier raises inside F36 | `runner.py:66-73` returns `None` | raise from the carrier into job completion | 11a-0 |
| A12e | `tests/test_invariant_persistence.py::test_write_refused_when_status_not_success` | flip the job row to `failed` before the hook transaction | no table exists at `d8908f4`; RED against the slice's first commit, whose stub writer inserts unconditionally | write without the `status = 'success'` clause | 11a-1 |
| A12g | `tests/test_invariant_persistence.py::test_db_error_rolls_back_hook_job_succeeds` | DB error injected in the hook session | quality hook pattern `runner_hook.py:141-153` | re-raise | 11a-1 |
| A12h | `tests/test_invariant_persistence.py::test_cascade_migration_round_trip_exact_bytes` | delete parent row; upgrade, downgrade, upgrade; downgrade leaves `jobs` columns as at `d8908f4` | no migration | drop `ON DELETE CASCADE`; leave the column on downgrade | 11a-1 |
| A12j | `tests/test_invariant_persistence.py::test_late_hook_from_earlier_run_is_refused` (the ordering test) | run N starts (ordinal 1) and its hook is held; the job is re-pended by the reaper path and run N+1 starts (ordinal 2), succeeds, and persists R2; then run N's held hook delivers R1 | no table at `d8908f4`; the copied quality pattern (`api/quality/service.py:77` append plus `:114-119` highest `id`) returns R1. RED recorded against the slice's first commit, whose stub writer checks only `status = 'success'` and whose stub reader takes the highest `id`: the reader returns R1 | drop `invariant_run_ordinal = :ordinal` from the write predicate (R1 row exists, test RED) | 11a-1 |
| A12k | `tests/test_invariant_persistence.py::test_reader_returns_only_current_ordinal` | a row with `run_ordinal = 1` and a larger `id` inserted directly after the current-ordinal row (bypassing the writer); then the job re-pended and restarted with no report yet | stub reader takes the highest `id` | drop the `r.run_ordinal = j.invariant_run_ordinal` join from the reader | 11a-1 |
| A12l | `tests/test_invariant_persistence_facts.py::test_every_run_passes_the_start_owner[flag_off, flag_on, sequential_readmission, retry_promotion]` (characterization) | each path driven to its run start with a spy on `v2_orchestrator.py:132-134` | green at `d8908f4` by F-P9; it is the premise of the ordering rule | not applicable; if any case is RED at the 11a-0 reconcile check, the slice stops (repeat-class note) | 11a-0 |
| A12i | `tests/test_invariant_artifact_api.py::test_endpoint_surface[case]`, cases `owner_only`, `admin`, `no_row_fixed_body`, `hostile_path`, `hostile_query`, `hostile_body`, `openapi_no_response_model`, `adapter_only_response` | per case | no endpoint | add a `response_model`; return a dict | 11b |
| A13 | `tests/test_invariant_b9_f35.py::test_b9_section_and_f35_eligibility[case]`, cases `one_section_per_committed_run`, `score_grade_outcome_unchanged`, `zero_evaluated_refused`, `sampled_refused`, `transition_refused` | output observation with `rows_evaluated == 0` offered to F35 | no section | drop `rows_evaluated > 0` from eligibility | 11b |
| A14a | `tests/perf/invariants/test_scale.py::test_100m_narrow_rows_within_budgets` | 100,000,000 narrow rows | none | raise a budget | 7c |
| A14b | `tests/perf/invariants/test_scale.py::test_row_limit_plus_one_before_staging[side]` | 100,000,001 rows on source, then output | none | check the limit after staging (staging counter positive) | 7c |
| A14c | `tests/perf/invariants/test_scale.py::test_checksum_throughput_10m_evidence` | 10M rows per scheme; records evidence only | none | not applicable (evidence) | 7c |
| A14d | `tests/perf/invariants/test_scale.py::test_feed_overhead_ratio_evidence[route]` | invariants on versus off; records RF-T evidence | none | not applicable (evidence) | 7c |
| A15a | `tests/integration/invariants/test_rfail_matrix.py::test_fault_kind_does_not_change_run[route-kind]` | generated: routes times the ten R-FAIL kinds; output bytes, commit call, result, and `quality_metrics` equal the no-F36 run | commit sites listed in A8a | guard re-raises (Mutation A); guard swallows without reason (Mutation B) | 1b, 5a-8c |
| A15b | `tests/integration/invariants/test_rfail_matrix.py::test_route_exception_still_fails_run[route]` | route read error raised outside F36 frames | characterization | widen the guard around route code (Mutation C) | 5a-7b |
| A16a | `tests/unit/invariants/test_deadline.py::test_in_process_deadline_is_max_drain_from_claim` | fake monotonic clock; a feed never released, in-process | `_isolated_run.py:258-319` has no bound | measure from run start, or return `inf` | 1b |
| A16b | `tests/integration/invariants/test_isolated_deadline.py::test_payload_carries_exec_budget_and_spawn_wall` | `timeout_s=10` | `_isolated_run.py:385-392` payload has no such keys | set `exec_budget_s = timeout_s` (no reserve) | 8b |
| A16c | `tests/integration/invariants/test_isolated_deadline.py::test_starved_finalize_still_completes` | `timeout_s=6`; fault-injected finalize that blocks until its deadline | `_isolated_run.py:450-454` kills at `timeout_s` with no F36 cutoff | ignore `cutoff_s` or `skew_s` (child killed, outcome `crashed`) | 8b |
| A16d | `tests/integration/invariants/test_isolated_deadline.py::test_kill_during_sidecar_write_leaves_no_partial_report` | SIGKILL after the temp write and before the rename, with a caller-owned `staging_dir` | `_isolated_run.py:499-531` abnormal path | write the final name directly, or write the envelope before the sidecar | 8c |
| A16e | `tests/integration/invariants/test_isolated_deadline.py::test_timeout_none_uses_in_process_rule` | `isolate=True`, `timeout_s=None` | `_isolated_run.py:450` with `timeout=None` | apply a zero cutoff when `timeout_s` is `None` | 8b |
| A17a | `tests/unit/invariants/test_ooc_verdict_join.py::test_missing_row_nr_gives_alignment_lost` | drop `row_nr` 5 from the spilled source verdicts | `_stream_driver.py:317` spills no verdict; `:416-443` joins none | join by position | 7a |
| A17b | `tests/unit/invariants/test_ooc_verdict_join.py::test_duplicate_row_nr_gives_alignment_lost` | repeat `row_nr` 5 | same | skip the duplicate check | 7a |
| A17c | `tests/unit/invariants/test_ooc_verdict_join.py::test_out_of_order_row_nr_gives_alignment_lost` | swap `row_nr` 5 and 6 | same | sort before joining | 7a |
| A17d | `tests/unit/invariants/test_ooc_verdict_join.py::test_accounted_quarantine_excluded_before_alignment` | accounted set {5}, output lacks row 5 | same | check alignment before exclusion | 7a |
| A17e | `tests/unit/invariants/test_ooc_verdict_join.py::test_ooc_accounted_set_is_empty_and_fed` (characterization of C7) | OOC run with a validator config | `_pipeline_route_dispatch.py:55-77` passes no `quarantine_config` | bypass step 1 | 7b |
| A17f | `tests/unit/invariants/test_ooc_verdict_join.py::test_ipc_read_error_is_rfail_internal_failure` | truncate the spill after phase 1 | same | classify the read error as `alignment_lost`, or re-raise | 7a |
| A18a | `tests/unit/invariants/test_measure_invariants.py::test_signature_and_typed_return` | call with keyword inputs | no such function | return bytes | 3c-1 |
| A18b | `tests/unit/invariants/test_measure_invariants.py::test_rejects_inputs_before_read[case]`, cases `record_batch_reader`, `pandas_frame`, `table_subclass`, `missing_referenced_table`, `quarantine_unsorted`, `quarantine_out_of_range` | counting wrapper proves zero reads | none | use `isinstance`; validate after the first read | 3c-1 |
| A18c | `tests/unit/invariants/test_measure_invariants.py::test_graph_parity_with_run_pipeline[shape]` | every FK-shape fixture | graph built inline at `execution/_pipeline_context.py:318-326` | build the graph without the orphan-policy lookup | 3c-1 |
| A18d | `tests/unit/invariants/test_measure_invariants.py::test_source_none_gives_no_source_side` | `source=None` | none | measure output only and mark transition `pass` | 3c-1 |
| A18e | `tests/unit/invariants/test_measure_invariants.py::test_inputs_unchanged` | deep copy and buffer addresses compared before and after | none | write telemetry into `config` | 3c-1 |
| A18f | `tests/unit/invariants/test_measure_invariants.py::test_internal_failure_returns_not_run_report` | fault injected in a kernel | none | re-raise | 3c-1 |
| A18g | `tests/sentry/test_validate_no_mutation.py::test_extra_targets_are_scanned` and `::test_catches_mutating_measure_invariants` | fixture `measure_invariants` that writes `config["x"] = 1` | `test_validate_no_mutation.py:55` regex misses the name (C5) | remove the name from `_EXTRA_TARGETS` | 3c-2 |
| A18h | `tests/sentry/test_capability_matrix.py::test_capability_matrix_is_up_to_date` with the regenerated invariant section | add a check code without regenerating | `scripts/gen_capability_matrix.py:160-214` renders no invariant section (C5) | drop the invariant section from `render()` | 3c-2 |
| A19 | `tests/unit/invariants/test_result_fields.py::test_result_field_order_and_keyword_construction` | field order of `ExecutionResult` and `IsolatedRunResult`; AST scan for positional construction | `_isolated_common.py:386-388` requires `memory_evidence` last (C4) | append `invariant_report` after `memory_evidence` | 8b |

The marker corpus (raw cell, key, name, path, DSN, credential, exception, class-name, Unicode, short, numeric, UUID-shaped, SQL-shaped, and control-character markers) is `tests/unit/invariants/test_marker_corpus.py::test_marker_absent[surface-marker]` over every V1 rule 3 surface in "Output surfaces" (admin-log rows are V2-LOG); the field walk is `tests/unit/invariants/test_field_walk.py::test_every_report_field_is_count_enum_or_ref`; the exit matrix is `tests/unit/invariants/test_exit_matrix.py::test_owner_exit[owner-exit]` generated from "Owned state and exit paths". Each enumerates the rows listed in this plan; making a new surface, field, or exit without a row fail collection is completeness machinery and is V2-LOG.

## Hostile corpus: gate breaks, each with a RED-first test

B5 (conflict on retry) is deleted with the conflict design; B11 replaces it.

| # | Break | Test (file::function) | Seeded violation | Fail-before | Slice |
|---|---|---|---|---|---|
| B1 | CLI prints a summary with no F31 factory | engine `tests/unit/invariants/test_safe_output_family.py::test_factories_are_the_only_event_constructors`; CLI `tests/test_report_invariants.py::test_no_cli_local_event_construction` | a CLI-local event dict | engine: no `safe_output/families/invariants.py` exists; CLI: unverified, repository not attached | 4, 10 |
| B2 | Sidecar, CLI file, or Platform byte writer is an unregistered sink | the F31 analyzer test `test_unregistered_writer_flagged[writer]` run once per writer before its X4 entry | writer added without its manifest entry | the analyzer does not exist until F31 lands (unverifiable at baseline; the slice reconcile check stops if absent) | 8c, 10, 11a-1, 11b |
| B3 | DuckDB error drops publication or is swallowed | A15a cells `[route-duckdb_error]` | DuckDB raises inside an F36 query | commit sites in A8a | 1b, 5a-7b |
| B4 | Close starvation, stranded `DRAINING` | A5a cells `[OPEN-close]`, `[DRAINING-close]`; A5c; A5d | feed after `close_pending`; claimant cancel; deadline | stub collector | 1a |
| B6 | OOC source scope re-reads source | A8b cells `[ooc_reorder]`, `[ooc_resident]`, `[ooc_batch_join]` | a second read in the hook | `_stream_driver.py:317` is the only raw read | 7b |
| B7 | Numeric marker surfaces as a measurement | `tests/unit/invariants/test_marker_corpus.py::test_marker_absent[report-numeric]` and Platform `[db-numeric]`, `[http-numeric]` | cell value 987654321987, larger than every fixture count | no report exists; RED against the stub that adds a deviation field | 0c, 2c |
| B8 | Full-frame hook before quarantine | `tests/integration/invariants/test_route_matrix.py::test_full_frame_counts_are_post_quarantine` | quarantine fixture whose pre-quarantine counts differ | `_pipeline_full_frame.py:123` runs before `:194-200` | 5a |
| B9 | All-quarantined output reports exact pass | `tests/unit/invariants/test_verdicts.py::test_all_quarantined_output_is_indeterminate` and A13 `[zero_evaluated_refused]` | every output row quarantined | stub reports `pass` | 2a, 11b |
| B10 | Self-reference on OOC claims OOC measured it | A8a cell `[ooc_requested-self_reference-*]` asserting telemetry `full_frame` and `measured_exact` | self-referencing FK with OOC requested | `out_of_core/_compat.py:482-492` rejects and falls back | 7b |
| B11 | A late hook from an earlier run is read as the current report | A12j, A12k | run N's hook delivered after run N+1's | `api/quality/service.py:114-119` pattern takes the highest `id` | 11a-1 |

## Implementation slices

**Per-slice bound.** Every slice changes at most 10 production files and at most 800 lines in total, counting added plus deleted lines (`git diff --numstat` against the slice's base), including tests, test data, generated docs, and migrations. A moved block counts on both sides. The estimates below are plan estimates; when a slice's real diff would cross either bound, the worker stops at the last green commit and the slice is split by the owner of the plan, never by widening the bound. Each slice writes its RED tests first, implements, reruns prior parity, mutates its mechanism once, then gets adversarial review.

New modules have a 450-line plan ceiling and need no census entry. Census rule: existing `tests/sentry/test_module_size.py` entries may only ratchet down to the exact new LOC; no new entries and no upward bumps. Hook sites at the merged head (`wc -l`, census line): `_sequential.py` 699 (`:116`, one under the hard max), `out_of_core/_runner.py` 644 (`:120`), `native/_chunked_entry.py` 665 (`:98`), `_isolated_run.py` 651 (`:71`); `out_of_core/_stream_driver.py` 566 and `_chunked.py` 586 are now off the census and must stay at or below 600; `_pipeline_route_exec.py` is at 600 and must stay at or below 600. Each hook is a single guard call; extract first where needed.

| Slice | Repo | Content | RED | Prod files (est.) | Changed lines incl. tests (est.) |
|---|---|---|---|---|---|
| 0a | engine | grammar models, limits, `PipelineConfig.invariants` optional field, dump-shape rule and digest characterization | A1a, A1b, A1c, A1e | 3 | 650 |
| 0b | engine | member binding by exact edge key | A1d | 2 | 400 |
| 0c | engine | report types, frozen enum table, public exports | field walk | 3 | 400 |
| 0d | engine | codec structure, canonical bytes, byte cap | A10a, A10b | 2 | 500 |
| 0e | engine | codec count bounds, balance equations, shape table, verdict recomputation | A10c, A10d | 2 | 550 |
| 1a | engine | collector state table, lock, condition, claimant | A5a-A5d, B4 | 1 | 750 |
| 1b | engine | `InvariantRunGuard`, precomputed fallback, `InvariantDeadline` D-IN | A16a, unit-level A15 kinds, B3 unit | 2 | 500 |
| 2a | engine | verdict rules, count assembly, deterministic sampling, zero-evaluated rule | A2, A3a-A3c, B9 unit | 2 | 550 |
| 2b | engine | row compiler and compare, null, null_pair, sum kernels | A4a-A4c | 2 | 750 |
| 2c | engine | checksum kernel reusing `checksums.validate`; numeric-marker report test | A4d, B7 | 1 | 250 |
| 3a-1 | engine | descriptor-anchored scratch lease and TOCTOU refusals | A7a | 1 | 550 |
| 3a-2 | engine | DuckDB bridge, temp-size cap, budget, `interrupt()` spike (stop if it fails) | A7b-A7d | 2 | 450 |
| 3b-1 | engine | relationship evaluator (pk, fk coverage, orphan, join change), graph-order first-hit | A6a-A6d | 1 | 750 |
| 3b-2 | engine | reconciliation evaluator | A6e, A6f | 1 | 450 |
| 3c-1 | engine | shared graph-resolution helper extracted from `_pipeline_context.py` (net LOC not grown), `measure_invariants` | A18a-A18f | 3 | 650 |
| 3c-2 | engine | `_EXTRA_TARGETS` sentry tightening, capability-matrix section and regenerated doc | A18g, A18h | 2 prod (generator, doc) | 250 |
| 4 | engine | X1-X3 family module, reasons profile and minor bump, F31 reconcile check (needs F31 engine landed) | B1 engine half | 3 | 350 |
| 5a | engine | route-matrix harness (capability manifest, oracle, read counter, all test code); full-frame and unified hooks | resident A8a-A8c cells for these two, A11, B8 | 3 | 750 |
| 5b | engine | mixed and multi-table hooks | their A8a-A8c and A15 cells | 2 | 300 |
| 6a | engine | behaviour-preserving extraction from `_sequential.py` (699) to make room, census ratchet down; sequential hook | characterization, sequential A8 and A15 cells | 3 | 500 |
| 6b | engine | auto-chunk hook, direct chunk-API refusal | streamed A8a-A8c cells, A9e | 3 | 450 |
| 7a | engine | OOC verdict join (pure unit) | A17a-A17d, A17f | 1 | 550 |
| 7b | engine | phase-1 spill carrier and phase-3 output verdicts, OOC hooks, extraction from `out_of_core/_runner.py` where needed | A17e, OOC A8a-A8c and A15 cells, B6, B10 | 4 | 750 |
| 7c | engine | scale evidence and the pre-staging row-limit check | A14a-A14d | 1 | 300 |
| 8a | engine | extract `_write_payload` into `_isolated_payload.py`, census ratchet down, no behaviour change | characterization (existing isolated tests unchanged and green) | 3 | 250 |
| 8b | engine | D-ISO partition (payload keys, worker cutoff), `IsolatedRunResult.invariant_report` before `memory_evidence` | A16b, A16c, A16e, A19 | 5 | 500 |
| 8c | engine | sidecar lease with temp-rename-then-envelope, parent decode on the completed path only | A9a, A9b, A16d, B2 engine writer | 4 | 600 |
| 8d | engine | governor and probe | A9c, A9d | 2 | 250 |
| 9 | engine | compatibility, snapshots after F31 (X7), docs, release identity | snapshots | up to 6 (docs) | 600 |
| 10 | `decoy` CLI | `report invariants`, X4 file publisher, exact pin | B1 CLI half, B2 CLI cell | unverified (repo not attached) | unverified; stop and split if over |
| 11a-0 | Platform | characterization of F-P1 to F-P9 and the report carrier to both hooks, no persistence | A12a-A12d, A12l | 5 | 400 |
| 11a-1 | Platform | one migration (table plus ordinal column), ORM, ordinal bump at run start, writer, reader, hook, X4 DB entry | A12e, A12g, A12h, A12j, A12k, B11, B2 DB cell | 6 | 700 |
| 11b | Platform | artifact endpoint (X5), B-9 section, F35 fixture | A12i, A13, B9 | 5 | 550 |

Slice 0 (revision 4 estimate about 2,000 lines over 8 files) and slice 7 (about 1,700 lines) exceeded the bound and are split into 0a-0e and 7a-7c. Slice 8 is split into payload extraction (8a), the isolated run (8b), the sidecar (8c), and governor plus probe (8d). The tightest estimates (1a, 2b, 3b-1, 5a, 7b at 750) are the first candidates to split again if a RED test family grows.

## Verification and worker evidence rules

Per slice record repository and lock identities, the RED assertion, green commands, changed files with before and after line counts, non-vacuity counters, the mutation killed, checks not run, and review findings. `scripts/dev/bootstrap.sh` and `gates.sh` do not exist in `decoy-engine` at baseline (LOW 4); until they land, baselines come from `uv sync --locked` then the named `pytest`, `ruff`, and `mypy` commands in the branch's own environment, and the evidence says so.

- Name the defect class of every finding and fix the class structure, not one instance.
- Write generated tests first and record the behavioral RED, not an import error.
- Attack your own diff: mutate each new mechanism once and show the agreed test turns RED.
- Run baselines with `uv.lock` in the same environment as the branch. State omissions.
- Never widen a threshold, add a census entry, skip a test, reduce a budget, or weaken an assertion.
- If a defect of an already catalogued class reappears, stop and report it. Do not patch a third time.
- No worker merges, publishes, enables F35, or exposes a raw diagnostic without owner approval.
- Never commit while a mutation is applied. Respect the per-slice bound; stop and split rather than widen it.
- Apply the V1 bar: fix only rules 1 to 3; record admin-log, crash-output, traceback, hostile-in-process and completeness-census items under V2-LOG.

## Pre-mortem

The class table and the gate breaks above are the pre-mortem. Known risks:

1. The Slice 3a-2 `interrupt()` spike passes on one host and fails on the gate host: stop, do not patch.
2. F31 changes an X row during its own revision: the reconcile check stops the slice.
3. The run-start owner premise (F-P9) is false on some path, so a run gets no fresh ordinal: A12l at 11a-0 stops the slice. This is the fourth appearance risk of the persistence class; stop and report.
4. The ordinal bump adds one short committed transaction at every run start. If it contends with the claim transaction, it fails closed (carrier ordinal `None`, nothing persisted) and never fails the job. A12g covers the error path.
5. The D-ISO reserve is too small on a slow host: A16c on the gate host is the evidence, and a failure stops Slice 8b for the owner rather than raising a constant.
6. Merge drift: cited engine lines moved (see "Revision 5 baseline drift"); a slice that cannot place a cited mechanism stops.

## Open questions for the owner (each with the default the plan proceeds on)

1. Linux-only relationship and reconciliation checks in V1. **Default: accept** (non-Linux gets `unsupported` with `invariant.platform_unsupported` at config time).
2. Closed: K-JOB, append-only, decided by the orchestrator under owner delegation. Not reopened.
3. Checksum exact coverage above 10M rows. **Default: keep exact** and record throughput (A14c).
4. Engine-declared ownership of the Platform-detected code `invariant.report_not_persisted`. **Default: engine-declared, owner `platform`.**
5. The ordering mechanism adds `jobs.invariant_run_ordinal` in the same migration as the table, bumped at the single run-start owner. **Default: yes.** The alternative of reusing `jobs.started_at` as the run token was rejected (F-P8: retry promotion keeps it, and the start block sets it only when `None`), and so was adding an ordinal at every claim path (three or more claim sites, more scope).
6. Should a job relabelled `success` to `failed` after its hook (flag-on F4) show its report? **Default: no;** the reader requires `status = 'success'`.

## V2-LOG (logged per the V1 bar, not built; one line each, with defect class)

- Class 3 and 2: `close` releases scratch leases at the deadline while `inflight_feeds > 0` (this plan, "Collector lifecycle" table, `OPEN` and `DRAINING` rows). No V1 path was found. Probe: `.venv/bin/python -I <scratch>/probe/close_race.py {conn|cursor} 40` and `close_race_nointr.py {conn|cursor} 30`, run with duckdb 1.5.4, pyarrow 24.0.0 and CPython 3.11.18 from `uv sync --locked` at merge `92f4b83`. A thread runs a spilling `ORDER BY` over 30M rows while the main thread interrupts (or does not), closes the connection, and deletes `temp_directory`. Result: 140 of 140 runs ended with a Python `InterruptException` or `OutOfMemoryException` in the feed thread, exit 0, and no native fault. The script is not committed.
- Class 1: engine server-admin logs, warnings to stderr in server contexts, crash output, traceback locals and `__cause__` chains from DuckDB or Arrow errors inside F36 frames (future `invariants/_collector.py` guard log path). The guard emits only X1 events; admin-log value-freedom is not proven.
- Class 1: Platform server logs, audit, telemetry, support bundles (this plan, "Output surfaces"). Same rule.
- Class 8: completeness census machinery: marker corpus, field walk and exit matrix "generated so that a new surface, field, or exit without a row fails collection" (this plan, the paragraph after the acceptance matrix). V1 keeps the tests for the listed surfaces and exits; the fails-on-new-entry generator is V2.
- Class 9: comment drift, "declared order" prose versus the sorted edge order at `relationships/_graph.py:166-167`, `:177` and `execution/_pandas_adapter.py:490-493` (lines at `fdd08a89`, C1).
- Class 6: hostile code in the caller's process calling `measure_invariants` (non-goal; use `run_pipeline_isolated`).

Found, out of scope, reported not fixed: class 4, legacy `validate_reconciliation_holds` resolves its edge by table pair and takes the first matching child (`validators/_relationship_checks.py:31-56`, `:258-262` at `fdd08a89`, C8). This may give wrong legacy validator output (V1 rule 1) and belongs to its own plan.

## What revision 5 did not verify

- `plan-lint` was not run (the tool is absent; see the header).
- Engine line numbers were not re-read after the merge of `origin/main` `0173c054`; only module sizes and census lines were re-measured.
- Platform facts F-P1 to F-P6 were not re-read at `d8908f4`; F-P7 to F-P9 were read there.
- Whether every Platform claim path reaches `v2_orchestrator.py:132-134`: established only from the call graph (`v2_submission.py:140` is the only caller) and the comment; A12l is the test.
- Whether a zombie run can land `success` over a later run's `running` row through the existing terminal CAS: not examined. The ordinal fence makes the report correct either way; the job-status question is Platform's.
- Platform tests run on SQLite or Postgres: not checked. `FOR UPDATE` is a no-op on SQLite, so A12j relies on the ordinal predicate, not on the lock.
- The `decoy` CLI repository is not attached; slice 10 is unverified.
- No test or code was written. This is a plan-only revision, so the RED and mutation evidence rules apply to the slices, not to this commit.
