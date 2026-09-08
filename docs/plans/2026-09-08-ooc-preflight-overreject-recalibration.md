# OOC capacity preflight: make it advisory and fix the streaming over-rejection

Status: plan v3 (Opus, 2026-09-08). Scope set by Cam after two plan-gate NO-GOs
showed that a fully-correct loosening of the preflight is a multi-slice job-memory
model (topological live-set, execution-shape classification, an authoritative
memory-envelope with provenance, dual predicates, fail-closed UNKNOWN, API
migration) -- overbuild for a self-hosted single-org engine. Cam's call: demote the
cardinality PREDICTION from a hard refusal to an ADVISORY recommendation, and fix
the one genuine defect (the bounded streaming shape is wrongly over-rejected).
Risk R2 downgraded toward R1 by this scope: the gate no longer claims to PREVENT
OOM, so a wrong prediction advises rather than blocks or admits-then-crashes. Not
started. No build until Codex-plan-gated.

## Frame

The preflight predicts a job's out-of-core relation-build memory and REFUSES the
job up front when the prediction exceeds the DuckDB cap (`enforce_ooc_memory_preflight`
raises INSUFFICIENT, `_capacity_eval.py:269`; the CLI `estimate_job_capacity`,
`capacity.py`). Its value is failing fast with a clear "you need ~N GB" instead of a
mid-run out-of-memory crash. Its defect is over-rejection: the row-linear floor
(`24 MiB + 190 B/row`, fit to a retired pre-Phase-4 cloud OOM) refuses jobs the
current spillable split-dedup builder survives.

Two plan-gate rounds established that a prediction accurate enough to SAFELY admit
those jobs (a hard "predicted RSS <= host ceiling" guarantee) needs a full
job-level memory model: the bounded shape is not `sink is not None` (a resident
`pa.Table` source or a retaining `_CallableSinkAdapter` still materializes -- only
`ParquetTransactionalSink` streams, `_transactional_sink.py:107,172`); the resident
path accumulates every table's whole output in one dict across the topological loop
(`_runner.py:203`) and never prices leaf tables; and host RAM / cgroup-max / process
RSS / RLIMIT are not the same resource. That model is out of proportion to a
single-org tool whose operator controls the box and the data size.

So this slice changes what the gate CLAIMS rather than building the model behind the
claim:

- The real OOM backstop already exists on the production path: the isolated worker
  applies a hard RLIMIT before any allocation (`_isolated_worker._run` ->
  `apply_mem_cap`, verified) and runs through a streaming `ParquetTransactionalSink`.
  A job that truly overflows stops cleanly at that cap. For the direct in-process
  `run_pipeline` caller (no isolation), residency is already declared
  caller-managed (`_residency_warning.py`), warned not policed.
- Therefore the preflight does not need to BE the guarantee. It becomes an advisory
  build-feasibility check: a structured recommendation ("this looks like it needs
  ~N GB; on a smaller host it will stop at the memory cap"), not a hard block.

## What changes (and what does not)

- **The cardinality PREDICTION becomes advisory.** The build-floor comparison stops
  raising INSUFFICIENT. Where it would have refused, it emits a structured advisory
  (the existing warn channel) with a recommended host size, and the job proceeds.
- **The fan-in guards stay HARD refusals.** `out_of_core_fanin_exceeds_budget`
  (both the build-phase split and the pure-joiner-leaf guard) is exact arithmetic --
  N co-live DuckDB instances cannot each hold DuckDB's 1 MB minimum under the budget.
  That is a genuine impossibility, not a soft prediction, so it stays INSUFFICIENT.
  It is decidable from the budget alone (no host-ceiling needed), so it runs
  regardless of ceiling detectability (Codex HIGH: never skip the cap-based guards).
- **The streaming shape gets an accurate advisory.** The one real defect is that a
  bounded streaming job is priced with the row-linear floor and told it needs far
  more memory than it does (or refused, pre-advisory). This slice classifies the
  bounded streaming shape correctly and prices its build row-independently, so the
  advisory number is honest and does not cry wolf on every large streaming job.
- **The resident / unknown shapes stay conservative.** Their advisory keeps the
  existing (pessimistic) estimate; we do not loosen where we cannot cheaply measure.
  We simply no longer BLOCK on it.
- **No job-level model, no topological live-set, no ceiling-resource redesign, no
  cap re-sizing.** Deferred as out of scope; the gate is honest advice, not a proof.

## Design

1. **Classify the execution shape** with a small helper (Codex BLOCKER-1). A run is
   `BOUNDED_STREAMING` iff every resolved source is a `LazySource`, no `source_loader`
   is in play, and the sink is a `ParquetTransactionalSink` (the one proven
   non-retaining sink; a `_CallableSinkAdapter` or missing sink is not bounded).
   Otherwise `RESIDENT_OR_UNKNOWN`. The runtime caller
   (`_pipeline_route_exec.py`) has the real sources and sink; pass the classified
   shape into the evaluator instead of the `sink is not None` boolean. The CLI
   estimator (`capacity.py`) cannot see the eventual sink, so it classifies
   `RESIDENT_OR_UNKNOWN` (its current conservative `sink=False` stance), now
   expressed as advice, not a block.
2. **Advisory verdict for the prediction.** Replace the build-floor hard-fail branch
   (`_capacity_eval.py:269-293`) with an advisory: compute the same recommended host
   size and return FIT with a structured warning (`warned=True`, a recommendation
   message), not INSUFFICIENT. The fan-in guards above keep their INSUFFICIENT return.
   `enforce_ooc_memory_preflight` then raises only on the fan-in codes; the build-floor
   prediction never raises. Update its docstring: the shim enforces impossibilities,
   advises on predictions.
3. **Accurate streaming advisory.** For `BOUNDED_STREAMING`, price the build with a
   cap-relative envelope `envelope_factor * cap + C_fixed` (row-independent), set from
   a light local measurement (below), instead of `24 MiB + 190 B/row`. The recommended
   host size for a streaming job is derived from that envelope. For
   `RESIDENT_OR_UNKNOWN`, keep the existing estimate for the advisory number.
4. **Recommendation number** (`declared_minimum_ceiling_bytes`). Keep it as the "you
   need ~N GB" advisory figure. Correct the one factual error the gate surfaced: the
   asymptotic cap/host ratio above the 10 GiB reserve knee is `0.8 * 1e6 / 1_048_576
   = 0.7629`, so the envelope factor that still fits asymptotically is `~1.311`, not
   1.25 (Codex HIGH). Because the number is now advice, it need not be a proven
   minimum; a sane, slightly-conservative recommendation is sufficient. Note this in
   the docstring.
5. **Public result contract** (Codex MEDIUM). Do not silently repurpose
   `CapacityEstimate.floor_bytes` / `cap_bytes` meanings. The advisory reuses the
   existing FIT + `warned` + `needed_bytes` fields (their documented meanings still
   hold: `needed_bytes` = recommended ceiling, `warned` = advisory present). Document
   that a former-INSUFFICIENT build-floor case is now FIT-with-advisory, and that
   `estimate_job_capacity` is a build-feasibility ADVISORY, not an OOM guarantee.
6. **Docstrings**: state the advisory contract, name the RLIMIT + residency-warning
   backstops, cite the split-dedup as why the streaming build is row-independent,
   record the retired 33.3M anchor as historical.

## Measurement (light; enough for an honest advisory number)

The advisory number must be reasonable, not provably tight, so a small local sweep
suffices; the large-scale characterization is the separate >100M benchmark that runs
AFTER this slice. Use `scripts/fk_memory_probe.py --mode out_of_core` (streaming
path, `ParquetTransactionalSink`) and VmHWM from `/proc/self/status`.

- Sweep rows in {1M, 5M, 10M, 20M} at a couple of memory_limit tiers (e.g. 512 MiB,
  1024 MiB) to confirm peak RSS is flat across rows at a fixed cap and scales with the
  cap, and to fit `envelope_factor * cap + C_fixed`. The 100k smoke point (281 MB at a
  512 MiB limit) is one anchor already. Record the DuckDB `"NNMB"` setting and the
  derived `actual_duckdb_cap_bytes` SEPARATELY (Codex nit: never relabel the decimal
  setting as MiB). A handful of points is enough for an advisory constant; if the fit
  is noisy, round the factor UP (advice errs toward recommending slightly more).
- One resident-path point (no sink) at a mid row count to confirm the resident
  advisory stays in the pessimistic direction (we are not loosening it).

Record the points in the sprint-testing ledger.

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Build-floor prediction no longer blocks.** A case that previously returned
   INSUFFICIENT on the build-floor (e.g. 20M rows on a 4 GiB host) now returns FIT
   with `warned=True` and a recommended-size message. `enforce_ooc_memory_preflight`
   does NOT raise for it.
2. **Fan-in still refuses, and runs regardless of ceiling.** `(64 MiB, 67 live)`
   admits / `(68 live)` raises `out_of_core_fanin_exceeds_budget`; the pure-joiner-leaf
   guard still raises. With an explicit budget and an UNKNOWN host ceiling, the fan-in
   guards still run and still raise (not skipped).
3. **Streaming shape is classified correctly.** All-`LazySource` sources + no
   `source_loader` + `ParquetTransactionalSink` -> `BOUNDED_STREAMING`. A resident
   `pa.Table` source, a `source_loader`, a `_CallableSinkAdapter`, or no sink ->
   `RESIDENT_OR_UNKNOWN`. (`sink is not None` alone must NOT classify as bounded.)
4. **Streaming advisory is accurate and row-independent.** For `BOUNDED_STREAMING`,
   the recommended size is derived from the cap-relative envelope and is flat across
   {1M, 10M, 20M, 100M} rows at a fixed cap -- it does not grow with rows, matching
   the measured fit within tolerance. Directly kills the `190 * rows` shape for the
   streaming advisory.
5. **Resident advisory stays conservative.** For `RESIDENT_OR_UNKNOWN`, the advisory
   number is at least the current estimate (not loosened).
6. **Advisory never claims a guarantee.** `estimate_job_capacity`'s contract/docstring
   and result state build-feasibility advice; a formerly-INSUFFICIENT prediction case
   is FIT-with-advisory. No public field changes meaning.
7. **Recommendation number is sane.** `declared_minimum_ceiling_bytes` returns a whole
   GiB that comfortably covers the (advisory) envelope at the tested points; the
   corrected ~1.311 asymptotic factor is used where relevant. (No minimality proof
   required, since it is advice.)
8. **Fail-open / NOT_APPLICABLE unchanged.** `budget_bytes is None` -> UNKNOWN; CSV /
   non-OOC route -> NOT_APPLICABLE / UNKNOWN, as today.
9. **evaluate/enforce parity** on the new boundary points (both return the same code
   and message; only the build-floor case's code flips from INSUFFICIENT to
   FIT-with-advisory, in both).

## Steps

1. Light local streaming sweep to set the envelope constant + confirm the resident
   advisory stays conservative; record points (setting AND actual cap separately).
2. Add the execution-shape classifier; thread the classified shape from
   `_pipeline_route_exec.py` into the evaluator; the CLI estimator classifies
   `RESIDENT_OR_UNKNOWN`.
3. Demote the build-floor branch to an advisory FIT-with-warning; keep the fan-in
   guards as hard INSUFFICIENT running regardless of ceiling detectability; price the
   streaming shape row-independently; correct the recommendation factor.
4. Update tests to the advisory behavior; add the shape-classification and
   fan-in-runs-regardless cases.
5. Verify: ruff + format + mypy on every changed file; full `execution` unit suite +
   the OOC parity suites; mutation grade on the classifier, the advisory-vs-hard-fail
   branch split, and the streaming envelope.
6. dennis gate, then Codex final gate. Merge only on Cam's go, after green CI. Then
   run the separate >100M streaming benchmark.

## Risks

- **Advisory lets a job start that then overflows.** Accepted and bounded by scope:
  the isolated/production path stops it cleanly at the RLIMIT; the direct in-process
  path is already caller-managed (residency warning). The advisory message tells the
  user the expected size up front, so this is a clean, explained stop, not a mystery
  crash. This is the deliberate contract change Cam chose.
- **Misclassifying a retaining shape as bounded** would give a too-low advisory.
  Mitigated by the strict classifier (exact `ParquetTransactionalSink` + all-lazy
  sources + no loader) and test 3; and because it is only advice with the RLIMIT
  backstop, a wrong advisory does not itself cause an admitted-then-OOM guarantee
  break.
- **Streaming envelope constant off** only shifts the advisory number; rounding the
  factor up keeps advice conservative. The separate >100M benchmark will refine it if
  needed.
- **Fan-in regression.** Mitigated by keeping those guards byte-for-byte as hard
  fails and testing they run even when the ceiling is unknown.
