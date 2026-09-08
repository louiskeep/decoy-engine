# OOC capacity preflight: make it advisory and fix the streaming over-rejection

Status: plan v4 (Opus, 2026-09-08). Scope set by Cam after two plan-gate NO-GOs
showed a fully-correct loosening is a multi-slice job-memory model (overbuild for a
self-hosted single-org engine). v3 was NO-GO'd only for a bounded integration gap
(the external CLI renders advisory FIT as a hard PASS) plus wording precision; the
gate stated the plan is GO once those are folded in "without expanding into the
declined job-level model." v4 folds them in. Risk R2 downgraded toward R1: the gate
no longer claims to PREVENT OOM, so a wrong prediction advises rather than blocks.
Cross-repo: touches decoy-engine AND the decoy CLI. Not started; build proceeds
under the two build-phase gates (dennis, Codex-final); merge on Cam's go.

## Frame

The preflight predicts a job's out-of-core relation-build memory and REFUSES the job
up front when the prediction exceeds the DuckDB cap (`enforce_ooc_memory_preflight`
raises INSUFFICIENT, `_capacity_eval.py:269`; the CLI `estimate_job_capacity`,
`capacity.py`). Its value is failing fast with a clear "you need ~N GB" instead of a
mid-run out-of-memory crash. Its defect is over-rejection: the row-linear floor
(`24 MiB + 190 B/row`, fit to a retired pre-Phase-4 cloud OOM) refuses jobs the
current spillable split-dedup builder survives.

Two plan-gate rounds established that a prediction accurate enough to SAFELY admit
those jobs (a hard "predicted RSS <= host ceiling" guarantee) needs a full job-level
memory model: the bounded shape is not `sink is not None` (a resident `pa.Table`
source or a retaining `_CallableSinkAdapter` still materializes -- only
`ParquetTransactionalSink` streams, `_transactional_sink.py:107,172`); the resident
path accumulates every table's whole output in one dict across the topological loop
(`_runner.py:203`) and never prices leaf tables; and host RAM / cgroup-max / process
RSS / RLIMIT are not the same resource. That model is out of proportion to a
single-org tool whose operator controls the box and the data size.

So this slice changes what the gate CLAIMS rather than building the model behind the
claim.

### Backstops (stated honestly, not overstated)

The preflight's hard refusal is NOT the only thing standing between a big job and an
OOM, but the backstops are CONDITIONAL, not universal:

- The isolated worker applies a hard RLIMIT before any allocation
  (`_isolated_worker._run` -> `apply_mem_cap`, verified) and streams through a
  `ParquetTransactionalSink` -- BUT only when the caller passes `mem_cap_bytes`, and
  `run_pipeline_isolated` defaults it to `None` (`_isolated_run.py:131`). So the
  RLIMIT backstop exists only for callers that opt into a cap.
- The direct in-process `run_pipeline` / `decoy run` path has NO RLIMIT containment;
  its residency is declared caller-managed (`_residency_warning.py`) and signalled by
  a post-run contract WARNING, which is not runtime containment.

Therefore the honest contract is: **process OOM remains possible outside capped
isolation.** The advisory does not remove a guarantee that was universally relied on
(the direct path never had RLIMIT containment; it had a pessimistic pre-run refusal
that also over-rejected). It trades a hard, often-wrong pre-run block for an honest
recommendation, and leaves real containment to the caller's RLIMIT where one is set.
This is the deliberate contract change Cam chose; the plan states it plainly rather
than claiming a backstop that is not always present.

## What changes (and what does not)

- **The cardinality PREDICTION becomes advisory.** The build-floor comparison stops
  raising INSUFFICIENT. Where it would have refused, it emits a structured advisory
  (recommended host size) and the job proceeds.
- **The fan-in guards stay HARD refusals.** `out_of_core_fanin_exceeds_budget` (both
  the build-phase split and the pure-joiner-leaf guard) is exact arithmetic -- a
  genuine impossibility, decidable from the budget alone. It stays INSUFFICIENT and
  runs regardless of host-ceiling detectability (Codex: never skip the cap-based
  guards when the ceiling is unknown but a usable budget exists).
- **The streaming shape gets an accurate advisory.** The one real defect is that a
  bounded streaming job is priced with the row-linear floor and told it needs far
  more memory than it does. This slice classifies the bounded streaming shape
  correctly and prices its build row-independently, so the advisory is honest.
- **The resident / unknown shapes stay conservative.** Their advisory keeps the
  existing (pessimistic) estimate; we do not loosen where we cannot cheaply measure.
  We simply no longer BLOCK on it.
- **`FIT` is redefined to "no hard impossibility detected."** `warned` distinguishes
  an adverse prediction from a clean fit. This is the documented enum-semantics
  change the CLI companion change depends on.
- **The decoy CLI renders advisory FIT as a warning, not a PASS** (companion change,
  below). Without this, the engine change would silently turn a former CLI refusal
  into a green PASS.
- **No job-level model, no topological live-set, no ceiling-resource redesign, no
  cap re-sizing.** Deferred as out of scope.

## Design

1. **Classify the execution shape** with a single canonical helper (Codex
   BLOCKER-1 + drift note). A run is `BOUNDED_STREAMING` iff every resolved source is
   a `LazySource`, no `source_loader` is in play, and `type(sink) is
   ParquetTransactionalSink` -- EXACT type equality, not `isinstance` (a subclass can
   override `write_batches` to retain). Otherwise `RESIDENT_OR_UNKNOWN`. Make this
   helper the ONE source of truth shared by both the capacity classifier and the
   existing residency-warning logic (`caller_managed_residency_shapes` in
   `_residency_warning.py`), so the two cannot drift. The runtime caller
   (`_pipeline_route_exec.py`) has the real sources and sink; pass the classified
   shape into the evaluator instead of the `sink is not None` boolean. The CLI
   estimator (`capacity.py`) cannot see the eventual sink, so it classifies
   `RESIDENT_OR_UNKNOWN` (its current conservative stance), now expressed as advice.
2. **Advisory verdict for the prediction.** Replace the build-floor hard-fail branch
   (`_capacity_eval.py:269-293`) with an advisory: compute the recommended host size
   and return FIT with `warned=True` and a recommendation message, not INSUFFICIENT.
   The fan-in guards keep their INSUFFICIENT return. `enforce_ooc_memory_preflight`
   then raises ONLY on the fan-in codes; assert that in a test. Update the FIT enum
   docstring to "no hard impossibility detected"; `warned` marks an adverse
   prediction. Other validation failures outside this estimator (invalid budget,
   missing source, schema/compat) stay hard, unchanged.
3. **Accurate streaming advisory.** For `BOUNDED_STREAMING`, model the measured
   streaming envelope as `predicted_peak_rss(actual_duckdb_cap)` (row-independent),
   set from the light local sweep (below), instead of `24 MiB + 190 B/row`. For
   `RESIDENT_OR_UNKNOWN`, keep the existing estimate.
4. **Recommendation number, with units kept distinct** (Codex MEDIUM). Two separate
   quantities, never conflated: (a) the measured process-RSS envelope
   `predicted_peak_rss(actual_duckdb_cap)`; (b) the arithmetic conversion from a
   DuckDB decimal-byte cap to the binary-byte host ceiling, whose asymptotic ratio
   above the 10 GiB reserve knee is `1 / (0.8 * 1e6 / 1_048_576) = 1.31072` (the
   corrected figure; the old 1.25 was wrong). `declared_minimum_ceiling_bytes` uses
   (b) to turn a target into a whole-GiB "you need ~N GB" recommendation, carried in
   `needed_bytes` + the warning message. Do NOT put process RSS into `floor_bytes` /
   `cap_bytes` (their documented meanings hold). Describe the number as provisional
   within the measured domain, not a memory guarantee. It is advice, so no proven
   minimality is required.
5. **Public result contract** (Codex MEDIUM/HIGH). Reuse FIT + `warned` +
   `needed_bytes`; document the redefined FIT semantics. `estimate_job_capacity` is a
   build-feasibility ADVISORY, not an OOM guarantee -- state it in the docstring, CLI
   help, and changelog.
6. **Companion decoy CLI change** (Codex HIGH; separate repo `decoy`). Today
   `cli/preflight.py` maps every `FIT` to "estimated resident floor is within budget"
   (a PASS), ignoring `warned`. After the engine change, `FIT && warned` must render
   as a WARNING/advisory (recommended size shown), not a PASS; default execution
   proceeds, and the existing `--fail-on-warning` policy decides the exit code. Update
   the preflight/runtime capacity PARITY test (`test_run_preflight_capacity_parity.py`)
   so only hard fan-in impossibilities are expected to produce a capacity refusal on
   both sides. Land the two repos together (engine semantics + CLI rendering).
7. **Docstrings**: state the advisory contract, the conditional backstops, cite the
   split-dedup as why the streaming build is row-independent, record the retired
   33.3M anchor as historical.

## Measurement (light; enough for an honest advisory number)

The advisory number must be reasonable, not provably tight, so a small local sweep
suffices; the large-scale characterization is the separate >100M benchmark AFTER this
slice. Use `scripts/fk_memory_probe.py --mode out_of_core` (streaming path,
`ParquetTransactionalSink`) and VmHWM from `/proc/self/status`.

- rows in {1M, 5M, 10M, 20M} at two cap tiers (512 MiB, 1024 MiB), REPEATED runs per
  point, recording variance (Codex build-verification). Confirm peak RSS is flat
  across rows at a fixed cap and scales with the cap; fit
  `predicted_peak_rss(actual_duckdb_cap)`. The 100k smoke point (281 MB at a 512 MiB
  limit) is one anchor. Record the DuckDB `"NNMB"` SETTING and the derived
  `actual_duckdb_cap_bytes` SEPARATELY (Codex nit). Round the envelope UP if noisy
  (advice errs toward recommending slightly more). NOTE: 512/1024 MiB share the
  low-thread regime; broader thread-transition and >100M characterization are
  EXPLICITLY DEFERRED to the benchmark, not claimed here.
- One resident-path point (no sink) at a mid row count to confirm the resident
  advisory stays pessimistic (not loosened).

Record the points in the sprint-testing ledger.

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Build-floor prediction no longer blocks.** A case that previously returned
   INSUFFICIENT on the build-floor (e.g. 20M rows on a 4 GiB host) now returns FIT
   with `warned=True` and a recommended-size message; `enforce_ooc_memory_preflight`
   does NOT raise for it.
2. **Fan-in still refuses, and runs regardless of ceiling.** `(64 MiB, 67 live)`
   admits / `(68 live)` raises `out_of_core_fanin_exceeds_budget`; the
   pure-joiner-leaf guard still raises. With an explicit budget and an UNKNOWN host
   ceiling AND/OR unpriceable rows, the fan-in guards still run and still raise. With
   NO usable budget at all, the result is UNKNOWN.
3. **`enforce` raises only for fan-in codes.** Assert the shim's raised codes are
   exactly the two fan-in codes; the build-floor case never raises.
4. **Streaming shape classified correctly, via the canonical helper.** All-`LazySource`
   + no `source_loader` + `type(sink) is ParquetTransactionalSink` -> `BOUNDED_STREAMING`;
   a resident `pa.Table` source, a `source_loader`, a `_CallableSinkAdapter`, a
   `ParquetTransactionalSink` SUBCLASS, or no sink -> `RESIDENT_OR_UNKNOWN`. The same
   helper drives the residency warning (one classifier, no drift).
5. **Streaming advisory is accurate and row-independent.** For `BOUNDED_STREAMING`,
   the recommended size derives from `predicted_peak_rss(actual_duckdb_cap)` and is
   flat across {1M, 10M, 20M, 100M} rows at a fixed cap (kills `190 * rows` for the
   streaming advisory; a 100M point tests row-independence of the FORMULA only, not
   empirical 100M accuracy).
6. **Resident advisory stays conservative** (>= current estimate, not loosened).
7. **Advisory never claims a guarantee.** `estimate_job_capacity` docstring/result and
   CLI help state build-feasibility advice; a formerly-INSUFFICIENT prediction case is
   FIT-with-advisory; no public field changes meaning; FIT enum redefined.
8. **CLI renders advisory as a warning, not a PASS** (decoy repo). `FIT && warned`
   prints the advisory + recommended size and is NOT a green "within budget" PASS;
   `--fail-on-warning` governs the exit code; default run proceeds. The preflight/
   runtime parity test expects capacity refusal ONLY for hard fan-in impossibilities.
9. **Recommendation number sane, units distinct.** `declared_minimum_ceiling_bytes`
   returns a whole GiB covering the advisory envelope at the tested points, using the
   corrected `~1.311` cap->ceiling conversion; the measured RSS envelope and the
   conversion are separate quantities.
10. **Fail-open / NOT_APPLICABLE unchanged.** `budget_bytes is None` -> UNKNOWN; CSV /
    non-OOC route -> NOT_APPLICABLE / UNKNOWN.
11. **evaluate/enforce parity** on the new boundary points (only the build-floor
    case's code flips from INSUFFICIENT to FIT-with-advisory, in both).

## Steps

1. Light local streaming sweep (repeated runs, variance recorded) to set the envelope
   + confirm the resident advisory stays conservative; record points (setting AND
   actual cap separately) in the ledger.
2. Add the canonical execution-shape classifier; share it with the residency-warning
   logic; thread the classified shape from `_pipeline_route_exec.py` into the
   evaluator; CLI estimator classifies `RESIDENT_OR_UNKNOWN`.
3. Demote the build-floor branch to advisory FIT-with-warning; keep fan-in guards as
   hard INSUFFICIENT running regardless of ceiling detectability; price the streaming
   shape row-independently; correct + separate the recommendation units; redefine the
   FIT enum docstring.
4. Companion decoy CLI change: render `FIT && warned` as a warning honoring
   `--fail-on-warning`; update CLI help + changelog; update the preflight/runtime
   parity test. (Two repos, landed together.)
5. Update every engine test asserting build-floor INSUFFICIENT to the advisory
   behavior; add the shape-classification, fan-in-runs-regardless, no-budget->UNKNOWN,
   and enforce-raises-only-fan-in cases.
6. Verify: ruff + format + mypy on every changed file (both repos); full `execution`
   unit suite + OOC parity suites (engine) + CLI preflight/parity suites; mutation
   grade on the classifier, the advisory-vs-hard-fail branch split, and the streaming
   envelope.
7. dennis gate, then Codex final gate (both repos' diffs). Merge only on Cam's go,
   after green CI. Then run the separate >100M streaming benchmark.

## Risks

- **Advisory lets a job start that then overflows.** Accepted and bounded by scope:
  where the caller sets an RLIMIT (isolated worker with `mem_cap_bytes`), it stops
  cleanly at the cap; the direct in-process path never had containment and is
  caller-managed. The advisory states the expected size up front, so this is a clean,
  explained outcome, not a mystery crash. Deliberate contract change (Cam).
- **CLI drift turning a refusal into a silent PASS.** The single most important
  integration risk; mitigated by the companion CLI change + the parity test update
  (test 8) landed with the engine change.
- **Misclassifying a retaining shape as bounded** -> too-low advisory. Mitigated by
  exact-type classification + the canonical shared helper + test 4; and it is only
  advice with the caller's RLIMIT as containment where set.
- **Streaming envelope constant off** only shifts the advisory; rounding up keeps it
  conservative; the >100M benchmark refines it.
- **Fan-in regression.** Mitigated by keeping those guards byte-for-byte hard and
  testing they run even when the ceiling is unknown (tests 2, 3).
