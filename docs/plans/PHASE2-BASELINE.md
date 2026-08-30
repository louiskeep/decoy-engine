Status: record

# Phase 2 performance baseline (Task 2.0): pandas full-frame oracle on W2

This is the measured pandas oracle baseline for the Phase 2 native-masking hot path, captured
BEFORE any Rust is written (Task 2.0, moved ahead of Task 2.1 per the Codex plan-gate). It freezes
the numeric targets in the plan's §2.3 (targets 4 and 5). The correctness, intended-route, and
kernel criteria (targets 1, 2, 3) are not derived from measurement and stay hard.

## Environment

- Host: single-org devbox, 12 GiB RAM, 2 GiB swap. Engine venv `/home/cam/vscode/decoy-engine/.venv`.
- Captured 2026-08-29. Engine at `feat/native-phase1` off engine main.
- Harness (committed for reproducibility): `scripts/native-baseline/bench_driver.py` (spawns one
  fresh process per rep, samples the child's `VmHWM` externally) and `scripts/native-baseline/bench_worker.py`
  (builds W2, times `run_pipeline` only, reports a per-strategy hash-time breakdown). Raw results:
  `scripts/native-baseline/results_1x.json`, `results_4x.json`.

## Workload (W2, as measured)

One mask table, 10 columns, fixed 32-byte `mask_key`, fixed seed, `post_validation=False`:

- 3 keyed-`hash` columns (the dominant cost): `h_email` (utf8), `h_token` (utf8), `h_uid` (int64),
  each with its own namespace.
- 3 `passthrough`: `pt_amount` (int64), `pt_flag` (bool), `pt_ts` (timestamp-with-tz, `us`, UTC).
- 2 `redact`: `rd_ssn`, `rd_notes` (utf8).
- 2 `truncate`: `tr_phone` (length 3, head), `tr_card` (length 4, tail) (utf8).

Route: the pinned pandas full-frame oracle, `substrate="pandas"`, `execution_mode="full_frame"`,
`auto_chunk=False`. Types span utf8 / int64 / bool / timestamp-with-tz across the frame.

## Method

Per §2.2: 1 discarded warmup + timed reps in fresh processes per tier; wall time reported as
median / IQR / p95-of-reps; peak RSS measured externally (parent samples the child's `VmHWM`);
keyed-hash throughput is per-hash-column rows/sec from the worker's in-process strategy timing.

## Measured results

| Tier | Rows | Reps | Wall median | Wall IQR | Wall p95 | Peak RSS | Hash throughput (per hash col) | Whole-job throughput |
|---|---|---|---|---|---|---|---|---|
| 1x | 1,000,000 | 5 | 50.7 s | 0.67 s | 51.4 s | 1,617 MB | 70,730 rows/s | 19,709 rows/s |
| 4x | 4,000,000 | 5 | 177.3 s | 0.21 s | 179.5 s | 4,937 MB | 81,701 rows/s | 22,563 rows/s |

Variance is tight (4x IQR 0.21 s over 5 fresh-process reps). No spill observed (full-frame in RAM).

## 16x: not run to completion, extrapolated (the headline finding)

The 16x (16,000,000 rows) full-frame run was **stopped deliberately, not measured to completion.**
Peak RSS is linear in rows (fit: `~1,107 MB per 1M rows + 510 MB`, 4x/1x RSS ratio 3.05), so the
16x full-frame peak extrapolates to **~18.2 GB, which exceeds the 12 GiB box.** A real 16x
full-frame run OOMs or thrashes swap on this host; that is itself the concrete case for the Phase 2
native/streaming work, and it is why target 5 caps native peak RSS at the flat Phase 1 streaming
ceiling rather than the full-frame curve. Wall time is slightly sublinear (fit `~42.2 s per 1M rows
+ 8.6 s`, 4x/1x ratio 3.49 as fixed overhead amortizes), extrapolating to a **~683 s** oracle 16x
wall. Both 16x figures are extrapolations from the measured 1x/4x fit, labeled as such; the full
native-vs-oracle 16x comparison is measured at the Task 2.7 perf gate on hardware that can hold the
oracle side (or with the oracle streamed), not required here.

## Frozen targets (fill §2.3 targets 4 and 5)

Oracle references, frozen from the measurements above:

- Oracle keyed-hash throughput: **81,701 rows/s per hash column** (the stable 4x measurement).
- Oracle 16x wall: **~683 s** (extrapolated from the 1x/4x linear fit).
- Full-frame peak RSS at 16x: **~18.2 GB** (extrapolated; exceeds the box).
- Phase 1 streaming peak-RSS ceiling: **6.5 GiB** (from the 2026-08-29 streaming qualification).

Frozen gate numbers:

- **Target 4 (wall + hash).** Native-route keyed-hash throughput **>= 163,403 rows/s per hash
  column** (2.0x the 81,701 oracle). Native 16x end-to-end wall **<= 410 s** (0.60x the ~683 s
  extrapolated oracle). Non-regression floor: native wall **<= oracle wall at every tier** (<= 50.7 s
  at 1x, <= 177.3 s at 4x).
- **Target 5 (peak RSS).** Native-route peak RSS **<= 6.5 GiB (6,656 MB)** at 1x, 4x, and 16x, and
  flat (must not scale with row count: native peak RSS at 4x and 16x <= 1.5x its 1x value). This is
  the Phase 1 streaming ceiling, well under the ~18.2 GB the full-frame oracle would need at 16x.
  Per-batch Rust transient SCRATCH allocation (peak outstanding minus the returned output buffer)
  **<= 2x the input batch's Arrow byte size**, measured via a peak-tracking `#[global_allocator]`
  over a frozen 4,096-row-per-type fixture (the output is excluded because the derived ~64-byte
  hash strings legitimately exceed a narrow int64/bool input).

## Caveats

- 16x is extrapolated, not measured (the oracle exceeds the box); the fit is from two points
  (1x, 4x) with tight variance, and RSS/wall linearity held (ratios 3.05 / 3.49 vs the 4.0 a
  perfectly linear 4x would give, consistent with a fixed startup overhead).
- The keyed-hash columns in W2 are utf8 and int64; bool and timestamp-with-tz appear as passthrough
  inputs, so the pandas hash cost here reflects utf8/int64 sources. The Rust canonicalizer's full
  type surface (bool, tz-timestamp as hash inputs) is covered by the Task 2.2 KAT vector fixture,
  not by this throughput baseline.
- The worker sets a benign flag to avoid an engine size-estimator crash on tz-timestamp columns
  under the oracle path; it does not affect the masking cost being measured. Flagged as a minor
  pre-existing engine rough edge, not a Phase 2 blocker.
- Whole-job throughput (~20-22k rows/s) is lower than per-hash-column throughput because the job
  runs 3 hash columns plus 7 other columns and the CSV/profile boundary; the gate uses the
  per-hash-column figure for the hash-specific 2.0x target and end-to-end wall for the 0.60x target.
