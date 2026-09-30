# Unified lane per-column timings (Rust engine program A6)

Status: plan (revision 2: folds the Codex plan-gate GO-with-revisions)

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase A (record gap P6). Evidence: `docs/records/2026-09-30-rust-coverage-evidence-audit.md`, runs R029, R030, R082. Branch `fix/unified-lane-timings`, off engine main `8dc559e5`.

## Problem

The unified slice (the Rust lane) returns `ExecutionResult(timings=(), boundary_conversion_ms=0.0)` (`execution/_unified_slice.py` ~369-372). The platform computes a job's execute time as the sum of `timings` (`api/jobs/v2_full_frame.py:83-91`), so every Rust job reports 0 ms (R029, R082), while the same job on the pandas route reports real time (R030: 49.7 ms). The engine #180 strict xfail `test_admitted_job_reports_per_column_timings` documents the gap.

## Design

Use the timing instrumentation the pandas route already uses (`instrumentation/timing.py`: `TimingCollector`, `use_collector`, `timed_strategy`, `StrategyTimingRecord`), so the Rust lane's records have the same shape and labels.

- In the unified lane's admitted execution path, create one `TimingCollector` and run the coordinator under `use_collector(collector)`.
- In the coordinator's per-node loop (`physical/_shadow_coordinator.py`), use `node.strategy` and `",".join(node.columns)` directly (`PhysicalNode` already carries the configured strategy, `physical/_plan.py:192`); do not widen `ExecutionBinding`. Create exactly one `timed_strategy` scope per bound node, enclosing lazy compiled-kernel loading, Faker pool resolution if present, the entire batch loop, evidence validation, and `assemble_column`. That yields one record per node regardless of batch count and captures both Arrow and compiled-kernel work.
- `timed_strategy` is a no-op when no collector is active, so the shadow coordinator's other callers (shadow parity tests, the chunked shadow driver) keep zero overhead and unchanged behavior.
- Return `timings=tuple(collector.records)` from the lane.
- `boundary_conversion_ms`: add accumulated conversion time to `CheapCandidate`. Measure `to_pandas_fk_safe` and the admission round-trip `Table.from_pandas` (`_unified_slice_admission.py` ~365 and ~397), then add the whole output bridge in `_unified_slice.py` (~305): Arrow column extraction and overlay through the final `Table.from_pandas`. `boundary_conversion_ms` means all Arrow to pandas / Python boundary work an admitted lane run actually performed. A declined candidate discards the value; the legacy route reports its own.
- Out of scope on this lane: FK-resolved nodes (relationships decline at `_unified_slice_admission.py` ~260) and Faker (not admitted by the operator and type allowlists). Rayon threads are safe: the Python timer encloses the synchronous FFI call and the collector is never touched from Rayon. Future Python-level parallel node dispatch would need explicit collector propagation.

Timings stay outside the D9 parity contract (plan `docs/plans/2026-09-20-unified-slice-activation.md` ~101): they are measurements, not outputs.

## Acceptance tests (written first)

1. Remove the strict xfail on `test_admitted_job_reports_per_column_timings`; it passes.
2. For admitted jobs across the native operators (hash, categorical deterministic, bucket_perturb, group_key with its sibling passthrough node, date_shift, redact, truncate, passthrough), a `Counter[(strategy_type, column)]` of the lane's timings has exactly one record per admitted physical node, and its key set equals the pandas route's for the same config. Include a multi-batch case (small batch size) to prove batches do not multiply records.
3. Every record has `elapsed_ms >= 0` and a non-negative `peak_memory_delta_kb`; for a 150k-row admitted job (`auto_chunk=False`), `sum(elapsed_ms) > 0` and is not larger than the job's wall time.
4. `boundary_conversion_ms` equals the exact accumulated value under a controlled clock (patched `perf_counter`), covering the admission conversion, the round-trip and the output bridge.
5. Outputs unchanged: the unified-slice parity suites (`tests/physical/test_unified_slice_parity.py`, `test_unified_slice_input_formats.py`, `test_unified_slice_admission.py`) pass unmodified apart from the removed xfail.
6. No overhead off the lane: with no active collector, a shadow-coordinator run makes no clock or RSS sampling and allocates no records (patch `_rss_kb` and the timing module's `perf_counter` to fail if called).
7. `tests/physical/test_characterization_full_frame.py` timing assertions still hold.

Tests needing the companion carry `@_NEEDS_COMPANION` and run in the native-companion CI job.

## Out of scope

Timings for the chunked dispatcher (Phase B1 owns that contract); platform-side display changes (the platform already sums `timings`).

## Gates

Codex plan-gate, Sonnet build, dennis, Codex final. Also run `test_unified_slice_performance.py` and `test_bench_unified_slice_harness_smoke.py` locally, and update `scripts/bench-unified-slice/bench_worker_unified.py`'s obsolete "no per-node timing" explanation while keeping its isolated-wall methodology. The activated arm now samples the clock and RSS per node, so the previous D9 certification no longer covers this revision: re-certify with `bench_compare.py --require-cert` on the candidate revision after A7 lands (so the absolute memory gate applies), as a GCP run under the existing budget with a Slack message first. Merges under the standing Rust rule once CI is back and the re-certification passes.
