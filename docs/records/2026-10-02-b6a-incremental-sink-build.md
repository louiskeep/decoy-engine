# B6a incremental output sink: build record

Status: record
Date: 2026-10-02. Branch `feat/b6a-incremental-sink`, base engine main `02dc2827`. Plan:
`docs/plans/2026-10-02-b6a-incremental-output-sink.md` (revision 2.1, Codex GO). Builder: Sonnet.
Nothing is pushed or merged. One acceptance item is open: the oracle 10M benchmark cell (see
"Benchmark"). Gates (dennis, Codex final) have not run.

## Commits

- `daa71702` tests first: acceptance tests 1 to 12 and the shared helpers.
- `8097cbfb` implementation: `_chunked_output_sink.py`, the wiring, docs and sentries.
- `f005e358` the one authorized change to an existing test (below) and the benchmark harness.
- `d7b56c88` extra tests that kill surviving hand mutants and cover the empty-stream guard.
- the commit holding this record, the CHANGELOG entry and the benchmark artifact.

## Red before

Run on `daa71702` with the implementation module absent (the working tree had no
`_chunked_output_sink.py`): `pytest-one <py> tests/unit/execution/test_b6a_output.py
test_b6a_publish.py test_b6a_split_worker.py test_b6a_bookkeeping.py
tests/sentry/test_physical_seam_disconnection.py -q`. Result: 223 failed, 7 passed in 12.6 s.
The failures were for the right reasons: `outputs` still resident (`{...} == {}`), the sink
never called (abort count 0), `KeyError: 'output'`, the missing module, and the seam sentry's
missing guarded module. The 7 that passed are tests that assert behavior that already holds
(for example a non-routed run never entering the new code). Test 13 is the benchmark.

One test-side defect was fixed after the first green run, not an assertion change: the
pyarrow-default row-group test built its probe column as `int8`, which overflows at 1,048,581
values; it now uses `int64` (`test_row_group_rows_is_the_pyarrow_default`). Another was a
test-harness path bug in `test_non_routed_runs_never_enter_the_new_code`: it built the job in
two directories, so the path-dependent `unified_slice_activation` hashes differed between the
compared runs; both runs now share one job.

## Final counts, lint, types

- Full suite, once after the last test commit: `/home/cam/bin/pytest-one
  /home/cam/.cache/decoy-native-venv/bin/python tests -q --no-header -p no:randomly` with
  `PYTHONPATH=<worktree>/src:<worktree>`: 1 failed, 20501 passed, 168 skipped, 21 deselected, 59
  xfailed in 29 min. The one failure is the known pre-existing
  `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client`
  (`No module named 'google'`).
- An earlier full run, before the plan-mandated test edit and the log-line change below, had the
  same gcs failure plus three more, which is how those divergences were found.
- `ruff check`, `ruff format --check` over `src tests scripts/bench-auto-chunk` clean; `mypy
  src/decoy_engine testflight`: no issues in 505 files.
- `_pipeline.py` stays at its 679-line ratchet (the knob, the session and the commit line are
  paid for by dropping the eight `step_result` unpack assignments); the new module is under the
  600-line goal and needs no ratchet entry. `_unified_slice_admission.py` stays at 629 lines.

## Deviations and plan-mandated composition changes

1. Existing test edit, authorized by the owner in-session: `_check_evidence` in
   `tests/unit/execution/test_multi_table_run.py` pins the exact key set of a dispatched table
   entry in `auto_chunk.tables`. Guarantee 6 adds an `output` key there on every routed run, so
   `"output"` was added to the expected set. The assertion is still an exact set equality, and
   nothing else in `test_evidence_under_default_auto_chunk_knobs` or
   `test_evidence_under_non_default_auto_chunk_knobs` changed. This is a plan-mandated
   composition change, not a weakening.
2. INFO log line (Design 6 says it gains the output mode): resident runs keep today's exact
   message, because `test_run_auto_chunk_logs_the_lane_without_values` pins it. A streamed run
   appends ` output=streamed`. Accepted by the owner as a deviation.
3. `stream_chunked_output` is validated by the `OutputPublish` constructor (it calls
   `require_bool`), which `run_pipeline` builds right after `require_lane_knobs`, so it still
   fails before profiling. Done to hold the 679-line ratchet.
4. Stream reason for an eligible run is the string `eligible`; the plan names no value.
5. The resident `output` block for a routed single table or a split's dispatched tables is
   stamped by the callers (`_pipeline_generate_mask`, `run_multi_table_split`), so
   `run_auto_chunk` without a sink is unchanged line for line, as the plan says.
6. `decide_output_mode` for a split passes `resident_names` from `merged_sources`, so a stray
   caller frame is `split_extra_sources_present`; the full-frame check comes first.
7. Test 5's "equal to the reference" for `boundary_conversion_ms` is asserted as exact equality
   (0.0) on the native route and as non-negative on the oracle route, because the oracle value is
   a wall-clock sum that differs between any two runs. Timings are compared by (strategy, column)
   keys for the same reason.
8. Test 1 checks the published bytes against `pq.write_table` on every case of the matrix in the
   same run as the recording-sink check (the recording sink forwards to a real
   `ParquetTransactionalSink`), instead of a second run.
9. The full suite was run once after the final test commit plus once earlier (above); the
   targeted b6a suites were run many more times during the build.

## Coverage

`coverage 7.16.2` (installed into the scratchpad), branch mode, over the four b6a test files
(230 tests at the time), `PYTHONPATH` on this worktree. Changed executable lines, intersected
with the diff against the base:

- `_chunked_output_sink.py`: 215 changed lines, one missed (the `no chunks to join` guard); a
  direct test for it was added afterwards (`test_an_empty_chunk_stream_is_a_schema_mismatch...`).
  Module total 99% line, one partial branch, same line.
- `_pipeline_auto_chunk.py`: 17 changed lines, none missed. `_pipeline_generate_mask.py`: 10,
  none. `_pipeline_multi_table.py`: 11, none; one untaken branch (`lane_block` without an
  `output` key, reachable only by calling `run_multi_table_split` directly without a reason).
- `_pipeline.py`: 17 changed lines, one missed (the `explain_plan` block, re-indented but
  otherwise unchanged and covered by other suites).
- `_pipeline_route_exec.py`: one changed line (the telemetry flag), covered.

## Hand mutation

mutmut is not installed here and the plan's mutmut run was not done; this is a manual substitute,
not equal to it. 28 mutants were applied one at a time to the worktree source (restored after
each, `git status` clean after the run), each checked to be the imported tree (module file under
the worktree and the mutated text present), then run against the b6a output, publish and split
suites plus `test_multi_table_run.py` with `-x` (the 2.2M-row test deselected for speed). Raw
results: `docs/records/b6a-bench-2026-10-02/hand-mutation-round1.json` and `round2.json`.

Round 1: 21 killed, 7 survived. The survivors were: row cut `>` for `>=`, byte cut `>` for `>=`,
the fixed-columns rule ignored (twice, two spellings), the column-name mismatch check dropped,
abort on a normal exit, and the split group computed from the wrong dict. New tests were written
for each (early emission at exactly one row group, a byte cap equal to one chunk, a null-typed
passthrough column that must not hold the stream back, a renamed column in a later chunk, and a
streamed split that must not call the full-frame adapter). Round 2 re-ran those seven: six
killed. The survivor is "abort even without error" in `OutputPublish.__exit__`: an open session
that exits normally has always committed (`commit()` is the last action and sets the finished
flag), so the mutant is equivalent. Not killed by a test; offered as an equivalence argument for
dennis.

## Benchmark (test 13): partial, frozen bars unchanged

Run ID `B6A-BENCH-2026-10-02-3CELLS` (partial). Raw JSON, including every trial:
`docs/records/b6a-bench-2026-10-02/b6a-bench-3cells.json`. Harness:
`scripts/bench-auto-chunk/bench_sink.py` and `bench_worker_sink.py`, run from a frozen copy of
commit `f005e358`. Method as in the plan: B2's section 11 workload, `native_threads=1`,
`OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`, seed 20261002 for round order, three discarded
warmups and twenty measured rounds per configuration, one fresh process per trial, each cell run
under `/home/cam/.cache/pytest-one.lock`, `VmHWM` reset through `/proc/self/clear_refs` after the
source read, SHA-256 of every staged file against the resident reference. Percentiles are
nearest rank. Host: Intel i5-7500, Linux 6.17.2-1-pve. Peak and increment in MiB, wall in s.

| Cell (source / output bytes) | Config | wall p50 / p95 / max | run increment p50 / p95 / max | lifetime peak max |
|---|---|---|---|---|
| native 2M (160,000,000 / 210,000,000) | resident | 3.54 / 3.78 / 3.87 | 293 / 301 / 302 | 769 |
| native 2M | streamed | 3.49 / 3.92 / 4.02 | 222 / 225 / 225 | 692 |
| native 10M (800,000,000 / 1,050,000,000) | resident | 45.38 / 52.92 / 83.71 | 1518 / 1523 / 1525 | 2877 |
| native 10M | streamed | 42.82 / 53.24 / 56.41 | 10.5 / 10.8 / 12.6 | 1369 |
| oracle 2M (178,666,464 / 226,000,000) | resident | 47.79 / 64.57 / 67.75 | 389 / 395 / 395 | 883 |
| oracle 2M | streamed | 47.68 / 66.52 / 66.91 | 280 / 291 / 294 | 773 |
| oracle 10M | both | pending, see below | | |

Correctness: all 120 measured trials of these three cells (20 per configuration per cell)
matched the reference bytes; every streamed trial recorded `outputs_streamed` true,
`byte_cut_row_groups == 0` and `row_groups == ceil(rows / 1,048,576)` (2 at 2M, 10 at 10M);
native cells reported `native_admitted` true, the oracle cell the reroute reason
`categorical_not_native_chunked_route:tier`. No problems were recorded.

Bars on the completed cells:

- W (streamed p50 at most 1.10 times resident p50, p95 at most 1.15 times): native 2M 0.986 and
  1.037, native 10M 0.944 and 1.006, oracle 2M 0.998 and 1.030. Pass.
- Ceilings (lifetime peak of every streamed trial): native 2M 692 MiB against 1.5 GiB, native
  10M 1369 MiB against 3.0 GiB, oracle 2M 773 MiB against 1.5 GiB. Pass.
- M1 native (streamed p95 increment at 10M at most 0.25 times resident p50 increment): 10.8 over
  1518, ratio 0.007. Pass.
- M2 native (streamed p95 at 10M at most `max(1.25 x streamed p95 at 2M, that + 128 MiB)`): 10.8
  against a limit of 353 MiB. Pass.
- M1 and M2 for the oracle route, W and the ceiling for oracle 10M: not evaluated. Pending.

Pending: the oracle 10M cell (46 trials at about 250 s each) was interrupted when the orchestrator
stopped the run because it held the machine-wide lock for hours. The partial trials were not
kept. The orchestrator will rerun it alone in an idle window with `--workloads oracle --rows
10000000` and merge the result; M1 and M2 for the oracle family need that cell plus the saved
oracle 2M cell. No bar, ceiling or constant was changed. The first 16 oracle 10M trials seen in
the log showed streamed increments near 46 to 50 MiB against resident near 1830 MiB and wall times
of 244 to 282 s streamed against 249 to 255 s resident, from the console only; those are not
evidence.

## Notes for the gates

- The ledger row for the benchmark was not written (the ledger lives in the vault, outside this
  worktree).
- A streamed run keeps the input resident (B6b), and the isolated worker's driver still reads
  every staged table back, as the plan's Known issues say.
