# Job-time estimator: a shared advisory `estimate_job` primitive (PLAN)

Status: PLAN (FRAME + PLAN; not built). Head-start plan for the review/merge agents — independent
plan review required before DEVELOP. Risk: **R2** (new public engine primitive shared by CLI +
platform; adds a calibrated runtime model; advisory, never a gate).

Rules consulted: 00-universal, development-loop, risk-and-exceptions, architecture, api-and-compatibility,
performance, testing, verification, reuse-first, scope-discipline, observability-and-resilience.

Implements the roadmap "Job-time estimator" direction (ROADMAP.md:79-81, 130-133) under the §CAP
lock (ROADMAP.md:83-84, Cam 2026-10-22): capacity/time is **ADVISORY — warn, never gate**; jobs
fail naturally on memory/disk.

---

## Goal / definition of done

A pure engine primitive that predicts, WITHOUT running a job, its route, peak memory, and
**runtime**, as a confidence band — so the CLI (pre-run) and the platform (Job Studio preview /
job create) can show an advisory "this will take ~X and need ~Y RAM" line:

```
estimate_job(config_dump, base_dir, *, budget_bytes=None) -> JobEstimate
JobEstimate = { route, peak_bytes, runtime_seconds: Band, confidence, warnings, unpriceable? }
```

DONE when: `estimate_job` is public in `decoy_engine/__init__.py`, returns a banded
runtime+peak+route for the common routes, never raises/gates (advisory), its runtime band is
validated against held-out benchmark points, and the CLI (`decoy preflight`) + platform
(`estimate_job_admission` / `preview_iteration`) surface it as a warning only.

---

## Current state (from the 2026-10-22 survey)

Memory estimation already exists and is reusable; **time does not exist anywhere**.

- **Public, advisory, memory-only:** `decoy_engine.estimate_job_capacity(config_dump, base_dir, *,
  budget_bytes=None) -> CapacityEstimate` (`execution/capacity.py`) — OOC-FK memory feasibility,
  build-floor warns (does not refuse), already consumed by `decoy preflight`.
- **Pure peak-bytes model:** `execution/_mem_estimate.py` (`estimate_peak_bytes`, `raw_data_bytes`,
  `estimator_basis_bytes`, `fits`) with calibrated per-route slopes/intercepts (TB-4), priced from
  schema / profile / **Parquet footer** / row counts (`_mem_estimate_schema.py`). OOC predictors
  `predict_ooc_build_floor_bytes`, `predict_ooc_disk_bytes`.
- **Route decision:** `decide_execution_route` (`_pipeline_routing.py`); per-table native preflight
  `plan_native_route` (`execution/native/_dispatch.py`, currently PRIVATE — §NATIVE-PLAN-AT-CLAIM).
- **No duration/throughput/rows-per-second predictor exists** in the engine. `instrumentation/
  timing.py` only measures elapsed time (observability), it does not predict. This is the gap.
- Platform prices every streaming job with the oracle-inclusive `2 * 57` bytes/cell working set
  (`api/jobs/native_budget.py`) because `plan_native_route` is private (§NATIVE-PLAN-AT-CLAIM).

---

## Flaws / gaps this closes

1. **No time dimension.** Users and Job Studio get no "how long will this take" signal; only memory.
2. **Two half-answers, no single primitive.** Memory lives in `estimate_job_capacity` (OOC-FK) +
   `_mem_estimate`; there is no one call returning {route, peak, runtime} that CLI and platform
   share, so each surface stitches its own partial view.
3. (Related, optional) **§NATIVE-PLAN-AT-CLAIM:** the platform over-prices native jobs with the
   pandas oracle term because the native-route decision is not exported.

---

## Guarantee pinned + non-goals

**Guarantees:** a single public `estimate_job` that returns a **confidence-banded** {route,
peak_bytes, runtime_seconds, confidence}, reusing the existing calibrated memory model and adding a
benchmark-calibrated throughput→runtime layer; **advisory only** — it never raises to gate a job,
never changes routing, and returns an explicit `unpriceable`/low-confidence result rather than a
false-precise number.

**Non-goals (recorded follow-ups, not blockers):**
- Not a gate or admission control (§CAP: warn only). Routing is unchanged.
- Not a per-operator exact-millisecond model — v1 is per-route throughput with a band; rare
  operators/shapes return a wider band or `low` confidence.
- Not a new benchmark campaign — v1 calibrates from the EXISTING Phase D/E/F bench records; if a
  common route has no bench point, it returns low confidence, not a guess.
- The §NATIVE-PLAN-AT-CLAIM native-route export is a SEPARATE slice (Slice 4b), not required for the
  CLI/preview runtime estimate.

---

## Established solutions (researched: codebase + prior art)

- **Reuse before building (reuse-first):** the peak-memory half is already solved and calibrated —
  `estimate_job` wraps `estimate_peak_bytes` / `estimate_job_capacity` / `decide_execution_route`
  rather than re-deriving memory. Only the time layer is new.
- **Runtime model:** `runtime ≈ rows / throughput(route, dominant-operator-class, shape)`, a simple
  linear throughput model per route — the same shape the memory estimator already uses
  (`intercept + bytes*slope`). Calibrated from measured rows/s in the Phase D/E/F bench records
  (e.g. the 100M run at ~40k rows/s, the native thread-sweep ~3.0M rows/s at 100M, the F4a
  windowed_date ~14k rows/s floor). This matches the roadmap's "rows x RAM x route/operator/shape
  -> runtime + peak memory … a confidence band not a false-precise number" (ROADMAP.md:130-133).
- **Confidence band, not point estimate:** mirrors the memory estimator's `K_CALIBRATION_ERROR_BAND
  = 0.30` asymmetric margin; the runtime result is a band with a `confidence` tag, so advisory text
  reads "~8-13 min" not "11m02s". Prior art: the memory estimator's `fits(..., error_band=0.30)`.
- **Schema + Parquet-footer pricing** (per §RUST-PATH-ESTIMATOR, ROADMAP.md:1025-1030): price from
  schema and footer metadata (row counts, byte sizes), never by reading resident data — so the
  estimate is cheap and runs in `decoy preflight` / preview with zero execution cost. `capacity.py`
  already runs the router with `use_byte_estimate_routing` OFF for exactly this reason; reuse it.

No new heavy machinery, scheduler, or statistical method is introduced; the time layer is a
calibrated linear model over existing route/shape facts.

---

## Design notes (architecture)

- **New structure = one estimator seam + one calibration table.** `estimate_job` lives next to
  `estimate_job_capacity` in `execution/capacity.py` (or a sibling `execution/_job_estimate.py` if
  capacity.py nears its LOC ceiling), and a single `_throughput_calibration.py` holds the per-route
  rows/s constants + their source bench refs — one place for "how fast is route R", read by the
  estimator and the test. (Change-pain: a new route adds one calibration row + one route arm, not a
  scattered set of magic numbers.)
- **Typed result over a tuple:** `JobEstimate` (route, peak_bytes, runtime band, confidence,
  warnings, unpriceable) — a frozen dataclass exported beside `CapacityEstimate`.
- **One place per fact:** memory from `_mem_estimate`/`capacity`; route from `decide_execution_route`;
  throughput from `_throughput_calibration`. The estimator composes; it does not restate.
- House style: pure functions (no I/O beyond the footer read the memory estimator already does),
  no behavior-switching flags, confidence as data not exceptions.

---

## Slices (build order; each independently gateable)

### Slice 1 — throughput calibration table (data + validation, no public API)
Extract measured rows/s per common route (full_frame, out_of_core/bounded, sequential; native vs
pandas) from the Phase D/E/F bench records into `_throughput_calibration.py` as constants with
their source refs, plus a held-out validation test asserting the model predicts each recorded run
within the confidence band. **Open input:** the bench JSON/records to calibrate from (the survey
located runs but not a single machine-readable corpus — confirm the source or capture a small one).

### Slice 2 — `estimate_job` primitive + public export
STATUS: **BUILT 2026-10-23** (`claude/job-time-estimator`). New `execution/_job_estimate.py`:
public `estimate_job(config, base_dir) -> JobEstimate{route, priceable, peak_bytes, runtime,
notes}`, exported from `decoy_engine` (+ `execution`). Composes the route
(`decide_execution_route`, row-count path — the byte-estimate promotion is left to capacity.py by
design), peak (`estimate_peak_bytes`), and the Slice-1 runtime band. Advisory: it never raises (an
error or unpriceable case returns a flagged result). Pre-run string pricing is honestly UNPRICEABLE
(no resident sample), matching the engine's own byte-estimate router. The prelude is mirrored (not
shared) because capacity.py early-returns for non-relational jobs before compiling. Added both new
modules to the physical-seam byte-identical sentry's permitted list (fixing a Slice-1 omission) and
to the public-API `__all__` test. 12 estimator tests + the seam sentry + public-API test green;
ruff + mypy clean.

Compose route (`decide_execution_route`, router OFF) + peak bytes (`estimate_peak_bytes` /
`estimate_job_capacity`) + runtime (Slice 1) into `JobEstimate`; export from `__init__.py`.
Advisory: unpriceable columns / missing calibration → `unpriceable` or `low` confidence, never a
raise. Acceptance: banded {route, peak, runtime} for common shapes; never raises; byte-identical
memory numbers to `estimate_job_capacity` on its covered cases.

### Slice 3 — CLI surface (`decoy preflight`)
`decoy/src/decoy/cli/preflight.py:_check_capacity` additionally renders the runtime band + route
beside the existing capacity line; advisory only (no new non-zero exit — EXIT_CAPACITY stays the
hard fan-in refusal only). Acceptance: `decoy preflight` prints "~X-Y, route R, peak ~Z"; a
low-confidence estimate prints a hedge, not a number.

### Slice 4 — platform surface (create + Job Studio preview)
Surface the advisory estimate at `api/jobs/_create_phases.py:estimate_job_admission` (record on the
Job row) and `api/pipelines/router.py:preview_iteration` (warning in the preview payload). Warn
only (§CAP). Acceptance: the estimate appears in the preview response + Job row; no gating.

### Slice 4b — (OPTIONAL, separate) export native-route decision → §NATIVE-PLAN-AT-CLAIM
Export the per-table native-admission decision so the platform can drop the `2 * 57`/cell oracle
term for natively-admitted tables and lower reservations. Engine, then platform. Own gate; not
required for the runtime estimate.

---

## Verification / acceptance (program-level)

- Slice 1's held-out calibration test is the core evidence the runtime band is honest (predict each
  recorded bench run within band; a run outside band fails the test).
- `estimate_job` is pure + advisory: a property test that it NEVER raises on any config the
  memory estimator accepts, and returns `unpriceable`/`low` rather than a number when inputs are
  thin.
- Test-strength measurement (testing.md) on the new estimator + calibration units.
- DOCUMENT: move the roadmap "Job-time estimator" item to the shipped log as slices land; note the
  §NATIVE-PLAN-AT-CLAIM resolution if Slice 4b ships.

## Open questions (owner / plan-review)
1. **Calibration source:** is there a machine-readable D/E/F bench corpus to calibrate from, or
   should Slice 1 capture a small dedicated throughput bench? (Determines Slice 1 size.)
2. **v1 route scope:** common routes first (full_frame / bounded-OOC / sequential) with low
   confidence elsewhere — confirm that's acceptable vs. waiting for full coverage.
3. **Bundle §NATIVE-PLAN-AT-CLAIM (Slice 4b) now or defer** to its own effort.
