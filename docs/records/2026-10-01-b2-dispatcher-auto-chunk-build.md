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
| `172ec9fc` | This record and the benchmark artifact (dennis MEDIUM 3, the missing bookkeeping) |

## Red before

At `8ad01011` (tests only, no implementation): 257 failed, 1152 passed. Test 0 (the output-delta record) passes there by design: it records today's lane and a B1-as-the-lane fixture, so it can be written before B2 exists.

## Coverage

`_pipeline_auto_chunk.py`: 100% line and 100% branch (92 statements, 22 branches, 0 missed).

Per-unit measurement for the other changed units. Command: the six `tests/unit/execution/test_auto_chunk_*.py` files plus `tests/physical/test_lossless_forwarding.py` (1477 passed), run with `pytest --cov-branch` over `src/decoy_engine/execution`, reported for the five units below. pytest-cov and coverage came from a scratch `--target` directory outside the repo, so no project dependency changed. The whole-file figures are low for units B2 touched only at the edges, so the table also gives the result for the lines B2 changed (taken from `git diff origin/main..HEAD`).

| Unit | Whole file, line / branch | B2-changed lines | Changed-line line / branch |
|---|---|---|---|
| `_pipeline_auto_chunk.py` | 100% / 100% (0 missed) | all (new file) | 100% / 100% |
| `_pipeline_route_exec.py` | 27% line (misses at 163, 290-437, 458-527) | 8-9, 541-569 (the `run_mask_chunked` delegate and its import) | 100% line, no branch missed |
| `_pipeline_finalize.py` | 41% line (misses at 126, 175-444) | 63-75, 139-153 (lane-stamp merge call) | 100% line, no branch missed |
| `_pipeline_generate_mask.py` | 95% line (miss at 204; branch 124->211 not taken) | 76-77, 149-150, 177 (`native_threads` and kill-switch forwarding) | 100% line, no branch missed |
| `_pipeline.py` | 92% line (misses at 137, 140, 332, 343, 526, 616) | 36-38, 79, 174-175, 232-253, 327, 560-561, 578-579 (new kwargs, wiring, docstring) | 100% line, no branch missed |

The uncovered lines in the four shared units are legacy lanes (full-frame finalize, sequential and out-of-core route execution, generate paths) that B2 did not change and that other suites cover. None sits in a line B2 changed. These numbers come from the B2 suites only.

Assessed only by hand mutants (no mutmut run, since mutmut cannot grade them cheaply): the route-exec delegate (`_pipeline_route_exec.py`), the finalize merge call (`_pipeline_finalize.py`), the `_pipeline.py` wiring, the `generate_mask` forwarding and the physical adapter. Section Mutation lists the 16 mutants. mutmut graded only `_pipeline_auto_chunk.py`.

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

Verdict: GO. 0 BLOCKER, 0 HIGH, 3 MEDIUM, 3 LOW. All three MEDIUM findings are fixed in `52c88524` and `172ec9fc`.

- MEDIUM 1: `_legacy_route_evidence` built its profile from `empty_input_profile` (object placeholders), so the kill-switch lane could report a different planned backend than the dispatcher lane when admission depends on type (a routed hash column of type `duration` reported `rust_companion` on the kill-switch lane and `pandas_oracle` on the dispatcher lane). Fixed in `52c88524`: the legacy lane now profiles `source.slice(0, chunk_size_rows)` with `first_chunk_profile`, as the dispatcher preflight does. New tests compare `planned_backend` across lanes for seven non-trivial source types by three strategies and for the whole strategy matrix; the `duration` hash case failed before the fix and passes after. One existing unit test spied on `empty_input_profile`; it now spies on `first_chunk_profile` with its assertions unchanged.
- MEDIUM 2: test 0 never ran the shipped lane, because the matrix patched `run_mask_chunked` with a test copy. Fixed in `52c88524`: a `disp_live_<route>` lane calls `run_default` with no patch and asserts its record and table equal `disp_<route>`. It only adds checks; the allowed-difference set and the fixture are untouched.
- MEDIUM 3: missing bookkeeping (no build record, no benchmark artifact). Fixed in `172ec9fc`, which adds this record and `docs/records/b2-bench-2026-10-01/b2-bench-1m.json`.
- LOW 1: the unneeded `_pipeline_chunk_route.py` entry in the permitted-edit list of `tests/sentry/test_physical_seam_disconnection.py` was removed in `52c88524` (the file is unchanged).
- LOW 2: peak RSS is dominated by building the source table in the worker, so the 2 GiB ceiling does not discriminate between lanes (see Benchmark).
- LOW 3: the shadow harness is pinned to the kill switch (`0c872b4d`, accepted by dennis). The underlying route-output difference is carried to the `§ROUTE-OUTPUT-CONTRACT` roadmap item.

Codex final gate: GO, with two LOW record gaps (no per-unit coverage numbers, no doc-inventory disposition table). Both are closed in this record: see Coverage above and the table below.

## Active-doc inventory: route-neutral and byte-identical claims

The plan's inventory step (revision 5, "Active-doc inventory") searched `src/`, `README.md`, `CODEMAP.md`, `CHANGELOG.md` `[Unreleased]` and `docs/` outside `docs/plans/`, `docs/records/` and archives for claims that auto-chunk, or every route, is route-neutral or byte-identical. Hits that concern the pandas oracle `run_mask_pipeline_chunked` against the full frame stay true because the oracle is unchanged. Commit `29fd009f` is the B2 implementation commit; the doc amendments landed in it.

| File:line | Original claim | Disposition |
|---|---|---|
| `src/decoy_engine/execution/_pipeline.py:243` (on `origin/main`, `run_pipeline` docstring) | "route is byte-output-neutral versus full_frame (only peak memory ..." | Amended in `29fd009f`: "Masked values are route-neutral; on the auto-chunk route schema, types, nullability and metadata follow guarantee 3" |
| `src/decoy_engine/execution/__init__.py:59` (module docstring; on `origin/main`, line 59-60) | "Output is unaffected -- every route is byte-output-equivalent by design." | Amended in `29fd009f`: "Masked values are unaffected -- they are route-neutral; on the auto-chunk route schema, types, nullability and metadata follow guarantee 3" |
| `CODEMAP.md:24` | auto-chunk "a memory-only win with byte-identical output" | Amended in `29fd009f`: masked values equal full frame; schema, passthrough types, nullability and metadata follow guarantee 3; kill switch named. The new module's ownership row was added in the same commit |
| `docs/compatibility-contract.md:210` | None before B2 (the contract had no auto-chunk neutrality claim) | Added in `29fd009f`: records the cutover, masked values route-neutral, schema and metadata route-dependent, `§ROUTE-OUTPUT-CONTRACT` as follow-up |
| `CHANGELOG.md` `[Unreleased]`, line 12 onward | None before B2 | Added in `29fd009f`: the output-contract change entry. No other line in `[Unreleased]` (lines 10-46) makes an auto-chunk neutrality claim; the byte-identical claims at lines 536 and below sit in released sections and describe earlier slices |
| `src/decoy_engine/execution/_chunked.py:139` | "chunked output is byte-identical to a serial run by construction" | Left as a true oracle-only claim: it describes `run_mask_pipeline_chunked`, whose code B2 does not change. B2 added 4 lines to `physical/drivers/_chunked.py`, not to this file's chunk loop |
| `src/decoy_engine/execution/_strategies/_top_code.py:34` | "breaks the byte-identical-to-full-frame guarantee `run_mask_pipeline_chunked` makes" | Left as a true oracle-only claim: it names `run_mask_pipeline_chunked` explicitly and explains a chunk-boundary bug in the strategy |
| `src/decoy_engine/execution/_chunked_fk_dtype_safety.py:6` | "a FK key's hash stays byte-identical across the chunked-route boundary ONLY for a specific dtype set" | Left as a true oracle-only claim: it is about FK-bearing jobs on the oracle chunked route, which B2 does not route to the dispatcher (auto-chunk is single-table, non-relationship) |
| `src/decoy_engine/execution/_chunked.py:50, 127, 367` | oracle output "byte-identical to the full-frame run", hash "proven byte-identical", concatenation "byte-identical" | Left, same reason as `_chunked.py:139` |
| `src/decoy_engine/execution/_chunked_fk.py` (105, 261, 277, 312, 404, 453, 465, 539, 694, 778, 818), `_chunked_fk_dtype.py:130`, `_chunked_group_key.py:13` | Cross-adapter or chunk-boundary byte-identity of FK and group-key handling on the oracle chunked route | Left: FK-bearing and group-key jobs do not take the auto-chunk dispatcher lane |
| `src/decoy_engine/execution/_pipeline_finalize.py:104` | "The all-default path stamps nothing so golden and compat-corpus fixtures stay byte-identical" | Left as true: a non-routed call is unchanged (plan guarantee 8), and acceptance test 1 checks equal `quality_metrics` on and off |
| `src/decoy_engine/generation/statistical/_sample.py:34`, `execution/native/_draw_site_providers.py:634`, `_determinism_protocol.py:513`, `docs/native/draw-site-inventory.md:316` | Reseed-per-row draws make any chunking byte-identical to a serial pass | Left as true: draw determinism, a property of the sampler. It says nothing about output schema |
| `docs/ci-regression-gate.md:17` | parity tests: "pandas chunked equals pandas full-frame byte-for-byte" | Left as true: the parity suite exercises the oracle |
| `docs/quality/mutation-ledgers/*.md`, `docs/security/*`, `docs/backlog/*`, other `docs/*` hits | Per-module mutation, security and backlog notes using "byte-identical" for kernel or strategy parity | Left: none states a route-level auto-chunk claim. `README.md` has no hit |

