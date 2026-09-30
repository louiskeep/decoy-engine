# Retire the D9 peak-memory ratio gate (Rust engine program A7)

Status: plan (implemented on branch chore/retire-d9-rss-ratio; merge and v2 recert pending)

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md` (on decoy-engine branch `docs/reality-2026-09-30` until it merges), Phase A (record gap R14). Branch `chore/retire-d9-rss-ratio`, off engine main `8dc559e5`.

## Why

Cam dropped the D9 peak-memory ratio bar on 2026-09-30: speed matters more than a few extra GB, and nobody trades a ~4x slowdown for the memory. The rule that remains is absolute: a run's peak memory must fit the box it runs on, with headroom. The certification harnesses still enforce the ratio:

- `scripts/bench-unified-slice/bench_compare.py`: `gates["rss"]` requires the Rust arm's peak to stay within `rss_budget_ratio(n_rows)` (1.10 below 1M rows, 1.25 at 1M and above) of the pandas arm's peak (~76-96, ~202-206).
- `scripts/bench-generation-pool/bench_compare_gen.py`: `apply_rss_gate` with `_DEFAULT_MAX_RSS_RATIO = 1.25` and `_DEFAULT_MAX_RSS_DELTA_KB = 51_200` (~87-88, ~212).

A Rust slice that is much faster but uses 30% more memory would fail certification today, which is the outcome Cam ruled out.

## Design

In both harnesses, replace the ratio gate with an absolute peak ceiling, `--max-peak-rss-mb`: a finite positive number of MiB, compared inclusively as `on_rss_max_kb <= ceiling_mib * 1024` against the Linux `ru_maxrss` KiB readings. Non-positive, `nan` and `inf` are rejected at argument parsing. The harnesses are Linux-only for memory evidence; non-Linux execution is rejected rather than silently mis-scaled.

The reference-host D9 command uses the existing documented ceiling, 6.5 GiB (`--max-peak-rss-mb 6656`, the "6.5GiB-at-100M reference-host ceiling" in `bench_compare.py`'s comments), not a new number.

State transitions:

| Harness | Ceiling | Result |
|---|---|---|
| D9 (`bench_compare.py`) | not declared | `run_ok` can be true; `d9_certified=false`; `gates["rss"]=null`; `memory_gate_declared=false` |
| D9 | not declared, `--require-cert` | fails before any artifact or measurement, with a clear error |
| D9 | declared and exceeded | `gates["rss"]=false`; `run_ok=false` |
| D9 | declared and met | `gates["rss"]=true`; `d9_certified = run_ok and cert_shape and memory_gate_declared` |
| GP1 (`bench_compare_gen.py`) | not declared or exceeded | `run_ok` and `full_sweep` unchanged; `recommendation` withheld with an explicit reason, for pooled cells at or above the raw recommended tier. GP1 gains no `d9_certified`. |
| Both | missing or zero RSS evidence | fatal, with or without a ceiling |

Schema: both harness result versions go from `1.0.0` to `2.0.0`. GP1's `max_rss_ratio` and `max_rss_delta_kb` flags and result keys are removed. Both add `memory_gate_declared` and `max_peak_rss_mb`. Both keep the arms' peaks and `rss_ratio` as information; GP1 keeps `rss_delta_kb`. D9 keeps `d9_certified` and `gates["rss"]`.

Docs and CI: update both harness READMEs and module comments; update the stale docstring in `tests/physical/test_unified_slice_performance.py` (~6) that says D9 requires a peak-memory ratio; add dated supersession notes to plans that mandate the ratio (for example `docs/plans/2026-09-20-unified-slice-activation.md` ~184); add the unified-benchmark script and physical test paths to both path filters in `.github/workflows/native-companion.yml` (~35) so harness changes trigger the job that runs them (~308).

## Acceptance tests (written first)

1. D9 memory gate at the boundary: equal to the ceiling passes; the ceiling plus 1 KiB fails; a Rust arm at 1.5x the pandas peak but under the ceiling passes.
2. Every row of the transition table above, for both harnesses, including `--require-cert` without a ceiling failing before measurement.
3. `--max-peak-rss-mb` rejects 0, negative, `nan` and `inf`.
4. Exact-schema tests for both 2.0.0 result shapes (removed keys absent, new keys present, information fields kept).
5. Missing or zero RSS evidence is fatal in both harnesses.
6. Existing tests that pinned the ratio (`tests/physical/test_bench_compare_harness.py` ~237-245, `test_bench_gen_pool_harness.py`) move to the new contract; wall-time gate tests are unchanged.
7. Targeted runs of `tests/physical/test_bench_compare_harness.py`, `test_bench_driver_harden.py` and `test_bench_gen_pool_harness.py` pass.

## Gates

Codex plan-gate, Sonnet build, dennis, Codex final. Merges under the standing Rust rule once CI is back.
