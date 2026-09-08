# OOC capacity preflight: recalibrate the build-floor slope and make it advisory

Status: plan v5 (Opus, 2026-09-08). The measure-first step falsified the premise of
v1-v4 (that the streaming build is row-INDEPENDENT). Measurement shows the build's
memory is row-LINEAR on every path, so the over-rejection is a MISCALIBRATED SLOPE,
not a shape error. This collapses the fix to: recalibrate the slope from measured
data and make the prediction advisory (stop hard-blocking). No shape classifier, no
job-level model. Cam's advisory scope decision stands; only the model detail changed
(row-linear, not flat), which makes the slice smaller. Risk R1: the gate no longer
blocks on the prediction; the slope stays conservative (never under-predicts a
measured completion requirement). Cross-repo: decoy-engine + the decoy CLI. Not
started; build proceeds under the two build-phase gates (dennis, Codex-final); merge
on Cam's go.

## Frame

The preflight prices each parent table's out-of-core relation-build memory with a
row-linear floor (`predict_ooc_build_floor_bytes = 24 MiB + 190 B/row`,
`_memory_estimate.py:327`) and REFUSES the job when the floor exceeds the DuckDB cap
(`_capacity_eval.py:269`). Its value is failing fast with "you need ~N GB" instead
of a mid-run OOM crash; its defect is over-rejection.

Two plan-gate rounds pushed toward a "streaming shape is bounded / row-independent"
model. The mandatory measure-first step (`build_floor_probe`, which drives the real
`_build_relation` that EVERY build entrypoint funnels through, including the
streaming `emit_to_sink` path, with `preserve_insertion_order=False` set) refuted
that premise:

| DuckDB memory_limit | 1M | 5M | 10M | 20M |
|---|---|---|---|---|
| 512 MiB  | ~407 MB | ~818 MB | ~870 MB | OOM (fails inside DuckDB) |
| 1024 MiB | ~413 MB | ~789 MB | ~1330 MB | ~1461 MB (passes) |

Two facts, both row-DEPENDENT:
1. The **memory_limit needed to complete** grows with rows: 10M completes at 512 MiB;
   20M OOMs at 512 MiB and needs ~1024 MiB. Extrapolating (~50 MB per million rows)
   matches the GCP data (33.3M/table completed only at a ~3.3 GB per-instance limit;
   at ~1 GB it OOMed).
2. **Peak RSS** saturates at ~1.4-1.6x the memory_limit once the working set exceeds
   it (10M/512 MiB -> 870 MB = 1.6x; 20M/1024 MiB -> 1461 MB = 1.36x).

The split-dedup DID remove the pre-Phase-4 `arg_max(struct)` O(distinct-key)
RESIDENT blowup (the retired 33.3M cloud OOM anchor). But a real row-dependent floor
remains, shared by all build paths. So the plateau proof's "flat 10M->20M" was
measuring an isolated operator that bypasses the real Python/Arrow staging loop
(Codex flagged this in P2); it is not the full build.

Therefore the over-rejection is NOT "we priced a bounded streaming job as
row-linear." The build IS row-linear on every path. The over-rejection is that the
`190 B/row` slope over-predicts the measured peak (~73 B/row-equiv on this devbox at
saturation, ~114 B/row-equiv on the July cloud run) by roughly 2-4x. The gate refuses
jobs that would complete because its slope is too steep.

## The fix (small, data-matched)

Two changes, keeping the existing structure:

1. **Recalibrate the slope** from `190 B/row` to a measured, still-conservative value.
   The slope must never under-predict a measured COMPLETION requirement (an
   under-prediction in advisory mode would recommend a too-small host that then OOMs),
   so it covers the highest measured peak-per-row across environments (the July cloud
   ~114 B/row) with margin. Target ~130-150 B/row, finalized from the build-step
   repeated measurements (below); `_BUILD_FLOOR_BASE_BYTES` (24 MiB) is unchanged (it
   still must keep tiny-fixture floors under the 32 MB routing-knob cap).
2. **Make the build-floor prediction advisory.** The hard-fail branch
   (`_capacity_eval.py:269-293`) stops returning INSUFFICIENT; it returns FIT with
   `warned=True` and the recommended host size, and the job proceeds. The real OOM
   backstop is caller-side and conditional (the isolated worker's RLIMIT when
   `mem_cap_bytes` is set, `_isolated_worker._run` -> `apply_mem_cap`; the direct
   in-process path is caller-managed via the residency warning). The advisory states
   the expected size up front, so a too-small host stops cleanly (at the RLIMIT) or
   is the caller's declared risk, not a mystery crash.

The **fan-in guards stay HARD** (`out_of_core_fanin_exceeds_budget`, both the
build-phase split and the pure-joiner-leaf guard): exact arithmetic impossibilities,
decidable from the budget alone, that run regardless of host-ceiling detectability.

### Why no shape classifier (dissolving the prior blocker)

Codex's BLOCKER-1 ("`sink is not None` is not the bounded shape") applied to the
FLAT-envelope model, which would have mispriced a resident job with a
cardinality-independent number. v5 has no flat model and no separate bounded model:
one row-linear advisory prices every shape. So there is nothing to misclassify. The
resident path's ADDITIONAL whole-output residency (`outputs[table_name]` accumulated
across the topological loop) stays UNPRICED, exactly as today; the advisory does not
claim to catch it, and the existing residency warning covers that shape. Deferring it
is unchanged behavior, not a new gap.

## Non-goals

- No shape classifier, no `CapacityInputs` change, no job-level topological live-set,
  no leaf-table pricing, no whole-output residency model, no ceiling-resource
  redesign, no DuckDB-cap re-sizing. All deferred; the gate becomes honest advice,
  not a proof.
- No change to the FK drivers, the split-dedup, the route policy, the reserve model,
  or budget resolution.

## Measurement (finalizes the slope)

Repeat the `build_floor_probe` sweep already run (rows {1M,5M,10M,20M} x memory_limit
{512,1024 MiB}, plus finer memory_limit tiers to bracket the "needed to complete"
edge at 5M/10M/20M), REPEATED per point with variance recorded (Codex build
verification). Record the DuckDB `"NNMB"` setting and `actual_duckdb_cap_bytes`
SEPARATELY (never relabel the decimal setting as MiB). Fit the slope as the max
peak-per-row across the swept points AND the retained July cloud ~114 B/row, then add
margin. Confirm the chosen slope's floor exceeds every measured peak at its tested
(rows, memory_limit). The 512/1024 MiB points share the low-thread regime; broader
thread-transition and >100M characterization stay DEFERRED to the separate benchmark.
Record all points in the sprint-testing ledger.

## Design

1. **Recalibrate `_BUILD_FLOOR_BYTES_PER_ROW`** (190 -> measured, ~130-150). Rewrite
   its derivation comment: the current split-dedup build IS row-linear (all paths via
   `_build_relation`); the slope covers the measured peak-per-row with margin; the
   retired 33.3M `arg_max` cloud anchor is recorded as historical (a different,
   removed operator), not a binding point.
2. **Advisory verdict.** Replace the build-floor INSUFFICIENT branch with FIT +
   `warned=True` + the recommended-size message. `enforce_ooc_memory_preflight` then
   raises ONLY on the two fan-in codes (assert this). Redefine the `FIT` enum
   docstring to "no hard impossibility detected"; `warned` marks an adverse
   prediction. Other validation failures outside this estimator (invalid budget,
   missing source, schema) stay hard.
3. **Recommendation number.** Keep `declared_minimum_ceiling_bytes` as the advisory
   "you need ~N GB", now fed by the recalibrated floor. Correct the one factual error
   the gate surfaced: the asymptotic cap/host ratio above the 10 GiB reserve knee is
   `1 / (0.8 * 1e6 / 1_048_576) = 1.31072`, not 1.25. It is advice, so no proven
   minimality is required; describe it as provisional within the measured domain.
4. **Public result contract.** Reuse FIT + `warned` + `needed_bytes` (documented
   meanings hold); document that a formerly-INSUFFICIENT build-floor case is now
   FIT-with-advisory and that `estimate_job_capacity` is a build-feasibility ADVISORY,
   not an OOM guarantee (docstring, CLI help, changelog).
5. **Companion decoy CLI change** (separate repo). `cli/preflight.py:459` maps every
   FIT to a green "within budget" PASS, ignoring `warned`. Change `FIT && warned` to
   render as a WARNING/advisory (recommended size shown) via `add_warn`, honoring
   `--fail-on-warning` for the exit code; default execution proceeds. Re-anchor the
   preflight/runtime parity test (`test_run_preflight_capacity_parity.py`): its
   `test_insufficient_agrees` currently uses a 300k build-floor case, which now
   becomes mutual-advisory; switch that mutual-refusal assertion to a FAN-IN case
   (many incoming edges over a tiny budget) so both sides still hard-refuse, and add
   a case asserting the build-floor 300k case now WARNS (not PASS, not FAIL) on both.
6. **Docstrings**: state the advisory contract, the conditional backstops, and that
   the build is row-linear on all paths.

## Acceptance tests (define behavior before build; no later contributor weakens)

1. **Recalibrated slope never under-predicts a measured peak.** For each measured
   (rows, memory_limit, peak_rss) point in the sweep, `predict_ooc_build_floor_bytes(
   rows) >= measured peak_rss`. The floor stays a conservative upper bound of the
   real build memory.
2. **Slope is meaningfully gentler than 190.** `predict_ooc_build_floor_bytes(20M)` is
   materially below the old `24 MiB + 190*20M`, matching the recalibrated constant
   (the loosening that fixes over-recommendation).
3. **Build-floor prediction no longer blocks.** A case that previously returned
   INSUFFICIENT on the build-floor (e.g. 20M on a 4 GiB host) now returns FIT with
   `warned=True` and a recommended-size message; `enforce_ooc_memory_preflight` does
   NOT raise for it.
4. **Fan-in still refuses, and runs regardless of ceiling.** `(64 MiB, 67 live)`
   admits / `(68 live)` raises; the pure-joiner-leaf guard still raises; with an
   explicit budget and UNKNOWN host ceiling the fan-in guards still run and raise; no
   usable budget -> UNKNOWN.
5. **`enforce` raises only for fan-in codes** (assert the raised code set).
6. **Advisory never claims a guarantee.** `estimate_job_capacity` docstring/result +
   CLI help state build-feasibility advice; a formerly-INSUFFICIENT case is
   FIT-with-advisory; no public field changes meaning; FIT enum redefined.
7. **CLI renders advisory as a warning, not a PASS.** `FIT && warned` prints the
   advisory + recommended size and is NOT a green "within budget" PASS;
   `--fail-on-warning` governs the exit code; default run proceeds. The parity test's
   mutual-refusal case is a FAN-IN impossibility; the 300k build-floor case is
   mutual-advisory on both sides.
8. **Recommendation number sane, units distinct.** `declared_minimum_ceiling_bytes`
   returns a whole GiB covering the advisory floor at the tested points, using the
   corrected `~1.311` cap->ceiling conversion.
9. **Fail-open / NOT_APPLICABLE unchanged.** `budget_bytes is None` -> UNKNOWN; CSV /
   non-OOC route -> NOT_APPLICABLE / UNKNOWN.
10. **evaluate/enforce parity** on the new boundary points (only the build-floor
    case's code flips from INSUFFICIENT to FIT-with-advisory, in both).
11. **Resident whole-output residency stays as today** (unpriced by the estimator;
    the residency warning still fires for the caller-managed shape) -- no regression.

## Steps

1. Finalize the slope from repeated `build_floor_probe` measurements (variance
   recorded; floor >= every measured peak); record in the ledger.
2. Recalibrate `_BUILD_FLOOR_BYTES_PER_ROW` + rewrite its derivation comment
   (row-linear, measured anchors, retired arg_max anchor as historical).
3. Demote the build-floor branch to advisory FIT-with-warning; keep fan-in hard,
   running regardless of ceiling; redefine the FIT enum docstring; correct the
   `~1.311` factor.
4. Companion decoy CLI change: render `FIT && warned` as a warning honoring
   `--fail-on-warning`; update CLI help + changelog; re-anchor the parity test on a
   fan-in impossibility + add the build-floor-warns-both-sides case.
5. Update every engine test asserting build-floor INSUFFICIENT to the advisory
   behavior; add fan-in-runs-regardless, enforce-raises-only-fan-in, no-budget->UNKNOWN.
6. Verify: ruff + format + mypy on every changed file (both repos); full `execution`
   unit suite + OOC parity suites (engine) + CLI preflight/parity suites; mutation
   grade on the recalibrated floor, the advisory-vs-hard-fail branch split, and the
   fan-in guards.
7. dennis gate, then Codex final gate (both repos' diffs). Merge only on Cam's go,
   after green CI. Then run the separate >100M streaming benchmark.

## Risks

- **A gentler slope under-predicts and advises a too-small host.** Central risk;
  mitigated by test 1 (floor >= every measured peak, across devbox + the retained
  cloud anchor) and by keeping margin. In advisory mode a wrong-low recommendation is
  bounded by the caller's RLIMIT where set; the direct path is caller-managed.
- **Advisory lets a job start that then overflows.** Accepted by scope (Cam):
  clean stop at the RLIMIT where set; caller-managed otherwise; the advisory states
  the size up front.
- **CLI drift turning a refusal into a silent PASS.** Mitigated by the companion CLI
  change + the parity-test re-anchor (test 7), landed with the engine change.
- **Fan-in regression.** Mitigated by keeping those guards byte-for-byte hard and
  testing they run even when the ceiling is unknown (tests 4, 5).
