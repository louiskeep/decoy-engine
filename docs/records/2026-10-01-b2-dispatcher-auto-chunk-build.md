# B2 dispatcher auto-chunk: build record

Status: record

Date: 2026-10-01. Plan: `docs/plans/2026-10-01-dispatcher-auto-chunk.md` (revision 5, Codex plan-gated; not edited by this record). Branch `feat/dispatcher-auto-chunk`. This record covers the build, the evidence for it, and dennis's gate verdict. The plan stays as written.

## What shipped

A routed large single-table mask job now masks through B1's `run_mask_chunked` and joins the chunks with `join_dispatcher_chunks`. The kill switch `chunked_dispatcher_enabled=False` runs the previous lane unchanged. The lane lives in `src/decoy_engine/execution/_pipeline_auto_chunk.py`; every routed result records its lane, the reason, and per-column backend evidence in `quality_metrics`.

## Commits

| SHA | What |
|---|---|
| `8ad01011` | Tests first: the B2 acceptance tests, before any implementation |
| `4a5a9412` | Four test expectations corrected on first contact with the code (each a test mistake, none weakened) |
| `29fd009f` | The implementation |
| `0c872b4d` | Extra test edit: shadow harness pinned to the kill switch |
| `220c805f`, `5ed646cc`, `5bca89f2`, `9ae73bf4` | Free-function refactor of the lane-stamp merge, plus tests that kill mutation survivors |
| `99da42cb` | Pins the `run_pipeline` `native_threads` default (kills the one hand-mutant survivor) |
| `46c3c72d` | Benchmark driver and worker |
| `52c88524` | dennis MEDIUM 1, MEDIUM 2 and LOW 1 (see below) |

## Red before

At `8ad01011` (tests only, no implementation): 257 failed, 1152 passed. Test 0 (the output-delta record) passes there by design: it records today's lane and a B1-as-the-lane fixture, so it can be written before B2 exists.

## Coverage

100% line and 100% branch on `_pipeline_auto_chunk.py`.

## Mutation

299 mutants on `_pipeline_auto_chunk.py`: 292 killed, 7 equivalent. dennis accepted the equivalence arguments for all seven: `join_dispatcher_chunks` mutants 27, 30, 32, 33 and `_target_field` mutants 7, 9, 10.

Hand-applied mutants covered the units mutmut cannot grade cheaply: the route-exec delegate, the finalize merge call, `_pipeline` wiring, `generate_mask` forwarding and the physical adapter (16 mutants). 15 were killed on the first pass. The survivor was "pipeline: default native_threads"; `99da42cb` adds the test that kills it.

## Benchmark

Run ID `B2-BENCH-2026-10-01-1M`. Raw JSON: `docs/records/b2-bench-2026-10-01/b2-bench-1m.json` (the durable artifact). Driver `scripts/bench-auto-chunk/bench_auto_chunk.py`, worker `bench_worker_auto_chunk.py`.

Setup: 1,000,000 rows, 7 interleaved rounds in a randomized order per round (seed 20261001), a fresh subprocess per trial, 2 GiB ceiling. Intel Core i5-7500, Python 3.11.15, pyarrow 25.0.1, pandas 2.3.3, engine 0.7.0, companion 0.1.0. The run was made under the box-wide test lock (`flock /home/cam/.cache/pytest-one.lock`), one process at a time, in the companion venv.

Variants: `base` (hash, redact, truncate and passthrough string columns), `extra` (base plus an unconfigured column, so B1 reroutes to its oracle route), `pandas` (base with pandas schema metadata on the source).

Measured results (seconds are wall time; RSS is the worst of 7 trials):

| Config | Variant | Dispatcher | Threads | p50 s | max s | peak RSS MiB |
|---|---|---|---|---|---|---|
| a | base | off | 1 | 13.45 | 13.85 | 642 |
| b | base | on | 1 | 4.46 | 4.70 | 644 |
| c | base | on | 4 | 3.83 | 4.06 | 645 |
| d0 | extra | off | 1 | 14.70 | 15.03 | 725 |
| d1 | extra | on | 1 | 14.75 | 15.44 | 725 |
| e0 | pandas | off | 1 | 13.39 | 14.27 | 674 |
| e1 | pandas | on | 1 | 4.32 | 4.45 | 676 |

All bars met (run `B2-BENCH-2026-10-01-1M`): b and c beat a at p50 and at max; e1 beats e0 at p50 and at max; d1 p50 is within 1.05x of d0 (the oracle reroute is not slower than the old lane); every trial stayed under the 2 GiB ceiling; correctness comparisons held; no problems were logged. Measured p50 speedup of the dispatcher lane over the old lane on `base` at one thread: 3.0x (13.45 s to 4.46 s, run `B2-BENCH-2026-10-01-1M`).

dennis LOW 2: peak RSS is dominated by building the source table in the worker, so the 2 GiB ceiling does not discriminate between lanes. The RSS column shows the lanes within a few MiB of each other because of that, not because the lanes use equal memory. Read it as "no trial came near the ceiling", nothing more.

## Test edit outside the plan's list

`0c872b4d` pins the physical shadow harness's oracle call to the kill switch. With the dispatcher as the default lane, two existing tests (`test_unkeyed_multi_chunk_parity_always_run`, which spies the oracle chunk boundaries, and `test_chunked_degenerate_all_null_column_parity`, which compares an all-null string passthrough column that the dispatcher keeps as `string`) no longer compared against the pandas lane. The edit passes the kill switch on the oracle call and keeps every assertion. dennis accepted it. His LOW 3 is carried to `§ROUTE-OUTPUT-CONTRACT`.

## dennis's verdict and the follow-ups applied

Verdict: GO with follow-ups.

- MEDIUM 1: `_legacy_route_evidence` built its profile from `empty_input_profile` (object placeholders), so the kill-switch lane could report a different planned backend than the dispatcher lane when admission depends on type (a routed hash column of type `duration` reported `rust_companion` on the kill-switch lane and `pandas_oracle` on the dispatcher lane). Fixed: the legacy lane now profiles `source.slice(0, chunk_size_rows)` with `first_chunk_profile`, as the dispatcher preflight does. New tests compare `planned_backend` across lanes for seven non-trivial source types by three strategies and for the whole strategy matrix; the `duration` hash case failed before the fix and passes after. One existing unit test spied on `empty_input_profile`; it now spies on `first_chunk_profile` with its assertions unchanged.
- MEDIUM 2: test 0 never ran the shipped lane, because the matrix patched `run_mask_chunked` with a test copy. Added a `disp_live_<route>` lane that calls `run_default` with no patch and asserts its record and table equal `disp_<route>`. It only adds checks; the allowed-difference set and the fixture are untouched.
- LOW 1: removed the unneeded `_pipeline_chunk_route.py` entry from the permitted-edit list in `tests/sentry/test_physical_seam_disconnection.py` (the file is unchanged).
- LOW 2: noted above (RSS does not discriminate lanes).
- LOW 3: carried to `§ROUTE-OUTPUT-CONTRACT`.
