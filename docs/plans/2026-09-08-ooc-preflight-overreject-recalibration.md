# OOC capacity preflight: separate streaming vs resident, gate peak RSS on the host ceiling

Status: plan v2 (Opus, 2026-09-08). Fresh REDO per Codex's OOC-fix verdict; v1
was NO-GO'd at the plan gate for comparing incompatible quantities (process RSS
vs a DuckDB connection cap) and for treating the split-dedup plateau as license to
drop the row term on every path. v2 selects one coherent model, separates the two
execution shapes, and carries the host ceiling explicitly. Risk R2 (a refusal gate
loosens; admitting a job that then OOMs is the failure to prevent, so the
never-under-predict bound is the load-bearing acceptance criterion). Not started.
No build until this plan is Codex-plan-gated.

## Frame

The out-of-core route runs a memory PREFLIGHT before either FK driver
(`enforce_ooc_memory_preflight`, `_pipeline_route_exec.py:389`; and the CLI
`estimate_job_capacity`, `capacity.py`). It prices each parent table's
relation-build phase with a strictly row-linear floor and refuses when that floor
exceeds the DuckDB cap the build would receive:

```
predict_ooc_build_floor_bytes(rows) = 24 MiB + 190 B/row       (_memory_estimate.py:327)
refuse when floor(rows) > actual_duckdb_cap_bytes(budget, live) (_capacity_eval.py:269)
```

The 190 B/row slope was fit to one binding anchor, a **real-route cloud OOM at
33.3M rows** (GCP n2-standard-8; build OOMed at memory_limit ~1638 MB, completed at
2457 MB). That anchor measured the **pre-Phase-4 co-resident dedup**
(`arg_max(struct_pack(...))` / window form) that held **O(distinct-key) resident
state** (`_relation.py:470-508`). Phase 4 replaced it with a **spillable
split-dedup** (`_relation.py:466-538`): COPY keys to `staged.parquet`,
`max(row_nr) GROUP BY key` to `winners.parquet` (aggregate state external), then an
external hash join. The winners land on disk between the aggregate and the join, so
the O(distinct-key) resident state the slope prices no longer exists in the build.

But v1 concluded from this that the row term is spurious on every path. It is not,
because the preflight serves **two execution shapes** with different memory laws,
and the current model conflates them.

### Two execution shapes, two memory laws

The OOC route's cardinality-bounded memory guarantee holds ONLY for the structural
bounded shape (`_residency_warning.py`): every source a `LazySource` AND a sink
that consumes `write_batches` incrementally (`ParquetTransactionalSink`). Two
shapes result:

- **Streaming (sink present).** `emit_to_sink` writes bounded batches straight to
  the sink; nothing whole-table is retained (`_runner.py:440-457`). Peak RSS is
  bounded with respect to row cardinality. This is the shape the isolated worker
  always sets up (`_isolated_worker.py:225`, always hands a
  `ParquetTransactionalSink`) and the shape the >100M benchmark exercises. The
  100k smoke measurement confirms the law: at 100k rows / 512 MiB limit, peak RSS
  is 281 MB, driven by the DuckDB memory_limit, not the row count.
- **Resident (sink=None).** `_runner.py:458-465` does `batches = list(rewritten())`
  then `outputs[table_name] = table_out`, materializing every table's whole masked
  output in memory, holding the source, keeping joiners open through the build.
  Peak RSS is genuinely O(rows). The engine declares this residency
  **caller-managed** (`_residency_warning.py`): it warns
  (`RESIDENCY_WARNING_CODE`), does not police it. The direct `run_pipeline` /
  `decoy run` CLI takes this path (no sink flag), which is exactly why
  `capacity.py:416-419` models the estimator as `sink=False`.

The runtime preflight already passes the real shape (`sink=sink is not None`,
`_pipeline_route_exec.py:392`). The CLI estimator hardcodes `sink=False`
(`capacity.py:445`) because the direct-library CLI is resident.

### What the model actually gets wrong

- The build-floor's ROW TERM (`190 * rows`) prices O(distinct-key) BUILD residency
  that Phase 4 removed. On BOTH shapes the split-dedup build is spillable, so the
  build-phase row term is spurious on both.
- On the STREAMING shape, once the build row term is removed, nothing else is
  row-dependent: the true quantity is peak RSS as a function of the DuckDB cap
  (`E(cap) ~= envelope_factor*cap + C_fixed`), cardinality-independent. The gate
  over-rejects here: it refuses streaming jobs the bounded builder survives.
- On the RESIDENT shape, whole-output + source residency is a real O(rows) cost the
  build-floor never modeled (the 190 B/row line was a crude, wrong-magnitude proxy
  for it at best). Dropping the row term with no replacement would ADMIT resident
  jobs that then OOM. This is the central hazard v1 missed.
- The comparison itself is incoherent (v1 P0): peak RSS is a PROCESS quantity;
  `actual_duckdb_cap_bytes` is one connection's cap. Comparing them refuses every
  priced job at the 1.6x envelope. The correct gate compares predicted peak RSS
  against the **process/host ceiling**, carried explicitly.

## Goal and the coherent model

Gate each table on **predicted peak process RSS vs the host process ceiling**, per
execution shape:

```
STREAMING:  peak_rss ~= envelope_factor(cap) * cap + C_fixed        # cardinality-independent
RESIDENT:   peak_rss ~= resident_base + per_row_resident * rows      # genuine O(rows), whole-output + source
gate:       admit iff predicted_peak_rss <= process_ceiling_bytes    # NOT the DuckDB connection cap
```

`cap` is `actual_duckdb_cap_bytes(budget, live)` (kept, decimal-correct). The
`process_ceiling_bytes` is the host/cgroup ceiling that budget resolution already
knows (`resolve_ooc_memory_limit` starts from it before subtracting the reserve),
threaded into `CapacityInputs` so the evaluator can gate RSS against it. Both
`envelope_factor(cap)`, `C_fixed`, `resident_base`, and `per_row_resident` come
from measurement (section "Measurement"), not a guess. `envelope_factor(cap)` is a
FUNCTION of the cap (piecewise if the sweep steps at DuckDB thread transitions),
not a single constant.

### Safety property (the acceptance bar)

Define it precisely, per Codex: under a frozen environment (allocator, DuckDB
version, thread policy), the model **bounds every measured peak RSS AND refuses
every measured failing boundary**. A completion peak alone is not a required-memory
boundary, so:
- Never claim FIT for a point outside the measured/validated domain. Outside it,
  return UNKNOWN (fail-open to the caller's judgment, as the route already does for
  undetectable budgets) or restrict the supported calibration domain.
- The streaming 100M/8 GiB admission requires a real capped full-route run at that
  scale (GCP), not a shape extrapolation.
- Where a genuine fail/pass boundary of the current builder can be measured, pin
  the refusal to it, with repeated runs and a one-sided conservative margin.

## Non-goals

- No change to the gate's typed-error surface, the two fan-in guards
  (`out_of_core_fanin_exceeds_budget`, both the build-phase and pure-joiner-leaf
  splits), the decimal `actual_duckdb_cap_bytes`, or the
  evaluate_capacity/enforce parity. Those gate on the cap and stay.
- No change to the two FK drivers, the split-dedup, the route policy
  (`decide_route`, 2M reorder threshold), the reserve model, or budget resolution.
- No new preflight coverage of the reorder route's separate streaming-phase budgets
  (`resolve_reorder_budgets`/`ReorderCaps`). BUT (Codex P2): the preflight result
  is a job-level verdict, so if measurement finds the reorder streaming phase can
  OOM outside the modeled build phase, that admission must be BLOCKED or the verdict
  narrowed to a build-feasibility claim, not silently deferred.
- No implementation of DuckDB-cap re-sizing (spill harder instead of refusing) in
  this slice. It is noted as a design option and a likely follow-up (see Design 5);
  this slice makes the gate CORRECT first.

## Measurement (measure-first; every constant comes from data)

All RSS via VmHWM from `/proc/self/status` in an allocator-pinned fresh subprocess
(the plateau-proof harness; never `ru_maxrss`). Record the DuckDB `"NNMB"` SETTING
and the derived `actual_duckdb_cap_bytes` SEPARATELY in every row (Codex nit: never
relabel the decimal setting as MiB; this repo has shipped a unit-conflation defect).

Harnesses (extend, do not rewrite):
- `scripts/build_floor_probe.py` -- isolates the relation-build floor under a fixed
  RLIMIT_DATA. Sweep `--rows` x `--memory-limit-mib`.
- `scripts/fk_memory_probe.py` -- the full real-route peak-RSS harness. EXTEND it
  (Codex P1-2) to pass `budget_bytes` AND a temp-disk budget to `run_fk_out_of_core`
  so the reorder route can actually select (both are required,
  `_route_policy.py:121`), and to REPORT per run: the selected internal route
  (batch-join vs reorder), the DuckDB setting + `actual_duckdb_cap_bytes`, the
  thread count, the process ceiling, and the sink/resident shape. Distinguish
  build-only VmHWM from full-route VmHWM.

Matrix:
1. **Streaming envelope vs cap, across thread transitions.** rows in {1M, 5M, 10M,
   20M} x memory_limit spanning the full supported production range and BOTH sides
   of DuckDB's thread-count steps (threads rise ~every 2 GiB of limit,
   `_duckdb.py:62`), including the real host-derived caps for 4/8/10 GiB hosts
   (sink build ~= budget; 8 GiB host -> ~6 GiB cap, a multi-threaded regime the
   v1 2048 MiB ceiling never reached). Fit `envelope_factor(cap)` PIECEWISE if it
   steps or curves; the factor may RISE with parallel operator buffers, so do not
   assume the 350 MiB 1.6x point generalizes. Record whether each point completes.
2. **Streaming row-independence.** At a FIXED cap in each thread regime, confirm
   peak RSS is flat across the row sweep. Any residual slope, if real, is measured
   and priced.
3. **Resident-shape model.** Run the RESIDENT path (fk_memory_probe with no sink /
   `--mode full` as the resident analogue, plus a resident OOC run) across the row
   sweep and payload widths; fit `resident_base + per_row_resident * rows` covering
   whole-output + source residency. This is the model the CLI `sink=False`
   estimator needs; it is genuinely O(rows).
4. **Both internal routes.** With budgets passed so reorder selects (>2M deduped
   keys), measure batch-join AND reorder build phases separately; confirm both share
   the streaming build envelope, and check the reorder STREAMING phase for any
   unmodeled OOM (Non-goals bullet 3).
5. **Corpus breadth** (Codex P2): long/variable-width keys, composite keys, all-
   distinct vs duplicate-heavy vs skewed distributions, nulls, wide masked payloads.
   Repetitions per point; record maxima/tails, not a single fitted line.
6. **Measured refusal boundaries.** Reproduce the enshrined refusal points that are
   locally tractable (20M streaming; 9.2M/fan-in-1 RESIDENT; 300k/64 MiB) and record
   pass/fail. These measured outcomes REPLACE the hard-coded expectations. Where a
   current-builder OOM can be provoked (small host, high fan-in, resident wide
   payload), capture it as the refusal anchor.
7. **Large-scale GCP (authorized, Cam 2026-09-08).** On the STREAMING path (the only
   shape where >100M is feasible): confirm the streaming envelope at large caps and
   benchmark Phase-4 efficiency at 100M AND beyond (target the largest tractable
   scale within the bench budget). This both validates the 100M streaming admission
   (Codex P0-2 requires a real capped run) and delivers the >100M efficiency
   benchmark. Ping Slack before the run per standing rule; the bench budget (50 runs)
   is pre-authorized.

Retire the stale 33.3M cloud anchor as a binding point (it measured a builder that
no longer exists) but RECORD it in the docstring as historical with the reason it no
longer binds.

## Design

1. **Carry the ceiling and provenance in `CapacityInputs`** (Codex P1-3). Add
   `process_ceiling_bytes: int | None`, the resolved DuckDB budget, the reserve, and
   an explicit-vs-auto `budget_provenance`. `resolve_ooc_memory_limit` already knows
   the host ceiling before subtracting the reserve; thread it through both callers
   (`capacity.py`, `_pipeline_route_exec.py`). For an EXPLICIT budget with no known
   ceiling, RSS gating is undecidable -> return UNKNOWN (do not invent a ceiling).
2. **Streaming model + gate.** `predicted_peak_streaming(cap) = envelope_factor(cap)
   * cap + C_fixed`; admit iff `<= process_ceiling_bytes`. If the resolved cap makes
   the envelope exceed the ceiling but a smaller cap would fit (spillable job), the
   message RECOMMENDS the reduced cap rather than only refusing (advisory; actual
   re-sizing is Design 5 / follow-up). Refuse only when even the minimum viable cap's
   envelope exceeds the ceiling, or a fan-in guard fires.
3. **Resident model + gate.** `predicted_peak_resident(rows, width) = resident_base +
   per_row_resident * rows` (from Measurement 3); admit iff `<= process_ceiling`.
   This keeps the genuine O(rows) refusal the CLI path needs. If `process_ceiling` is
   unknown (explicit budget), return UNKNOWN and rely on the existing residency
   warning. Do NOT claim job-level FIT on the resident path without modeling
   residency.
4. **Replace `predict_ooc_build_floor_bytes` and re-home the logic.** The evaluator
   already holds the cap and (now) the ceiling at `_capacity_eval.py:223`; compute
   the shape-appropriate predicted peak there. Reduce `predict_ooc_build_floor_bytes`
   to the fixed build-phase minimum (its near-zero / tiny-fixture role) or fold it
   in, whichever keeps `_memory_estimate.py` / `_capacity_eval.py` under the ~600 LOC
   cap (`test_module_size.py`); extract a small helper module if needed.
5. **Re-derive `declared_minimum_ceiling_bytes` against the real predicate** (Codex
   P1-4). The inverse must now answer "smallest whole-GiB HOST CEILING whose resolved
   cap yields `predicted_peak(cap) <= ceiling`", proving monotonicity (or handling
   the no-solution case) and MINIMALITY (the returned GiB passes, the preceding GiB
   fails). Cover the 10 GiB reserve transition (below 10 GiB reserve is a flat 2 GiB;
   at/above it is 20%, so an asymptotic streaming envelope only fits if its factor
   stays under 1.25 -- if measurement shows a higher factor at large caps, the
   recommendation must reduce the cap or the inverse must report no whole-GiB host
   fits at that cap policy), decimal-MB rounding, fan-in, and `_MIN_BUDGET_BYTES`
   clamping. Rebase the warn band on the SAME quantity as the hard-fail comparison.
6. **Docstrings**: state the two-shape model, cite the split-dedup as why the build
   row term is gone, cite `_residency_warning` for why the resident shape keeps one,
   record the retired 33.3M anchor as historical, and cite the new measured anchors.

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Streaming row-independence (replaces the row-linear pin).** At a fixed cap, the
   streaming predicted peak is flat across {300k, 1M, 10M, 20M, 100M} rows. Kills
   `base + 190*rows` for the streaming shape. Pure-function test.
2. **Streaming envelope tracks the cap, piecewise.** Predicted peak matches the
   measured `envelope_factor(cap)*cap + C_fixed` fit within tolerance across the
   swept cap tiers, including a thread-transition step if measurement shows one.
   Constants/piecewise breakpoints named, with measured anchors in docstrings.
3. **Resident model is O(rows) and matches measurement.** Predicted resident peak
   grows with rows per the measured `resident_base + per_row_resident*rows` fit; a
   large resident job on a small ceiling is REFUSED, with a measured OOM witness.
4. **Gate compares RSS to the ceiling, not the cap.** A constructed case proves the
   comparison uses `process_ceiling_bytes`; feeding a DuckDB cap where the old code
   would refuse but the ceiling admits (streaming) now ADMITS.
5. **Never under-predicts a measured peak; refuses every measured failing boundary.**
   For each measured (shape, rows, cap, peak) point, predicted >= peak; for each
   measured OOM/failing boundary, verdict INSUFFICIENT. One-sided margin.
6. **The streaming over-rejections are admitted, each with a completion witness.**
   20M streaming and (via GCP) 100M+ streaming ADMIT, each with a real capped-run
   witness. No point flips to FIT without one. 300k/64 MiB: resolved per the
   measured shape (admit if the measured builder completes it; the estimator models
   it resident, so gate on the resident model + ceiling).
7. **Outside the validated domain -> UNKNOWN, never FIT.** A point beyond the swept
   cap range or with an unknown ceiling returns UNKNOWN, not FIT.
8. **Explicit budget without a ceiling -> UNKNOWN.** RSS gating is undecidable there;
   assert UNKNOWN rather than a fabricated ceiling.
9. **Both fan-in guards unchanged.** `(64 MiB, 67 live)` admit / `(68 live)` raise and
   the pure-joiner-leaf guard hold verbatim (cap-based, not RSS-based).
10. **Hard-fail precedes any DuckDB run**; **warn band fires and never blocks**, both
    rebased on the predicted-peak-vs-ceiling quantity.
11. **Declared-minimum inverse: correctness, monotonicity, minimality.** The returned
    whole-GiB ceiling makes `predicted_peak(resolved_cap) <= ceiling` while the
    preceding GiB fails; covers the 10 GiB reserve transition, decimal rounding,
    fan-in, min-budget clamp, and the no-solution case.
12. **Evaluator/enforcer parity** on the new boundary points.
13. **Both internal routes measured** (opt-in/benchmark perf test): batch-join and
    reorder build phases both stay within the streaming envelope at a cardinality
    above the 2M threshold; the reorder streaming phase shows no unmodeled OOM (or
    the verdict is narrowed).
14. **Fail-open on undetectable budget unchanged** (`budget_bytes is None` ->
    UNKNOWN; CSV/non-OOC -> NOT_APPLICABLE/UNKNOWN).

## Steps

1. Extend the harnesses (budgets + route/thread/ceiling/shape reporting); run the
   full matrix incl. resident-shape and both routes; run the GCP large-scale +
   >100M streaming benchmark (Slack ping first). Record every point (setting AND
   actual cap separately) in the sprint-testing ledger and the constants' docstrings.
   This GATES the constants.
2. Add ceiling+provenance to `CapacityInputs`; thread through both callers.
3. Implement the two-shape predicted-peak model + ceiling gate; reduce/replace
   `predict_ooc_build_floor_bytes`; re-derive `declared_minimum_ceiling_bytes`
   against the real predicate.
4. Rewrite the enshrined expectations to the measured outcomes; add the UNKNOWN-
   outside-domain and explicit-budget-without-ceiling cases.
5. Verify: ruff + format + mypy on every changed file; full `execution` unit suite +
   OOC perf suite + parity suites; mutation grade on the new model logic (both
   shapes, ceiling comparison, warn/hard-fail, inverse monotonicity/minimality,
   never-under boundary).
6. dennis gate, then Codex final gate. Merge only on Cam's go, after green CI.

## Risks

- **Loosening admits a real OOM (central risk).** Mitigated by test 5 (bound every
  measured peak; refuse every measured failing boundary) and the UNKNOWN-outside-
  domain rule (test 7): the model never claims FIT where it has no evidence.
- **Envelope factor rises at large multi-threaded caps.** Mitigated by sweeping both
  sides of the thread transitions to the max production cap and fitting piecewise
  (Measurement 1); if the factor exceeds the reserve's headroom above 10 GiB, the
  inverse reports it rather than silently admitting.
- **Resident model under-fit.** Mitigated by measuring the resident path directly
  (Measurement 3) with payload-width and distribution breadth, not by reusing the
  streaming numbers.
- **Reorder streaming-phase OOM outside the modeled build phase.** Measured
  (Measurement 4); if present, the admission is blocked or the verdict narrowed, not
  deferred.
- **Module LOC caps.** Extract a helper module if the two-shape logic pushes
  `_memory_estimate.py` / `_capacity_eval.py` over ~600 LOC.
- **Scope.** The gate is made CORRECT here; DuckDB-cap re-sizing (spill instead of
  refuse) is deliberately deferred to keep this slice bounded for a single-org,
  ~100M-row engine.
