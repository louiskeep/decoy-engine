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
3. The same batch semantic kernel is used by full-frame, sequential, auto-chunk, unified-slice, and out-of-core execution. A route may not silently skip a check or substitute a different null, type, sample, subset, or alignment rule.
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
10. CLI and Platform expose only F31-sealed projections containing codes, positions, statuses, counts, and measurements. Human labels resolve only from F31 registry codes at the final renderer and never print configured names or data values.
11. The new artifact has compatibility-corpus coverage, deterministic canonical bytes, a schema tamper test, and a documented reader policy.
12. New or split engine modules stay below 450 physical lines. No touched module exceeds the 600-line target or 700-line hard cap. Existing oversized CLI `src/decoy/cli/report.py` is not expanded.
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

## Established methodology surveyed before design

The implementation must cite these adopted patterns in the relevant module docstrings:

- [Great Expectations expectation management](https://docs.greatexpectations.io/docs/0.18/cloud/expectations/manage_expectations/) uses named column-pair expectations such as pair equality and ordered comparison. F36 adopts named, typed pair operations, but not its free-form result details.
- [Great Expectations result formats](https://docs.greatexpectations.io/docs/0.18/reference/learn/expectations/result_format/) distinguish aggregate counts from lists of unexpected values. F36 keeps counts and rejects every unexpected-value field.
- [dbt data tests](https://docs.getdbt.com/docs/build/data-tests?version=1.12) establish `unique`, `not_null`, and `relationships` as separate assertions. F36 preserves those separate meanings rather than compressing them into one score.
- [PostgreSQL comparison semantics](https://www.postgresql.org/docs/current/functions-comparison.html) distinguish ordinary comparisons with unknown null results from explicit null-safe predicates. F36 adopts that separation and makes null rules explicit.
- [Deequ key concepts](https://github.com/awslabs/deequ/blob/master/docs/key-concepts.md) separate analyzers, metrics, constraints, and shared scans. F36 adopts shared scan collectors and keeps F35 constraints outside the measurement kernel.
- [SAP HANA join cardinality](https://help.sap.com/docs/hana-cloud-database/sap-hana-cloud-sap-hana-database-performance-guide-for-developers/join-cardinality) defines cardinality in terms of matches per row. F36 measures changes in per-parent-row child match counts, not merely total row-count drift.
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
| CLI run JSON and human fidelity | `/home/cam/vscode/decoy/src/decoy/cli/run.py:1182-1214`, `:1241-1249`, `:1370-1424` | Current code forwards report dictionaries and prints table names and messages. F36 output must use only its sealed projection and numeric positions. |
| CLI post-hoc fidelity | `/home/cam/vscode/decoy/src/decoy/cli/report.py:1613-1745`, `:1810-1913` | Reads paths, emits them and exception text on errors, writes arbitrary report JSON, and renders names and actual values. F36 gets a separate thin module after F31. Do not grow this oversized file. |
| Platform measurement hook and persistence | `/home/cam/vscode/decoy-platform/api/quality/runner_hook.py:180-318`; `/home/cam/vscode/decoy-platform/api/quality/service.py:33-95` | Current hook samples files, computes one quality report, logs grade and score, and stores arbitrary JSON. F36 adds a separate F31-sealed report path and no raw exception or path output. |
| Platform quality APIs | `/home/cam/vscode/decoy-platform/api/jobs/routes_quality.py:56-164`, `:170-239` | `/quality` returns the stored report verbatim and `/quality/details` allowlists aggregates but still exposes column names. Add a separate invariant endpoint and section. Do not insert F36 into either legacy shape. |
| Platform quality row | `/home/cam/vscode/decoy-platform/api/models.py:1552-1585` | `JobQualityReport` is specialized for distribution score and grade. Do not overload it. Add a reversible, value-free invariant-report row in the Platform slice. |

This inventory explains why F36 cannot be a new validator name or a new `quality_metrics` dictionary. Both existing carriers can represent values that the hard constraint forbids.

## Design

### 1. F31 prerequisite and safe-output assumption

F31 must land before any F36 ordinary emitter or consumer. F36 assumes exactly these F31 capabilities from `origin/plan/f31-safe-output:docs/plans/2026-10-30-f31-shared-safe-output-boundary.md:141-178` and `:180-214`:

- immutable provenance-backed `SafeEvent`, `SafeCount`, `SafeMeasurement`, and `SafeLocation` types;
- one registry-backed serializer and registered final sinks;
- a `families.quality` home for named factories;
- rejection of plain mappings, free strings, raw exceptions, paths, names, and unregistered IDs at ordinary sinks;
- static and runtime canary sentries shared by engine, CLI, and Platform.

F36 adds invariant-specific fixed reasons, count-name enums, measurement-name enums, and named quality factories through that registry. It does not add a redactor, serializer, generic event factory, or alternative safe boundary. The internal `InvariantReportV1` is a typed computation product. Ordinary JSON, text, log, evidence, persistence, and API output is produced only by the F31 registered invariant projection. If F31 is absent, engine-only model and evaluator work may be reviewed, but no public emitter, CLI slice, Platform slice, or release may land.

The F31 plan's proposed 0.8.0 release number is stale release bookkeeping, not a usable artifact number. This repository already has tags `v0.8.0rc1` and `v0.9.0` through `v0.16.0`; `v0.16.0` resolves to `5da40c2c6ce67db86703996dd8cf245955f05eca`, whose `pyproject.toml:10` is 0.16.0. The owner must amend F31 to release its additive safe-output contract as 0.17.0. F36 then releases as 0.18.0. Reusing 0.8.0 or publishing a lower version is forbidden.

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
                   parent_table: configured-table-name,
                   child_table: configured-table-name, coverage: exact }
```

Further closure rules:

- `checks` contains 1 to 256 entries.
- A table or column reference must resolve uniquely in the validated V1 pipeline. Relationship checks must resolve exactly one declared parent-to-child edge, including one child entry of a multi-child relationship.
- `position` is explicitly authored in the range 1 to 65,535. It is not inferred from list order.
- `sample_rows` is required only for sampled row-local checks and forbidden otherwise.
- `absolute_tolerance` is in `[0, 10^12]`, finite, parsed as `Decimal`, and never copied to an ordinary output. Relative tolerance is not in V1.
- Constants, arbitrary literal sets, operator aliases, implicit column lookup, cross-table row-wise comparison, and nested expressions are rejected.
- Validation errors are `InvariantConfigError` instances with a fixed reason code plus check, table, column, or relationship numeric position. No error reads or formats the offending name, value, Pydantic input, or exception text.

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
- relationship position is the zero-based position in `PipelineConfig.relationships`, plus child position for a multi-child entry;
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

For an aligned mask or subset result, a second fixed accumulator reports `introduced_failure_count`, `resolved_failure_count`, `persistent_failure_count`, and `comparable_row_count`. It compares boolean verdict states by row position only. Unknown or unevaluable states are not failures and have separate transition counts. If alignment cannot be proved, all transition fields are absent and status is indeterminate with a fixed reason.

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

For each relationship check, evaluate the selected source side and output side independently with the declared composite parent and child keys. Null semantics match the current validators:

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

Duplicate or null parent keys make the affected relationship's join-change verdict indeterminate because a parent row does not identify one stable join group. Source/output parent misalignment also makes it indeterminate. PK, FK, and orphan observations still report their exact counts. This prevents a duplicate-key defect from being hidden inside an apparently valid cardinality delta.

Subset runs compare the post-closure selected source population to its output, not the original full source. Quarantine applies the existing aligned removal mask pattern in `src/decoy_engine/execution/_pipeline_finalize.py:193-205` before source/output transitions are counted. A report never compares a subset or quarantine output to an unintended full population.

### 7. Bounded execution and route integration

Add these focused engine modules, each with a 450-line plan ceiling:

```text
src/decoy_engine/invariants/
  __init__.py
  _model.py              closed config, report, count, and enum types
  _registry.py           check and reason registries
  _compile.py            name resolution to numeric structure positions
  _row_eval.py           Arrow batch kernels
  _relationship_eval.py  exact DuckDB relationship and cardinality scan
  _collector.py          shared source/output accumulators and report assembly
  _safe_projection.py    named F31 quality factories only
  _api.py                public measure_invariants entry point
```

`measure_invariants(program, source_frames, output_frames, structure_index, limits)` is the narrow engine API. `run_pipeline` uses it through a route-owned collector; B-9 uses the same API for approved reference-backed generation. The public result is immutable and value-free. It has no generic `to_dict` or JSON method.

Route integration rules:

- Full-frame passes resident Arrow batches after quarantine alignment and before `ExecutionResult` construction at `src/decoy_engine/execution/_pipeline_full_frame.py:190-222`.
- Sequential feeds raw and final output batches into the collector before sink commit, then carries the assembled report through `src/decoy_engine/execution/_pipeline_route_exec.py:157-192`.
- Auto-chunk shares one run collector across chunks and finalizes after aggregation at `src/decoy_engine/execution/_pipeline_auto_chunk.py:444-468`. It never averages per-chunk rates.
- Unified slice feeds the reconstructed source-shaped output at `src/decoy_engine/execution/_unified_slice.py:285-350`. It must not create a second evaluator.
- Out-of-core calls the row collector after every FK join, not at the pre-join legacy-validator hook, in `src/decoy_engine/execution/out_of_core/_runner.py:460-498`. Relationship measurement stages only the configured key columns and parent row ordinal.

Relationship evaluation reuses the existing Arrow IPC spill, DuckDB connection, external group-by/join, memory-limit, disk-preflight, and temp-budget machinery. Scratch paths and DuckDB text never enter the report or an event. The evaluation retains O(number of checks) counters plus one configured batch. DuckDB owns spillable key grouping. There is no O(rows) Python set, list of failures, or match-count vector.

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
  reason: optional fixed InvariantReasonCode
  source_counts: optional RowCounts | RelationshipCounts
  output_counts: optional RowCounts | RelationshipCounts
  transition_counts: optional TransitionCounts | JoinChangeCounts
  measurements: tuple[BoundedMeasurement, ...]     # fixed names and units
```

The type has no timestamp, duration, job ID, fingerprint, table name, column name, relationship name, path, DSN, query, exception, message, detail, arbitrary metadata, row index, cell value, min, max, quantile, unexpected example, expected value, or actual value. Canonical order is `(check_ref.position, check_ref.code, structure positions)`. The F31 projection revalidates every field, converts each observation through named `families.quality` factories, and seals canonical bytes. Unknown fields, forged codes, out-of-range numbers, non-finite measurements, excessive observations, or unregistered types fail to F31's fixed invalid event.

### 9. B-9 and F35 consumption

B-9 remains report-only as required by `/home/cam/vscode/decoy-platform/docs/plans/2026-10-26-di1-measure-completion.md:19-24`, `:51-75`, and `:87-97`. Its Platform wrapper adds an `invariants` sibling section per matched source/output table pair. It preserves successful sections when another is `not_run`. It does not change the primary distribution report, its denormalized columns, grade, or job outcome.

Platform adds a reversible `job_invariant_reports` migration instead of overloading `JobQualityReport`. Each row stores job ownership, fixed schema version, numeric section position, fixed status, and one F31-sealed canonical payload. No denormalized value, name, detail, or path column is permitted. `GET /api/jobs/{job_id}/quality/invariants` returns the sealed schema through the F31 response sink with the existing owner-or-admin authorization pattern. The B-9 details assembler may embed that same sealed section without parsing it into an application mapping.

F35 consumes `CheckRef`, observation status, verdict, counts, and fixed measurements. A contract refers to the pair `(code, position)`. It does not re-evaluate data, match prose, inspect configured names, or read a legacy validator report. V1 eligibility is deliberately strict: only `measured_exact` plus `pass` may satisfy a required check. Exact fail blocks when F35 enables a gate. Sampled, unsupported, not-run, partial, or indeterminate observations cannot satisfy it. F36 adds the seam and contract fixtures only. It does not implement F35 enforcement.

### 10. Compatibility class and release sequence

Compatibility class: **additive pre-GA minor, opt-in public config and API, plus a new versioned artifact**.

- Existing pipeline V1 remains valid because `invariants` is optional and defaults to `None`.
- Existing output bytes, determinism, route choice, validator behavior, quality grades, and `quality-report/v1` remain unchanged when it is omitted.
- `decoy-invariant-report/v1` is a new artifact. Its shape is frozen under that tag. Later additions require optional fields that old readers ignore or a new `/v2` reader and writer.
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
8. **Relationship defects:** separate fixtures exercise duplicate parent keys, null parent keys, all-null child keys, partial-null composite keys, unmatched keys, zero/one/many parent matches, and an empty child. Each observation pins exact counts and the required indeterminate join status.
9. **Exact and sampled honesty:** exact clean data passes; a deterministic sample containing a failure fails; a clean sample is indeterminate; a failure deliberately outside the sample proves the report never claims zero total failures; sampled relationship config is rejected.
10. **Scope and alignment:** a subset excludes a known full-source violation, so the selected-population report passes while a deliberately incorrect full-source comparison would fail. Quarantine removes a different row and source/output transition counts remain aligned.
11. **Route parity:** equivalent eligible fixtures force full-frame, sequential, auto-chunk, unified-slice, and out-of-core routes. Reports are canonical-byte equal across batch sizes 1, 7, and 65,536. Route telemetry proves each intended route actually ran.
12. **Sink and failure timing:** out-of-core invariant failure remains report-only and the transactional output commits; evaluator infrastructure failure before sink commit yields fixed `not_run` evidence and no raw exception text. Config failure occurs before the first source read.
13. **Pure generation and reference-backed generation:** pure generate measures output row checks but reports source and join change as `not_run`; the same generated frame with an approved reference reports exact source/output transitions through the same engine API.
14. **Value-free corpus:** unique raw cell, table, column, path, DSN, URL, credential, exception-message, Unicode, UUID-shaped, short, numeric, and control-character canaries are seeded. Scan object repr, sealed JSON, text, CLI JSON/human/stderr, Platform persistence/API, logs, evidence, and failure paths. Assert every canary is present in its input, at least one observation exists, and no canary or canary length derivative appears in any ordinary output.
15. **Schema and construction sentries:** direct construction, subclass, `dataclasses.replace`, `model_dump`, `asdict`, arbitrary code injection, extra report field, non-finite measurement, negative count, and excessive observation attempts are rejected. Mutation tests weaken each sentry and prove it turns RED.
16. **B-9 isolation:** two matched pairs yield two invariant sections in declaration order; one section can be not-run while the other remains exact; no invariant changes `overall_score`, grade, job status, or the legacy quality payload.
17. **F35 seam:** a contract fixture resolves a stable exact passing `CheckRef`; exact fail, sampled, unsupported, not-run, and indeterminate fixtures are all ineligible. The test imports no F35 evaluator and causes no publication decision.
18. **100M boundary:** a generated narrow 100M-row Arrow/Parquet workload completes exact row counts and one relational metric within the existing memory and scratch budgets; a 100,000,001-row declared scope returns fixed `not_run` before staging. The evidence records wall time, HWM, Arrow pool peak, DuckDB limit, and scratch peak without paths or values.

## Implementation slices

### Slice 0: typed model, registry, RED corpus, and prerequisite sentries

Scope:

- Run the topology gate. Record the engine baseline, `v0.16.0`, F31 reviewed commit, CLI remote HEAD, Platform remote HEAD, dependency pins, and migration heads. Stop on incompatible ancestry or an unamended F31 release number.
- Rerun the shipped ordering tests and the joint-mask remediation tests. Add a mutation sentry that removes one dependency edge or source anchor and proves the prerequisite suite fails.
- Author the shared canary corpus and acceptance tests 1, 2, 14, and 15 first. Capture RED on the missing F36 types and grammar behavior.
- Create `_model.py`, `_registry.py`, strict config models, `CheckRef`, `StructureRef`, all count types, report types, registry manifests, and fixed config-error codes.
- Add the module-size sentry entries and public import contract, but no evaluator and no ordinary serializer.

Self-check:

```bash
pytest -q tests/unit/execution/test_cross_column_ordering.py tests/unit/execution/test_cross_column_joint_mask_remediation.py
pytest -q tests/unit/invariants/test_model.py tests/unit/invariants/test_registry.py tests/security/test_invariant_report_boundary.py tests/sentry/test_module_size.py
ruff check src/decoy_engine/invariants tests/unit/invariants tests/security/test_invariant_report_boundary.py
mypy src/decoy_engine/invariants
```

Targeted verification: inspect every model field and registry member mechanically; assert no `Any`, `dict`, `Mapping`, `bytes`, exception, path type, unconstrained `str`, or public serialization helper occurs in report models. Confirm omission of `invariants` preserves a baseline config dump.

Adversarial review and remediation: attempt grammar smuggling, enum subclassing, bool-as-int, negative and huge counts, duplicate identities, input echo through Pydantic errors, and construction forgery. Resolve every BLOCKER and HIGH before Slice 1. Retain RED evidence and green rerun logs.

### Slice 1: row-local compiler and evaluator

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

Scope:

- Author acceptance tests 7, 8, the relational half of 9 and 13, and the small-scale part of 18 RED first.
- Implement `_relationship_eval.py` with narrow Arrow staging, DuckDB external aggregation, aligned parent ordinals, exact FK/orphan counts, and join-change counts.
- Reuse `connect_duckdb`, Arrow IPC spill patterns, memory limits, temp-disk preflight, and budget checks. Keep scratch lifecycle inside the existing run-owned temp root.
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

Adversarial review and remediation: force spill, exhausted disk budget, connection failure, malformed Arrow input, duplicate parents, partial-null composites, row reorder, and source/output count mismatch. No exception, scratch path, key, or SQL may enter the report. Resolve every BLOCKER and HIGH before route wiring.

### Slice 3: all engine routes, F31 projection, artifact, and 0.18.0 release candidate

Scope:

- Author acceptance tests 10, 11, 12, 14, 15, and the full 100M test RED first.
- Add one collector seam to full-frame, sequential, auto-chunk, unified-slice, and out-of-core execution as specified above. Do not duplicate evaluation code in route modules.
- Add `ExecutionResult.invariant_report`, the public `measure_invariants` API, and F31 `families.quality` projection factories.
- Add `decoy-invariant-report/v1` canonical serialization, schema tamper tests, compatibility-corpus fixture, package exports, changelog entry, and the coordinated engine 0.18.0 release metadata after F31 0.17.0.
- Keep route behavior and outputs unchanged when `invariants` is omitted.

Self-check:

```bash
pytest -q tests/unit/invariants tests/integration/invariants tests/parity/test_invariant_route_parity.py
pytest -q tests/security/test_invariant_report_boundary.py tests/security/test_safe_output_boundary.py tests/sentry
pytest -q tests/integration/compat_corpus tests/integration/golden
python scripts/test_flight.py
```

Targeted verification: force every route and batch size, compare canonical report bytes, verify transactional sink behavior, run the 100M workload in the approved benchmark environment, and compare no-invariant golden artifacts byte for byte with the baseline.

Adversarial review and remediation: trace every report field from raw batch through collector, result, safe factory, serializer, sink, and failure path. Search route modules for alternate mappings or serialization. Resolve every BLOCKER and HIGH, rerun the full engine suite, and record exact artifact hashes.

### Slice 4: CLI consumer, independently gateable

Prerequisite: published engine 0.18.0 and F31-migrated CLI boundary.

Scope:

- Topology-gate the remote CLI. Pin exactly `decoy-engine==0.18.0` and refresh its lock and package evidence.
- Author CLI portions of acceptance tests 13 and 14 RED first.
- Add a small `src/decoy/cli/report_invariants.py` command module and register `decoy report invariants`. Do not add logic to the existing oversized `report.py`.
- Make `decoy run` render an invariant summary only from F31-sealed events when a program ran. JSON and human modes show check code, explicit position, structure positions, status, verdict, counts, and bounded measurements only.
- Preserve report-only exit code 0 for measured violations. Bad config stays usage failure with a fixed reason. F35 later owns a gate-oriented exit code.

Self-check:

```bash
pytest -q tests/e2e/test_report_invariants.py tests/e2e/test_run_invariants.py tests/security/test_value_free_output.py
ruff check src/decoy/cli/report_invariants.py tests/e2e/test_report_invariants.py tests/e2e/test_run_invariants.py
mypy src/decoy/cli/report_invariants.py
```

Targeted verification: exercise human, JSON, quiet, verbose, report-file, malformed-config, missing-file, read-error, exact, sampled, and pure-generate paths. Assert no path or exception text appears and violation exit status remains report-only.

Adversarial review and remediation: seed canaries into filenames, config names, cells, exception messages, environment values, and output arguments. Attempt to pass the internal report directly to Rich or JSON. Resolve every BLOCKER and HIGH, then run the full CLI suite and package smoke test.

### Slice 5: Platform B-9 consumer and F35 seam, independently gateable

Prerequisite: published engine 0.18.0, F31-migrated Platform boundary, and B-9 topology reconciliation.

Scope:

- Topology-gate Platform remote HEAD, B-9 state, exact engine pin, API authorization helpers, and Alembic heads. Pin exactly `decoy-engine==0.18.0`.
- Author acceptance tests 16, 17, and Platform portions of 14 RED first.
- Add the reversible `job_invariant_reports` migration, typed persistence adapter, B-9 invariant section assembler, and owner-scoped `/quality/invariants` endpoint.
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

The resident direct API on small fixtures is the semantic oracle. Every other route is compared to its canonical `decoy-invariant-report/v1` bytes. The oracle is not a separately coded checker: it invokes the same compiler and collectors with resident batches. Hypothesis varies row counts, null positions, batch partitions, operators, numeric types, and relationship fanout. Mutation tests invert an operator, change null handling, drop a final batch, average chunk rates, move the out-of-core hook before FK rewrite, or treat sampled zero as pass. Every mutation must be killed.

### Security and value-free evidence

The F31 shared analyzer scans all new ordinary sinks. F36 adds exact AST sentries that reject:

- serialization of `InvariantReportV1` outside `_safe_projection.py`;
- `dataclasses.asdict`, Pydantic dump methods, `json.dumps`, Rich rendering, logging, or API return of an invariant domain object or mapping;
- report models containing forbidden field types or unconstrained strings;
- log, warning, error, or reason construction from exception text or configured names;
- a `quality_metrics` key containing `invariant`;
- direct imports of DuckDB scratch paths or raw key arrays into report/projection modules.

### Performance evidence

Run correctness at tiny and medium sizes in normal CI. Run 1M, 10M, and 100M exact workloads in the approved benchmark environment for row-local and relational paths. Acceptance is bounded scaling and completion within existing run budgets, not a new marketing throughput target. Failure above the cap, below required scratch space, or below the non-spillable memory floor must be controlled and coded. No multi-node test or capacity abstraction beyond the existing budget machinery is added.

### Release and rollback

Release units are engine 0.17.0 F31, engine 0.18.0 F36, CLI consumer, and Platform consumer. Engine publishes before consumers. Consumers initially exact-pin 0.18.0. The CLI and Platform slices are additive and can roll back independently by reverting their consumer commit and dependency pin. The engine 0.18.0 artifact remains readable and the optional config remains accepted. Platform migration downgrade removes only F36 report rows and its new table; it does not touch jobs or legacy quality reports. No rollback claims restoration of already deleted report rows after a downgrade.

Post-release checks are a clean exact and sampled CLI fixture, one Platform mask job, one reference-backed generate job, owner and non-owner API checks, invariant persistence read-back, no grade change, and canary scan. Abort consumer rollout on any raw token, route mismatch, wrong stable ID, count imbalance, migration failure, unexpected job outcome, or F31 rejection.

## Documentation plan

Documentation is part of each repository slice, synchronized before its code gate:

- Engine: update `README.md` public entrypoints, `CODEMAP.md`, configuration reference, relationship guide, API reference, `docs/compatibility-contract.md`, changelog, and a new invariant-language reference with truth tables, null rules, sampling limits, exact-only relationship rules, 100M cap, and schema examples containing synthetic names only.
- CLI: add `decoy report invariants` to command reference, examples, exit-code docs, and migration notes. State that output uses numeric positions and registry codes by design.
- Platform: document the new endpoint, authorization, persistence schema, B-9 separation, report-only behavior, migration rollback, and F35 eligibility seam.
- Product capability statement: say F36 measures declared checks and relationship changes. Do not claim automatic discovery, population fidelity, or release gating.
- Architecture record: document why `CheckRef` is code plus explicit numeric position, why reports stay outside `quality_metrics`, and why sampled clean observations are indeterminate.

Per repository guidance, the build session delegates documentation synchronization to Barry before objective-green. Barry changes documentation only. Documentation sync is implementation work, not an additional adjudication gate.

## Risks and controls

| Risk | Control and evidence |
|---|---|
| A value leaks through a report, error, log, API, or persistence row | Closed models, F31-only projection, forbidden-field sentry, seeded canary matrix, and adversarial sink trace. |
| A sampled clean result is mistaken for exact zero | Sampled zero is structurally indeterminate; relationship sampling is rejected; F35 eligibility test refuses it. |
| Route-specific placement changes semantics | One collector, post-FK out-of-core hook, canonical-byte route parity, forced-route telemetry, and batch partition property tests. |
| Join change is confused with row-count or orphan drift | Acceptance fixture keeps row totals and orphans constant while changing two parent multiplicities. |
| Duplicate parent keys produce a misleading cardinality comparison | Count duplicates, mark join change indeterminate, and keep other relational observations separate. |
| Subset or quarantine compares the wrong population | Post-closure selected source scope, existing quarantine alignment, and fixtures where full-source and selected-source answers differ. |
| Exact relationship measurement becomes unbounded | Narrow IPC staging, external DuckDB group-by/join, existing budgets, no Python key sets, HWM/scratch evidence, and a 100M hard cap. |
| Stable IDs drift with list reorder or names | Explicit authored position plus fixed code, canonical sorting, reorder test, and name/value replacement test. |
| Existing validators and new measurements disagree | Fixture parity for checksum, FK, orphan, and reconciliation semantics; discrepancies block the slice rather than being normalized away. |
| F36 accidentally becomes a gate | Report-only outcome tests in engine, CLI, and Platform; no enforcement API; F35 seam tests only eligibility. |
| Existing quality consumers break | New artifact and endpoint, no mutation of `quality-report/v1`, exact compatibility corpus, no-invariant goldens, and separate Platform table. |
| Release numbers collide with existing tags | Owner-approved F31 numbering amendment, 0.17.0 then 0.18.0, release ancestry check, exact pins, and tag-existence preflight. |
| Module growth creates another oversized seam | New modules target 450 lines, module-size sentry, thin route hooks, and no growth in oversized CLI `report.py`. |

## Gate chain and remote handoff

### Plan gate

This file is the only artifact of the current task. The plan gate after the Codex author is:

1. argon review with `google/gemini-4-argon`;
2. exactly one Opus-4.8 Dennis repo-grounded review;
3. owner decision.

Every argon and Dennis BLOCKER or HIGH must be remediated in this file and the revised file must repeat the same ordered gate. No build starts before owner GO.

### Code gate

For every repository slice, the terminal code gate is ordered exactly:

1. objective-green evidence;
2. Codex review;
3. argon review;
4. exactly one Opus-4.8 Dennis repo-grounded gate;
5. owner merge decision.

Barry documentation sync occurs before objective-green. It is not a second review verdict. After the one Dennis pass and documentation sync, record the exact-HEAD repository gate receipt with `bash .claude/hooks/decoy-gate-pass.sh` where that hook applies. Any new commit invalidates the receipt and repeats the terminal gate. There is no second Dennis pass.

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
