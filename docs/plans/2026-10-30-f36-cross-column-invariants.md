Status: plan only. This document grants no implementation, release, merge, or publication approval.

Baseline: `decoy-engine` commit `fdd08a894d5dd4440a1adea0ee62bf0b670808bb`, equal to `origin/main` when framed on 2026-10-30. Planning branch: `plan/f36-cross-column-invariants`.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `/home/cam/dev-rules/risk-and-exceptions.md`, `/home/cam/dev-rules/README.md`, and the README-selected rulebooks `/home/cam/dev-rules/development-loop.md`, `/home/cam/dev-rules/workspace-and-vcs.md`, `/home/cam/dev-rules/reuse-first.md`, `/home/cam/dev-rules/exploration.md`, `/home/cam/dev-rules/scope-discipline.md`, `/home/cam/dev-rules/verification.md`, `/home/cam/dev-rules/feature-dev.md`, `/home/cam/dev-rules/testing.md`, `/home/cam/dev-rules/architecture.md`, `/home/cam/dev-rules/api-and-compatibility.md`, `/home/cam/dev-rules/data-and-migrations.md`, `/home/cam/dev-rules/security.md`, `/home/cam/dev-rules/performance.md`, `/home/cam/dev-rules/release-and-rollback.md`, `/home/cam/dev-rules/observability-and-resilience.md`, `/home/cam/dev-rules/config-secrets-and-supply-chain.md`, `/home/cam/dev-rules/code-review.md`, and `/home/cam/dev-rules/documentation.md`. Repository guidance consulted: `AGENTS.md`, `README.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and `SECURITY.md`.

Risk: **R2**. F36 adds public configuration, result fields, a versioned artifact, bounded relational scratch, concurrency, and a Platform schema. Owner approval, RED-first evidence, independent adversarial review, and exact-artifact gates apply to every slice.

# F36: Cross-column invariants and relational fidelity

## FRAME

### Problem

The engine has point validators and marginal fidelity reports, but no closed, bounded contract for declared row relations or join-cardinality change. Existing validator reports contain names, row positions, prose, and free mappings. F36 needs stable value-free identities, exact or honestly sampled counts, route parity, and a report-only seam for B-9 and future F35.

The source/output ordering prerequisite is already present at `src/decoy_engine/execution/_runner.py:234-378` and is covered by `tests/unit/execution/test_cross_column_ordering.py:83-233`. F36 retains those tests and does not reimplement ordering.

### Guarantee as properties

For every valid F36 program within the limits below:

1. The language is a closed tagged union. It has no expression text, SQL, callback, plugin, arbitrary literal set, free metadata, or user-defined operation.
2. Every observation identity is `ObservationRef(CheckRef(code, authored_position), scope)`, where scope is exactly `source`, `output`, or `transition`. Reordering declarations does not change identity. Duplicate identities fail before data read.
3. Source, output, and transition are separate observations with separate status, verdict, counts, and reason. Future F35 eligibility is only `scope=output`, `status=measured_exact`, and `verdict=pass`.
4. Exact observations report exact counts. Sampled observations report observed and unobserved populations and never claim total failures. A sampled observation with no observed failure is `indeterminate`, never `pass`.
5. Ordinary comparisons treat null as unknown. Only null checks judge nullity. Non-finite, overflowing, drifting, or non-comparable values are unevaluable, never silently passed or coerced.
6. All supported execution routes feed one semantic collector. A route either measures, delegates, or refuses before its first read with a registered reason. No `skip` capability exists.
7. Join-cardinality change compares aligned selected parent-row ordinals. No report, artifact, event, error, log, file, database row, or HTTP body contains a cell, key, row index, configured name, path, DSN, query, exception text, or value-derived label.
8. Findings are measurement only. They do not change output bytes, quarantine, publication, job success, quality grade, or legacy `quality-report/v1`. F35 alone may later enforce eligible output observations.

Every absence test first proves its seeded marker exists in input and that the expected surface emitted or was invoked.

### Non-goals

- Automatic discovery, inference, anomaly detection, arbitrary formulas, regex, Python, SQL, plugins, or set-membership syntax.
- Gating, quarantine policy, output mutation, a new quality score, or a change to legacy validators.
- Distributed execution or support above the selected-row limits.
- Containment against malicious Python or native code already executing in the caller's process.
- A generic aggregate registry, generic serializer, generic unseal API, or engine-owned file, database, or HTTP transport.

### Definition of done

- The closed grammar, side-specific report, schema-specific codec, collector, and every V1 evaluator are public and tested.
- All route and FK manifests are executable test data. Supported cells are byte-equal to the resident oracle; refused cells perform zero reads.
- F31 ordinary events render summaries, while F36 canonical bytes use only the F36 codec.
- Engine, CLI, and Platform surfaces pass the hostile marker corpus, field walk, exit matrix, barrier matrix, compatibility corpus, and schema snapshots.
- B-9 stores a sibling invariant section without changing score, grade, job outcome, or legacy payloads. The F35 fixture accepts only exact output pass.
- Each slice is independently gateable and small enough for at most two worker remediation rounds.

## F31 assumptions

F31 is a prerequisite only for ordinary output. The branch plan is read from `origin/plan/f31-safe-output:docs/plans/2026-10-30-f31-shared-safe-output-boundary.md`.

| Item | Promise or owner |
|---|---|
| Provenance-backed `SafeEvent` and sealed `SerializedSafePayload` | F31, "The single model" and "One serializer and fail-closed behavior" |
| `SafeOutputScope`, exception conversion, process-entry containment | F31, "Exception boundary", "Third-party and ambient containment", Slices 2-3 |
| Named `families.quality` factories and shared analyzer | F31, "Ownership, layout, public API", Slices 1 and 5 |
| Canonical `decoy-invariant-report/v1` codec | Built here in Slice 0 |
| Collector feed lease and drain state | Built here in Slice 1 |
| Descriptor-anchored relationship scratch lease | Built here in Slice 3 |
| Bounded isolated invariant sidecar lease | Built here in Slice 7 |
| CLI artifact publisher | Built in the CLI repository in Slice 9 |
| Platform persistence and HTTP response adapters | Built in `decoy-platform` in Slice 10 |
| Generic aggregate registry or generic unseal | Not promised by F31 and explicitly not built |

No F36 module imports SQLAlchemy, FastAPI, CLI, or Platform code. If the implemented F31 surface differs from the three promised rows above, reconcile topology before Slice 0; do not amend F31 from this feature.

## Established solutions and design notes

- Named pair checks and aggregate-only result shapes follow [Great Expectations expectations](https://docs.greatexpectations.io/docs/0.18/cloud/expectations/manage_expectations/) and [result formats](https://docs.greatexpectations.io/docs/0.18/reference/learn/expectations/result_format/), without unexpected-value fields.
- Null and ordinary comparison separation follows [PostgreSQL comparison semantics](https://www.postgresql.org/docs/current/functions-comparison.html).
- Shared analyzers and separate constraints follow [Deequ key concepts](https://github.com/awslabs/deequ/blob/master/docs/key-concepts.md). F36 measures; F35 constrains.
- Stable assertion identity follows [OpenLineage data quality assertions](https://openlineage.io/docs/spec/facets/dataset-facets/data_quality_assertions/), excluding actual values and free text.
- Descriptor-relative operations follow Python's [`dir_fd` and no-check-before-use guidance](https://docs.python.org/3.12/library/os.html). DuckDB spill uses its documented [`temp_directory`, `memory_limit`, and `max_temp_directory_size`](https://duckdb.org/docs/lts/configuration/overview). `/proc/self/fd` is admitted only after the behavior described by [`proc_pid_fd(5)`](https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html) is proven by the Slice 3 spike.

No new runtime dependency is planned. High cohesion puts grammar, row evaluation, relationship evaluation, collector state, and codec in separate modules. One fact lives in one place: enums and limits in `_registry.py`, transition rules in `_collector.py`, wire order in `_codec.py`, and route capabilities in one executable manifest. The public API uses typed context objects, not long parameter lists or behavior flags.

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

All Pydantic models use `extra="forbid"`. `position` is authored in `1..65535`. `sample_rows` is required only for sampled row-local checks. Tolerance is finite `Decimal` in `[0, 10^12]`. References resolve once during compile and names never enter report types. Relationship members must be the complete declared-order first-hit group for one child tuple.

### Hard sizes and frame meanings

| Constant | Value and meaning |
|---|---|
| `MAX_CHECKS` | 256 declarations |
| `MAX_RELATIONSHIP_MEMBERS` | 8 complete ordered members |
| `MAX_SAMPLE_ROWS` | 1,000,000 observed rows per sampled scope |
| `MAX_SELECTED_ROWS_PER_SIDE` | 100,000,000, independently for source and output |
| `MAX_EVAL_BATCH_ROWS` | 65,536; larger Arrow input batches are sliced internally before a kernel feed |
| `MAX_OBSERVATIONS` | 1,792: 256 relationship checks times 7 scoped observations |
| `MAX_ARTIFACT_BYTES` | 16,777,216 canonical bytes, for file, isolated sidecar, DB, and HTTP alike |

A relationship declaration emits source and output observations for `rel.pk.unique`, `rel.fk.coverage`, and `rel.fk.orphan`, plus one transition observation for `rel.join.cardinality_change`: seven total. A row-local declaration emits source, output, and transition observations: three total. The maximum legal report fixture must encode below `MAX_ARTIFACT_BYTES`; otherwise Slice 0 stops and narrows the schema before implementation. There is no second envelope or transport frame size. Isolated execution uses the same bounded canonical sidecar bytes and keeps them out of `result.json`.

### Counts, status, and verdict

`ExactRowCounts` fields are `rows_total`, `rows_evaluated`, `rows_passed`, `rows_failed`, `rows_null_unknown`, `rows_unevaluable`.

```text
rows_total = rows_evaluated + rows_null_unknown + rows_unevaluable
rows_evaluated = rows_passed + rows_failed
```

`SampledRowCounts` fields are `population_rows`, `observed_rows`, `observed_passed`, `observed_failed`, `observed_null_unknown`, `observed_unevaluable`, `unobserved_rows`.

```text
population_rows = observed_rows + unobserved_rows
observed_rows = observed_passed + observed_failed
              + observed_null_unknown + observed_unevaluable
```

No sampled type has `rows_failed` or any population-failure field. `ExactTransitionCounts` contains comparable, introduced, resolved, persistent-fail, persistent-pass, unknown, and unevaluable counts. `SampledTransitionCounts` adds `population_rows`, `observed_rows`, and `unobserved_rows` and uses the same observed transition buckets. `RelationshipCounts` and `JoinChangeCounts` are exact-only and use the field list below.

For source or output scope: a known failure yields `fail`; no failure plus any unevaluable row yields `indeterminate`; exact clean yields `pass`; sampled clean yields `indeterminate`. For transition scope: an introduced failure or changed parent multiplicity yields `fail`; no such failure plus unknown or unevaluable alignment yields `indeterminate`; exact clean yields `pass`; sampled clean yields `indeterminate`. `not_run` and `unsupported` always pair with `indeterminate` and a registered reason.

F35 identifies `(code, position, scope)` and may satisfy a required invariant only from exact output pass. It never treats a source or transition pass as output eligibility.

### Collector lifecycle

`InvariantCollector` owns one lock, one condition, `inflight_feeds`, `close_pending`, bounded accumulators, and the table below. Feed admission increments `inflight_feeds` under the lock; kernel or DuckDB work runs outside it; merge and decrement run under it in `finally`, then notify. Finalize changes `OPEN` to `DRAINING`, rejects new feeds, waits for admitted feeds to reach zero, then snapshots and enters `FINALIZING`. No callback, query, codec, or sink runs under the lock.

| State | `feed` | `finalize` | `close` |
|---|---|---|---|
| `OPEN` | admit lease, evaluate, merge | become `DRAINING` | become `CLOSED` if no feed; otherwise set `close_pending` |
| `DRAINING` | reject | one finalizer waits; others reject | set `close_pending`; draining feeds still merge |
| `FINALIZING` | reject | reject | set `close_pending` |
| `FINALIZED` | reject | reject | release and become `CLOSED` |
| `CLOSED` | reject | reject | idempotent no-op |

After a draining feed decrements to zero, `close_pending` wins and closes without a report; otherwise the claiming finalizer enters `FINALIZING`. Failed assembly closes with a fixed reason. Feed/feed merge is serialized. Cancellation cannot strand an inflight count.

### Row and relationship semantics

Comparison and sum kernels use lossless Arrow common types only. Null is unknown. NaN, infinity, overflow, unsupported types, and drift are unevaluable. Checksums call `decoy_engine.checksums.validate`. Deterministic sampling selects `m=min(sample_rows,population_rows)` midpoint positions `floor(((2k+1)*population_rows)/(2m))` and aligns source/output on those positions.

Relationship semantics reuse `src/decoy_engine/relationships/_graph.py:71-204` and existing validator truth definitions at `src/decoy_engine/validators/_fk_validators.py:72-301` and `_relationship_checks.py:31-342`. Complete composite tuples are atomic. All-null child tuples are null orphans; partial-null tuples are unmatched. Multi-parent groups use declared-order first-hit. Duplicate non-null parents remain visible and make only join change indeterminate. Parent ordinals are `(member_position,row_position)` and never leave the evaluator. Subset compares the post-closure selection. Quarantine alignment happens before both side and transition collection.

`InvariantScratchLease` retains root and child descriptors, creates fixed names relative to a dirfd with exclusive non-following opens, checks type, owner, device, inode, and link count, and gives Arrow duplicated file objects. DuckDB receives only a proven live `/proc/self/fd/<fd>` bridge and configured memory and temp-size limits. It streams aggregate results, never keys. Missing capability, procfs mismatch, identity drift, or unsupported platform refuses before staging. The worker does not proceed from the capability spike to SQL until every evasion test is RED then green.

### Artifact and isolation ownership

`encode_invariant_report_v1` and `decode_invariant_report_v1` are schema-specific engine functions. They require exact types, closed fields, canonical order, duplicate-key rejection, finite bounded numerics, F29 membership, UTF-8, byte cap, and byte-for-byte re-encode equality. `SerializedInvariantReportV1` is an opaque exact type with no mapping or generic unseal. It is not a `SerializedSafePayload`.

`measure_invariants` is a normal in-process library API. It accepts typed Arrow inputs and executes no callback, plugin, expression, or user code. It does not claim containment against malicious same-process code. A caller with that threat model uses existing `run_pipeline_isolated`; isolation parity is required, but isolation is not mandatory for trusted direct API use.

F36 builds only an engine sidecar lease for existing isolated execution. The CLI owns exclusive stage, fsync, rename, abort, and cleanup for requested report files. Platform owns its DB transaction and HTTP response. F31 owns ordinary status, error, log, and human/JSON events.

### Platform persistence contract

Slice 10 adds `job_invariant_reports` with exactly:

| Column | Contract |
|---|---|
| `job_id` | Existing job ID type; FK to `jobs.id`; `ON DELETE CASCADE`; composite PK part 1 |
| `section_position` | Nonnegative integer `<=65535`; composite PK part 2 |
| `schema_version` | Fixed `decoy-invariant-report/v1` token |
| `artifact_bytes` | Non-null binary, `1..MAX_ARTIFACT_BYTES`; exact canonical bytes |

The composite PK is the uniqueness and idempotency key. A same-key retry with byte-identical content is a no-op. Different bytes at the same key raise fixed `invariant.report_conflict` and never overwrite. Reads are ordered by `section_position ASC`. The service flushes but never commits the caller-owned transaction. Owner-or-admin authorization applies before read. Downgrade drops only this table and its rows.

## Defect-class closure table

| # | Class | Closing structure | Non-vacuous RED-first proof | Required mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | F36 exact report/codec plus F31 ordinary events and repository-owned transports | Seed markers through every surface; prove input marker and output unit exist | Bypass named event factory or add mapping decode |
| 2 | Cleanup | Scratch lease, isolated sidecar lease, CLI file owner, caller-owned DB transaction | Owner by exit matrix covers success, refusal, error, cancel, `BaseException`, double close | Remove one `finally`, abort, or rollback path |
| 3 | State/concurrency | Lock, condition, inflight feed leases, `DRAINING`, close-pending | Barriers after admission and before merge for every transition pair and feed/feed | Remove admission increment, wait, or merge lock |
| 4 | Route parity | Executable capability manifest with measure, delegate, or pre-read refusal | Generated route matrix asserts telemetry, nonempty observations, bytes, zero-read refusal | Delete one route hook or decline reason |
| 5 | Referential integrity | Complete ordered FK-shape manifest and exact member groups | Route by stage by FK shape product with independent counts | Drop one key component or later batch |
| 6 | Untrusted producer | Exact frozen types, bounded codec, fixed errors, no producer exception formatting | Recursive field walk and hostile artifact corpus | Replace exact type check with `isinstance` |
| 7 | TOCTOU | Descriptor-anchored scratch capability and identity checks | Symlink, rename, hard link, fd reuse, absent procfs, bridge drift | Use one bare path or remove post-query check |
| 8 | Weak evidence | Same-lock RED logs, branch counters, non-vacuity, mutation per mechanism | Reviewer reruns baseline and named failing assertion | Remove marker or observation non-vacuity |
| 9 | Refactor drift | Characterization first and route-family slices | No-invariant bytes, ordering, quarantine, rewrite placement, route choice | Move unified hook before quarantine or OOC hook before rewrite |
| 10 | Frozen drift | Complete field ledger, schema snapshots, compatibility corpus | Field/default snapshots and old-reader fixtures | Reorder a field or mutate `/v1` in place |

## Reuse inventory

| Existing capability | Binding decision |
|---|---|
| F29 reason registry and manifest at `src/decoy_engine/reasons/` | Add append-only F36 reasons. Do not create another reason registry. |
| F31 ordinary events, quality factories, process containment, and analyzer | Use only the promises in the F31 assumptions table. Do not treat event payloads as invariant artifacts. |
| Strict `PipelineConfig` at `src/decoy_engine/config/_pipeline.py:45-99` | Add one optional strict block and characterize every default-inclusive dump consumer. |
| Relationship config and graph at `config/_relationships.py:24-49` and `relationships/_graph.py:71-204` | Resolve the existing complete graph. Do not build a second graph or change first-hit and cycle rules. |
| FK, orphan, reconciliation, and checksum truth definitions | Reuse algorithms or semantics from `validators/_fk_validators.py`, `validators/_relationship_checks.py`, and `checksums.py`; never reuse value-bearing result shapes. |
| Arrow, DuckDB, and out-of-core budgets | Reuse formats, memory limits, temp-size budgets, and streaming aggregates behind the new descriptor capability. No unbounded Python key map. |
| `OutputPublish` and transactional sinks | Retain publication ownership. F36 feeds and finalizes before the existing commit point and adds no output-data sink. |
| Ordering, quarantine, route, golden, and compatibility tests | Characterize before hooks and retain as release sentries. |
| Automatic module-size and public-import sentries | New files are discovered without allowlist entries. Add only the focused F36 import-boundary sentry. |
| `test_physical_seam_disconnection.py` | Keep its actual narrow role: it protects `execution.physical` imports only. Do not cite it as a general layering proof. |

## Code-filled enumerations

### Execution routes and hook placement

| Cells | Grounding and binding placement |
|---|---|
| Full-frame resident, pure mask, pure generate, generate then mask | `_pipeline_generate_mask.py:119-157,276-308`; collect after `finalize_validators_and_quarantine` and before result construction |
| Independent multi-table split and residual group | `_pipeline_multi_table.py:260-402`; one enclosing collector, one finalize |
| Mixed generate-mask and runtime decline | `_pipeline_mixed_route.py:148-282`; carry the same collector through decline |
| Unified slice | `_unified_slice.py:275-350`; collect only after `finalize_validators_and_quarantine` at `:314-320` |
| Sequential resident or transactional sink | `_sequential.py:531-638`; collect final rewritten and quarantine-aligned batches before commit |
| Auto-chunk dispatcher resident, legacy resident, dispatcher streamed | `_pipeline_auto_chunk.py:218-350,395-450`; one run collector, never average chunk rates |
| Out-of-core resident, sink batch-join, sink reorder | `out_of_core/_runner.py:125-341,450-531`; collect after FK rewrite, before commit |
| Isolated false/true | `_isolated_run.py:141-243,409-622`; in-process parity or bounded canonical sidecar |
| Governor off/on and failed rungs | `_governor.py:243-436`; only final completed `IsolatedRunResult` may carry a report |
| Probe | `_probe.py:222-352`; F36 config rejects before spawn with `invariant.probe_unsupported` |
| Direct public native and legacy chunk iterators | `native/_chunked_entry.py:601-690`, `_chunked.py:369-449`; reject F36 before first iterator pull |
| Direct `PandasExecutionAdapter.run` | `_adapter.py:121-210`; remains config-unaware; callers use `measure_invariants` explicitly |

### Owned state and exits

| Owner | Every required exit |
|---|---|
| `InvariantCollector` | finalize, feed failure, assembly failure, feed/feed, feed/finalize, finalize/finalize, feed/close, finalize/close, cancel, `BaseException`, repeated close |
| `InvariantScratchLease` | capability refusal, root/child open failure, bridge mismatch, Arrow or DuckDB failure, budget refusal, success, cancel, `BaseException`, cleanup failure, repeated close |
| `InvariantSidecarLease` | create, write, flush, fsync, close, decode, child crash, timeout, malformed envelope, parent discard, parent commit, cleanup failure, repeated dispose |
| Existing `OutputPublish` and transactional sink | context exception, normal no-commit, write or close failure, commit success/failure, abort before/after failure, repeated abort |
| Sequential, auto-chunk, out-of-core route owners | resident/sink success, decline, validation failure, stream error, input/spill close failure, exception before/after commit, caller-owned root |
| Governor and probe | flag off/on, failed or killed rung, successful later rung, exhaustion, callback failure; probe refusal before spawn |
| CLI report file owner | validation refusal, exclusive stage failure, write/fsync/rename failure, success, cancel, cleanup failure, repeated abort |
| Platform caller transaction | same retry, conflicting retry, flush failure, caller rollback, authorization refusal, missing job, cascade delete, upgrade/downgrade |

### FK shapes

The generated manifest contains:

- single-column cross-table;
- composite all-present, partial-null, and all-null;
- one parent with multiple children, including a child in a later batch;
- the same parent table with different child tuples;
- one child tuple with its complete multi-parent first-hit group;
- self-reference with a parent row in a later batch;
- multi-hop chain and diamond fan-in;
- junction table with two independent parent tuples;
- zero, one, and many child multiplicity;
- duplicate and null parent keys, unmatched keys, and an empty child;
- an earlier parent relation consumed by a later table or batch;
- generate-parent to mask-child;
- generate-child to mask-parent existing refusal;
- cycle existing refusal;
- direct public chunked refusal;
- Platform `job_invariant_reports.job_id -> jobs.id ON DELETE CASCADE`.

### Public result fields and defaults

All new domain records are frozen, slotted, exact dataclasses with no inheritance. Fields are in this order:

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
| `BoundedMeasurement` | `name`, `value`, `unit` |
| `InvariantObservation` | `observation_ref`, `structures`, `status`, `verdict`, `reason`, `counts`, `measurements` |
| `InvariantReportV1` | `schema_version`, `status`, `coverage`, `observations` |

`ExecutionResult` becomes `outputs`, `timings=()`, `boundary_conversion_ms=0.0`, `warnings=()`, `quality_metrics={}`, `table_kinds={}`, `row_errors=()`, `invariant_report=None`.

`IsolatedRunResult` becomes `outcome`, `peak_rss_mb`, `outputs`, `quality_metrics`, `table_kinds`, `returncode`, `signal_number`, `error`, `isolated`, `pid=None`, `committed_output_dir=None`, `row_errors=()`, `extra={}`, `reason=None`, `memory_evidence=None`, `invariant_report=None`.

F36 does not change these public records:

- `GovernorTripRecord`: `route`, `budget_bytes`, `observed_peak_mb`, `trip_kind`, `reroute_to`, `error`.
- `GovernorResult`: `outcome`, `final_route`, `result`, `trips=()`, `diagnostic=None`.
- `ProbePoint`: `rows`, `peak_bytes`.
- `ProbeResult`: `conclusive`, `reason`, `estimated_peak_bytes=None`, `slope_bytes_per_row=None`, `intercept_bytes=None`, `low_point=None`, `high_point=None`.

The field walk pins type, order, defaults, bounds, and union members. `SerializedInvariantReportV1` is an opaque non-dataclass with no public fields or mapping protocol.

### Output surfaces

| Surface | F36 disposition |
|---|---|
| Direct API and object `repr` | Exact typed report only; schema-specific codec only |
| Canonical bytes and isolated sidecar | F36 codec and one 16 MiB cap; no event payload reuse |
| CLI stdout, stderr, JSON, quiet, verbose, help, parse errors, root callbacks | F31 named events; quiet is silent; findings exit 0 |
| CLI requested file | CLI-owned exact artifact publisher; never print the path |
| Engine logs, warnings, progress, errors | F31 events only; no report dump or exception text |
| Governor result, trips, callback, diagnostics | Existing fields unchanged; failed rungs carry no report |
| Probe result | Existing fields unchanged; F36 refuses before child spawn |
| Platform DB and authorized API body | Exact canonical artifact, separate row and byte response |
| FastAPI 404, 422, framework exceptions, response-model serialization, OpenAPI | F31 process boundary; artifact route has no response model and returns approved byte response; hostile path/query/body tests |
| Platform logs, job events, audit, evidence, telemetry, trackers | F31 events; no new F36 audit row; B-9 gets one sibling artifact section |
| Notifications, vaults, callbacks, support bundles | No F36 payload; tests assert non-invocation or registered status event only |

## Hostile-review finding closure

| ID | Concrete design change | RED-first test | Slice effect |
|---|---|---|---|
| B1 | Assume only promised F31 events/containment; F36 owns codec and sidecar; repositories own transports | Import sentry rejects aggregate prerequisite, SQLAlchemy, FastAPI, CLI, Platform imports | Add Slice 0 and keep transports in Slices 9-10 |
| B2 | Separate exact and sampled count types; sampled has observed and unobserved only | Clean sample balances without total failures; forged population failure field rejects | Slice 2 before routes |
| B3 | One observation and verdict per scope; F35 exact output pass only | Source fail/output pass and inverse produce independent verdicts | Slices 0 and 2 |
| H4 | Inflight feed lease, condition, `DRAINING`, close-pending | Barrier after admission proves finalize waits; feed/feed included | Dedicated Slice 1 |
| H5 | Complete Platform columns, PK, FK/cascade, retry conflict, ordering, transaction rule | Same retry, conflict, rollback, cascade, ordered read | Slice 10 owns schema and service together |
| H6 | Governor/probe fields stay unchanged; probe refuses F36 before spawn | Field snapshots, failed-rung absence, zero probe spawn | Dedicated Slice 7 |
| H7 | Split resident, sequential/chunked, OOC, process, and artifact integration | Each slice starts with only its route cells RED and reruns prior parity | Replace old combined route slice with Slices 4-8 |
| M1 | Add framework-generated 404/422/OpenAPI/response-model surfaces | Hostile path/query/body through each generated surface | Slice 10 analyzer and route tests |
| M2 | Ground dirfd/procfs/DuckDB protocol and gate it with a capability spike | Symlink, rename, hard link, fd reuse, procfs absence, bridge drift, spill cap | Slice 3 spike precedes evaluator |
| L1 | Do not add module-size sentry entries or exclusions | Existing automatic sentry plus temporary oversized fixture | Every slice: new modules target 450 and no allowlist edit |
| L2 | Existing physical sentry remains narrow; add F36 import-boundary sentry | Forbidden JSON, Rich, logging, persistence, CLI, Platform imports turn RED | Slice 0 |

## Frozen contracts touched

| Contract | Classification and proof |
|---|---|
| Pipeline V1 config | Additive `invariants: InvariantProgramV1 | None=None`; default-inclusive dump gains `"invariants": None`; every dump consumer is characterized |
| `run_pipeline`, route choice, determinism, output bytes | Unchanged when omitted; no-invariant golden and route tests |
| `ExecutionResult` | Additive trailing defaulted field; full field snapshot |
| `IsolatedRunResult` and worker envelope | Additive trailing defaulted field; sidecar name is fixed envelope metadata, report bytes stay outside JSON |
| `GovernorTripRecord`, `GovernorResult`, `ProbePoint`, `ProbeResult` | Fields, defaults, `error`, `diagnostic`, and `reason` meanings unchanged; snapshots and no-F36 behavior tests |
| Direct public chunked APIs | New F36 config refuses before first pull; all old inputs and return shapes unchanged |
| `decoy_engine.__all__`, F29 reasons | Exact additive names and append-only reasons only |
| F31 `decoy-safe-output/v1` | Used unchanged for ordinary events; invariant artifact is not nested or registered there |
| `quality-report/v1`, grade, legacy validator report | Frozen unchanged; no invariant `quality_metrics` key |
| `decoy-invariant-report/v1` | New closed schema; field order, enum tokens, limits, canonical bytes, and strict reader frozen; changes require `/v2` |
| CLI commands and exits | Additive report command/file option; measured findings remain exit 0; existing no-F36 modes unchanged |
| Platform tables and endpoints | Additive table and owner-scoped endpoint; legacy quality routes, audit behavior, and B-9 primary sections unchanged |
| Release identity and pins | Chosen from the current lineage after F31 is gated; exact artifact and consumer pins are release evidence, not hard-coded plan numbers |

## Acceptance tests, all RED first

1. **Grammar and identity:** current config rejects a valid block; implementation accepts it while rejecting extra keys, expression/SQL/callable shapes, duplicate identities, partial relationship groups, and 257 checks. Reorder and name/value replacement preserve observation refs and canonical order.
2. **Scoped verdict matrix:** source fail/output pass, source pass/output fail, introduced/resolved/persistent transitions, pure-generate no-source, and alignment loss all produce independent status and verdict.
3. **Exact and sampled balance:** every bucket is nonzero across fixtures. Exact and sampled equations hold. Clean sampled is indeterminate with `unobserved_rows>0`; sampled observed failure fails; no total-failure field can be encoded.
4. **Truth tables:** every compare and null operator, arithmetic tolerance, overflow, null, non-finite, incompatible type, and checksum scheme has independent expected counts. A spy proves checksum registry reuse.
5. **Collector races:** barrier after feed admission and before merge covers feed/feed, feed/finalize, finalize/finalize, feed/close, finalize/close, cancel, and close-pending in both orders. Exactly one report or one close wins and no admitted feed is lost.
6. **Relationships:** independently computed counts cover all FK shapes, first-hit multi-parent shadowing, duplicates, nulls, empty child, later parent/child batches, and constant row totals with changed multiplicity.
7. **Scratch capability:** every TOCTOU evasion and unsupported capability refuses before staging; low memory and temp limits spill within budget; no Python collection grows with rows.
8. **Route product:** force every capability cell and batch partitions `1`, `7`, `65_536`, including unified post-quarantine and OOC post-rewrite. Compare canonical bytes and route telemetry. Refusals record zero reads.
9. **Process ownership:** isolated false/true parity, child crash/timeout/malformed envelope/oversize sidecar, governor two failures then success, exhausted governor, callback failure, probe zero-spawn refusal, and direct chunk zero-pull refusal.
10. **Artifact corpus:** maximum legal report is below 16 MiB. Mutate schema, keys, order, bounds, UTF-8, duplicate keys, trailing bytes, reason membership, numeric form, and size. Valid round trip is byte-identical; invalid input returns no partial report.
11. **Selected scope:** subset excludes a full-source violation and quarantine removes another row. Selected and aligned results differ from the deliberately wrong full-source oracle.
12. **Platform contract:** same retry, conflicting retry, rollback, cascade, authorization, old job, missing report, deterministic order, migration round trip, exact bytes, 404, 422, OpenAPI, and response-model bypass attempt.
13. **B-9 and F35:** two sections preserve declaration order and partial status; score, grade, job outcome, and legacy payload stay fixed. Only exact output pass is eligible.
14. **100M boundary:** 100,000,000 narrow rows complete within existing memory/scratch budgets; 100,000,001 on either side returns fixed `not_run` before staging. Evidence records HWM and scratch counts, never paths or values.

Generated tests include the recursive public-field walk, owner by exit matrix, transition-pair barrier matrix, seeded marker corpus, route by FK product, balance properties, and artifact mutation corpus. Each asserts a positive visit/emission/branch count before its main assertion.

## Hostile corpus: ten gate breaks

| Attack | RED witness | Slice |
|---|---|---|
| Source and output disagree but one verdict hides it | Scoped verdict matrix | 0, 2 |
| Clean sample claims zero failures or breaks balance | Sampled type and forged-field rejection | 2 |
| Finalize overtakes an admitted feed | Post-admission barrier | 1 |
| Composite FK uses only its first component | Composite relational oracle | 3 |
| A later parent or child batch is omitted | Batch-partition relationship test | 3, 5-6 |
| Failed governor rung or probe publishes a report | Failed-rung absence and zero-spawn probe | 7 |
| Scratch identity swaps after validation | Before/after query identity evasion | 3 |
| Platform duplicate stores conflicting bytes | Same-key different-bytes conflict test | 10 |
| FastAPI 404/422/OpenAPI echoes hostile input | Framework-generated surface corpus | 10 |
| Unified or OOC hook runs before final row semantics | Move-before-quarantine/rewrite mutations | 4, 6 |

The seeded corpus includes unique raw cell, key, table, column, path, DSN, URL, credential, exception, class-name, Unicode, short, numeric, UUID-shaped, SQL-shaped, and control-character markers. It scans object `repr`, canonical bytes, CLI modes/files, isolated/governor/probe state, Platform DB/API/framework errors, logs, audit, evidence, telemetry, and failure paths. These are defensive fixtures, not exploit scripts.

## Implementation slices

Every new module has a 450 physical-line plan ceiling, stays out of the module-size allowlist, and is discovered automatically by `tests/sentry/test_module_size.py`. Existing 600-line or allowlisted seams get zero net growth; extract a helper before adding a hook. Each slice writes the named behavior test first, records the intended assertion RED, implements, runs targeted plus prior parity tests, mutates its mechanism once, then receives adversarial review. Resolve every BLOCKER and HIGH before the next slice.

### Slice 0: closed models, limits, codec, and import boundary

Repository: `decoy-engine`. RED: acceptance 1, field walk, maximum-report byte cap, artifact corpus, forbidden-import fixtures. Build strict config/report models, scoped observation refs, F29 reasons, exact codec, `measure_invariants` signature, public exports, F36 import sentry, compatibility fixture skeleton. No evaluator or transport.

### Slice 1: collector feed leases and drain state

Repository: `decoy-engine`. RED: acceptance 5 and generated transition pairs. Build the lock/condition lifecycle only, with fake bounded accumulators. Prove cancellation-safe decrement, close-pending, one finalizer, feed/feed merge, and no work under the lock.

### Slice 2: row compiler and evaluator

Repository: `decoy-engine`. RED: acceptance 2-4 and sampled transition properties. Build name-to-position compile, Arrow kernels, deterministic sampling, exact/sample count types, checksum delegation, scoped report assembly. Compare whole-table with random batches.

### Slice 3: scratch capability spike and relational evaluator

Repository: `decoy-engine`. RED: acceptance 6-7. First prove the dirfd/procfs/DuckDB bridge and cleanup matrix. Stop if unavailable or identity checks are not stable. Then build bounded relationship and reconciliation scans, first-hit groups, aligned ordinals, exact counts, and validator parity.

### Slice 4: resident, mixed, multi-table, and unified routes

Repository: `decoy-engine`. RED: only those route cells from acceptance 8, including the mutation that moves unified collection before quarantine. Wire one run collector after final selected-row semantics. Rerun Slices 0-3 and no-invariant goldens.

### Slice 5: sequential and managed chunked routes

Repository: `decoy-engine`. RED: sequential resident/sink, dispatcher/legacy, resident/streamed, later-batch, and direct-public zero-pull cells. Wire one enclosing collector and eager direct-public refusal. Rerun resident parity.

### Slice 6: out-of-core routes and scale

Repository: `decoy-engine`. RED: resident, batch-join, reorder, post-rewrite mutation, budget refusal, and acceptance 14. Wire post-FK batches, no second evaluator, and measure 1M/10M/100M in the approved environment. Rerun every prior route.

### Slice 7: isolated sidecar, governor, and probe

Repository: `decoy-engine`. RED: acceptance 9, owner exit matrix, and all existing field snapshots. Build bounded sidecar lease and exact decode. Carry a report only on completed isolated results and final successful governor rung. Keep trip, diagnostic, and probe fields unchanged. Probe rejects F36 before spawn.

### Slice 8: engine compatibility and release integration

Repository: `decoy-engine`. RED: old-reader, schema snapshot, public import, no-invariant, full route artifact, and compatibility-corpus tests. Finish changelog and durable engine docs through Barry, run `pytest tests`, `ruff`, `mypy`, `scripts/test_flight.py`, coverage and mutation evidence, then select the next release identity from current lineage after F31. No hard-coded stale version.

### Slice 9: CLI rendering and artifact file owner

Repository: `decoy`. Prerequisites: released exact engine artifact and F31 CLI boundary. RED: CLI marker cells, help/parse/root errors, file exit matrix, violation exit 0. Build thin `report invariants`, run summary factories, optional exact artifact output, and exact engine pin. Existing oversized modules receive registration-only replacement-sized calls.

### Slice 10: Platform persistence, API, B-9, and F35 seam

Repository: `decoy-platform`. Prerequisites: released exact engine artifact and F31 Platform boundary. RED: acceptance 12-13 and all framework-generated surfaces. Build the exact migration/ORM/service, byte response without a response model, owner authorization, B-9 sibling section, and eligibility fixture. Do not implement F35 gating. Run migration up/down/up, full Platform tests, shared analyzer, and dependency lock checks.

## Verification, handoff, and evidence rules

Per slice record exact repository and lock identities, RED assertion, green commands, changed files, before/after line counts, route/exit/non-vacuity counters, mutation killed, full checks run and not run, review findings, remediation identity, and safe performance facts. The resident direct API is the semantic oracle, but it calls the same compiler and collector. Route telemetry proves route selection; report equality alone does not.

Release units are the gated F31 engine, gated F36 engine, CLI consumer, and Platform consumer. Consumers exact-pin the released engine. CLI and Platform roll back by reverting their consumer commit and pin. Platform downgrade deletes only F36 rows and table; deleted report rows are not recoverable. Post-release canaries cover exact, sampled, isolated, one Platform mask job, one reference-backed job, owner/non-owner API, read-back, unchanged grade, and marker absence.

Binding worker rules:

- Name the defect class and fix the class structure, not one instance.
- Write generated tests first and record the intended behavioral RED, not an import error.
- Mutate each new mechanism once before handoff and show the agreed test turns RED.
- Run baselines and implementation with `uv.lock` in the same environment. State omissions.
- Never widen a threshold, add a size allowlist, skip a test, reduce a budget, or weaken an assertion to pass.
- If a defect from an already-remediated class reappears, stop and report it. Do not patch a third round.
- No worker may merge, publish, enable F35, or expose a raw diagnostic without owner approval.

## Pre-mortem

| Likely failure | Preventive plan element |
|---|---|
| Worker waits for F31 aggregate machinery that will never exist | Short assumptions table and F36-owned exact codec |
| Sampled or side ambiguity lets F35 accept the wrong state | Separate count types, scoped observations, exact-output-only eligibility |
| Finalize loses the final batch | Inflight leases, `DRAINING`, post-admission barrier, dedicated lifecycle slice |
| Broad route remediation reaches a third round | Five route/process/integration slices with prior-parity reruns |
| Platform retry or framework error leaks or returns the wrong artifact | Composite PK conflict semantics, caller transaction, deterministic order, generated-surface corpus |
| DuckDB reopens a swapped path | Standards-grounded capability spike, retained fds, identity checks, fail-closed platform gate |
| Public fields drift while fixing governor/probe | Complete frozen field list and unchanged semantics snapshots |
| Plan bloat hides binding rules | Guarantee, manifests, design, RED tests, slices, pre-mortem, and frozen ledger are the only retained narrative |
