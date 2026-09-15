# Unified-slice benchmark (Task 4.5 D9): DEFERRED, not yet statistically certified

Status: **deferred**. The large-tier statistical performance claim for the Task
4.5 unified-slice lane is **not measured or certified yet**. This directory holds
only the frozen workload substrate; the statistical comparison harness is owed as
a follow-up.

## What is here now

- `bench_worker_unified.py` -- the frozen workload substrate: one process that
  masks the fixed nine-column W2-minus-`pt_ts` shape (3 keyed-hash, 2
  passthrough, 2 redact, 2 truncate) through `run_pipeline`, selected between the
  legacy route (`UNIFIED_BENCH_FLAG=off`) and the unified lane (`on`) by an
  environment flag, and prints one `BENCH_JSON` record (per-strategy timing,
  `out_rows`, `unified_slice_activated`, `workload_fingerprint`). It is
  smoke-tested end-to-end by `tests/physical/test_bench_unified_slice_harness_smoke.py`.

## What is DEFERRED (the follow-up: FOLLOWUP-BENCH-D9)

The statistical comparison driver (`bench_compare.py`) was removed: a
half-implemented version can print a false "D9 PASSED", which is worse than not
shipping it. Build it properly as a separate task, **required before Task 4.6
caller activation** (a default-off engine lane can merge without it; activating
the lane for a real caller cannot).

The rebuilt harness MUST honor every D9 requirement below (do not weaken any):

- **Tiers:** 10k, 100k, 1M rows. **Warmups:** >= 3 discarded. **Reps:** >= 20 timed.
- **Per-repetition pairing/alternation:** alternate the off (legacy) and on
  (unified) arms at the REPETITION level, not whole-arm sweeps, so a short host
  load spike cannot land entirely inside one arm.
- **Identical workload, enforced:** both arms run this same worker/source; assert
  the `workload_fingerprint` matches between arms AND that every rep's `out_rows`
  equals the requested `n_rows` (a worker that silently masks the wrong row count
  must fail, not pass).
- **Activation, enforced:** the `on` arm must actually activate the unified slice
  (`unified_slice_activated == True`); a silent legacy fallback must fail.
- **Thresholds:** 100k & 1M median new/old wall <= 1.10 and p95 <= 1.15; 10k median
  regression <= max(10%, 50 ms); peak RSS <= 1.10x at every tier.
- **Fail-closed RSS:** a missing peak-RSS sample (per rep OR aggregate) is a
  FAILURE -- the RSS bound cannot be certified without the evidence, so the harness
  must never silently drop it and report PASS.
- **Statistics:** report median, p95, and a bootstrap CI on the new/old ratio; the
  CI upper bound must also sit within the applicable threshold.
- **Flag-off-overhead-vs-main (unresolved):** the "flag-off overhead vs main <=
  1%" cross-revision check is NOT yet solved -- measuring today's flag-off arm
  against a baseline on pre-task `main` is version-skewed (that revision predates
  the `unified_slice_enabled` kwarg). It is unmeasured residual risk with strong
  structural evidence (flag-off returns at the flag check before importing any
  `execution.physical` code, proven by `tests/physical/test_unified_slice_inertness.py`),
  so the added cost is a bounded constant, not per-row work. A version-compatible
  baseline recorder is part of this follow-up.

## Separate follow-up: FOLLOWUP-BENCH-DRIVER-HARDEN

`scripts/native-baseline/bench_driver.py` is a SHARED driver used by other perf
programs (native-baseline). Task 4.5 reverted its changes rather than modify
shared tooling unilaterally. Harden it with its own consumers in scope: None-safe
tier-summary formatting (a `None` `hash_tput` must not crash the summary line) and
fail-closed per-rep RSS aggregation.
