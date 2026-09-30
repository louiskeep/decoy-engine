# Retire the D9 peak-memory ratio gate (Rust engine program A7)

Status: plan

Date: 2026-09-30. Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase A (record gap R14). Branch `chore/retire-d9-rss-ratio`, off engine main `8dc559e5`.

## Why

Cam dropped the D9 peak-memory ratio bar on 2026-09-30: speed matters more than a few extra GB, and nobody trades a ~4x slowdown for the memory. The rule that remains is absolute: a run's peak memory must fit the box it runs on, with headroom. The certification harnesses still enforce the ratio:

- `scripts/bench-unified-slice/bench_compare.py`: `gates["rss"]` requires the Rust arm's peak to stay within `rss_budget_ratio(n_rows)` (1.10 below 1M rows, 1.25 at 1M and above) of the pandas arm's peak (~76-96, ~202-206).
- `scripts/bench-generation-pool/bench_compare_gen.py`: `apply_rss_gate` with `_DEFAULT_MAX_RSS_RATIO = 1.25` and `_DEFAULT_MAX_RSS_DELTA_KB = 51_200` (~87-88, ~212).

A Rust slice that is much faster but uses 30% more memory would fail certification today, which is the outcome Cam ruled out.

## Design

In both harnesses:
- Replace the ratio gate with an absolute gate: the candidate arm's peak memory must be at or below a declared ceiling (`--max-peak-rss-mb`).
- The ceiling is required for certification. A run without a declared ceiling can still complete (`run_ok`) but cannot certify, and the report says the memory gate was not declared. No silent default.
- Keep reporting the peak-memory ratio and the absolute peaks of both arms as information in the result JSON, so regressions stay visible without blocking.
- The wall-time gates are unchanged.
- Update the harness READMEs and the module comments that describe the ratio band.
- The generation-pool harness's absolute delta cap (50 MB) is also a relative-to-pandas measure; replace it the same way (absolute ceiling on the pooled arm).

## Acceptance tests (written first)

1. `bench_compare.apply_gates`: a Rust arm at 1.5x the pandas peak but under the declared ceiling passes the memory gate; the same arm over the ceiling fails it.
2. No declared ceiling: `run_ok` can be true, `d9_certified` is false, and the result records that the memory gate was not declared.
3. The result JSON still carries both arms' peaks and their ratio.
4. `bench_compare_gen`: the same three behaviors for the pooled arm.
5. Existing tests that pinned the ratio (`tests/physical/test_bench_compare_harness.py` ~237-245, and the gen-pool harness tests) are updated to the new contract; no other assertion is weakened (wall-time gate tests unchanged).
6. The CLI accepts `--max-peak-rss-mb` and rejects a non-positive value.

## Gates

Codex plan-gate, Sonnet build, dennis, Codex final. Merges under the standing Rust rule once CI is back.
