Status: plan only. This document grants no implementation, release, or merge approval.

Baseline: `decoy-engine` commit `fdd08a894d5dd4440a1adea0ee62bf0b670808bb`, equal to `origin/main` when framed on 2026-10-30. Planning branch: `plan/f36-cross-column-invariants`.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `/home/cam/dev-rules/risk-and-exceptions.md`, `/home/cam/dev-rules/README.md`, and the README-selected rulebooks `/home/cam/dev-rules/development-loop.md`, `/home/cam/dev-rules/workspace-and-vcs.md`, `/home/cam/dev-rules/reuse-first.md`, `/home/cam/dev-rules/exploration.md`, `/home/cam/dev-rules/scope-discipline.md`, `/home/cam/dev-rules/verification.md`, `/home/cam/dev-rules/feature-dev.md`, `/home/cam/dev-rules/testing.md`, `/home/cam/dev-rules/architecture.md`, `/home/cam/dev-rules/api-and-compatibility.md`, `/home/cam/dev-rules/data-and-migrations.md`, `/home/cam/dev-rules/security.md`, `/home/cam/dev-rules/performance.md`, `/home/cam/dev-rules/release-and-rollback.md`, `/home/cam/dev-rules/observability-and-resilience.md`, `/home/cam/dev-rules/config-secrets-and-supply-chain.md`, `/home/cam/dev-rules/code-review.md`, and `/home/cam/dev-rules/documentation.md`. Repository guidance consulted: `AGENTS.md`, `README.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, and `SECURITY.md`.

Risk: **R2**. F36 adds public configuration, a versioned report contract, exact relational measurement at bounded scale, and coordinated engine, CLI, and Platform consumers. It does not gate publication itself, but a wrong count can later make F35 accept or reject a release incorrectly. Every slice therefore needs RED evidence, targeted green evidence, adversarial review, and remediation before the next slice.

# F36: Cross-column invariants and relational fidelity, FRAME + PLAN

## FRAME

### Problem

The engine has useful point checks, but it has no one bounded declaration and measurement contract for cross-column business invariants. Existing validators are configured through a free-form parameter bag, return row positions and prose, and are attached to a free-form `quality_metrics` mapping. Existing distribution fidelity reports measure marginal and pairwise shape, not declared row relations or changes in join multiplicity. A caller cannot refer to one check by a stable, value-free identity across engine, CLI, Platform, B-9 measurement, and a future F35 contract.

The MASTER specification classifies F36 as partial and names the missing language, join-cardinality-change metric, stable check identity, safe counts, and exact-versus-sampled semantics at `/home/cam/obsidian-vault/10_Projects/decoy/Market Expansion Specs/MASTER - Decoy Feature Specification.md:1062-1086`. Platform Roadmap names the same scope and the ordering prerequisite at `/home/cam/vscode/decoy-platform/docs/ROADMAP.md:185-195`. B-9 is explicitly the report-only measurement foundation at `/home/cam/vscode/decoy-platform/docs/ROADMAP.md:141-144`.

The ordering prerequisite is already satisfied on this baseline. Merge commit `d613c804` is an ancestor of the framed HEAD. The implementation now snapshots source-read anchors and orders masked-driver reads in `src/decoy_engine/execution/_runner.py:234-270` and `src/decoy_engine/execution/_runner.py:273-378`. Non-vacuous sort-flip tests prove masked and source semantics in `tests/unit/execution/test_cross_column_ordering.py:83-120`, `:122-136`, `:139-171`, and `:174-233`. F36 must retain these as release sentries. It must not reimplement that fix.

### Guarantee, precise and testable

For every validated F36 program whose selected population is at most 100,000,000 rows on either side, where the side total is the sum of its involved tables:

1. Each declared check is one member of a closed tagged union. There is no expression text, parser, `eval`, SQL fragment, callable, plugin, arbitrary metadata, or free-form parameter mapping.
2. Each check expands to one or more immutable `CheckRef(code, position)` identities. `code` is a fixed registry member and `position` is an explicit integer in the pipeline config. Reordering checks does not change an identity. Duplicate identities are rejected before data is read.
3. The same batch semantic kernel is used by full-frame, sequential, mixed generate-mask, independent multi-table split, auto-chunk dispatcher, auto-chunk legacy oracle, auto-chunk streamed output, unified-slice, and every out-of-core subroute. Public isolated execution delegates to that pipeline. The governor returns only the final successful rung's report. Memory probes strip F36 from downscaled runs and publish no report. A route may not silently skip or change semantics. Public direct chunked iterator entry points reject any F36 program before consuming the first batch because they have no whole-run report carrier.
4. Ordinary comparisons treat any row with a null operand as unknown. Only a declared null check can pass or fail on nullity. Non-finite or non-comparable values are unevaluable, never silently coerced to pass.
5. Exact checks report exact bounded counts. A sampled check is marked sampled. A sampled check with no observed violation is indeterminate, never pass, and cannot claim zero total failures or orphans. Relationship and reconciliation checks are exact-only.
6. Join-cardinality change compares, by aligned selected parent-row position, the source and output number of matching child rows. It reports only counts and a bounded rate. It never emits a key, cell, row, table name, column name, path, DSN, query, exception text, or value-derived label.
7. Every F36 domain object and ordinary projection is value-free by construction. Its variable fields are limited to fixed schema and registry codes, closed enums, booleans, bounded nonnegative counts, bounded finite measurements, and numeric table, column, relationship, row-scope, and check positions.
8. Violations are measurement only. They do not change job success, output bytes, quarantine, or publication. Invalid F36 configuration fails before data read with a registry code and numeric config position. F35 alone may later turn eligible observations into a publication decision.

The acceptance suite proves each clause with fixtures whose source and output values differ while expected codes and counts stay fixed. Every leak test first proves that its canary exists in the input and that at least one observation was produced, so absence assertions cannot pass vacuously.

### Definition of done

F36 is done only when all of the following are true:

1. The engine exports the closed program and report types, evaluates all declared V1 checks, and emits `decoy-invariant-report/v1` through the F31 safe-output boundary.
2. Equality, inequality, ordering, null, row-sum arithmetic, parent-child reconciliation, and the existing checksum schemes have one documented V1 semantic definition.
3. Each declared relationship can produce exact primary-key, foreign-key, orphan, and join-cardinality-change observations against the intended selected population.
4. Stable `CheckRef` identities survive declaration reorder, batch size changes, execution route changes, and source/output value changes.
5. The report contains no row indices or free strings. Counts balance mechanically, and all unavailable or indeterminate cases carry a fixed reason code.
6. Exact, sampled, reference-absent, pure-generate, subset, quarantine, empty, null, non-finite, incompatible-type, duplicate-parent-key, and 100M-cap cases have non-vacuous tests.
7. All execution routes either run the shared collector or fail before data read. No configured check is reported as success when it did not run.
8. B-9 consumes F36 as a separate report-only invariant section. It does not fold invariant results into `overall_score`, `grade`, or the existing `quality-report/v1` schema.
9. The future F35 consumer contract is pinned: only `measured_exact` plus `pass` is gate-eligible. `measured_sampled`, `not_run`, `unsupported`, and `indeterminate` cannot satisfy a strict contract.
10. CLI and Platform ordinary messages use only F31 event factories. Aggregate file, DB, and HTTP surfaces use only `InvariantAggregateProjectionV1` and `SerializedSafeArtifact`. Both contain codes, positions, statuses, counts, and measurements only. Human labels resolve only from F31 registry codes at the final renderer and never print configured names or data values.
11. The new artifact has compatibility-corpus coverage, deterministic canonical bytes, a schema tamper test, and a documented reader policy.
12. New engine, CLI, and Platform modules stay below 450 physical lines. No touched module newly crosses the 600-line target or 700-line hard cap. The existing 622-line isolated runner and 681-line out-of-core runner get no net line growth, as does the existing 600-line route-exec seam. Isolated artifact work lives in `_isolated_invariant_codec.py`. Existing oversized CLI and Platform modules are registration-only or untouched as pinned in the size inventory below.
13. Engine, CLI, and Platform slices pass their own targeted and full gates at recorded commit identities, followed by the specified cross-model gate chain.

### Boundaries and non-goals

In scope:

- a V1 closed invariant grammar;
- row-local comparison, null, arithmetic, and checksum measurement;
- exact relationship integrity and join-cardinality-change measurement;
- source-versus-output deltas for aligned selected populations;
- a value-free typed report and F31 projection;
- route parity through 100M selected rows;
- report-only CLI, B-9, and Platform consumption;
- the stable seam that future F35 contracts reference.

Out of scope:

- arbitrary formulas, Python, SQL, regex, callbacks, user-defined functions, constants derived from customer cells, or plugins;
- automatic invariant discovery, correlation learning, anomaly detection, or business-rule inference;
- new set-membership syntax. The existing `column_in_set` validator remains a legacy validator because its user-provided allowed values do not belong in the new fixed V1 grammar;
- gating, quarantine, output mutation, or publication policy. F35 owns enforcement;
- a new fidelity grade, cross-check aggregate score, or claim of population fidelity;
- distributed execution, multi-organization policy, or support beyond the 100M selected-row cap;
- changing the already-shipped cross-column ordering behavior;
- replacing `quality-report/v1`, legacy validators, or STORM in this feature.

### Phase pillars

1. **Close the language:** strict tagged models and registry codes make unsupported operations unrepresentable.
2. **Count without carrying values:** batch kernels retain counters and bounded scratch state, not failing values or row positions.
3. **Keep one semantic kernel:** route adapters feed the same collector instead of implementing route-specific checks.
4. **Separate measurement from judgment:** F36 reports; B-9 presents; F35 later gates exact eligible observations.
5. **Prove the negative:** canary corpus, construction sentries, route parity, mutation tests, and independent adversarial review prove value freedom and non-vacuity.

## Defect-class closure table

All ten catalog classes apply. F36 adds a public result, bounded scratch files, a stateful collector, many execution routes, relationship measurement, public producers, and three repository consumers, so treating any class as N/A would hide a real failure mode.

| # | Catalog class | Applies | Structure that closes the class | Sentry or property test | Required mutation that turns it RED |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes | F31 event types own ordinary messages. Its required exact `InvariantAggregateProjectionV1`, `AggregateProjectionProvenance`, `SerializedSafeArtifact`, schema-specific reader/writer, file owner, and DB/HTTP adapters own aggregate output. F36 domain types have no general serializer or unseal. | Seeded marker corpus across every event and artifact surface, emitter/reader AST sentry, canonical round-trip and tamper tests. Each proves nonempty input and output. | Bypass the exact factory, accept a mapping in a sink, add a generic unseal, or make the reader skip canonical re-encode validation. |
| 2 | Cleanup missing on an exit path | Yes | One private `InvariantScratchLease` context manager owns the DuckDB connection and every evaluator-created scratch child. Existing `OutputPublish` and transactional sinks retain publication ownership. | Generated owner-by-exit matrix covers success, refusal, evaluator error, sink error, timeout, cancellation, `BaseException`, commit, abort, double close, and close after commit. | Delete the lease `finally` cleanup or one publication abort call. |
| 3 | State machine and concurrency | Yes | `InvariantCollector` has one lock and the written `OPEN -> FINALIZING -> FINALIZED -> CLOSED` transition table below. Its lock and state are never shared with scratch or publication ownership. | Barrier-forced tests cover feed/finalize, finalize/finalize, feed/close, and finalize/close in both orders and prove both actors crossed the barrier. | Remove the collector lock or permit feed while `FINALIZING`. |
| 4 | Route parity and silent fallback | Yes | One executable route-capability manifest enumerates every route and subroute below. Every supported cell invokes the shared collector; every refused cell returns a registered F29 reason before data read. | Generated route-by-program matrix asserts route telemetry, nonempty observations, canonical-byte parity, and a registered refusal. | Remove the mixed-route hook or omit the fallback reason at one runtime-decline site. |
| 5 | Referential integrity | Yes | The FK-shape manifest below is consumed by compile-time admission tests and relationship route tests. Unsupported cyclic and direct-chunked relationship shapes are explicit refusals. | One relational test per processing stage, route, and FK shape, including later-batch children and partial-null composites. | Drop one composite component or finalize a parent relation before its later child batch. |
| 6 | Untrusted producer | Yes | Exact frozen types, bounded tuple lengths and integers, fixed error reconstruction, F31 entry containment, and the schema-specific byte-capped aggregate reader. | Recursive field walk and hostile exact-type/artifact corpus cover subclasses, forged codes, excessive tuples, duplicate JSON keys, noncanonical bytes, hostile exceptions, and isolated child results. | Replace one exact-type check with `isinstance`, remove the reader byte cap/canonical re-encode check, or format a producer exception. |
| 7 | Check then use on a path | Yes | Linux V1 `InvariantScratchLease` anchors a private root and children with live dir/file descriptors, `O_DIRECTORY`, `O_NOFOLLOW`, `O_CREAT|O_EXCL`, and `fstat`; Arrow uses descriptor-backed file objects. DuckDB receives only live `/proc/self/fd` bridges with before/after identity checks. Unsupported platforms refuse relational measurement before staging. | AST exclusivity sentry plus symlink, rename, hard-link, fd-close, `/proc` absence, and identity-swap evasions. | Replace one descriptor-backed open with a bare `Path`, close an fd before DuckDB use, or remove one identity check. |
| 8 | Weak tests and overclaimed evidence | Yes | The evidence rules below require same-lock baselines, explicit RED assertions, branch counters, non-vacuity, exact size census, and one mutation per mechanism. | Reviewer reruns recorded baselines in the branch environment and compares the declared failing assertion and evidence counts. | Delete the observation-count assertion so a marker scan passes on empty output. |
| 9 | Behavior-preserving refactor drift | Yes | Characterization tests freeze no-invariant bytes, ordering, quarantine alignment, FK rewrite placement, and route choice before thin hooks are added. Physical-seam sentries scan new modules. | Golden and ordering tests plus one line mutation per slice. | Move the out-of-core feed before FK rewrite or the full-frame feed before quarantine alignment. |
| 10 | Frozen contract drift | Yes | The frozen-contract ledger below classifies every touched contract as additive or unchanged and gives each versioned schema a snapshot. | Schema snapshots, compatibility corpus, public import contract, and no-invariant golden bytes. | Add a field to `quality-report/v1`, change an existing `ExecutionResult` default, or change `decoy-invariant-report/v1` without a new version. |

### Collector transition table

All public operations acquire one collector lock for the state check and counter update. No callback, DuckDB operation, serialization, or sink call runs while that lock is held. `finalize` changes `OPEN` to `FINALIZING` under the lock, assembles from an immutable snapshot outside the lock, and changes `FINALIZING` to `FINALIZED` under the same lock. A failed assembly closes the collector and emits a fixed reason rather than reopening it.

| Current state | `feed` | `finalize` | `close` |
|---|---|---|---|
| `OPEN` | update bounded counters, remain `OPEN` | claim finalization, become `FINALIZING` | release owned state, become `CLOSED` |
| `FINALIZING` | reject with fixed state reason | reject with fixed state reason | mark close pending; finalizer releases and becomes `CLOSED` |
| `FINALIZED` | reject with fixed state reason | reject with fixed state reason | release owned state, become `CLOSED` |
| `CLOSED` | reject with fixed state reason | reject with fixed state reason | idempotent no-op |

## Established methodology surveyed before design

The implementation must cite these adopted patterns in the relevant module docstrings:

- [Great Expectations expectation management](https://docs.greatexpectations.io/docs/0.18/cloud/expectations/manage_expectations/) uses named column-pair expectations such as pair equality and ordered comparison. F36 adopts named, typed pair operations, but not its free-form result details.
- [Great Expectations result formats](https://docs.greatexpectations.io/docs/0.18/reference/learn/expectations/result_format/) distinguish aggregate counts from lists of unexpected values. F36 keeps counts and rejects every unexpected-value field.
- [dbt data tests](https://docs.getdbt.com/docs/build/data-tests?version=1.12) establish `unique`, `not_null`, and `relationships` as separate assertions. F36 preserves those separate meanings rather than compressing them into one score.
- [PostgreSQL comparison semantics](https://www.postgresql.org/docs/current/functions-comparison.html) distinguish ordinary comparisons with unknown null results from explicit null-safe predicates. F36 adopts that separation and makes null rules explicit.
- [Deequ key concepts](https://github.com/awslabs/deequ/blob/master/docs/key-concepts.md) separate analyzers, metrics, constraints, and shared scans. F36 adopts shared scan collectors and keeps F35 constraints outside the measurement kernel.
- [SAP HANA join cardinality](https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-performance-guide-for-developers/join-cardinality) defines cardinality in terms of each row's match count. F36 measures changes in each parent row's child match count, not merely total row-count drift.
- [JSON Schema object closure](https://json-schema.org/understanding-json-schema/reference/object) documents closed objects with `additionalProperties: false`. Every F36 config and report model uses the Pydantic equivalent `extra="forbid"` and no extension mapping.
- [OpenLineage data quality assertions](https://openlineage.io/docs/spec/facets/dataset-facets/data_quality_assertions/) demonstrates stable assertion identities and status. F36 adopts identity plus status, but rejects its actual-value, expected-value, and free-form assertion fields.

No new runtime dependency is justified. Pydantic, PyArrow, DuckDB, Hypothesis, the existing relationship graph, and `decoy_engine.checksums` supply the needed primitives.

## Grounded current-state inventory of F36-adjacent emitters

Engine rows are from the framed baseline. CLI was inspected read-only at `/home/cam/vscode/decoy` commit `e86b97cc468e49f34de7193ad4de51225132e34c`. Platform was inspected read-only at `/home/cam/vscode/decoy-platform` commit `b290593fc3d4e47bae06a6e8d6d052ff1e381c1d`. Cross-repository workers must repeat the topology gate against remote HEAD before editing.

| Emitter or seam | Grounded source | Current exposure and F36 disposition |
|---|---|---|
| Pipeline configuration | `src/decoy_engine/config/_pipeline.py:45-99` | Strict top-level Pydantic model, but no invariant block. Add one optional `invariants` field. Omission must preserve current behavior and config validity. |
| Legacy validator configuration | `src/decoy_engine/config/_validators.py:29-75` | `name` is free text and `params` is `dict[str, Any]`. Do not extend this bag for F36. Keep legacy configs readable. |
| Validator registry and aggregate report | `src/decoy_engine/validators/_registry.py:63-75`, `:78-138`; `src/decoy_engine/validators/_types.py:25-75` | Existing checks return validator names, table and column names, failing row indices, prose detail, and elapsed time. These are not safe F36 report fields. Reuse check algorithms where named below, not this report shape. |
| Full-frame validator attachment | `src/decoy_engine/execution/_pipeline_finalize.py:358-465` | Serializes the full validator report into `quality_metrics`, creates name-bearing row-error keys, and emits quarantine metadata. F36 adds no mapping here and does not reuse quarantine as its output channel. |
| Out-of-core validator attachment | `src/decoy_engine/execution/out_of_core/_validate_batch.py:193-264`, `:267-316` | Batch collector retains every failing row position and finally serializes the legacy report. Reuse its collector lifecycle pattern, but F36 retains only counters. |
| FK and orphan validators | `src/decoy_engine/validators/_fk_validators.py:72-142`, `:150-232`, `:235-301` | Internally materialize raw key tuples and externally emit row positions and named prose. F36 matches their null and orphan definitions, proves count parity, and uses bounded relation scans instead of their output type. |
| Relationship arithmetic and ordering validators | `src/decoy_engine/validators/_relationship_checks.py:31-64`, `:72-189`, `:196-342` | Parent-window and reconciliation checks exist. Reconciliation supports sum or count with tolerance, but exposes named structures and worst observed delta. F36 reuses its operation semantics and emits counts only. |
| Checksum registry | `src/decoy_engine/checksums.py:302-322`, `:351-369` | Seven reviewed schemes already have one boolean validator. The F36 checksum variant delegates to this registry and never reimplements a checksum algorithm. |
| Distribution snapshot | `src/decoy_engine/quality/snapshot.py:143-226`, `:401-501`, `:547-585` | Intermediate snapshots contain raw column names, numeric extrema and quantiles, categorical values, datetime bounds, and joint cell keys. F36 must not adapt or serialize this structure. |
| Fidelity comparator | `src/decoy_engine/quality/fidelity.py:132-218` | Emits names and aggregate similarities. It measures distributions, not declared invariants. F36 remains a sibling report and does not change its score. |
| Shape fidelity comparator | `src/decoy_engine/quality/shape_fidelity.py:58-132` | Emits column and joint names plus scores. F36 does not fold invariant counts into shape fidelity. |
| Quality report assembly | `src/decoy_engine/quality/report.py:90-172`, `:175-229`, `:260-365` | `quality-report/v1` emits time, job identity, fingerprints, names, free-form detail, warnings, and omissions. It is not the F36 schema and stays byte-compatible. |
| Inline fidelity integration | `src/decoy_engine/execution/_pipeline_finalize.py:156-190`; `src/decoy_engine/execution/_pipeline_full_frame.py:171-222` | Per-table report dictionaries are keyed by raw table name in `quality_metrics`. F36 instead adds a typed `ExecutionResult.invariant_report` field after F31, never another `quality_metrics` key. |
| Post-validation and STORM attachments | `src/decoy_engine/execution/_pipeline_finalize.py:271-355`, `:468-501` | Emit free messages and arbitrary report dictionaries. They are current exposure evidence, not F36 extension points. F31 owns their migration. |
| Execution result | `src/decoy_engine/execution/_adapter.py:42-84` | Public result currently carries a free-form `quality_metrics` dict. F36 adds only an optional immutable, value-free typed field. |
| Sequential and out-of-core route packaging | `src/decoy_engine/execution/_pipeline_route_exec.py:157-192`, `:420-467` | Both routes return early and currently package only legacy metrics. Route adapters must carry the shared F36 report without forcing full-frame residency. |
| Out-of-core rewrite hook | `src/decoy_engine/execution/out_of_core/_runner.py:450-531` | Raw and rewritten batches coexist, but legacy validation runs before FK rewrite at line 491. F36 must observe the final batch after all joins, with the raw batch retained only for that call. |
| Bounded relationship infrastructure | `src/decoy_engine/execution/out_of_core/_relation.py:41-83`, `:421-560`; `src/decoy_engine/execution/out_of_core/_payload_store.py:126-167`, `:169-245`; `src/decoy_engine/execution/out_of_core/_budget.py:508-595` | Existing Arrow IPC staging, DuckDB external aggregation, memory limits, and scratch-budget checks are the required reuse. Do not add an unbounded Python key map. |
| CLI run JSON and human fidelity | `/home/cam/vscode/decoy/src/decoy/cli/run.py:1182-1214`, `:1241-1249`, `:1370-1424` | Current code forwards report dictionaries and prints table names and messages. F36 human/JSON output must use only registered F31 events and numeric positions. |
| CLI post-hoc fidelity | `/home/cam/vscode/decoy/src/decoy/cli/report.py:1613-1745`, `:1810-1913` | Reads paths, emits them and exception text on errors, writes arbitrary report JSON, and renders names and actual values. F36 gets a separate thin module after F31. Do not grow this oversized file. |
| Platform measurement hook and persistence | `/home/cam/vscode/decoy-platform/api/quality/runner_hook.py:180-318`; `/home/cam/vscode/decoy-platform/api/quality/service.py:33-95` | Current hook samples files, computes one quality report, logs grade and score, and stores arbitrary JSON. F36 adds the exact aggregate artifact path and no raw exception or path output. |
| Platform quality APIs | `/home/cam/vscode/decoy-platform/api/jobs/routes_quality.py:56-164`, `:170-239` | `/quality` returns the stored report verbatim and `/quality/details` allowlists aggregates but still exposes column names. Add a separate invariant endpoint and section. Do not insert F36 into either legacy shape. |
| Platform quality row | `/home/cam/vscode/decoy-platform/api/models.py:1552-1585` | `JobQualityReport` is specialized for distribution score and grade. Do not overload it. Add a reversible, value-free invariant-report row in the Platform slice. |

This inventory explains why F36 cannot be a new validator name or a new `quality_metrics` dictionary. Both existing carriers can represent values that the hard constraint forbids.

## Code-filled enumerations

These are manifests, not examples. A worker updates the manifest and its generated test together if remote topology has added a route or sink. A new unlisted cell is a hard stop.

### Owned resource, state, and publication exits

F36 introduces exactly two domain owners: `InvariantCollector` for mutable measurement state and `InvariantScratchLease` for relational scratch. It delegates aggregate artifact file ownership to prerequisite F31 `InvariantArtifactFilePublish` and participates in existing publication owners. The generated exit test expands every owner against every applicable exit below and asserts a branch-hit counter, terminal state, cleanup result, and absence of raw diagnostics.

| Owner | Every exit F36 must handle | Current code grounding and required disposition |
|---|---|---|
| Planned `InvariantCollector` | normal `finalize`; exact refusal before feed; failure during feed; failure during report assembly; concurrent feed/finalize; concurrent finalize/finalize; close before finalize; close during finalize; close after finalize; repeated close; cancellation and `BaseException` | Planned symbol, not present on the baseline. It implements the transition table above. Every non-finalized exit becomes `CLOSED`; only one finalizer can publish a report. |
| Planned `InvariantScratchLease` | platform-capability refusal; root create/open/identity failure; child descriptor create/verify failure; DuckDB bridge identity/connect failure; IPC write/flush/fsync/close failure; disk-budget refusal; SQL/Arrow evaluator failure; normal measurement; report assembly failure; cancellation; `BaseException`; repeated close; unlink/close cleanup failure | Planned symbol, not present on the baseline. Reuse the current relation builder's independent connection-close ordering at `src/decoy_engine/execution/out_of_core/_relation.py:420-560`, but replace its bare-path ownership with the descriptor-anchored design below. The lease removes only exact dirfd-relative children it created and reports a fixed cleanup reason if disposal fails. |
| Prerequisite F31 `InvariantArtifactFilePublish` | validation refusal before stage; stage create, write, flush, file-fsync, or close failure; successful stage; atomic publish success or failure; abort before publish; abort after failed publish; cancellation; `BaseException`; repeated abort/dispose; parent-directory fsync failure; cleanup failure | Planned F31 aggregate-extension owner, absent from this worktree and a hard prerequisite. It owns optional CLI artifact output and isolated artifact staging. F36 never duplicates its stage, rename, fsync, abort, or cleanup logic. Every non-published exit removes its exact stage; cleanup failure emits a fixed F31 event. |
| Existing `OutputPublish` | exception in context; normal exit without commit; successful commit | `src/decoy_engine/execution/_chunked_output_sink.py:129-175`; full-frame enters it at `src/decoy_engine/execution/_pipeline_full_frame.py:114-115` and commits as its last action at `:243`. F36 report-only findings do not abort. Infrastructure failure before commit aborts. A post-commit report mutation is forbidden. |
| Existing transactional sink | batch iterator raises; writer close fails; empty commit; successful rename; rename/commit failure; abort before commit; abort after failed commit; repeated abort | Protocol at `src/decoy_engine/execution/_transactional_sink.py:58-97`, batch close paths at `:209-269`, and staging commit/abort at `:315-380`. F36 adds no second sink lifecycle. |
| Existing sequential route state | in-memory completion; sink completion; state eviction; validation failure before commit; commit success; commit failure; quarantine side-output failure; exception before commit; exception after observed commit | Writes and eviction at `src/decoy_engine/execution/_sequential.py:531-560`; commit, quarantine, abort, and post-commit failure handling at `:582-638`. Collector finalization is before commit; measurement verdicts remain report-only. |
| Existing auto-chunk input and spill | resident dispatcher; legacy oracle; streamed success; stream raises; first-batch/schema refusal; input close fails; spill replay success/failure; spill close; output sink refusal | Lanes are `src/decoy_engine/execution/_pipeline_auto_chunk.py:218-350`, selection is `:395-450`, input closes in `finally` at `:314-350`, and spill replay/close are `src/decoy_engine/execution/_chunked_output_sink.py:434-483`, `:537-557`. One run collector closes in the enclosing owner even if a lane fails. |
| Existing out-of-core runner | preflight refusal; resident completion; sink batch-join completion; sink reorder completion; validation failure; commit success/failure; exception before commit; exception after commit; relation/join/staged cleanup; caller-owned root; runner-owned root; faker-pool close | Entry and route policy at `src/decoy_engine/execution/out_of_core/_runner.py:125-186`, subroute selection at `:250-299`, and finish/commit/abort/final cleanup at `:311-341`. `InvariantScratchLease` does not assume ownership of the caller's root. |
| Existing isolated process owner | pre-spawn validation refusal; in-process delegation; child completed; child failed; child crashed; timeout/kill; `BaseException`; malformed envelope; abnormal exit; output commit; output discard; cleanup of owned staging root; caller-owned staging root | Public dispatch is `src/decoy_engine/execution/_isolated_run.py:141-243`, current result reconstruction is `:258-319`, owned-root cleanup is `:322-365`, process exits are `:409-530`, and discard/commit exits are `:533-622`. The worker envelope currently omits F36 at `src/decoy_engine/execution/_isolated_worker.py:196-238`; Slice 3 stages `SerializedSafeArtifact`, reconstructs only through the exact reader in `_isolated_invariant_codec.py`, and deletes staging on every non-commit exit. No raw child exception becomes an F36 reason. |
| Existing governor ladder | flag-off single rung; route-ineligible rung; OOM/governor-killed rung; crashed rung; successful later rung; exhausted ladder; `on_trip` callback succeeds or raises | `run_job_with_governor` is public at `src/decoy_engine/execution/_governor.py:243-436` and re-exported at `src/decoy_engine/execution/__init__.py:83-90`, `:169-175`. Failed rung projections and staged artifacts are discarded with their isolated result. Only the final successful rung's exact `invariant_projection` is returned. For an F36 job, `GovernorTripRecord.error` is `None` and `diagnostic` is rebuilt from fixed route/trip codes and numeric facts, never raw rung error. `on_trip` receives that sanitized existing type only; callback failure cannot publish a failed-rung report. |
| Existing memory probe | guard refusal; low probe failure; high probe failure; two clean points; timeout/crash/OOM; downscale with F36 config | `_run_one_probe` downscales and invokes isolated full-frame at `src/decoy_engine/execution/_probe.py:222-274`; public `probe_peak_bytes` is `:277-352` and is re-exported at `src/decoy_engine/execution/__init__.py:169-175`. The downscaled probe strips F36 before spawn and refuses any caller attempt to reintroduce it through kwargs. For an original F36 job, `ProbeResult.reason` is rebuilt from a fixed probe reason plus numeric facts and never includes reference table or child error. It keeps the honest peak estimate and never publishes an invariant report from a non-representative population. |
| Platform transaction | insert success; duplicate delivery; flush/commit failure; caller rollback; retry; job missing; unauthorized read; old job with no row; migration upgrade and downgrade | Current quality service returns an added row for caller commit at `/home/cam/vscode/decoy-platform/api/quality/service.py:33-95`; runner hook commits at `/home/cam/vscode/decoy-platform/api/quality/runner_hook.py:298-309`. The new adapter follows that ownership and never commits independently. |

### Execution-route manifest

The current top-level route decision returns `full_frame`, `sequential`, `out_of_core`, or `mixed` at `src/decoy_engine/execution/_pipeline_routing.py:510-543` and the continuation below that range. F36 covers these concrete paths:

1. Full-frame resident adapter, including pure mask, pure generate, and generate-then-mask, at `src/decoy_engine/execution/_pipeline_generate_mask.py:119-157`, `:276-308`.
2. Full-frame independent multi-table split, with dispatched tables and a residual full-frame group, at `src/decoy_engine/execution/_pipeline_generate_mask.py:158-237` and `src/decoy_engine/execution/_pipeline_multi_table.py:260-402`.
3. Full-frame auto-chunk single-table selection at `src/decoy_engine/execution/_pipeline_generate_mask.py:238-275`.
4. Unified-slice early return before the ordinary full-frame publish block at `src/decoy_engine/execution/_pipeline_full_frame.py:86-115` and its implementation at `src/decoy_engine/execution/_unified_slice.py:369-441`.
5. Sequential top-level route and its distinct result packaging at `src/decoy_engine/execution/_pipeline.py:313-320`, `src/decoy_engine/execution/_pipeline_route_exec.py:157-192`.
6. Mixed generate-mask top-level route, including runtime decline to full-frame with pregenerated tables, at `src/decoy_engine/execution/_pipeline.py:313-320`, `src/decoy_engine/execution/_pipeline_mixed_route.py:148-282`.
7. Auto-chunk dispatcher with resident output at `src/decoy_engine/execution/_pipeline_auto_chunk.py:252-288`, `:419-423`.
8. Auto-chunk legacy pandas oracle with resident output at `src/decoy_engine/execution/_pipeline_auto_chunk.py:218-249`, `:424-435`.
9. Auto-chunk dispatcher with streamed output and lazy or resident input at `src/decoy_engine/execution/_pipeline_auto_chunk.py:291-350`, `:408-418`.
10. Out-of-core without a sink, which returns resident tables, at `src/decoy_engine/execution/out_of_core/_runner.py:125-186`, `:313-320`.
11. Out-of-core sink batch-join driver at `src/decoy_engine/execution/out_of_core/_runner.py:250-299`.
12. Out-of-core sink reorder driver selected by `RouteDecision.reorder_caps` at `src/decoy_engine/execution/out_of_core/_runner.py:255-299`.
13. Public `run_pipeline_isolated`, both `isolate=True` child and `isolate=False` delegation, at `src/decoy_engine/execution/_isolated_run.py:141-243`. It stages and reconstructs sealed report bytes and must produce parity with in-process execution.
14. Managed auto-chunk internals at `src/decoy_engine/execution/_pipeline_route_exec.py:552-592` collect through a private run-owned callback or capability. Direct public native `run_mask_chunked` at `src/decoy_engine/execution/native/_chunked_entry.py:601-690` and public legacy `run_mask_pipeline_chunked` at `src/decoy_engine/execution/_chunked.py:369-449` return iterators and have no whole-run result carrier. They reject any F36 config eagerly during their existing preflight, before requesting the first input chunk, with the registered `invariant.direct_chunked_unsupported` reason. Internal managed auto-chunk does not call the rejecting public wrapper with an F36-bearing config; a sentry proves that the private collector path is used.
15. Public direct `PandasExecutionAdapter.run` remains below the config/program layer. It does not silently discover F36 config. Callers use planned `measure_invariants` explicitly; this disposition is tested at `src/decoy_engine/execution/_adapter.py:42-84`, `:121-210`.
16. Public `run_job_with_governor` at `src/decoy_engine/execution/_governor.py:243-436`, exported at `src/decoy_engine/execution/__init__.py:83-90`, `:169-175`, delegates each rung to isolated execution. Failed rung artifacts never publish or enter `GovernorTripRecord`, `diagnostic`, progress, or `on_trip`. For F36 jobs, raw rung errors are removed from the trip record and exhausted diagnostic. A completed result returns only the final successful rung's report. Flag-off preserves the single rung's report only if that rung completed.
17. Public `probe_peak_bytes` at `src/decoy_engine/execution/_probe.py:222-352`, exported at `src/decoy_engine/execution/__init__.py:169-175`, is not a measurement route for F36. Before isolated spawn, it copies the downscaled config and forces `invariants` to `None`; it rejects an F36 override in `run_pipeline_kwargs`. Probe child invariant projections must be `None`. For an original F36 job, its `ProbeResult` keeps the honest memory estimate but uses fixed, value-free diagnostics.

The capability manifest records for every cell: `collect_exact`, `collect_sampled`, `refuse_before_read`, or `delegate`, plus one F29 reason for each refusal or runtime decline. No `skip` state exists.

### FK-shape manifest

The config represents one parent and one or more children at `src/decoy_engine/config/_relationships.py:24-49`. Resolved edges preserve every parent and child column tuple at `src/decoy_engine/relationships/_graph.py:71-111`. The generated relationship matrix contains every shape below:

1. Single-column, cross-table parent and child.
2. Composite, cross-table parent and child with equal tuple widths, including an all-present match.
3. Composite child with one null component, which is partial-null and unmatched, and an all-null child tuple, which follows the pinned orphan semantics.
4. One parent declaration with multiple child entries, both children in the same batch schedule and one child arriving in a later batch.
5. Same parent table with different child key tuples. The graph's four-part edge identity supports this at `src/decoy_engine/relationships/_graph.py:149-156`.
6. One child tuple with multiple parents, represented by one complete ordered member group rather than per-edge checks. Declared-order first-hit-wins and policy agreement are current contracts at `src/decoy_engine/relationships/_graph.py:163-180`, the pandas merge is `src/decoy_engine/execution/_pandas_adapter.py:490-497`, and out-of-core merge is `src/decoy_engine/execution/out_of_core/_multi_parent.py:49-112`.
7. Self-reference within one table, including a child row whose parent occurs in a later batch. Current sequential ordering ignores only the table-level self-edge at `src/decoy_engine/execution/_sequential.py:454-461`, `:670-675`.
8. Multi-hop chain `grandparent -> parent -> child`.
9. Diamond or fan-in graph where one descendant is reached through two branches.
10. Junction table with two independent parent key tuples.
11. Parent with zero, one, and many matching children, including an empty child table and duplicate non-null parent keys.
12. Parent relation staged in an earlier batch or table and consumed by a child in a later batch or table.
13. Mixed generate-parent to mask-child, which current execution supports by merging generated outputs into sources at `src/decoy_engine/execution/_pipeline_generate_mask.py:149-157`.
14. Generate-child to mask-parent, which current config rejects as deferred at `src/decoy_engine/config/_pipeline.py:153-183`; F36 reports the existing config refusal and does not claim measurement.
15. A cycle, which current graph construction refuses at `src/decoy_engine/relationships/_graph.py:301-310`; F36 does not add a second cycle policy.
16. Direct chunked relationship measurement without a complete graph, refused before first-batch consumption as specified in the route manifest.

### Public result field manifest

All F36 public result symbols below are planned symbols and are absent from this baseline. The implementation must use frozen, slotted dataclasses with these exact fields and no inheritance. `StructureRef` is the public closed union alias `ColumnStructureRef | RelationshipStructureRef`, not another dataclass.

`ReasonCode` is the existing F29 public exact type at `src/decoy_engine/reasons/_types.py:107-130`, re-exported at `src/decoy_engine/reasons/__init__.py:51-58`, `:62-80`. An observation reason is accepted only when `type(reason) is ReasonCode`, `reason.namespace == "quality"`, and exact `(namespace, wire code)` membership passes the F29 registry at `src/decoy_engine/reasons/_registry.py:107-136`. F31 projection additionally requires its reviewed provenance token. Plain strings, subclasses, and unregistered foreign `ReasonCode` constructions are rejected at construction and projection boundaries.

| Planned public dataclass | Every field, in order |
|---|---|
| `CheckRef` | `code: CheckCode`; `position: int` |
| `ColumnStructureRef` | `side: Side`; `table_position: int`; `column_positions: tuple[int, ...]` |
| `RelationshipMemberRef` | `relationship_position: int`; `child_position: int` |
| `RelationshipStructureRef` | `side: Side`; `members: tuple[RelationshipMemberRef, ...]` |
| `RowCounts` | `rows_total: int`; `rows_evaluated: int`; `rows_passed: int`; `rows_failed: int`; `rows_null_unknown: int`; `rows_unevaluable: int` |
| `RelationshipCounts` | `parent_rows: int`; `child_rows: int`; `null_parent_key_rows: int`; `duplicate_parent_key_groups: int`; `duplicate_parent_key_rows: int`; `null_child_key_rows: int`; `matched_child_rows: int`; `unmatched_child_rows: int`; `parent_zero_match_rows: int`; `parent_one_match_rows: int`; `parent_many_match_rows: int` |
| `TransitionCounts` | `comparable_row_count: int`; `introduced_failure_count: int`; `resolved_failure_count: int`; `persistent_failure_count: int`; `persistent_pass_count: int`; `unknown_transition_count: int`; `unevaluable_transition_count: int` |
| `JoinChangeCounts` | `comparable_parent_count: int`; `changed_parent_count: int`; `increased_parent_count: int`; `decreased_parent_count: int`; `lost_all_children_count: int`; `gained_from_zero_count: int`; `unchanged_parent_count: int` |
| `BoundedMeasurement` | `name: MeasurementName`; `value: Decimal`; `unit: MeasurementUnit` |
| `InvariantObservation` | `check_ref: CheckRef`; `structures: tuple[StructureRef, ...]`; `status: ObservationStatus`; `verdict: Verdict`; `reason: ReasonCode | None`; `source_counts: RowCounts | RelationshipCounts | None`; `output_counts: RowCounts | RelationshipCounts | None`; `transition_counts: TransitionCounts | JoinChangeCounts | None`; `measurements: tuple[BoundedMeasurement, ...]` |
| `InvariantReportV1` | `schema_version: Literal["decoy-invariant-report/v1"]`; `status: ReportStatus`; `coverage: ReportCoverage`; `observations: tuple[InvariantObservation, ...]` |

The existing public `ExecutionResult` fields are `outputs`, `timings`, `boundary_conversion_ms`, `warnings`, `quality_metrics`, `table_kinds`, and `row_errors` at `src/decoy_engine/execution/_adapter.py:61-71`. F36 adds only `invariant_report: InvariantReportV1 | None = None` after `row_errors`. The generated field walk asserts the full eight-field order and all defaults, not only the new field.

F36 also adds `invariant_projection: InvariantAggregateProjectionV1 | None = None` to the end of public `IsolatedRunResult`. Its full resulting field order is `outcome`, `peak_rss_mb`, `outputs`, `quality_metrics`, `table_kinds`, `returncode`, `signal_number`, `error`, `isolated`, `pid`, `committed_output_dir`, `row_errors`, `extra`, `reason`, `memory_evidence`, and `invariant_projection`. The current fifteen fields and defaults are grounded at `src/decoy_engine/execution/_isolated_common.py:320-388`. The field walk pins all sixteen fields, and isolated parity mutates artifact staging or projection reconstruction to turn RED. There is no domain-report unseal across the process boundary.

`InvariantAggregateProjectionV1`, `AggregateProjectionProvenance`, and `SerializedSafeArtifact` are exact opaque F31 slot classes, not dataclasses and not public result records. They expose no public fields, iteration, mapping protocol, bytes accessor, `repr` payload, or unseal. Only the registered factory/reader can create the projection and provenance token; only the schema-specific renderer and registered file/DB/HTTP adapters can consume them internally.

### Output-surface manifest

Every category required by the plan contract is explicit:

| Surface | Current grounding | F36 rule |
|---|---|---|
| In-process result and direct API | `ExecutionResult` at `src/decoy_engine/execution/_adapter.py:42-84` | Typed report only. Planned `measure_invariants` returns the same exact type. No generic mapping or serializer. |
| Canonical JSON bytes | F31 symbols are absent from this worktree, and its current plan defines only `decoy-safe-output/v1` event payloads | Topology hard-stop until F31 implements the exact aggregate extension in Design section 1. Only `write_invariant_report_v1` can create `SerializedSafeArtifact`; only `read_invariant_report_v1` can validate and reconstruct `InvariantAggregateProjectionV1`. No mapping or generic unseal exists. |
| CLI stdout, human and JSON | `/home/cam/vscode/decoy/src/decoy/cli/run.py:1185-1215`, `:1222-1249`, `:1370-1399`; shared JSON emitter `/home/cam/vscode/decoy/src/decoy/ui/output.py:29-35`, `:47-93` | Render only sealed fields. Quiet mode emits nothing. Report violations keep exit 0. |
| CLI stderr and runtime errors | `/home/cam/vscode/decoy/src/decoy/cli/run.py:1048-1128` | Only fixed F31 failure events. No exception text, path, configured name, class name, or traceback-local value, including verbose mode. |
| CLI files | Existing evidence output at `/home/cam/vscode/decoy/src/decoy/cli/run.py:1141-1162` | F36 does not enter the legacy evidence payload. Optional `decoy run --invariant-report-out PATH` publishes only `SerializedSafeArtifact` through F31 `InvariantArtifactFilePublish`; the owner stages, fsyncs, renames, aborts, and never emits the path. `decoy report invariants REPORT` reads through the exact F31 reader and emits rendered stdout only. |
| CLI help, parse errors, and root callbacks | Root app and command registration at `/home/cam/vscode/decoy/src/decoy/__main__.py:69-100`; report app registration at `/home/cam/vscode/decoy/src/decoy/cli/report.py:2358-2364` | Static help text may name the command and fixed codes only. F31 containment begins before argument parsing. Unknown options, missing arguments, malformed config, and root no-command callbacks cannot echo hostile values through an F36 path. |
| Engine logs and progress | Route progress is selected and emitted around `src/decoy_engine/execution/_pipeline.py:267-320`; auto-chunk currently logs route facts at `src/decoy_engine/execution/_pipeline_auto_chunk.py:436-443` | No report object, configured name, count derived from a secret marker, scratch path, or exception is logged. Progress may carry existing fixed phase and aggregate row counts only. |
| Governor result, trip callback, diagnostic, and retry progress | Public result/callback behavior is `src/decoy_engine/execution/_governor.py:243-436`; result fields are `:193-211` and trip fields are `:169-187` | The final completed `IsolatedRunResult` alone carries the F36 report. Failed rungs publish none. For F36 jobs, trip `error` is `None`; exhausted diagnostic and retry events use fixed registered reason plus route literal and numeric facts. `on_trip` receives no raw error, report, artifact, or marker. |
| Probe result and diagnostic | Probe result fields are `src/decoy_engine/execution/_probe.py:132-172`; downscaled execution is `:222-274` | Probe strips F36 before spawn and asserts child `invariant_projection is None`; it preserves memory points/estimate but, for an original F36 job, rebuilds `reason` from a fixed registered reason and numeric facts. No reference name, child error, or downscaled invariant status escapes. |
| Platform persistence and API JSON | `/home/cam/vscode/decoy-platform/api/quality/service.py:33-95`; `/home/cam/vscode/decoy-platform/api/jobs/routes_quality.py:45-164` | Store exact `SerializedSafeArtifact` bytes in a separate owner-scoped row under the caller-owned DB transaction. Return them through the F31 invariant HTTP adapter with fixed content type and no decode to mapping. Duplicate delivery is idempotent. Existing quality shapes are unchanged. |
| Platform server and job logs | `/home/cam/vscode/decoy-platform/api/quality/runner_hook.py:127-177`, `:298-309`, `:514-525` | Fixed status/reason events only. No report dump, path, exception text, or marker derivative. |
| Audit | The current audit writer accepts value-bearing detail and structured fields at `/home/cam/vscode/decoy-platform/api/audit/logger.py:132-200`; current quality measurement paths at `/home/cam/vscode/decoy-platform/api/quality/runner_hook.py:180-318` do not call it | F36 adds no audit event. The surface test asserts zero new audit rows for measurement and read. |
| Evidence and B-9 | B-9 assembly is in `/home/cam/vscode/decoy-platform/api/evidence/assembly.py:1858-1940` | Add a sealed invariant sibling section only. Do not copy it into the CLI legacy evidence file, `overall_score`, grade, or `quality-report/v1`. |
| Notifications, telemetry, and outbound callbacks | CLI notification options are `/home/cam/vscode/decoy/src/decoy/cli/run.py:323-340` and dispatch is `:1649-1708`; engine callbacks include progress and token-vault writers, not F36 sinks | F36 writes no notification, telemetry payload, vault entry, root callback, or user callback. Surface tests assert absence and non-invocation. |
| Failure returns and exceptions | Current pipeline and isolated public entries are `src/decoy_engine/execution/_pipeline.py:113-190`, `src/decoy_engine/execution/_isolated_run.py:141-203` | Config errors expose fixed reason plus numeric position. Infrastructure failures are rebuilt at F31 boundaries. No raw exception is stored in a report. |

## Reuse inventory and physical seams

| Existing structure | Grounding | Binding reuse decision |
|---|---|---|
| F29 reason taxonomy and append-only registry | Public reason namespace at `src/decoy_engine/reasons/__init__.py:1-10`, `:38-50`; registry implementation at `src/decoy_engine/reasons/_registry.py:1-44`; sentry at `tests/sentry/test_reason_registry.py:1-20`, `:158-198` | Add F36 codes to F29 and its append-only manifest. Do not add `invariants/_registry.py` for reasons. The planned invariant `_registry.py` may contain only closed check, measurement, unit, status, and verdict enums. |
| F31 safe-output boundary | Planned only on `origin/plan/f31-safe-output`; absent here, and its current plan covers event wire only | Hard prerequisite includes the exact aggregate extension named in Design section 1. Do not start F36 or imitate missing artifact symbols. The F31 owner implements and gates the aggregate registry, provenance, exact reader/writer, file publisher, and DB/HTTP adapters first. |
| Strict top-level config | `PipelineConfig` and its optional blocks at `src/decoy_engine/config/_pipeline.py:45-99` | Add one optional field and a focused strict model module. Default-inclusive `model_dump()` honestly gains `"invariants": None`; validation and no-invariant execution semantics remain compatible. |
| Relationship model and graph | `src/decoy_engine/config/_relationships.py:24-49`; `src/decoy_engine/relationships/_graph.py:71-111`, `:134-204` | Compile from resolved edges. Do not build a second graph or change first-hit/policy/cycle semantics. |
| Existing check semantics | `src/decoy_engine/validators/_fk_validators.py:72-301`; `src/decoy_engine/validators/_relationship_checks.py:31-342` | Reuse algorithms or truth definitions, never their value-bearing result types. Count-parity tests freeze deliberate agreement. |
| Checksum registry | `src/decoy_engine/checksums.py:302-322`, `:351-369` | Call `decoy_engine.checksums.validate`. Do not copy algorithms. |
| DuckDB, Arrow staging, and budget machinery | `src/decoy_engine/execution/out_of_core/_relation.py:420-560`; `_payload_store.py:126-245`; `_budget.py:508-595`; current path-taking connection helper `src/decoy_engine/execution/out_of_core/_duckdb.py:28-54` | Reuse formats, queries, limits, and budget semantics. Do not expose the current bare-path ownership to F36. `InvariantScratchLease` supplies the descriptor-anchored Linux `/proc/self/fd` bridge and refuses unsupported platforms. No unbounded Python relationship map. |
| Publication and transactional ownership | `src/decoy_engine/execution/_chunked_output_sink.py:129-175`; `src/decoy_engine/execution/_transactional_sink.py:58-97`, `:209-269`, `:315-380` | Feed/finalize before the existing commit point. Never add a report-owned sink. |
| Ordering and alignment sentries | `tests/unit/execution/test_cross_column_ordering.py:83-233`; quarantine alignment at `src/decoy_engine/execution/_pipeline_finalize.py:193-205` | Characterize first and keep as release sentries. F36 hooks after FK rewrite and after selected-population alignment. |
| Route telemetry and route tests | Top-level route dispatch at `src/decoy_engine/execution/_pipeline.py:313-320`; route decision at `_pipeline_routing.py:510-543` | Generated manifest proves the intended route ran. A report alone is not route evidence. |
| Module-size sentry | Goals and hard limit at `tests/sentry/test_module_size.py:54-66`; allowlist and checks at `:118-126`, `:148-201` | New modules enter its census. No threshold or allowlist widening. |
| Physical-seam sentry | `tests/sentry/test_physical_seam_disconnection.py:64-140`, `:173-203` | Extend scan roots to the new package and consumer modules. Domain models cannot import CLI, Platform, JSON, Rich, logging, persistence, or sink modules. |
| Public import boundary and compatibility corpus | `tests/sentry/test_public_import_boundary.py`; `tests/integration/compat_corpus`; `tests/integration/golden` | Add explicit public names and versioned fixtures. No wildcard export drift. |
| Platform owner authorization and transaction ownership | `/home/cam/vscode/decoy-platform/api/jobs/routes_quality.py:45-98`; `/home/cam/vscode/decoy-platform/api/quality/service.py:33-95` | Reuse owner-or-admin dependency and caller-owned transaction. No bespoke authorization or internal commit. |

### Size census and mandatory seams

The baseline physical-line census is: `_pipeline_route_exec.py` 600, `_unified_slice.py` 450, `_isolated_run.py` 622, `out_of_core/_runner.py` 681, `decoy_engine/__init__.py` 480, CLI `run.py` 1717, CLI `report.py` 2373, Platform `api/models.py` 3303, Platform `api/quality/runner_hook.py` 525, Platform `api/quality/service.py` 215, and Platform `api/jobs/routes_quality.py` 420. The engine policy is a 600-line goal and 700-line hard maximum at `tests/sentry/test_module_size.py:54-66`; `_isolated_run.py` is explicitly allowlisted at `:67-69` and the 681-line runner at `:123-126`.

Therefore:

- each new engine, CLI, or Platform module has a 450 physical-line plan ceiling and a 600-line sentry target;
- `_pipeline_route_exec.py`, `_unified_slice.py`, `_isolated_run.py`, `out_of_core/_runner.py`, CLI `run.py`, CLI `report.py`, and Platform `api/models.py` get zero net line growth. Before isolated reconstruction is changed, extract all F36 artifact staging, validation, reconstruction, and cleanup into new `src/decoy_engine/execution/_isolated_invariant_codec.py`; `_isolated_run.py` may contain only a replacement-sized call to that seam. Workers extract another existing helper before adding any hook that cannot fit within these budgets;
- new Platform ORM types live in `api/quality/invariant_models.py` and are imported by the existing model registry pattern at `/home/cam/vscode/decoy-platform/api/model_registry.py:1-22`, not appended to `api/models.py`;
- new CLI behavior lives in `src/decoy/cli/report_invariants.py`; existing command modules receive registration-only edits with a changed-line budget recorded in evidence;
- every slice reports before/after physical lines. Raising a threshold, expanding an allowlist, excluding a directory, or moving code outside a scan is forbidden.

## Frozen contracts touched

| Contract | Classification and rule |
|---|---|
| Pipeline V1 config and unknown-key behavior | Additive opt-in: `invariants: InvariantProgramV1 | None = None`. Existing unknown-key rejection remains. Pydantic's current default-inclusive dump adds `"invariants": None`, so normalized config dictionaries are not byte- or key-identical. The compatibility rule for optional config additions is `docs/compatibility-contract.md:84-96`; execution, output artifacts, and route choice remain unchanged for that null field. |
| `run_pipeline` signature, route choice, determinism, and output bytes | Unchanged when `invariants` is absent. Existing deterministic-output contract is `docs/compatibility-contract.md:115-137`. |
| Public direct chunked entries | Additive validation only when F36 config is present. Existing non-F36 iterator and tuple return shapes at `src/decoy_engine/execution/_chunked.py:369-449` and `_pipeline_route_exec.py:552-592` remain unchanged. |
| `ExecutionResult` | Additive trailing defaulted field only. Existing seven fields and defaults at `src/decoy_engine/execution/_adapter.py:61-71` are frozen. |
| `IsolatedRunResult` and worker envelope | Additive trailing defaulted field plus new sealed staged member. Existing fields at `src/decoy_engine/execution/_isolated_common.py:362-388`, outcome meanings, commit/discard behavior, and non-F36 envelope remain frozen. |
| `decoy_engine.__all__` and public imports | Additive exact names only. Current public export list is `src/decoy_engine/__init__.py:350-479`; no rename or wildcard. |
| F29 reason registry | Append-only. Existing codes and identity remain frozen by `tests/sentry/test_reason_registry.py:158-198`. |
| F31 event and aggregate contracts | Prerequisite. `decoy-safe-output/v1` event wire remains frozen. F31 first adds the exact aggregate types, registry entry, provenance, schema-specific reader/writer, file publisher, and DB/HTTP adapters named in Design section 1. `decoy-invariant-report/v1` is separate. No generic aggregate, mapping conversion, schema selector, or unseal is allowed. |
| `quality-report/v1`, fidelity score, grade, and legacy validator report | Frozen unchanged. F36 is a sibling artifact and cannot add a field, score component, or `quality_metrics` key. |
| `decoy-invariant-report/v1` | New and frozen at first release. Field order, enum tokens, bounds, canonical ordering, and reader policy are snapshot-tested. V1 readers reject every unknown field and schema ID. Any field addition or incompatible change requires `/v2` with a separate exact reader/writer and registry entry. |
| CLI command names, exit meanings, help, and ordinary output modes | Additive `decoy report invariants` and `decoy run --invariant-report-out`. Measured violations remain exit 0; existing usage/runtime exit meanings and no-F36 bytes remain unchanged. Artifact publication uses only the F31 file owner. |
| Platform quality tables and endpoints | Additive new table and owner-scoped endpoint. Existing `JobQualityReport`, `/quality`, `/quality/details`, grade columns, audit behavior, and B-9 primary sections stay unchanged. Migration downgrade removes only F36 rows and table. |
| Release identities and dependency pins | Coordinated additive minors only after F31 is real and gated. Exact pin and artifact hashes are evidence, not inferred from the plan branch. |

Default-inclusive dump consumers must be characterized before the field lands. Engine call sites include the documented choke point and override return at `src/decoy_engine/config/_pipeline.py:45-55`, `src/decoy_engine/config/_override.py:32-65`. CLI runtime consumers are `/home/cam/vscode/decoy/src/decoy/api.py:440-451`, `cli/preflight.py:454`, `cli/run.py:641`, `cli/subset.py:207`, `cli/report.py:1776`, `:2046`, and `cli/demo.py:226`. Platform validation/job consumers are `/home/cam/vscode/decoy-platform/api/pipelines/v2_validation.py:281-290` and `api/jobs/v2_config.py:31-41`. A sentry searches all three repositories for `PipelineConfig...model_dump` and fails on an unclassified caller. Tests assert each consumer tolerates the additive null key, does not serialize it into a frozen external schema, and produces unchanged no-invariant execution bytes. No boundary switches to `exclude_none=True` unless its existing external contract already uses that option and a characterization test pins it.

## Design

### 1. F31 prerequisite and required aggregate-artifact extension

F31 must land before any F36 ordinary emitter or consumer. `SafeEvent`, `SafeCount`, `SafeMeasurement`, `SafeLocation`, and `families.quality` do not exist in this framed worktree; repository search verified that they are proposed symbols only. The current F31 plan at `origin/plan/f31-safe-output:docs/plans/2026-10-30-f31-shared-safe-output-boundary.md:141-178`, `:180-214` specifies only event-oriented `SafeEvent` and `SerializedSafePayload` under `decoy-safe-output/v1`. It does not specify an aggregate artifact, schema-specific reader, or unseal operation. F36 must not reinterpret those event types as an artifact format.

Before F36 Slice 0 starts, the F31 owner must amend, implement, and gate this exact additive aggregate extension:

- `AggregateProjectionSpec`: a closed registry entry binding one exact aggregate projection type to one schema ID, validator, canonical encoder, and schema-specific reader constructor;
- `AggregateProjectionProvenance`: an opaque, non-forgeable token held only by registered aggregate factories and readers;
- `InvariantAggregateProjectionV1`: an exact opaque F31 slot type registered for `decoy-invariant-report/v1`; only `families.quality.invariant_report_v1(status, coverage, observations: tuple[SafeEvent, ...])` and the exact reader can construct it with provenance. F31 defines its closed artifact record IDs before F36, so it has no import or type dependency on `InvariantReportV1`;
- `SerializedSafeArtifact`: an opaque exact type containing the registered schema ID and canonical bytes, with no mapping, attribute exposing decoded values, generic deserialize, or general unseal method;
- `write_invariant_report_v1(projection: InvariantAggregateProjectionV1) -> SerializedSafeArtifact`: the only canonical writer, requiring exact type, valid provenance, bounds, fixed enums, F29 membership, canonical observation order, and finite measurements;
- `read_invariant_report_v1(source: BinaryIO | SerializedSafeArtifact) -> InvariantAggregateProjectionV1`: the only reader, with a byte cap, UTF-8 and duplicate-key rejection, exact schema ID, closed-field validation, bounds, exact registry membership, canonical-order validation, and byte-for-byte re-encode equality before returning a newly provenanced exact projection;
- `render_invariant_report_v1(projection: InvariantAggregateProjectionV1) -> tuple[SafeEvent, ...]`: the only read projection for human/JSON rendering. It returns registered value-free events, not a domain report or mapping;
- `InvariantArtifactFilePublish`: the file owner that stages, fsyncs, atomically publishes, aborts, and cleans a `SerializedSafeArtifact`; it never returns or emits the path;
- `InvariantArtifactDbAdapter.stage(session, artifact: SerializedSafeArtifact)`: the exact DB adapter. It validates exact type/schema and stages bytes only; the Platform service owns commit/rollback. It has no read-to-mapping method. Its exact read returns `SerializedSafeArtifact` after byte cap and schema checks.
- `InvariantArtifactHttpResponse.from_artifact(artifact: SerializedSafeArtifact)`: the exact HTTP adapter. It owns fixed status, content type, and canonical body and accepts no mapping or domain report.

`decoy-safe-output/v1` remains the F31 ordinary event schema. `decoy-invariant-report/v1` is a separate registered aggregate schema, not nested in, aliased to, or versioned by `decoy-safe-output/v1`. Ordinary status, error, log, progress, and human-renderer events still use `SafeEvent`. Artifact file, DB, and HTTP paths use only the exact aggregate projection, artifact, and adapters above. There is no generic aggregate factory, mapping serializer, arbitrary schema selector, generic reader, or unseal API.

The existing F31 capabilities still required are:

- immutable provenance-backed `SafeEvent`, `SafeCount`, `SafeMeasurement`, and `SafeLocation` types;
- one registry-backed serializer and registered final sinks;
- a `families.quality` home for named factories;
- rejection of plain mappings, free strings, raw exceptions, paths, names, and unregistered IDs at ordinary sinks;
- static and runtime canary sentries shared by engine, CLI, and Platform.

F36 adds invariant-specific fixed reasons through the existing F29 registry and adds count-name enums, measurement-name enums, and named event factories through the reviewed F31 registry. It does not create a second reason registry, redactor, serializer, generic event factory, aggregate registry, artifact writer, reader, or alternative safe boundary. The internal `InvariantReportV1` is a typed computation product. Ordinary event output uses F31 events. Aggregate output uses only `InvariantAggregateProjectionV1` and `SerializedSafeArtifact`. If any exact symbol, ownership rule, or validation rule above is absent or differs after F31 implementation, topology reconciliation and a reviewed F31 extension are mandatory. No F36 slice, including model-only Slice 0, starts before that exact prerequisite is green.

The F31 plan's proposed 0.8.0 release number is stale release bookkeeping, not a usable artifact number. This repository already has tags `v0.8.0rc1` and `v0.9.0` through `v0.16.0`; `v0.16.0` resolves to `5da40c2c6ce67db86703996dd8cf245955f05eca`, whose `pyproject.toml:10` is 0.16.0. The owner must amend F31 to release its event boundary plus the aggregate extension above as 0.17.0. F36 then releases as 0.18.0. Reusing 0.8.0, shipping event-only 0.17.0 as the F36 prerequisite, or publishing a lower version is forbidden.

### 2. Closed configuration grammar

Create `src/decoy_engine/invariants/` and add optional `invariants: InvariantProgramV1 | None = None` to `PipelineConfig`. Every model is strict with `extra="forbid"` and hidden input values in errors. The structural grammar is the language. There is no textual expression field.

```text
program       := { version: 1, checks: [check, ...] }
check         := compare | null_unary | null_pair | sum_equals |
                 reconciliation | checksum | relationship
column_ref    := { table: configured-table-name, column: configured-column-name }
compare       := { kind: "compare", position: u16, left: column_ref,
                   op: eq|ne|lt|le|gt|ge, right: column_ref,
                   coverage: exact|sampled, sample_rows?: 1..1000000 }
null_unary    := { kind: "null", position: u16, column: column_ref,
                   op: is_null|is_not_null,
                   coverage: exact|sampled, sample_rows?: 1..1000000 }
null_pair     := { kind: "null_pair", position: u16, left: column_ref,
                   op: same_nullity|different_nullity|
                       left_null_implies_right_null|right_null_implies_left_null,
                   right: column_ref, coverage: exact|sampled,
                   sample_rows?: 1..1000000 }
sum_equals    := { kind: "sum_equals", position: u16, result: column_ref,
                   terms: [column_ref, 1..8], absolute_tolerance: finite decimal,
                   coverage: exact|sampled, sample_rows?: 1..1000000 }
reconciliation:= { kind: "reconciliation", position: u16,
                   parent_table: configured-table-name,
                   parent_column: configured-column-name,
                   child_table: configured-table-name,
                   child_column: configured-column-name,
                   op: sum|count, absolute_tolerance: finite decimal,
                   coverage: exact }
checksum      := { kind: "checksum", position: u16, column: column_ref,
                   scheme: luhn|npi|iban|vin|isbn13|ean13|gtin,
                   coverage: exact|sampled, sample_rows?: 1..1000000 }
relationship  := { kind: "relationship", position: u16,
                   members: [{ relationship_position: zero-based configured index,
                               child_position: zero-based child index }, 1..8],
                   coverage: exact }
```

Further closure rules:

- `checks` contains 1 to 256 entries.
- A table or column reference must resolve uniquely in the validated V1 pipeline. A relationship check's member pairs must be in range, unique, declared in source order, share the exact same child table and child-column tuple, and equal the complete resolved member group for that tuple. Omitting a parent, adding an unrelated edge, or reordering first-hit precedence is rejected before data read. Names are not an alternate selector.
- `position` is explicitly authored in the range 1 to 65,535. It is not inferred from list order.
- `sample_rows` is required only for sampled row-local checks and forbidden otherwise.
- `absolute_tolerance` is in `[0, 10^12]`, finite, parsed as `Decimal`, and never copied to an ordinary output. Relative tolerance is not in V1.
- Constants, arbitrary literal sets, operator aliases, implicit column lookup, cross-table row-wise comparison, and nested expressions are rejected.
- Validation errors are instances of the planned `InvariantConfigError`, a `PipelineConfigError` subtype with exact fixed fields `reason: ReasonCode`, `check_position: int | None`, `table_position: int | None`, `column_position: int | None`, and `relationship_position: int | None`. It suppresses the Pydantic cause at ordinary boundaries. No error reads or formats the offending name, value, Pydantic input, or exception text.

### 3. Stable check registry and structure positions

`CheckRef` is the stable check ID. It contains only a fixed `CheckCode` and the explicit `position`. The V1 registry is closed to:

```text
inv.compare.eq                 inv.compare.ne
inv.compare.lt                 inv.compare.le
inv.compare.gt                 inv.compare.ge
inv.null.is_null               inv.null.is_not_null
inv.null.same                  inv.null.different
inv.null.left_implies_right    inv.null.right_implies_left
inv.arithmetic.sum_eq          inv.arithmetic.reconcile_sum
inv.arithmetic.reconcile_count
inv.checksum.luhn              inv.checksum.npi
inv.checksum.iban              inv.checksum.vin
inv.checksum.isbn13            inv.checksum.ean13
inv.checksum.gtin
rel.pk.unique                  rel.fk.coverage
rel.fk.orphan                  rel.join.cardinality_change
```

A relationship declaration expands to four observations that share the authored position and differ by code. Registry tests prove the list is unique and that no arbitrary code can be constructed.

`StructureRef` contains only numeric positions:

- table position is the zero-based position in `PipelineConfig.tables`;
- column position is the zero-based position in that table's configured mask or generate column list;
- `RelationshipMemberRef` pairs the zero-based relationship position in `PipelineConfig.relationships` with that declaration's zero-based child position. A relationship structure carries the complete ordered tuple of member pairs for one resolved child-key group;
- side is the closed enum `source` or `output`.

Configured names exist only inside the compiled evaluator plan. They are never stored in `CheckRef`, `StructureRef`, an observation, a reason, or an ordinary projection. Reordering a table can change its display position, but it cannot change the authored `CheckRef` identity.

### 4. Row semantics

All row-local checks compile to Arrow batch kernels and one `CountAccumulator`. The accumulator keeps only bounded integers. It does not retain row indices, values, names, arrays, examples, minima, maxima, or deltas.

For each side, counts are `rows_total`, `rows_evaluated`, `rows_passed`, `rows_failed`, `rows_null_unknown`, and `rows_unevaluable`. The invariant is:

```text
rows_total = rows_evaluated + rows_null_unknown + rows_unevaluable
rows_evaluated = rows_passed + rows_failed
```

Semantics are fixed as follows:

- `eq` and `ne` support boolean, numeric, decimal, temporal, and string pairs after the documented lossless common-type normalization. Ordered operators support numeric, decimal, temporal, and string domains. Binary, dictionary, list, map, struct, union, and extension values are unsupported in V1.
- Integer normalization may widen losslessly. Decimal normalization may widen scale and precision up to Arrow decimal128 limits. Temporal normalization may widen units but never discard or invent a timezone. No string-to-number or string-to-time coercion occurs.
- If either ordinary-comparison operand is null, the row increments `rows_null_unknown`. Null checks alone decide nullity.
- NaN, infinity, overflow, failed lossless normalization, and runtime type drift increment `rows_unevaluable` and set a fixed reason. They never become false comparisons silently.
- `sum_equals` accepts numeric or decimal columns only. It evaluates `abs(result - sum(terms)) <= tolerance`. A null operand is unknown. Overflow, a non-finite operand, or an unsupported common type is unevaluable.
- `checksum` accepts string or large-string columns only, skips null to unknown, delegates each non-null value to `decoy_engine.checksums.validate`, and converts an unexpected library failure to a fixed unevaluable reason without formatting the exception.
- `reconciliation` preserves the existing sum/count, null, and absolute-tolerance contract from `src/decoy_engine/validators/_relationship_checks.py:206-224` and `:279-340`, but the new accumulator emits counts only.

For an aligned mask or subset result, a second fixed accumulator reports the seven exact `TransitionCounts` fields in the public field manifest. It compares boolean verdict states by row position only. Unknown or unevaluable states are not failures and enter their two separate transition buckets. If alignment cannot be proved, `transition_counts` is `None` and status is indeterminate with a fixed reason.

### 5. Exact versus sampled

Exact is the default. A sampled row-local check selects `m = min(sample_rows, rows_total)` positions with the deterministic midpoint formula `floor(((2*k + 1) * rows_total) / (2*m))` for `k` from 0 to `m - 1`. Selection depends only on row count and requested sample size. Aligned source and output use the same positions.

Verdicts are:

- exact with zero failed and zero unevaluable: `pass`;
- exact with one or more failed: `fail`;
- exact with unevaluable rows: `indeterminate`, even when failed is zero;
- sampled with one or more failed: `fail` for the sampled observation;
- sampled with zero failed: `indeterminate`, never pass;
- not executed: `not_run`, never pass.

Relationship and reconciliation checks reject `coverage: sampled` at config validation. A pure-generate run may measure output row-local checks. Its source observation and every source-to-output transition are `not_run` with the fixed `invariant.no_reference` reason. Join-cardinality change is also `not_run` without a selected source or approved reference frame.

### 6. Relational and join-cardinality metrics

For each relationship check, evaluate the selected source side and output side independently with the complete declared member group. A singleton group is one ordinary parent-child edge. A multi-parent group is every edge sharing one child table and child-column tuple, ordered by relationship declaration then child position. This matches current declared-order first-hit execution at `src/decoy_engine/relationships/_graph.py:163-180`, `src/decoy_engine/execution/_pandas_adapter.py:490-497`, and `src/decoy_engine/execution/out_of_core/_multi_parent.py:49-112`.

Multi-parent identity and counts are exact:

- the authored `CheckRef(code, position)` remains the observation identity. `RelationshipStructureRef.members` is the complete ordered group and is identical across its four expanded codes;
- assign each parent row a stable numeric ordinal `(member_index, row_position_within_member)`. No table or key value enters the report;
- `parent_rows` is the sum of rows across every member parent table. `null_parent_key_rows` is the sum of rows with any null key component across all members;
- duplicate groups are computed across the union of all non-null parent key occurrences. `duplicate_parent_key_groups` counts distinct keys occurring more than once across or within members, and `duplicate_parent_key_rows` counts all parent rows in those groups;
- each child row is counted once. The winning member is the first declared member containing its non-null key. A child is unmatched only when no member contains it. A later member's same-key parent rows are shadowed by first-hit precedence;
- child match multiplicity is attributed to every parent row of the winning member that carries the winning key. Shadowed later-member rows receive zero. Null parent rows also receive zero. Therefore `parent_zero_match_rows + parent_one_match_rows + parent_many_match_rows = parent_rows`;
- source/output join change aligns parent rows by the stable member pair plus row position. Any null parent key, duplicate key across or within members, changed member list, or parent-row misalignment makes only join change indeterminate; PK, FK, orphan, and exact side counts still report;
- the compiler rejects a subset of the group, duplicate member, reordered member, mixed child tuple, more than eight members, or different orphan policies. There is no per-edge observation for a multi-parent group because it would contradict execution's merged orphan and first-hit semantics.

Other null semantics match the current validators:

- a parent key row with any null component is a null parent-key row;
- an all-null child key is a null child-key row and an orphan for `rel.fk.orphan`;
- a partial-null composite child key is non-null and fails coverage when it cannot match;
- a non-null child key not present in the parent key set is an unmatched orphan;
- duplicate non-null parent keys are counted as duplicate groups and duplicate rows.

The exact `RelationshipCounts` fields are `parent_rows`, `child_rows`, `null_parent_key_rows`, `duplicate_parent_key_groups`, `duplicate_parent_key_rows`, `null_child_key_rows`, `matched_child_rows`, `unmatched_child_rows`, `parent_zero_match_rows`, `parent_one_match_rows`, and `parent_many_match_rows`. Coverage is `matched_child_rows / (matched_child_rows + unmatched_child_rows)` when the denominator is nonzero. No key or multiplicity bucket label comes out of the kernel.

Join-cardinality change is defined over aligned selected parent-row positions. For each parent row, compute the number of matching child rows within that side. Compare the source and output integer counts at the same aligned parent position. Report:

- `comparable_parent_count`;
- `changed_parent_count`;
- `increased_parent_count`;
- `decreased_parent_count`;
- `lost_all_children_count`;
- `gained_from_zero_count`;
- `unchanged_parent_count`;
- `change_rate = changed_parent_count / comparable_parent_count` when nonzero.

Duplicate or null parent keys make the affected relationship group's join-change verdict indeterminate because a parent row does not identify one stable join group. Source/output parent misalignment or group-membership drift also makes it indeterminate. PK, FK, and orphan observations still report their exact counts. This prevents a duplicate-key defect from being hidden inside an apparently valid cardinality delta.

Subset runs compare the post-closure selected source population to its output, not the original full source. Quarantine applies the existing aligned removal mask pattern in `src/decoy_engine/execution/_pipeline_finalize.py:193-205` before source/output transitions are counted. A report never compares a subset or quarantine output to an unintended full population.

### 7. Bounded execution and route integration

Add these focused engine modules, each with a 450-line plan ceiling:

```text
src/decoy_engine/invariants/
  __init__.py
  _model.py              closed config, report, count, and enum types
  _registry.py           closed check, measurement, unit, status, and verdict enums
  _compile.py            name resolution to numeric structure positions
  _row_eval.py           Arrow batch kernels
  _relationship_eval.py  exact DuckDB relationship and cardinality scan
  _scratch.py            sole scratch lease and capability
  _collector.py          shared source/output accumulators and report assembly
  _safe_projection.py    named F31 quality factories only
  _api.py                public measure_invariants entry point
```

`measure_invariants(program, source_frames, output_frames, structure_index, limits)` is the narrow engine API. `run_pipeline` uses it through a route-owned collector; B-9 uses the same API for approved reference-backed generation. The public result is immutable and value-free. It has no generic `to_dict` or JSON method.

Route integration rules:

- Full-frame passes resident Arrow batches after quarantine alignment and before `ExecutionResult` construction at `src/decoy_engine/execution/_pipeline_full_frame.py:190-222`.
- Sequential feeds raw and final output batches into the collector before sink commit, then carries the assembled report through `src/decoy_engine/execution/_pipeline_route_exec.py:157-192`.
- Mixed generate-mask shares the run collector across generated and masked halves and carries pregenerated state through its runtime decline at `src/decoy_engine/execution/_pipeline_mixed_route.py:148-282`. If a source-side relationship is incomplete because one table is generated, that side is `not_run` with `invariant.no_reference`; the complete output side remains exact and transition/join change is `not_run`.
- Independent multi-table split passes one collector to dispatched tables and the residual full-frame group at `src/decoy_engine/execution/_pipeline_multi_table.py:260-402`; only the enclosing route finalizes it.
- Auto-chunk shares one run collector across dispatcher, legacy-oracle, and streamed lanes and finalizes after aggregation at `src/decoy_engine/execution/_pipeline_auto_chunk.py:395-450`. It never averages per-chunk rates.
- Unified slice feeds the reconstructed source-shaped output at `src/decoy_engine/execution/_unified_slice.py:285-350`. It must not create a second evaluator.
- Out-of-core calls the row collector after every FK join, not at the pre-join legacy-validator hook, in `src/decoy_engine/execution/out_of_core/_runner.py:460-498`. Relationship measurement stages only the configured key columns and parent row ordinal.
- Isolated execution delegates artifact staging and reconstruction to new `_isolated_invariant_codec.py`, using only `write_invariant_report_v1` and `read_invariant_report_v1`, and deletes the stage on every failed, crashed, timeout, discard, and malformed-envelope exit at `src/decoy_engine/execution/_isolated_run.py:258-365`, `:409-622`.
- Governor execution discards every failed rung's staged artifact and returns only the final successful rung's reconstructed report at `src/decoy_engine/execution/_governor.py:340-428`. Trip callbacks and diagnostics never receive it.
- Probe execution clears `invariants` on its private downscaled config before `run_isolated` at `src/decoy_engine/execution/_probe.py:239-263`, rejects attempted reintroduction, and asserts the probe child returned no invariant projection. The full-job config object is not mutated.
- Direct public native and legacy chunk iterators reject F36 eagerly because they expose no whole-run report carrier. Managed auto-chunk uses a private collector callback and never silently falls through to direct-public behavior.

Relationship evaluation reuses the existing Arrow IPC format, DuckDB external group-by/join, memory-limit, disk-preflight, and temp-budget semantics, but it does not pass the existing bare `Path` scratch API through as an F36 capability. Linux V1 `InvariantScratchLease` enforces this sequence:

1. Create a fresh unpredictable mode-0700 directory under the run-owned root, open it with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`, compare `lstat` and `fstat` device/inode, and retain the root dirfd until lease close.
2. Validate each fixed internal child name contains no separator. Create files relative to the dirfd with `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode 0600. Require `fstat` regular file, expected owner, and link count one. Create child directories relative to the root dirfd and retain their dirfds.
3. Give Arrow only `os.fdopen(os.dup(master_fd), ...)` file objects. The master fd remains live through the operation, and the lease rechecks device, inode, regular-file type, and link count after every writer close and reader completion.
4. DuckDB necessarily reopens names. The only admitted bridge is Linux `/proc/self/fd/<live-dirfd>` for its private `temp_directory` and `/proc/self/fd/<live-filefd>` for read-only IPC/Parquet inputs. Immediately before and after `connect_duckdb` and each query, compare `os.stat` of the bridge with `fstat` of the retained fd. SQL path tokens are generated only from lease-held integer fds, never a caller string. Query results stream back to Arrow; DuckDB does not create a named F36 result file.
5. If `O_NOFOLLOW`, dirfd-relative operations, `/proc/self/fd`, identity checks, or required DuckDB bridge behavior are unavailable, return fixed `invariant.secure_scratch_unavailable` before staging or opening DuckDB. V1 does not weaken to a checked bare path on another platform.
6. Close DuckDB, Arrow duplicates, master file fds, and directory fds on every exit, then unlink exact children relative to the retained root dirfd. A failed unlink/close is recorded with a fixed cleanup reason. Caller-owned directories are never recursively removed.

The AST sentry rejects `Path`, string path, `open`, `Path.open`, and unanchored DuckDB path use in F36 relationship modules, except the single reviewed `/proc/self/fd` bridge builder. Runtime evasions cover symlink substitution, rename, hard-link creation, fd closure/reuse, procfs absence, changed device/inode, link-count change, and bridge identity change. The evaluation retains O(number of checks) counters plus one configured batch. DuckDB owns spillable key grouping. There is no O(rows) Python set, list of failures, or match-count vector.

For one F36 measurement, both `sum(source table rows)` and `sum(output table rows)` are independently capped at 100,000,000. A source/output pair at the product cap is supported rather than being double-counted as 200,000,000. Above either side's cap, the report carries `not_run` with `invariant.row_cap_exceeded`; it does not attempt a larger plan. Scratch-budget or disk-preflight refusal similarly yields a fixed `not_run` reason in report-only mode. F35 will later treat either as unsatisfied.

`ExecutionResult` gains `invariant_report: InvariantReportV1 | None = None`. Existing constructors remain source-compatible. F36 does not add a `quality_metrics["invariants"]` key. With no `invariants` config, the field remains `None`, no collector is allocated, and golden outputs stay byte-identical.

### 8. Safe report schema

`InvariantReportV1` is a frozen, slotted exact type with no arbitrary mapping and this logical schema:

```text
InvariantReportV1
  schema_version: literal "decoy-invariant-report/v1"
  status: complete | partial | not_run
  coverage: exact | sampled | mixed
  observations: tuple[InvariantObservation, ...]  # max 1024

InvariantObservation
  check_ref: CheckRef(code, position)
  structures: tuple[StructureRef, ...]             # numeric positions only
  status: measured_exact | measured_sampled | not_run | unsupported
  verdict: pass | fail | indeterminate
  reason: optional exact registered ReasonCode from reasons.quality
  source_counts: optional RowCounts | RelationshipCounts
  output_counts: optional RowCounts | RelationshipCounts
  transition_counts: optional TransitionCounts | JoinChangeCounts
  measurements: tuple[BoundedMeasurement, ...]     # fixed names and units
```

The exact fields of `CheckRef`, both structure-reference variants, `RowCounts`, `RelationshipCounts`, `TransitionCounts`, `JoinChangeCounts`, `BoundedMeasurement`, `InvariantObservation`, and `InvariantReportV1` are the public result field manifest above. `TransitionCounts` is partitioned so `comparable_row_count = introduced_failure_count + resolved_failure_count + persistent_failure_count + persistent_pass_count`, while `unknown_transition_count` and `unevaluable_transition_count` are outside the comparable population. `JoinChangeCounts` satisfies `comparable_parent_count = changed_parent_count + unchanged_parent_count` and `changed_parent_count = increased_parent_count + decreased_parent_count`; lost/gained counts are subsets of decreased/increased. Generated balance properties enforce these equations.

The type has no timestamp, duration, job ID, fingerprint, table name, column name, relationship name, path, DSN, query, exception, message, detail, arbitrary metadata, row index, cell value, min, max, quantile, unexpected example, expected value, or actual value. Canonical order is `(check_ref.position, check_ref.code, structure positions)`. `_safe_projection.py` converts every field through named F31 event factories; `families.quality.invariant_report_v1` accepts only the resulting exact event tuple and produces opaque `InvariantAggregateProjectionV1`; `write_invariant_report_v1` alone produces canonical bytes. Unknown fields, forged codes, out-of-range numbers, non-finite measurements, excessive observations, or unregistered types produce only F31's fixed invalid event and no partial artifact.

### 9. B-9 and F35 consumption

B-9 remains report-only as required by `/home/cam/vscode/decoy-platform/docs/plans/2026-10-26-di1-measure-completion.md:19-24`, `:51-75`, and `:87-97`. Its Platform wrapper adds an `invariants` sibling section per matched source/output table pair. It preserves successful sections when another is `not_run`. It does not change the primary distribution report, its denormalized columns, grade, or job outcome.

Platform adds a reversible `job_invariant_reports` migration instead of overloading `JobQualityReport`. Each row stores job ownership, fixed schema version, numeric section position, fixed status, and exact `SerializedSafeArtifact` bytes. No denormalized value, name, detail, or path column is permitted. `GET /api/jobs/{job_id}/quality/invariants` returns those exact bytes through the F31 invariant HTTP adapter with the existing owner-or-admin authorization pattern. The B-9 details assembler may embed that same exact artifact without parsing it into an application mapping.

F35 consumes `CheckRef`, observation status, verdict, counts, and fixed measurements. A contract refers to the pair `(code, position)`. It does not re-evaluate data, match prose, inspect configured names, or read a legacy validator report. V1 eligibility is deliberately strict: only `measured_exact` plus `pass` may satisfy a required check. Exact fail blocks when F35 enables a gate. Sampled, unsupported, not-run, partial, or indeterminate observations cannot satisfy it. F36 adds the seam and contract fixtures only. It does not implement F35 enforcement.

### 10. Compatibility class and release sequence

Compatibility class: **additive pre-GA minor, opt-in public config and API, plus a new versioned artifact**.

- Existing pipeline V1 remains valid because `invariants` is optional and defaults to `None`.
- Existing output bytes, determinism, route choice, validator behavior, quality grades, and `quality-report/v1` remain unchanged when it is omitted.
- `decoy-invariant-report/v1` is a new closed artifact. Its shape is frozen under that tag and unknown fields are rejected. Every addition requires a new `/v2` registry entry, projection, reader, and writer.
- Engine release sequence is F31 `0.17.0`, then F36 `0.18.0`. `pyproject.toml` and `decoy_engine.__version__` move together in the release integration branch.
- CLI initially pins exactly `decoy-engine==0.18.0`; its current broad floor at `/home/cam/vscode/decoy/pyproject.toml:88-92` is not sufficient for the F31 and F36 boundary.
- Platform initially pins exactly `decoy-engine==0.18.0`; its current exact 0.15.1 ownership pin is documented at `/home/cam/vscode/decoy-platform/api/engine_compat.py:155-171` and must move with its dependency declaration and image lock.
- No worker may relabel the framed 0.7.0 source tree as 0.18.0 directly. The release coordinator first integrates the reviewed F31 and F36 commits onto the current release lineage after `v0.16.0`, reruns topology and compatibility gates, and records the exact ancestry.

## Acceptance tests, authored RED first

Each numbered test is committed and run against its slice's pre-implementation baseline. The evidence records the expected assertion failure, not an import error unrelated to the behavior. Every fixture asserts its canaries and row counts before asserting the new result.

1. **Closed grammar:** a valid `compare` block is rejected by current `PipelineConfig` and becomes accepted; unknown kind, unknown operator, extra key, expression text, SQL text, callable-shaped object, missing position, duplicate `(code, position)`, sampled relationship, and 257th check remain rejected with only fixed codes and numeric positions.
2. **Stable identity:** two semantically identical programs with reversed check order produce byte-identical sorted `CheckRef` sequences. Changing all table names, column names, and cell values while preserving positions and outcomes does not change any output token.
3. **Comparison truth table:** crafted rows independently exercise pass, fail, null unknown, non-finite unevaluable, and incompatible-type unsupported for every compare operator. Assertions pin each count and the balancing equations.
4. **Null truth table:** unary and pair null operations exercise all four `(null, null)`, `(null, value)`, `(value, null)`, and `(value, value)` states with unequal multiplicities so swapped or collapsed semantics fail.
5. **Arithmetic and checksum reuse:** a total with two terms includes one pass, one tolerance pass, one fail, one null, and one overflow. Each existing checksum scheme has a known pass, fail, and null. A spy proves calls go through `decoy_engine.checksums.validate`.
6. **Source/output transitions:** aligned source and output include one introduced, one resolved, one persistent, and one unchanged failure plus unknown transitions. Counts must equal independently computed fixture counts and never include row positions.
7. **Join-cardinality change:** source and output have identical parent and child row totals and zero orphans, but one parent loses a child and another gains it. `changed=2`, `increased=1`, and `decreased=1` prove the metric is not row-count or orphan drift in disguise.
8. **Relationship defects and merged parents:** separate fixtures exercise duplicate parent keys, null parent keys, all-null child keys, partial-null composite keys, unmatched keys, zero/one/many parent matches, and an empty child. The multi-parent fixture has member 0 keys `(A,B)`, member 1 keys `(B,C)`, and child keys `(A,B,C,X)`: assert `parent_rows=4`, `duplicate_parent_key_groups=1`, `duplicate_parent_key_rows=2`, `matched_child_rows=3`, `unmatched_child_rows=1`, `parent_zero_match_rows=1`, `parent_one_match_rows=3`, and `parent_many_match_rows=0`. The one zero-match parent row is member 1's shadowed `B`, not child `X`; `X` contributes only to unmatched children. Assert first-hit attribution to member 0 for `B` and indeterminate join change because `B` is duplicated. A second duplicate-free group pins exact join-change counts. Omitting or reordering a member is a config reject before read.
9. **Exact and sampled honesty:** exact clean data passes; a deterministic sample containing a failure fails; a clean sample is indeterminate; a failure deliberately outside the sample proves the report never claims zero total failures; sampled relationship config is rejected.
10. **Scope and alignment:** a subset excludes a known full-source violation, so the selected-population report passes while a deliberately incorrect full-source comparison would fail. Quarantine removes a different row and source/output transition counts remain aligned.
11. **Route parity:** equivalent eligible fixtures force every collecting cell in the execution-route manifest: resident full-frame, mixed and its decline, independent multi-table split, sequential, unified slice, dispatcher/legacy and resident/streamed auto-chunk, resident/batch-join/reorder out-of-core, isolated/in-process, and governor flag-on/off. Reports are canonical-byte equal across batch sizes 1, 7, and 65,536. A governor fixture fails two rungs then succeeds and exposes only the final rung's projection. Probe fixtures assert both children have `invariant_projection is None` while the memory estimate remains conclusive. Route telemetry proves each intended route actually ran; direct-public chunked refusal proves zero input pulls.
12. **Sink and failure timing:** out-of-core invariant failure remains report-only and the transactional output commits; evaluator infrastructure failure before sink commit yields fixed `not_run` evidence and no raw exception text. Config failure occurs before the first source read. F31 artifact-file commit/abort, DB rollback/duplicate delivery, HTTP exact-body, isolated discard, governor failed-rung discard, reader tamper, and cleanup failure are pinned separately.
13. **Pure generation and reference-backed generation:** pure generate measures output row checks but reports source and join change as `not_run`; the same generated frame with an approved reference reports exact source/output transitions through the same engine API.
14. **Value-free corpus:** unique raw cell, table, column, path, DSN, URL, credential, exception-message, Unicode, UUID-shaped, short, numeric, and control-character canaries are seeded. Scan object repr, sealed JSON, text, CLI JSON/human/stderr, Platform persistence/API, logs, evidence, and failure paths. Assert every canary is present in its input, at least one observation exists, and no canary or canary length derivative appears in any ordinary output.
15. **Schema and construction sentries:** direct construction, subclass, `dataclasses.replace`, `model_dump`, `asdict`, arbitrary code injection, extra report field, non-finite measurement, negative count, and excessive observation attempts are rejected. Mutation tests weaken each sentry and prove it turns RED.
16. **B-9 isolation:** two matched pairs yield two invariant sections in declaration order; one section can be not-run while the other remains exact; no invariant changes `overall_score`, grade, job status, or the legacy quality payload.
17. **F35 seam:** a contract fixture resolves a stable exact passing `CheckRef`; exact fail, sampled, unsupported, not-run, and indeterminate fixtures are all ineligible. The test imports no F35 evaluator and causes no publication decision.
18. **100M boundary:** a generated narrow 100M-row Arrow/Parquet workload completes exact row counts and one relational metric within the existing memory and scratch budgets; a 100,000,001-row declared scope returns fixed `not_run` before staging. The evidence records wall time, HWM, Arrow pool peak, DuckDB limit, and scratch peak without paths or values.

## Hostile corpus: ten gate breaks, RED first

These are the ten class-level attacks the gate reviewer is expected to make. Each acceptance test is authored against the pre-implementation baseline, fails at its named behavioral assertion, and is retained permanently. Mutations are scoped test substitutions or monkeypatches, not reusable exploit scripts.

1. **Value-bearing output break:** seed distinct markers into cells, names, paths, DSNs, URL, credential-shaped text, exception text, hostile option text, and a producer class name. Drive every event, aggregate artifact, file, DB, HTTP, governor callback/diagnostic, probe diagnostic, human, JSON, quiet, verbose, success, and failure surface. Assert seeded-input presence, a nonempty event/observation at every expected emitting surface, and marker absence. Mutation: pass one plain observation mapping to one sink or add a generic artifact unseal.
2. **Cleanup break:** parameterize every owner and applicable exit in the owned-exit manifest, including `BaseException`, cancellation, malformed isolated envelope, commit failure, double close, and close after commit. Assert exact children are gone, caller-owned roots remain, terminal state is correct, and cleanup failure has a fixed reason. Mutation: suppress one lease cleanup or sink abort.
3. **State race break:** barrier-force feed/finalize, finalize/finalize, feed/close, and finalize/close in both orders for at least 1,000 seeded schedules. Assert both actors crossed the barrier, exactly one final report exists, counts are conserved, and no post-finalize feed is accepted. Mutation: remove the state check under the collector lock.
4. **Silent-route break:** generate every route-capability cell, including mixed runtime decline, multi-table residual group, both auto-chunk engines and both output modes, all out-of-core subroutes, isolated/in-process, governor failed and successful rungs, probe stripping, and direct-public refusal. Assert actual route telemetry and nonempty canonical report or exact registered refusal. Mutations omit the mixed hook, return a failed-rung report, copy raw rung error into `on_trip`/diagnostic, or let a probe retain F36; each must fail.
5. **Relationship break:** cross every FK-shape manifest entry with each route/stage that claims support, including partial-null composite, multi-parent first-hit, self-reference with later parent batch, later child batch, and quarantine removal. Assert exact independent counts. Mutation: compare only the first composite component or finalize before the later batch.
6. **Hostile-producer break:** recursively forge subclasses, foreign dataclasses, bool-as-int, negative/oversized counts, long tuples, non-finite decimals, unknown enums, duplicate JSON keys, oversized/noncanonical artifacts, exceptions with marker text, and an isolated envelope with extra fields. Assert exact-type rejection and fixed bounded error reconstruction. Mutation: replace one exact-type check with `isinstance`, skip canonical re-encode, or include `str(exc)`.
7. **TOCTOU break:** seed scratch consumer aliases, wrapper calls, symlink substitution, rename, hard-link, closed/reused fd, absent procfs, identity swap, and caller-supplied path attempts. Assert Arrow uses descriptor-backed objects, DuckDB uses only retained-fd bridges, all identity/link checks run, and unsupported platforms refuse before staging. Mutation: add a bare `Path`, close a bridge fd, or remove a post-query identity check.
8. **Evidence break:** rerun the declared baseline with the branch lock, assert every RED test reaches its intended assertion, every route/exit branch counter is positive, every marker exists in input, and exact line counts match evidence. Mutation: remove the non-vacuity assertion and show the reviewer test detects the false green.
9. **Refactor-drift break:** freeze no-invariant bytes, current route selection, ordering anchors, quarantine alignment, FK rewrite placement, public imports, and sentry scan roots before wiring. Mutation: feed the out-of-core collector before rewrite or omit the new package from the physical-seam scan.
10. **Frozen-contract break:** snapshot both versioned schemas, public dataclass field order/defaults, reason manifest, CLI exits/help, Platform endpoint shape, default-inclusive config dump with `"invariants": None`, and no-invariant execution goldens. Mutations add a field to `quality-report/v1`, reorder a public result field, mutate the invariant schema under `/v1`, or make one dump consumer reject/externally serialize the additive null key.

## Generated acceptance tests

1. **Recursive field walk:** use `dataclasses.fields` on every planned public result dataclass and the resulting `ExecutionResult` and `IsolatedRunResult`. Recurse through union and tuple members, prove at least one object and every declared field were visited, and reject `Any`, mapping, bytes, path, exception, unconstrained string, subclass instance, excessive tuple, and unbounded integer. Separately assert F31 projection/provenance/artifact exact types are opaque non-dataclasses with no public attributes or protocols. Cross-check exact field order against the public result manifest. The mutation adds one forbidden field or exposes one opaque slot and must fail.
2. **Exit-path matrix:** generate `owner x exit x commit-state` from the owned-exit manifest. Instrument branch IDs and assert every applicable cell ran, every owner reached the required terminal state, all owned artifacts were disposed, caller-owned roots survived, and publication occurred only after commit. The mutation removes one `finally` branch and must fail.
3. **Barrier interleavings:** generate all transition pairs from the written state table with a two-party barrier immediately before each state claim. Assert both parties reached the barrier, a bounded timeout did not substitute for synchronization, outcomes match the table, and counts/report multiplicity remain valid. The mutation removes the lock or generation/state fence and must fail.
4. **Seeded marker corpus:** deterministically generate short, long, numeric, Unicode, control, UUID-shaped, path, DSN, URL, credential, SQL-shaped, exception, and class-name markers. Place a distinct marker in every producer field and scan every event/artifact/file/DB/HTTP/governor/probe surface. Assert each marker is present before execution, each expected surface emits at least one unit, silent surfaces were invoked, and no marker or marker-length derivative appears. Mutations bypass an F31 factory and add a generic unseal; both must fail.
5. **Route and FK product:** Hypothesis generates supported `route x lane x output mode x FK shape x batch partition x coverage` cells, governor rung sequences, and probe paths from the manifests. Each test asserts its generated cell count is positive, route telemetry matches, observations are nonempty for collecting cells, failed governor rungs and probes publish none, refusals happen before a counted read, and canonical bytes equal the resident oracle. Mutations delete one hook, return a failed-rung report, retain probe F36, or flip one capability cell; each must fail.
6. **Balance properties:** generate row-state and relationship-count partitions and assert every equation pinned in the schema, plus canonical order and batch-boundary independence. A fixture includes at least one member of every bucket. Mutating a bucket increment or averaging per-batch rates must fail.
7. **Aggregate artifact corpus:** generate valid exact projections, then mutate schema ID, duplicate keys, field order, observation order, numeric bounds, enum tokens, reason membership, UTF-8, trailing bytes, size, and canonical numeric representation. The exact writer-reader round trip must be byte-identical and nonempty; every mutation rejects without returning a mapping or partial projection. Mutations remove the byte cap, provenance check, or canonical re-encode check and must fail.

## Implementation slices

### Slice 0: typed model, registry, RED corpus, and prerequisite sentries

Repository and size: `decoy-engine`; new modules at most 450 lines each, zero net growth in `_pipeline_route_exec.py`, `_unified_slice.py`, and the out-of-core runner.

Scope:

- Run the topology gate. Record the engine baseline, `v0.16.0`, F31 reviewed commit, CLI remote HEAD, Platform remote HEAD, dependency pins, and migration heads. Verify all exact F31 aggregate symbols and ownership tests named in Design section 1, not only event-wire symbols. Stop before any F36 file is created on absent/mismatched aggregate support, incompatible ancestry, or an unamended F31 release number.
- Rerun the shipped ordering tests and the joint-mask remediation tests. Add a mutation sentry that removes one dependency edge or source anchor and proves the prerequisite suite fails.
- Author the shared canary corpus and acceptance tests 1, 2, 14, and 15 first. Add prerequisite tests that reject event `SerializedSafePayload` as an invariant artifact, reject mappings and generic unseal, and prove the exact F31 reader/writer, file owner, DB adapter, and HTTP adapter exist. Capture RED on missing aggregate support before F36 model RED.
- Create `_model.py`, the non-reason `_registry.py`, strict config models, `CheckRef`, both structure-reference variants, all exact public count/result types from the field manifest, and fixed F29 config-error codes.
- Add the module-size sentry entries and public import contract, but no evaluator and no ordinary serializer.

Self-check:

```bash
pytest -q tests/unit/execution/test_cross_column_ordering.py tests/unit/execution/test_cross_column_joint_mask_remediation.py
pytest -q tests/unit/invariants/test_model.py tests/unit/invariants/test_registry.py tests/security/test_invariant_report_boundary.py tests/sentry/test_module_size.py
ruff check src/decoy_engine/invariants tests/unit/invariants tests/security/test_invariant_report_boundary.py
mypy src/decoy_engine/invariants
```

Targeted verification: inspect every model field and registry member mechanically; assert no `Any`, `dict`, `Mapping`, `bytes`, exception, path type, unconstrained `str`, or public serialization helper occurs in report models. Confirm omission of `invariants` produces the expected additive `"invariants": None` in default-inclusive dumps, every enumerated dump consumer tolerates it, and no-invariant execution artifacts and route choices match the baseline.

Adversarial review and remediation: attempt grammar smuggling, enum subclassing, bool-as-int, negative and huge counts, duplicate identities, input echo through Pydantic errors, and construction forgery. Resolve every BLOCKER and HIGH before Slice 1. Retain RED evidence and green rerun logs.

### Slice 1: row-local compiler and evaluator

Repository and size: `decoy-engine`; `_compile.py`, `_row_eval.py`, and `_collector.py` at most 450 lines each. Split before crossing the ceiling; do not raise it.

Scope:

- Author acceptance tests 3, 4, 5, 6, 9, and the row-local half of 13 RED first.
- Implement `_compile.py`, `_row_eval.py`, and the row portion of `_collector.py` with the exact type, null, non-finite, arithmetic, checksum, alignment, and deterministic-sample semantics above.
- Delegate checksum decisions to `decoy_engine.checksums.validate`.
- Add property tests for count conservation, batch-boundary independence, operator truth tables, and canonical observation order.

Self-check:

```bash
pytest -q tests/unit/invariants/test_compile.py tests/unit/invariants/test_row_eval.py tests/property/test_invariant_counts.py
pytest -q tests/unit/validators/test_catalog.py tests/unit/validators/test_relationship_checks.py
ruff check src/decoy_engine/invariants tests/unit/invariants tests/property/test_invariant_counts.py
mypy src/decoy_engine/invariants
```

Targeted verification: compare direct whole-table evaluation with random RecordBatch partitions and independently computed Python fixture counts. Verify sampled clean data is indeterminate and that no accumulator grows with failed-row count.

Adversarial review and remediation: inject Arrow type drift midstream, decimal overflow, temporal unit mismatch, timezone mismatch, NaN, infinity, extension arrays, empty batches, and a checksum exception. Resolve every BLOCKER and HIGH and add each finding to the permanent corpus.

### Slice 2: exact relationship and join-cardinality evaluator

Repository and size: `decoy-engine`; `_relationship_eval.py` and `_scratch.py` at most 450 lines each, with no net growth in the 681-line allowlisted runner.

Scope:

- Author acceptance tests 7, 8, the relational half of 9 and 13, and the small-scale part of 18 RED first.
- Implement `_relationship_eval.py` with narrow Arrow staging, DuckDB external aggregation, aligned parent ordinals, exact FK/orphan counts, and join-change counts.
- Reuse DuckDB query/limit semantics, Arrow IPC format, temp-disk preflight, and budget checks through `InvariantScratchLease`. Keep scratch inside the run-owned root but use only retained dirfd/filefd capabilities and the reviewed Linux `/proc/self/fd` bridge. Do not pass the existing bare-path `connect_duckdb` ownership through unchanged.
- Add parity tests against existing FK, orphan, and reconciliation validators on resident fixtures. The parity oracle compares counts, never legacy prose.

Self-check:

```bash
pytest -q tests/unit/invariants/test_relationship_eval.py tests/integration/invariants/test_relationship_parity.py
pytest -q tests/unit/validators/test_relationship_checks.py tests/unit/validators/test_catalog.py
pytest -q tests/unit/execution/out_of_core tests/sentry/test_module_size.py
ruff check src/decoy_engine/invariants tests/unit/invariants tests/integration/invariants
mypy src/decoy_engine/invariants
```

Targeted verification: run composite-key, high-cardinality, all-duplicate, all-null, no-child, and fanout fixtures under low DuckDB memory and scratch caps. Inspect HWM and scratch peaks. Prove the implementation holds no Python collection proportional to rows.

Adversarial review and remediation: force spill, exhausted disk budget, connection failure, malformed Arrow input, duplicate parents, partial-null composites, row reorder, source/output count mismatch, symlink/rename/hard-link swaps, closed/reused fds, procfs absence, and identity drift before/after DuckDB query. No exception, scratch path, key, or SQL may enter the report. Unsupported platforms must refuse before staging. Resolve every BLOCKER and HIGH before route wiring.

### Slice 3: all engine routes, F31 projection, artifact, and 0.18.0 release candidate

Repository and size: `decoy-engine`; `_safe_projection.py`, `_api.py`, `_isolated_invariant_codec.py`, and any extracted route helper at most 450 lines each. The 600-line route-exec seam, 450-line unified-slice module, 622-line isolated runner, and 681-line out-of-core runner get zero net growth.

Scope:

- Author acceptance tests 10, 11, 12, 14, 15, and the full 100M test RED first.
- Add one collector seam to every collecting cell in the execution-route manifest, including mixed decline, independent multi-table, every auto-chunk and out-of-core subroute, isolated transport, and the governor's final-success ownership. Add probe stripping/refusal and eager registered refusal to both direct-public chunked iterators. Do not duplicate evaluation code in route modules.
- Add `ExecutionResult.invariant_report`, `IsolatedRunResult.invariant_projection`, `_isolated_invariant_codec.py`, the public `measure_invariants` API, and the reviewed F31 event and aggregate factories.
- Convert each domain observation to the prerequisite F31 closed artifact events, call the pre-registered `families.quality.invariant_report_v1`, and use only F31 `write_invariant_report_v1`, `read_invariant_report_v1`, and `render_invariant_report_v1`. Add duplicate-key, byte-cap, schema, canonical-order, provenance, noncanonical-byte, and round-trip tamper tests. Add the compatibility-corpus fixture, package exports, changelog entry, and coordinated engine 0.18.0 metadata after the extended F31 release.
- Keep route behavior and outputs unchanged when `invariants` is omitted.

Self-check:

```bash
pytest -q tests/unit/invariants tests/integration/invariants tests/parity/test_invariant_route_parity.py
pytest -q tests/security/test_invariant_report_boundary.py tests/security/test_safe_output_boundary.py tests/sentry
pytest -q tests/integration/compat_corpus tests/integration/golden
python scripts/test_flight.py
```

Targeted verification: force every route and batch size, governor success after failed rungs, governor exhaustion/callback failure, and both probe points. Compare canonical report bytes, prove failed rung and probe reports never publish, verify transactional and artifact-file sink behavior, run the 100M workload in the approved benchmark environment, and compare no-invariant execution artifacts byte for byte with the baseline.

Adversarial review and remediation: trace every report field from raw batch through collector, result, safe factory, serializer, sink, and failure path. Search route modules for alternate mappings or serialization. Resolve every BLOCKER and HIGH, rerun the full engine suite, and record exact artifact hashes.

### Slice 4: CLI consumer, independently gateable

Repository and size: read-only topology gate then edits in the `decoy` CLI repository; new `report_invariants.py` at most 450 lines. Existing 1717-line `run.py` and 2373-line `report.py` receive registration-only edits with no net growth after extraction.

Prerequisite: published engine 0.18.0 and F31-migrated CLI boundary.

Scope:

- Topology-gate the remote CLI. Pin exactly `decoy-engine==0.18.0` and refresh its lock and package evidence.
- Author CLI portions of acceptance tests 13 and 14 RED first.
- Add a small `src/decoy/cli/report_invariants.py` command module and register `decoy report invariants REPORT`. `REPORT` is a positional path to a canonical `decoy-invariant-report/v1` artifact; the command validates only through `read_invariant_report_v1` and renders registered event projections to stdout. It has no general decode or `--output` writer. Do not add logic to the existing oversized `report.py`.
- Make `decoy run` render an invariant summary only from F31 event factories when a program ran. JSON and human modes show check code, explicit position, structure positions, status, verdict, counts, and bounded measurements only. Add optional `--invariant-report-out PATH`; publish the exact aggregate only through `InvariantArtifactFilePublish`, with no path in stdout, stderr, logs, events, or artifact content.
- Preserve report-only exit code 0 for measured violations. Bad config stays usage failure with a fixed reason. F35 later owns a gate-oriented exit code.

Self-check:

```bash
pytest -q tests/e2e/test_report_invariants.py tests/e2e/test_run_invariants.py tests/security/test_value_free_output.py
ruff check src/decoy/cli/report_invariants.py tests/e2e/test_report_invariants.py tests/e2e/test_run_invariants.py
mypy src/decoy/cli/report_invariants.py
```

Targeted verification: exercise human, JSON, quiet, verbose, hostile positional report path, hostile `--invariant-report-out`, file commit/abort/cleanup, malformed/oversized/noncanonical artifact, duplicate key, missing file, read error, exact, sampled, and pure-generate paths. Assert no path or exception text appears and violation exit status remains report-only.

Adversarial review and remediation: seed canaries into filenames, config names, cells, exception messages, environment values, and output arguments. Attempt to pass the internal report directly to Rich or JSON. Resolve every BLOCKER and HIGH, then run the full CLI suite and package smoke test.

### Slice 5: Platform B-9 consumer and F35 seam, independently gateable

Repository and size: read-only topology gate then edits in `decoy-platform`; every new module at most 450 lines. Do not grow 3303-line `api/models.py`; place the ORM type in `api/quality/invariant_models.py` and register it through `api/model_registry.py`.

Prerequisite: published engine 0.18.0, F31-migrated Platform boundary, and B-9 topology reconciliation.

Scope:

- Topology-gate Platform remote HEAD, B-9 state, exact engine pin, API authorization helpers, and Alembic heads. Pin exactly `decoy-engine==0.18.0`.
- Author acceptance tests 16, 17, and Platform portions of 14 RED first.
- Add the reversible `job_invariant_reports` migration, F31 exact `SerializedSafeArtifact` DB adapter, B-9 invariant section assembler, and owner-scoped `/quality/invariants` endpoint using the F31 HTTP adapter. The service never decodes the artifact to a mapping and never commits the caller's transaction.
- Compute mask reports from the engine result. Compute approved reference-backed generation reports through `measure_invariants`. Pure-spec generation records fixed `not_run`.
- Add only the F35 consumer interface and eligibility fixture. Do not add publication gating or a threshold language.

Self-check:

```bash
pytest -q tests/test_invariant_report_persistence.py tests/test_invariant_quality_route.py tests/test_b9_invariant_sections.py tests/test_f35_invariant_seam.py
pytest -q tests/test_quality_route.py tests/test_di0_quality_contract.py tests/test_quality_runner_hook.py
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Targeted verification: test two matched pairs, partial measurement, retries, duplicate delivery, authorization, missing report, old jobs, migration round-trip, persistence rollback, API canonical bytes, and job-outcome parity. Confirm distribution grade and denormalized quality columns do not move.

Adversarial review and remediation: attempt cross-owner reads, arbitrary stored payload injection, canary-bearing database rows, duplicate report writes, F31 bypass, partial transaction, and exception echo. Resolve every BLOCKER and HIGH, run the full Platform suite, and record migration head plus dependency lock hash.

## Verification and evidence plan

### Required evidence per slice

Each slice records:

- exact repository commit and dependency artifact hashes;
- test-only RED commit, failing command, and expected assertion;
- implementation commit and targeted green command;
- changed-file list and module line counts;
- static, type, security, compatibility, and full-suite results appropriate to the slice;
- adversarial findings by severity and the remediation commit for each BLOCKER or HIGH;
- canary non-vacuity counts and output-surface scan results;
- for performance slices, row count, batch size, wall time, HWM, Arrow pool peak, DuckDB memory limit, and scratch peak, with no path or data value in evidence.

### Cross-route oracle

The resident direct API on small fixtures is the semantic oracle. Every collecting route, including the governor's final successful rung, is compared to its canonical `decoy-invariant-report/v1` bytes. Failed governor rungs and probe children must have no artifact and no report. The oracle is not a separately coded checker: it invokes the same compiler and collectors with resident batches. Hypothesis varies row counts, null positions, batch partitions, operators, numeric types, relationship fanout, governor rung outcomes, and probe outcomes. Mutation tests invert an operator, change null handling, drop a final batch, average chunk rates, move the out-of-core hook before FK rewrite, return a failed-rung report, retain probe F36, or treat sampled zero as pass. Every mutation must be killed.

### Security and value-free evidence

The F31 shared analyzer scans all new ordinary sinks. F36 adds exact AST sentries that reject:

- construction of `InvariantAggregateProjectionV1` outside its registered factory/reader, or serialization outside F31 `write_invariant_report_v1`;
- any generic aggregate decode/unseal, mapping return from the reader, event `SerializedSafePayload` used as an artifact, or domain report passed to file/DB/HTTP sinks;
- `dataclasses.asdict`, Pydantic dump methods, `json.dumps`, Rich rendering, logging, or API return of an invariant domain object or mapping;
- report models containing forbidden field types or unconstrained strings;
- log, warning, error, or reason construction from exception text or configured names;
- a `quality_metrics` key containing `invariant`;
- bare `Path` or string opens in F36 relationship modules, `/proc/self/fd` construction outside the one bridge helper, or raw key arrays imported into report/projection modules.

### Performance evidence

Run correctness at tiny and medium sizes in normal CI. Run 1M, 10M, and 100M exact workloads in the approved benchmark environment for row-local and relational paths. Acceptance is bounded scaling and completion within existing run budgets, not a new marketing throughput target. Failure above the cap, below required scratch space, or below the non-spillable memory floor must be controlled and coded. No multi-node test or capacity abstraction beyond the existing budget machinery is added.

### Release and rollback

Release units are engine 0.17.0 F31, engine 0.18.0 F36, CLI consumer, and Platform consumer. Engine publishes before consumers. Consumers initially exact-pin 0.18.0. The CLI and Platform slices are additive and can roll back independently by reverting their consumer commit and dependency pin. The engine 0.18.0 artifact remains readable and the optional config remains accepted. Platform migration downgrade removes only F36 report rows and its new table; it does not touch jobs or legacy quality reports. No rollback claims restoration of already deleted report rows after a downgrade.

Post-release checks are a clean exact and sampled CLI fixture, one Platform mask job, one reference-backed generate job, owner and non-owner API checks, invariant persistence read-back, no grade change, and canary scan. Abort consumer rollout on any raw token, route mismatch, wrong stable ID, count imbalance, migration failure, unexpected job outcome, or F31 rejection.

## Documentation plan

Documentation is part of each repository slice, synchronized before its code gate:

- Engine: update `README.md` public entrypoints, `CODEMAP.md`, configuration reference, relationship guide, API reference, `docs/compatibility-contract.md`, changelog, and a new invariant-language reference with truth tables, null rules, sampling limits, exact-only relationship rules, 100M cap, and schema examples containing synthetic names only.
- CLI: add `decoy report invariants` and `decoy run --invariant-report-out` to command reference, examples, exit-code docs, artifact ownership, and migration notes. State that rendered output uses numeric positions and registry codes by design.
- Platform: document the new endpoint, authorization, persistence schema, B-9 separation, report-only behavior, migration rollback, and F35 eligibility seam.
- Product capability statement: say F36 measures declared checks and relationship changes. Do not claim automatic discovery, population fidelity, or release gating.
- Architecture record: document why `CheckRef` is code plus explicit numeric position, why reports stay outside `quality_metrics`, and why sampled clean observations are indeterminate.

Per repository guidance, the build session delegates documentation synchronization to Barry before objective-green. Barry changes documentation only. Documentation sync is implementation work, not an additional adjudication gate.

## Risks and controls

| Risk | Control and evidence |
|---|---|
| A value leaks through a report, error, log, API, or persistence row | Closed models, F31 event and exact aggregate boundaries, forbidden-field sentry, seeded canary matrix, and adversarial sink trace. |
| F31 event wire is mistaken for an aggregate artifact | Pre-Slice-0 exact aggregate-extension hard stop, separate schema IDs, schema-specific reader/writer, and no mapping/unseal sentries. |
| A sampled clean result is mistaken for exact zero | Sampled zero is structurally indeterminate; relationship sampling is rejected; F35 eligibility test refuses it. |
| Route-specific placement changes semantics | One collector, post-FK out-of-core hook, canonical-byte route parity, forced-route telemetry, and batch partition property tests. |
| Governor or probe publishes the wrong population | Final-success-only governor ownership, failed-rung discard, probe F36 stripping/refusal, fixed diagnostics, and route mutations. |
| Join change is confused with row-count or orphan drift | Acceptance fixture keeps row totals and orphans constant while changing two parent multiplicities. |
| Duplicate parent keys produce a misleading cardinality comparison | Count duplicates, mark join change indeterminate, and keep other relational observations separate. |
| Subset or quarantine compares the wrong population | Post-closure selected source scope, existing quarantine alignment, and fixtures where full-source and selected-source answers differ. |
| Exact relationship measurement becomes unbounded | Narrow IPC staging, external DuckDB group-by/join, existing budgets, no Python key sets, HWM/scratch evidence, and a 100M hard cap. |
| DuckDB reopens a swapped scratch path | Live descriptor lease, fixed procfs bridge, identity/link rechecks, Linux-only refusal, and seeded TOCTOU evasions. |
| Multi-parent observation disagrees with first-hit resolution | Complete ordered member group, merged-union count semantics, shadowed-row attribution, and exact expected-count fixtures. |
| Stable IDs drift with list reorder or names | Explicit authored position plus fixed code, canonical sorting, reorder test, and name/value replacement test. |
| Existing validators and new measurements disagree | Fixture parity for checksum, FK, orphan, and reconciliation semantics; discrepancies block the slice rather than being normalized away. |
| F36 accidentally becomes a gate | Report-only outcome tests in engine, CLI, and Platform; no enforcement API; F35 seam tests only eligibility. |
| Existing quality consumers break | New artifact and endpoint, no mutation of `quality-report/v1`, exact compatibility corpus, no-invariant goldens, and separate Platform table. |
| Release numbers collide with existing tags | Owner-approved F31 numbering amendment, 0.17.0 then 0.18.0, release ancestry check, exact pins, and tag-existence preflight. |
| Module growth creates another oversized seam | New modules target 450 lines, module-size sentry, thin route hooks, and no growth in oversized CLI `report.py`. |

## Gate chain and remote handoff

### Plan gate

This file is the only artifact of the current task. Gate history is explicit:

1. one Codex repo-grounded hostile pass completed, including qmd/graphify recall;
2. one Barry contract revision completed;
3. exactly one Dennis-role repo-grounded cold read completed in this session; no second Dennis pass;
4. this edit is the single author remediation of that Dennis verdict. Dennis will not be run again;
5. no argon review was run because no argon tool was available in this session. Before owner GO, the owner must arrange exactly one argon review, then make the owner decision.

The plan revision budget is spent. If the completed Dennis verdict remains BLOCKER after this remediation, or the one future argon review finds a BLOCKER, stop for the owner with a class-level proposal. Do not patch again and do not request a second Dennis or argon pass. No build starts before owner GO.

### Code gate

For every repository slice, the terminal code gate is ordered exactly:

1. objective-green evidence;
2. Codex review;
3. argon review;
4. exactly one Opus-4.8 Dennis repo-grounded gate;
5. owner merge decision.

Barry documentation sync occurs before objective-green. It is not a second review verdict. Freeze the final code HEAD before the one Dennis pass, then record that exact-HEAD repository gate receipt with `bash .claude/hooks/decoy-gate-pass.sh` where that hook applies. A post-Dennis code change invalidates the receipt and stops for the owner; this plan does not authorize another Dennis pass.

### Handoff to remote workers

Remote `claude.ai/code` workers see only the remote `decoy-engine`, `decoy-platform`, and `decoy` CLI repositories. This plan therefore contains the binding F36 semantics and does not require the local Obsidian vault at build time. Workers must:

1. fetch all remote refs and record exact HEADs before editing;
2. verify the ordering-fix ancestry and rerun its sentries;
3. reconcile F31 onto release 0.17.0 and refuse a stale 0.8.0 publication;
4. implement engine slices first and publish 0.18.0 only after its gate;
5. implement and gate CLI and Platform consumers separately against the published artifact;
6. preserve unrelated remote changes and stop on topology, migration-head, dependency-pin, or schema conflicts;
7. return exact commits, artifact hashes, RED/green evidence, adversarial remediation, docs evidence, and gate receipts to the owner.

No worker may infer permission to merge, publish, alter a quality grade, enable F35 enforcement, or expose a raw diagnostic channel from this plan.

### Binding worker evidence rules

- Name the class of every finding and fix the class (the structure in the plan), not only the listed instance.
- Write the generated tests first (field walk, exit-path matrix, interleavings, marker corpus). Record them red.
- Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red.
- Run baselines with `uv.lock` in the same environment as the branch. State what you did not run.
- Never widen a threshold, skip a test or reduce a budget to make a refactor pass.
- If a new defect of a class already in this catalog appears, stop and report it. Do not patch a third time.

## Pre-mortem

| Likely gate finding | Plan element that prevents it |
|---|---|
| F36 treats F31 event payloads as an aggregate artifact or invents a generic unseal | Pre-Slice-0 F31 aggregate-extension hard stop, exact named types, separate schemas, schema-specific reader/writer, and mapping/unseal mutations. |
| Governor returns a failed rung's report or a downscaled probe emits a misleading result | Governor final-success ownership, probe config stripping/refusal, callback/diagnostic surface tests, and failed-rung/probe mutations. |
| DuckDB scratch reopens a swapped bare path | Live dirfd/filefd lease, Linux `/proc/self/fd` bridge, before/after identity and link checks, unsupported-platform refusal, and TOCTOU evasions. |
| Multi-parent counts disagree with first-hit execution | Complete ordered member-group grammar, stable member/row ordinals, exact merged-count definitions, shadowed-B fixture, and pandas/out-of-core parity. |
| Additive config/envelope wiring grows an allowlisted module or falsely promises dump identity | Honest `invariants: None` dump contract, all-caller sentry, `_isolated_invariant_codec.py`, exact size census, zero-growth seams, and no-invariant execution goldens. |
