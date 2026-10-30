Status: review only. Grants no implementation, merge, or release approval.

# F36 plan: Opus cross-model hostile review (2026-10-30)

Plan: `docs/plans/2026-10-30-f36-cross-column-invariants.md` at `plan/f36-cross-column-invariants` head `d99b604`. Baseline code: `decoy-engine` `fdd08a8` (the plan's stated baseline, which is the plan branch's parent), `decoy-platform` `origin/main` `ea68d8e`, F31 plan at `origin/plan/f31-safe-output` `a909118`. Contract: `decoy-platform:handoffs/queue:docs/handoffs/DEFECT-CATALOG.md` and `HOSTILE-PLAN-REVIEW.md`.

What I ran: `plan-lint plan` from `handoffs/queue` (stdlib script, system python3): `OK`. Everything else is reading the code. I did not run pytest, gates, baselines, or the engine. No `uv.lock` environment was needed because nothing was executed against the package. `scripts/dev/bootstrap.sh` and `scripts/dev/gates.sh` (catalog section 6) do not exist in `decoy-engine` at this baseline, so I could not run them. I did not verify the meaning of "B-9" beyond the Platform quality hook, and I did not open the decoy CLI repo.

## Verdict: GO-WITH-FIXES

The six Codex findings are mostly closed in shape, but two of them recur in a new form (F31 prerequisite, collector lifecycle), Platform persistence is specified against the wrong key, and the plan never says what a run does when F36 itself fails. All fixes are plan text. Slices 0 to 2 can start once BLOCKER 1 is answered; Slices 4 to 10 should not start until every BLOCKER and HIGH below is written into the plan.

Counts: BLOCKER 2, HIGH 5, MEDIUM 7, LOW 5.

## 1. Grounding check (29 citations checked, plus 3 omissions; every enumeration)

Correct (25): `execution/_runner.py:234-378` (`joint_mask_key_by_columns` at :234, ordering deps to :378); `tests/unit/execution/test_cross_column_ordering.py:83-233` (three invariant classes, file ends :233); `config/_pipeline.py:45-99` (`PipelineConfig`, `extra="forbid"` at :57); `config/_relationships.py:24-49`; `relationships/_graph.py:71-204` (`build_relationship_graph` :134, first-hit text :167); `validators/_fk_validators.py:72-301` and `_relationship_checks.py:31-342` (both run to EOF); `checksums.validate` (`checksums.py:351`); `_pipeline_multi_table.py:260-402`; `_pipeline_mixed_route.py:148-282`; `_unified_slice.py:275-350` with finalize at `:314`; `_pipeline_auto_chunk.py:218-350,395-450`; `out_of_core/_runner.py:125-341,450-531`; `_isolated_run.py:141-243,409-622`; `_governor.py:243-436`; `_probe.py:222-352`; `_chunked.py:369-449`; `ExecutionResult` fields (`_adapter.py:61-71`); `IsolatedRunResult` fields (`_isolated_common.py:362-388`); `GovernorTripRecord` and `GovernorResult` (`_governor.py:170-211`); `ProbePoint` and `ProbeResult` (`_probe.py:133-172`); `src/decoy_engine/reasons/`; `tests/sentry/test_physical_seam_disconnection.py`; `tests/sentry/test_module_size.py`; F31 section titles and `families/quality.py` (F31 plan :243-266, :280, :303, :322, :339, :613).

Wrong or phantom (4):
- Route table row 1 cites `_pipeline_generate_mask.py:119-157,276-308` and says "collect after `finalize_validators_and_quarantine`". That module never calls it. The full-frame finalize is `_pipeline_full_frame.py:194`, and `run_generate_and_mask_steps` (`_pipeline_full_frame.py:123`) runs before quarantine. See HIGH 5.
- `_adapter.py:121-210` as "Direct `PandasExecutionAdapter.run`": `_adapter.py:121` is `StrategyContext`. The adapter is `_pandas_adapter.py:105`, `run` at `:163`.
- `native/_chunked_entry.py:601-690`: the file is 681 lines (census `test_module_size.py:100`). `run_mask_chunked` is at `:601`.
- Reuse row "Reuse ... temp-size budgets" for DuckDB: `out_of_core/_duckdb.py:42` sets only `temp_directory` and `memory_limit`; there is no `max_temp_directory_size` today (`_budget.py:27` only names it as an analogy). New machinery, not reuse.

Missing from the route enumeration (3): `_pipeline_full_frame.py` (the actual full-frame owner, `publish.commit()` at `:243`); `_pipeline_route_exec.py` (600 lines, owns `run_sequential_route` :114 and `run_out_of_core_route` :195); `_pipeline_route_dispatch.py:27-82` (the three executors `_pipeline.py:313-320` dispatches to).

Enumerations: exit paths list no F36-failure disposition for the run itself (BLOCKER 2). FK shapes list no per-route expected disposition (MEDIUM 5) and mixes a Platform DB FK into the data FK manifest (LOW 2). Field list leaves `status`, `verdict`, `coverage`, `structures` and `BoundedMeasurement.name` untyped (HIGH 4, MEDIUM 2). Output surfaces are complete in shape but miss the F31 data-plane writer manifest (BLOCKER 1).

## 2. Re-check of the earlier Codex findings

| Codex finding | Fixed? | Evidence |
|---|---|---|
| Phantom F31 prerequisite | Partly. The aggregate registry is gone (plan :66). It recurs as unowned summary event families and unregistered data-plane writers. | BLOCKER 1 |
| Sampled-counts contradiction | Yes in types (plan :130-138, :282, :285). Residual test contradiction. | MEDIUM 4 |
| Single verdict for source/output/transition | Yes (plan :24-25, :140, :142). | Residual in MEDIUM 5 |
| Finalize overtakes an admitted feed | Yes for that pair (plan :146-156). New holes in the same table. | HIGH 1 |
| Unspecified Platform persistence | Specified (plan :174-185) but keyed wrong and in conflict with the existing hook. | HIGH 2 |
| Governor/probe fields missing from frozen list | Yes. Plan :297-300 matches `_governor.py:170-211` and `_probe.py:133-172` exactly. | none |

## 3. Findings

### BLOCKER 1. F31 prerequisite recurs: no owner for invariant summary events or artifact writers (class 1; recurrence of Codex B1)

Section: F31 assumptions (plan :51-68), Output surfaces (:310-317), Slice 9 (:432).
Evidence: F31 provenance is closure-held in the engine and "There is no generic public factory" (F31 plan :270, :282). `families/quality.py` holds "quality, fidelity, and diagnostic factories" (F31 plan :263), not invariant summaries. F31's analyzer allows an ordinary sink call only with a `SafeEvent` or `SerializedSafePayload` (F31 plan :464). Any other byte writer must sit on "a reviewed exact list of data-plane writer functions" (F31 plan :66). F36 says the artifact "is not a `SerializedSafePayload`" (plan :168) and forbids amending F31 (plan :68).
Failure: Slice 9 says the CLI builds "run summary factories". The CLI cannot mint a `SafeEvent`, so it either forges one (provenance fails closed) or prints outside the boundary (the analyzer goes red). The CLI file publisher, the Platform byte response (plan :316) and the engine sidecar writer are none of them on F31's data-plane manifest, so F31's shared analyzer fails Slices 7, 9 and 10. The worker then widens an exemption, which is forbidden.
Class-level fix: add to the assumptions table one row per F31 extension point F36 uses, by exact F31 mechanism: (a) a new engine `safe_output/families/invariants.py` module with named factories, built in an engine slice (Slice 0 or 2) through F31's own family pattern, not in the CLI; (b) exact data-plane writer entries for `InvariantSidecarLease.write`, the CLI artifact publisher and the Platform byte-response adapter, each added to that repo's F31 manifest in the slice that builds it. RED: the F31 analyzer flags each writer before registration.

### BLOCKER 2. What the run does when F36 itself fails is undefined (classes 2 and 4)

Section: Guarantee 8 (plan :30), Collector (:156), Scratch (:164), Acceptance 14 (:369).
Evidence: F36 feeds and finalizes "before the existing commit point" (plan :212). Commit is `_pipeline_full_frame.py:243` and the sequential commit protocol (`_sequential.py:23-63`). The plan only says "Failed assembly closes with a fixed reason" and that 100,000,001 rows give `not_run`.
Failure: a DuckDB error, scratch identity drift, procfs absence, codec cap overflow or collector assembly failure surfaces either as an exception before commit, so outputs are not published and guarantee 8 breaks, or as a swallowed error, so the run reports nothing and a silent fallback appears (class 4). Every route slice (4 to 7) will be gated on this, and each worker will choose differently.
Fix: one written rule: any F36 internal failure after config validation yields a report whose affected observations are `not_run` plus a registered reason, and never changes publication or job outcome. Config-time refusals (probe, direct chunked, limits) stay pre-read. Add the failure kinds as rows in the exit-path matrix per route, with a mutation that re-raises.

### HIGH 1. Collector table still lets a feed starve close and can strand `DRAINING` (class 3; recurrence of Codex H4)

Section: Collector lifecycle (plan :146-156).
Evidence: `OPEN` close with a feed in flight "set[s] `close_pending`" but stays `OPEN`, and `OPEN` feed is "admit". `DRAINING` finalize: "one finalizer waits; others reject".
Failure: (a) a producer thread keeps feeding after close, so close never completes, and the barrier matrix will assert this as correct because it is generated from the table. (b) The single waiting finalizer is cancelled (`KeyboardInterrupt`, governor kill in-process, thread cancel), so `DRAINING` has no claimer and every later finalize rejects. (c) `FINALIZING` plus `close_pending` does not say whether the report is returned or dropped. (d) No bound on the wait if a feed hangs in DuckDB.
Fix: `close_pending` rejects new feeds in every state; finalizer cancellation returns to a terminal `CLOSED` with a fixed reason; state the `FINALIZING` close outcome; give finalize a bounded wait tied to the run's existing timeout. Add these four as rows in the transition table so the generated matrix covers them.

### HIGH 2. Platform key ignores attempt fencing; conflict changes job outcome; transaction rule contradicts the existing hook (classes 3 and 2)

Section: Platform persistence contract (plan :174-185), Slice 10.
Evidence: Platform executes fenced generations: `JobAttempt` with `uq_job_attempts_job_generation` and one non-terminal attempt (`decoy-platform api/models.py:974-999`), and `job_outputs` hang off `job_attempts.id` (`models.py:1189`). The existing quality hook commits its own session and swallows errors (`api/quality/runner_hook.py:141-153`, `:273`).
Failure: generation 1 is fenced after it wrote; generation 2 re-reads a database source that changed and gets different bytes. Then `invariant.report_conflict` fails the completion transaction, which changes job outcome (breaks guarantee 8), or a stale generation's report wins. "Flushes but never commits the caller-owned transaction" has no named caller and contradicts the hook pattern a worker will copy. `section_position` is never defined: what produces section 2 of a job?
Fix: key by `(job_id, attempt_id, section_position)` or write only under the active-attempt fence; define the section; name the runner call site and its transaction; conflict is recorded as a registered reason, never raised into job completion.

### HIGH 3. Source-side feed on streamed routes is unspecified; OOC single-read invariant at risk (classes 9 and 4)

Section: Route table (plan :227-229), Row semantics (:160-162), Slice 6.
Evidence: the OOC reorder driver reads the raw child exactly once by design (`out_of_core/_stream_driver.py:11-16`). The masked output is resolved later from the payload store (`:32-37`). The plan places only the output hook ("after FK rewrite, before commit").
Failure: source-scope observations and row-local transitions need per-row source verdicts aligned to output rows. On OOC the raw batch is gone by phase 3, so a worker either re-reads the source (breaks the single-read invariant and its permutation-misalignment guard) or keeps per-row state in Python (breaks "no Python collection grows with rows"). The same choice arises in sequential and auto-chunk streamed modes.
Fix: per streamed route, name the source feed point (phase 1 batch for OOC), the aligned per-row transition carrier (spilled bitmap under the scratch lease), and add a source-read counter to acceptance 8 that must equal 1.

### HIGH 4. `BoundedMeasurement(name, value, unit)` is an open value slot (classes 1 and 6)

Section: Public result fields (plan :287-288), Guarantee 7.
Evidence: no closed measurement registry is defined anywhere in the plan. Guarantee 7 forbids "value-derived label" but not value-derived numbers.
Failure: a `sum_equals` or `reconciliation` measurement such as max absolute deviation on a one-row selection equals a cell value. The seeded numeric marker (plan :388) appears in the artifact, the DB row and the HTTP body. Or the worker makes `name` a free `str`, and the field walk passes because it pins type only.
Fix: enumerate every measurement as a closed enum per check kind in `_registry.py`, state which are value-derived, and either drop them or quantize them under a stated rule. Add a numeric-marker cell to the artifact corpus with a non-vacuity check.

### HIGH 5. Full-frame hook placement is grounded on the wrong module (classes 4 and 9)

Section: Execution routes row 1 (plan :223), Slice 4.
Evidence: see section 1. The only full-frame finalize is `_pipeline_full_frame.py:194`. `_pipeline_route_exec.py` (600 lines) and `_pipeline_route_dispatch.py` are absent from the table.
Failure: a worker following the citation wires collection inside `run_generate_and_mask_steps`, before quarantine. The only placement mutation listed is for unified and OOC (plan :199, :386), so nothing goes red.
Fix: correct the row to `_pipeline_full_frame.py:194-214`, add the two missing modules, and add a "move full-frame hook before quarantine" mutation.

### MEDIUM 1. Module-size rules contradict the exact-match census (classes 8 and 9)

Section: Slices preamble (plan :392), L1 (:333), worker rules (:450).
Evidence: the census records exact LOC and a shrink must ratchet the entry down (`test_module_size.py:14-31`). Hook sites already on it: `_sequential.py` 696 (:120), `out_of_core/_runner.py` 681 (:124), `native/_chunked_entry.py` 681 (:100), `_chunked.py` 627 (:96), `_isolated_run.py` 622 (:68). `_pipeline_route_exec.py` is at exactly 600.
Failure: "extract a helper before adding a hook" changes LOC, which requires editing the census line, but the plan forbids any allowlist edit. Any hook in `_pipeline_route_exec.py` crosses 600 and needs a new entry. The worker either stalls or breaks a rule.
Fix: "census entries may only ratchet down; no new entries; `_pipeline_route_exec.py` must stay at or below 600".

### MEDIUM 2. Frozen `/v1` enum tokens are not enumerated (class 10)

Section: Counts, status, verdict (plan :140); Frozen contracts (:349).
Evidence: status tokens appear piecemeal (`measured_exact` :25, `not_run`, `unsupported` :140); there is no `measured_sampled`, no list of `coverage` values for `InvariantReportV1`, and no `structures` union.
Failure: Slices 0 and 2 freeze different token sets, and the schema snapshot then blocks the fix without a `/v2`.
Fix: one table of every enum and union member in the plan.

### MEDIUM 3. F29 append needs a minor version bump and a new namespace profile (class 10)

Evidence: `tests/unit/test_reason_registry.py:70-72` and `:150-157` fail on added identities without a minor registry bump. Namespaces are fixed profiles in `reasons/_catalog.py:17-160`; there is no `invariant`. `invariant.report_conflict` is a Platform-side condition, so the plan does not say whether it is engine-registered.
Fix: state the profile, the version bump and the owner of each code.

### MEDIUM 4. Vacuous passes and a self-contradictory sampled test (class 8)

Evidence: "exact clean yields `pass`" (plan :140) includes `rows_total=0`. Acceptance 3 asserts clean sampled has `unobserved_rows>0` (:358), but when `sample_rows >= population_rows` the formula at :160 gives `m=population` and `unobserved_rows=0`.
Failure: a job that quarantines every output row yields exact output pass, which F35 accepts. The sampled test is either red for a correct implementation or the worker weakens it.
Fix: zero evaluated rows gives `indeterminate` with a reason; restate acceptance 3 for full-coverage samples.

### MEDIUM 5. FK shape by route has no expected-disposition table (class 5)

Evidence: OOC rejects self-referential edges and falls back to full-frame (`out_of_core/_compat.py:478-492`). For a generate-parent table, join change has no source parent.
Failure: the route-by-FK product (acceptance 6, 8) has no oracle for these cells, so the worker invents one per slice.
Fix: add an expected status, reason and route-telemetry column per shape and route.

### MEDIUM 6. Default-inclusive config dump consumers are not enumerated (class 10)

Evidence: `config/_override.py:65`, `execution/capacity.py:219`, `verification/postmask/runner.py:53` all depend on `model_dump()` shape. The plan says "every dump consumer is characterized" (:340) but lists none.
Fix: grep and list them with file:line, as the contract requires.

### MEDIUM 7. Relationship checks become Linux-only, with no platform matrix (class 4)

Evidence: the procfs bridge is the only DuckDB path, and other platforms "refuse before staging" (plan :164). No `dir_fd` or procfs use exists today (grep is empty).
Failure: on a macOS CLI every relationship check is `unsupported`. Route parity tests then differ by OS, and gate reviewers on macOS see different results.
Fix: state the supported OS per check kind and mark the parity matrix Linux-only explicitly.

### LOW

1. Two whole-file citations (`_fk_validators.py:72-301`, `_relationship_checks.py:31-342`) say nothing about which truth definition is reused (class 8).
2. The FK shape list includes `job_invariant_reports.job_id -> jobs.id` (plan :268); that is a schema FK, not a data FK shape.
3. The reuse inventory omits `tests/sentry/test_validate_no_mutation.py`, `test_methodology_citations.py` and `test_capability_matrix.py`, all of which a new `measure_invariants` or `validate_*` helper will meet.
4. The evidence rules depend on catalog section 6 gate scripts that do not exist in this repo (`scripts/dev/` absent).
5. Checksums at 100M rows are a per-value Python call (`checksums.py:351-369`) with no time budget in acceptance 14.

## 4. Catalog classes: is the closing structure real and sufficient?

| # | Real? | Case not closed |
|---|---|---|
| 1 | Partly | Summary events and writers unowned (BLOCKER 1); measurement values (HIGH 4) |
| 2 | Yes for leases | Run-level F36 failure disposition (BLOCKER 2); Platform transaction owner (HIGH 2) |
| 3 | Partly | close starvation, finalizer cancel, attempt fencing (HIGH 1, 2) |
| 4 | Yes in shape | Wrong full-frame site and missing modules (HIGH 5); OS matrix (MEDIUM 7) |
| 5 | Partly | No disposition per shape and route (MEDIUM 5) |
| 6 | Yes | Open `BoundedMeasurement.name` (HIGH 4) |
| 7 | Yes, gated by spike | Bridge is Linux-only (MEDIUM 7) |
| 8 | Yes | Vacuous pass and contradictory sampled test (MEDIUM 4) |
| 9 | Partly | OOC single-read (HIGH 3); census contradiction (MEDIUM 1) |
| 10 | Partly | Enum tokens, F29 bump, dump consumers (MEDIUM 2, 3, 6) |

## 5. Ten ways a gate reviewer breaks the built feature

| # | Break | RED-first test in plan? |
|---|---|---|
| 1 | CLI prints an invariant summary with no F31 factory to mint it | No |
| 2 | F31 analyzer flags the sidecar, CLI file or Platform byte writer as an unregistered sink | No |
| 3 | DuckDB error before commit drops published output, or is swallowed silently | No |
| 4 | Close while feeds keep arriving never completes; cancelled finalizer strands `DRAINING` | Partial: matrix exists but is generated from a table that encodes the bug |
| 5 | Retried attempt with changed source raises `report_conflict` and fails the job | No |
| 6 | OOC source-scope observation re-reads the source | No: acceptance 8 has no read counter |
| 7 | Numeric cell marker surfaces as a measurement value | Partial: corpus has numeric markers, no closed measurement set |
| 8 | Full-frame hook placed before quarantine | No: mutations cover unified and OOC only |
| 9 | All-quarantined output reports exact pass and F35 accepts it | No |
| 10 | Self-referencing FK on OOC: report claims OOC measured it, but the run fell back to full-frame | Partial: route telemetry is asserted, no expected disposition |

## 6. Frozen contracts, F31 assumptions, sizing, slice order

- Frozen: the additive fields match the code. Gaps are enum tokens (MEDIUM 2), the F29 version bump (MEDIUM 3) and dump consumers (MEDIUM 6). F31 itself lists `ExecutionResult` and `IsolatedRunResult` serialization as breaking (F31 plan :228), so the F36 snapshot must be taken after F31 Slice 5, or it will be rebaselined.
- F31 assumptions: the table assumes the ordinary-output factories and the data-plane exemptions exist without naming them (BLOCKER 1).
- Sizing: the 450-line ceiling is fine. Slice 3 (procfs spike plus DuckDB plus relational evaluator plus reconciliation) and Slice 10 (migration, ORM, service, API, B-9, F35 fixture, framework surfaces) are each too large for two rounds. Split Slice 3 into 3a (spike and lease) and 3b (evaluator), and Slice 10 into 10a (schema and service) and 10b (API, B-9 and framework surfaces).
- Order: put the invariant event family (BLOCKER 1 fix) in an engine slice before Slice 4, because route telemetry and decline events need it. Slice 8 waits on F31 being gated. That is stated.

## 7. Can a worker build it in at most two remediation rounds?

Slices 0, 1, 2 and 7: yes, once HIGH 1 is written into the table. Slices 4 to 6: yes only after BLOCKER 2 and HIGH 3 are written into the plan. Otherwise each route slice reopens the failure-policy and source-feed questions. Slices 3 and 10 as written: likely three rounds.

What would cause a third round: (a) the procfs bridge spike passing on the worker host and failing on the gate host (class 7, a different OS or a container without `/proc` semantics); (b) a reviewer applying the F31 analyzer to the Platform byte response; (c) attempt fencing found at Platform review. Each is a known class; the catalog rule is to stop and re-frame rather than patch.

## 8. Self-attack of this review

- "CLI cannot mint SafeEvents": this rests on F31 plan text (:270, :282), not on built code. If F31 ships a public consumer factory hook, BLOCKER 1 drops to HIGH (still unowned family and writers).
- "OOC re-read": a worker could carry source verdicts inside phase 1 batches. HIGH 3 asks the plan to say so; it does not claim the design is impossible.
- "Attempt fencing": if Platform persists only from the committing attempt under the existing fence, the conflict cannot occur. The plan does not say that, so the finding stands as a specification gap.
- Wrong-citation counts are from `grep` and `wc -l` at baseline `fdd08a8`; line drift on `main` after that is not checked.

## What to cut

- "Rules consulted" paragraph (plan :5) and the external links list (:70-76): keep one line each; workers do not need them.
- "Hostile-review finding closure" table (:320-334): history, not instructions; move it to the review record.
- "Pre-mortem" (:454-465): it repeats the class table.
- Release canary list (:442): belongs in the release handoff, not the build plan.
- The Platform FK from the FK-shape manifest (:268).
- The five sections that carry the load and should stay: Binding design (grammar, counts and verdict, collector table), Code-filled enumerations (routes, exits, fields), Acceptance tests, Implementation slices, Frozen contracts.
