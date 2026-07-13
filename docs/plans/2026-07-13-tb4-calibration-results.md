# TB-4 results: MEASURED k_path calibration for the OOM-avoidance estimator

- **Status:** COMPLETE (2026-07-13). Two OOM-unsafe placeholders found and fixed.
- **Sprint:** TB-4 of `docs/plans/2026-07-12-track-b-completion-program.md`.
- **Design:** `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` §13 / §13.1
  (the placeholder constants this replaces) + §3.4 / B5 (telemetry).
- **Harness:** `scripts/tb4_calibration.py` (manual/gated, not default-CI).
- **Acceptance test:** `tests/unit/execution/test_mem_calibration.py`.
- **Spend:** ZERO. Entirely on the devbox using Sprint 1a process isolation.

TB-4 replaces the unmeasured cold-start `k_path` multipliers in
`_mem_estimate.py` with values measured under real process isolation, so the
estimator's predicted peak matches observed peak in the direction that makes
routing trustworthy (a prerequisite for TB-5 flag enablement).

## Method

Each route runs in a fresh `run_pipeline_isolated` child (Sprint 1a), so
`peak_rss_mb` is that job's own attributable VmHWM, not a contaminated
process-wide `ru_maxrss`. For every (schema class, route) the harness takes
TWO row scales and fits a two-point SLOPE

    k = (peak_high - peak_low) / (basis_high - basis_low)

on the SAME basis each route divides by (full_frame / out_of_core:
`raw_data_bytes`; sequential: the two-largest-tables working set). The slope
CANCELS the fixed interpreter / pyarrow / DuckDB intercept the way `_probe.py`
fits it out; a single-point peak/basis ratio over-reads at small N.

Three schema classes span the range §13 says drives k in opposite directions:
pooled-string FK (`raw_data_bytes` over-prices pooled cells -> low k, the
safe-to-over-predict direction), numeric int64 FK + single-table (no pooling
-> high k, the worst case a conservative constant must cover), and
unique-string single-table (the middle case).

## Reference host and scale

- **Host:** devbox, pve2 LXC, 8 GB RAM, Linux 6.17.2-1-pve, Python 3.10, repo
  `.venv` (pyarrow 24.0.0, duckdb 1.5.4).
- **Scales (per shape, two points):** pooled_fk 400k/800k rows, numeric_fk
  500k/1M, numeric_single 1M/2M, unique_single 1M/2M. All full_frame peaks stay
  well under the 8 GB box.
- **out_of_core budget:** 2048 MB.

## Per-point measurements (isolated VmHWM)

`basis_mb` is the estimator basis for that route; `point_k = peak / basis`
(intercept-inflated at small N, which is why the two-point slope below is the
pinned quantity).

| shape | route | rows | basis MB | peak MB | point_k |
| --- | --- | --- | --- | --- | --- |
| pooled_fk | full_frame | 400,000 | 301.8 | 831.1 | 2.7538 |
| pooled_fk | out_of_core | 400,000 | 301.8 | 475.9 | 1.5768 |
| pooled_fk | sequential | 400,000 | 301.8 | 709.8 | 2.3518 |
| pooled_fk | full_frame | 800,000 | 603.9 | 1467.2 | 2.4294 |
| pooled_fk | out_of_core | 800,000 | 603.9 | 592.8 | 0.9816 |
| pooled_fk | sequential | 800,000 | 603.9 | 1208.7 | 2.0014 |
| numeric_fk | full_frame | 500,000 | 182.8 | 818.3 | 4.4768 |
| numeric_fk | out_of_core | 500,000 | 182.8 | 599.5 | 3.2798 |
| numeric_fk | sequential | 500,000 | 182.8 | 762.6 | 4.1721 |
| numeric_fk | full_frame | 1,000,000 | 365.9 | 1449.5 | 3.9615 |
| numeric_fk | out_of_core | 1,000,000 | 365.9 | 773.4 | 2.1137 |
| numeric_fk | sequential | 1,000,000 | 365.9 | 1362.6 | 3.7240 |
| numeric_single | full_frame | 1,000,000 | 152.6 | 652.5 | 4.2762 |
| numeric_single | full_frame | 2,000,000 | 305.2 | 1114.7 | 3.6526 |
| unique_single | full_frame | 1,000,000 | 69.6 | 360.1 | 5.1725 |
| unique_single | full_frame | 2,000,000 | 139.2 | 529.1 | 3.8000 |

(The acceptance test `test_mem_calibration.py` embeds the exact basis/peak
BYTES for every row above; this table is the human-readable MB rounding.)

## Two-point slope k (intercept-free) and the re-pin

| route | pooled | numeric | unique | MAX slope | old placeholder | NEW pinned |
| --- | --- | --- | --- | --- | --- | --- |
| `full_frame` | 2.11 | **3.45** | 2.43 | **3.45** | 3.0 | **4.0** |
| `out_of_core` | 0.39 | **0.95** | n/a | **0.95** | 2.0 | **1.5** |
| `sequential` | 1.65 | **3.28** | n/a | **3.28** | 1.5 | **4.0** |

- **Model check (STOP-and-report clause):** peak is LINEAR in bytes (point_k
  converges to the slope as rows grow; a consistent positive intercept). The
  `basis * k` model's shape holds; the estimator's model is not wrong.
- **Two placeholders were OOM-UNSAFE.** `full_frame` 3.0 and `sequential` 1.5
  both sat BELOW the measured numeric-FK worst case (3.45 / 3.28), the
  direction that silently admits a job that then OOMs. Re-pinned at max-slope
  rounded up: `K_FULL_FRAME_COLD_START = 4.0` (+16%), `K_SEQUENTIAL_COLD_START
  = 4.0` (+22%), both within `K_CALIBRATION_ERROR_BAND` above their slope.
- **`out_of_core` is confirmed budget-bounded.** Measured slope 0.95 (< 1.0):
  peak grows SUB-linearly with raw bytes because chunks are RAM-capped.
  `K_OUT_OF_CORE_COLD_START = 1.5` (tightened from the unmeasured 2.0; slope
  0.95 + intercept coverage for selected-job sizes, staying > 1.0). A
  through-origin `basis * k` under-predicts SMALL out_of_core jobs (a ~426 MB
  fixed intercept), which is expected and safe: the runtime budget + governor
  (TB-1/TB-2/TB-3) bound out_of_core's peak, not this estimate. The estimate's
  only job for out_of_core is keeping the fallback usable for large jobs.
- **`K_CALIBRATION_ERROR_BAND = 0.30`** (also `fits`'s default asymmetric
  margin) covers run-to-run variance (a few %, matching TB-3's ~+/-90 MB) plus
  headroom for unsampled shapes.

## Recalibration trigger (documented in `_mem_estimate.py`)

1. **Drift** -- B5 telemetry (`_mem_telemetry.recalibrate_k`) sees an isolated
   job whose `observed_k` exceeds `current_k` -> RAISE immediately (safety), or
   the max `observed_k` over >= `min_samples_for_lower` isolated jobs falls
   below `current_k / (1 + error_band)` -> LOWER (gated).
2. **Dependency / route change** -- a pyarrow / DuckDB / pandas major bump or a
   route buffering change invalidates the measured slope + intercept.
3. **New schema class** -- a production shape unlike pooled / numeric / unique
   (wide-binary, deeply nested).

## Telemetry (B5): what is emitted

The predicted-vs-observed pair is already emitted:
`GovernorTripRecord.(route, budget_bytes, observed_peak_mb)` and
`IsolatedRunResult.peak_rss_mb`, foldable into a `MemoryTelemetryRecord` via
`telemetry_record_from_isolated_run` / `telemetry_record_from_governor_trip`,
with `recalibrate_k` as the drift detector. The persistent STORE + auto-adoption
loop stays platform-owned (§4); the engine ships the record type + the
in-memory `MemoryTelemetryStore` primitive.

## Guardrails honored

- **No default flag flipped.** This changes estimate VALUES only; the
  byte-estimate routing / governor flags stay default-OFF (they flip at TB-5).
- **Golden test-flight unchanged:** 53/53 invariants + fingerprint 5/5 match
  golden -- calibration constants do not affect output determinism.
- **Not a default-CI test.** The harness is a manual/gated measurement run;
  reproduce with `.venv/bin/python scripts/tb4_calibration.py`
  (`--smoke` for a tiny plumbing check).
