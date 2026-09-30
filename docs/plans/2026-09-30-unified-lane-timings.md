# Unified lane per-column timings (Rust engine program A6)

Status: plan

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase A (record gap P6). Evidence: `docs/records/2026-09-30-rust-coverage-evidence-audit.md`, runs R029, R030, R082. Branch `fix/unified-lane-timings`, off engine main `8dc559e5`.

## Problem

The unified slice (the Rust lane) returns `ExecutionResult(timings=(), boundary_conversion_ms=0.0)` (`execution/_unified_slice.py` ~369-372). The platform computes a job's execute time as the sum of `timings` (`api/jobs/v2_full_frame.py:83-91`), so every Rust job reports 0 ms (R029, R082), while the same job on the pandas route reports real time (R030: 49.7 ms). The engine #180 strict xfail `test_admitted_job_reports_per_column_timings` documents the gap.

## Design

Use the timing instrumentation the pandas route already uses (`instrumentation/timing.py`: `TimingCollector`, `use_collector`, `timed_strategy`, `StrategyTimingRecord`), so the Rust lane's records have the same shape and labels.

- In the unified lane's admitted execution path, create one `TimingCollector` and run the coordinator under `use_collector(collector)`.
- In the coordinator's per-node loop (`physical/_shadow_coordinator.py`, the `for node in table.nodes` loop), wrap each mask node's operator execution in `timed_strategy(strategy_type, column)`. `strategy_type` is the node's configured strategy name (`hash`, `redact`, `categorical`, ...), the same label the pandas route records, not the internal operator id (`native_keyed_hash`). Map from the plan's node to its strategy name; if the node does not carry it, add it to the binding at compile time rather than reverse-mapping operator ids.
- `timed_strategy` is a no-op when no collector is active, so the shadow coordinator's other callers (shadow parity tests, the chunked shadow driver) keep zero overhead and unchanged behavior.
- Return `timings=tuple(collector.records)` from the lane.
- `boundary_conversion_ms`: time the Arrow-to-pandas and pandas-to-Arrow conversions the lane performs around the resident source (`source_frame`) and output assembly with `time.perf_counter`, matching what the pandas adapter reports as boundary conversion (`_pandas_adapter.py` ~332). If the lane performs no such conversion, report the measured value (possibly near 0) rather than a hardcoded 0.0, and say so in a comment.

Timings stay outside the D9 parity contract (plan `docs/plans/2026-09-20-unified-slice-activation.md` ~101): they are measurements, not outputs.

## Acceptance tests (written first)

1. Remove the strict xfail marker on `test_admitted_job_reports_per_column_timings`; it passes: an admitted CSV job with a hash column and a redact column returns timings keyed `{("hash","id"), ("redact","name")}`.
2. For the same admitted job, the set of `(strategy_type, column)` keys in `timings` equals the pandas route's set for the same config (`unified_slice_enabled=False`), across the audit's native operators: hash, categorical (deterministic), bucket_perturb, group_key (with its sibling), date_shift, redact, truncate, passthrough.
3. Every timing record on the lane has `elapsed_ms > 0` and a non-negative `peak_memory_delta_kb`.
4. `sum(elapsed_ms)` for a 150k-row admitted job (`auto_chunk=False`) is greater than 0 and within a sane bound of the job's wall time (not larger than the wall).
5. `boundary_conversion_ms` is a measured float, not the hardcoded 0.0 (assert it is recorded; do not assert a magnitude).
6. Outputs unchanged: the existing unified-slice parity suites (`tests/physical/test_unified_slice_parity.py`, `test_unified_slice_input_formats.py`, `test_unified_slice_admission.py`) pass unmodified apart from the removed xfail.
7. Zero overhead off the lane: a shadow-coordinator call with no active collector records nothing (assert `get_active_collector()` is None inside and no records are produced).
8. `tests/physical/test_characterization_full_frame.py` timing assertions still hold.

Tests needing the companion carry `@_NEEDS_COMPANION` and run in the native-companion CI job.

## Out of scope

Timings for the chunked dispatcher (Phase B1 owns that contract); platform-side display changes (the platform already sums `timings`).

## Gates

Codex plan-gate, Sonnet build, dennis, Codex final. Merges under the standing Rust rule once CI is back.
