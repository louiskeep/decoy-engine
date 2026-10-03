# B6b LazySource batch input: build record

Status: record
Date: 2026-10-03. Branch `feat/b6b-lazy-input`. Plan: `docs/plans/2026-10-02-b6b-lazy-batch-input.md` (revision 2.1). Builder: Sonnet. Nothing is pushed or merged. The build and benchmark are complete; five hand mutants survived (see "Mutation"), which is a test coverage gap for the owner to decide on.

## Commits

Newest first.

- `03890055` bench(b6b): pin the routing probe off in both configs and halt a cell on any trial problem. Made by the orchestrator.
- `aec0510c` docs(b6b): partial build record and benchmark artifacts.
- `8e8c3c30` test(b6b): gate the two `unified_slice_activation` tests with `@NEEDS_COMPANION`. Made by the orchestrator.
- `c39fb74f` test(b6b): three existing tests follow the plan's guarantees (authorized edits).
- `d544d707` bench(b6b): lazy-input benchmark harness.
- `bf0ec80c` feat(b6b): LazySource batch input on the auto-chunk lane.
- `816cbc43` test(b6b): acceptance tests, written first.

## Red before

On `816cbc43` with no implementation: the six non-equality b6b files gave 117 failed, 12 passed; `test_b6b_equality.py` gave 679 failed, 1 passed (660 of them the lazy run's sink calls differing from the twin's). Failure reasons were the missing module and classes, the lazy refusal text, and spills into a read-only `TMPDIR`. Tests that already passed before the change: the planner no-data-page test, the JSON-safe payload test, the zero-spill streaming test, missing-source-at-routing, transform-once, the small worker job and the low-cap completion.

## Existing tests changed, each forced by the plan

Plan-named: the dispatcher lazy-routing test, the split lazy test, the planner lazy-source test (plus a sibling for a missing footer null count), the physical-plan lazy marker, the four B6a hold-back spill tests with `_b6a_support.patched_tempdir` removed.

Authorized in session (`c39fb74f` and the earlier implementation commit):
- `test_b6a_output.py::test_live_chunks_stay_within_one_row_group_plus_one`: iterated per-chunk results, which the online accumulator no longer keeps. It now checks only the chunk count. The no-output-retained guarantee moved to the weakref and live-object checks in `test_b6b_sink.py`; it is not weakened.
- `test_multi_table_run.py` evidence tests: `"input"` added to the exact key set (guarantee 5).
- `test_multi_table_when.py[lazy_source]`: `expect_dispatched=True` (guarantee 1 reverses the old refusal).
- `test_isolated_run.py::TestMemCapOom`: the OOM test now runs with `auto_chunk=False` so the job stays resident and still classifies `oom_killed`; a sibling pins that the same job on auto-chunk streams and completes under the same 768 MiB cap.

## Deviations from the plan

- The lazy `input.reason` is `"eligible"`.
- Lazy candidates exclude zero-row tables, tables in a job with a generate table, and tables with an unprepared transform.
- Extra chunk sizes 16 and 7 and 120000-row layout cases were added to the tests.
- `decide_chunk_route` returns `keep_lazy`.
- `classify_job` and `decide_multi_table_split` take a `source_facts` keyword.
- `OutputEvidenceAccumulator` exposes `__len__` and `retained()`.
- Plan test 8 (the low-cap case) has no red-before: it already passed before the change.
- Two orchestrator fixes: the `@NEEDS_COMPANION` gate on the two `unified_slice_activation` tests (`8e8c3c30`), and the routing-probe pin plus halt-on-problem in the benchmark harness (`03890055`).
- Three authorized existing-test edits (`c39fb74f`, listed above).

## Test counts

- Full suite, companion present (native venv, pyarrow 25.0.1, `-p no:randomly`), on the final tree: 1 failed, 21334 passed, 168 skipped, 21 deselected, 59 xfailed in 30:06. The one failure is the known, pre-existing, environmental `test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client`.
- Companion absent (pyarrow 24.0.0), `test_b6b_modes.py`: 16 passed, 2 skipped. The two skips are the `unified_slice_activation` tests (reason: decoy-engine-native companion not installed).
- ruff check and format clean; mypy shows the same 6 errors in 2 files (opendp stubs, `decoy_engine.internal.logger`) as main `649f8fc1` (from the earlier run).

## Coverage

Line and branch, `.venv-decoy`, b6b plus B6a plus multi-table run tests (earlier run, restated): `_chunked_input` 93%, `_chunked_output_sink` 98%, `_planner` 94%, `_pipeline_auto_chunk` 91%, `_pipeline_multi_table` 92%, `_pipeline_chunk_route` 95%. The lower figures for `_readers` and `_pipeline_sources` mix in pre-existing paths that these tests do not target.

## Mutation (hand-mutation substitute)

mutmut cannot grade these execution modules (see `[tool.mutmut]` in `pyproject.toml`), so this is a manual substitute in the B6a style, not a mutmut score. One fault at a time, smallest relevant test selection first (the six non-equality b6b files, 130 tests), reverted with `git checkout` after each. Survivors were rerun against a wider selection (equality, B6a, multi-table run, auto-chunk routing and output tests, 1113 to 1133 tests). `git status` shows no `src/` or `tests/` change afterwards.

| Target | Mutation | Result |
|---|---|---|
| `rechunk` | `rows >= chunk_size_rows` to `>` | killed by `test_b6b_input.py::test_rechunk_cuts_exact_chunks_and_retains_fewer_than_two_chunks` |
| `capture_source_facts` | `kind == "mask"` to `!=` | killed by `test_b6b_input.py::test_the_lane_reads_with_the_frozen_reader_options` |
| `source_facts` | table `num_rows + 1` | killed by `test_b6b_routing.py::test_a_lazy_source_is_judged_against_the_threshold_like_a_table` |
| `lazy_stream_candidates` | `num_rows > 0` to `>= 0` | NOT KILLED (1113 passed) |
| `input_modes` | `out_mode == "streamed"` inverted | killed by `test_b6b_input.py::test_the_lane_reads_with_the_frozen_reader_options` |
| `facts_match` | `row_groups` comparison dropped | NOT KILLED (1113 passed) |
| owner close (`InputChunks.close`) | `self._owner.close()` replaced by `pass` | NOT KILLED (1113 passed) |
| `OpenedLazyBatches.close` | idempotence guard removed | killed by `test_b6b_input.py::test_open_batches_returns_an_owner_with_footer_facts_and_an_idempotent_close` |
| `fold_timings` | elapsed sum replaced by assignment | killed by `test_b6b_sink.py::test_the_accumulator_equals_b2s_list_aggregators[native]` |
| `fold_timings` | memory `max` replaced by last value | NOT KILLED (1133 passed) |
| `fold_warnings` | de-duplication removed | killed by `test_b6b_sink.py::test_the_accumulator_dedups_equal_warnings_and_keeps_first_emission_order` |
| `fold_corpora` | first-wins `setdefault` replaced by last-wins | NOT KILLED (1133 passed) |
| `fold_route_evidence` | `calls +=` replaced by `=` | killed by `test_b6b_sink.py::test_the_accumulator_equals_b2s_list_aggregators[native]` |
| `OutputEvidenceAccumulator.append` | conversion ms `+=` replaced by `=` | killed by `test_b6b_sink.py::test_the_accumulator_equals_b2s_list_aggregators[oracle]` |
| spill directory resolution | missing `spill_parent` check disabled | killed by `test_b6b_sink.py::test_a_sink_without_a_spill_parent_fails_clearly_when_the_hold_back_must_spill` |
| spill directory resolution | `dir=` argument dropped (falls back to `TMPDIR`) | killed by `test_b6b_sink.py::test_a_nested_target_spills_under_its_own_spill_parent_not_tmpdir` |
| planner null-count branch | integer-with-nulls threshold `> 0` to `> 1` | killed by `test_auto_chunk_routing.py::TestFailClosed::test_int_column_with_nulls_stays_full_frame` |
| planner null-count branch | bucketize-with-nulls threshold `> 0` to `> 1` | killed by `test_auto_chunk_routing.py::TestChunkStateGates::test_bucketize_null_in_one_chunk_stays_full_frame` |
| planner null-count gap | lazy-source gap rejection disabled | killed by `test_b6b_routing.py::test_an_integer_column_without_footer_statistics_declines_and_runs_full_frame` |

19 mutants, 14 killed, 5 not killed. Surviving mutants are coverage gaps, not known product defects:

- Owner close: `_guarded` closes the handle on exhaustion, error or generator close, so only a close of `InputChunks` before the stream is first iterated (the primed generator is suspended, the chain generator never started) depends on `self._owner.close()`. No test closes an `InputChunks` before iterating it.
- `facts_match` `row_groups`: no test changes the row-group count between routing and open while keeping the same maximum row-group size and row count.
- `lazy_stream_candidates` zero-row: no test asserts the mode of a zero-row lazy table.
- `fold_timings` memory max: no test feeds chunks whose memory deltas decrease.
- `fold_corpora` first-wins: no test feeds two chunks that report different entries for the same (table, column).

## Benchmark

Run ID B6B-BENCH-2026-10-03. Harness: `scripts/bench-auto-chunk/bench_lazy.py` and `bench_worker_lazy.py`. Raw trials: `docs/records/b6b-bench-2026-10-03/results.jsonl`; summary: `docs/records/b6b-bench-2026-10-03/summary-final.json` (all_pass true, problems empty). The routing probe was pinned off in both configurations (`03890055`), so the wall comparison is fair; no trial probed. An earlier run in which the baseline probed is kept as `results.jsonl.probe-tainted.bak` and is not used.

Baseline is the resident path on main; candidate is the lazy lane. Increment is peak RSS above the pre-run level. Sizes are MiB.

| Cell | Config | n | Wall p50 / p95 (s) | Increment p50 / p95 (MiB) | Lifetime peak max (MiB) |
|---|---|---|---|---|---|
| native 2M | baseline | 10 | 4.41 / 4.83 | 497 / 499 | 666 |
| native 2M | candidate | 10 | 4.17 / 4.62 | 396 / 402 | 570 |
| native 10M | baseline | 10 | 20.30 / 21.61 | 1215 / 1227 | 1395 |
| native 10M | candidate | 10 | 19.67 / 20.68 | 465 / 471 | 639 |
| native 50M | candidate | 3 | 98.46 / 108.02 | 470 / 473 | 640 |
| oracle 2M | baseline | 5 | 40.99 / 41.10 | 588 / 590 | 757 |
| oracle 2M | candidate | 5 | 41.06 / 41.77 | 473 / 478 | 646 |
| oracle 10M | baseline | 5 | 210.20 / 219.29 | 1317 / 1318 | 1486 |
| oracle 10M | candidate | 5 | 207.36 / 213.55 | 520 / 523 | 691 |
| oracle 50M | candidate | 3 | 1031.16 / 1061.31 | 529 / 533 | 700 |
| native 10M one row group | candidate | 3 | 20.51 / 20.77 | 453 / 453 | 621 |

Bars (value against limit, from `summary-final.json`):

| Bar | Value | Limit | Result |
|---|---|---|---|
| M2_native_10M | 470.9 MiB | 530.5 MiB | pass |
| M2_native_50M | 473.2 MiB | 530.5 MiB | pass |
| Mbase_native | 402.5 MiB | 561.1 MiB | pass |
| M1_native | 470.9 MiB | 607.4 MiB | pass |
| W_native_2M | p50 ratio 0.944, p95 ratio 0.956 | see plan | pass |
| W_native_10M | p50 ratio 0.969, p95 ratio 0.957 | see plan | pass |
| M2_oracle_10M | 523.3 MiB | 606.1 MiB | pass |
| M2_oracle_50M | 533.1 MiB | 606.1 MiB | pass |
| Mbase_oracle | 478.1 MiB | 651.8 MiB | pass |
| M1_oracle | 523.3 MiB | 658.7 MiB | pass |
| W_oracle_2M | p50 ratio 1.002, p95 ratio 1.016 | see plan | pass |
| W_oracle_10M | p50 ratio 0.987, p95 ratio 0.974 | see plan | pass |
| R | 453.4 MiB | 598.9 MiB | pass |
| ceiling_native_2M | 570.0 MiB | ceiling | pass |
| ceiling_native_10M | 638.6 MiB | ceiling | pass |
| ceiling_native_50M | 639.6 MiB | ceiling | pass |
| ceiling_oracle_2M | 646.0 MiB | ceiling | pass |
| ceiling_oracle_10M | 690.5 MiB | ceiling | pass |
| ceiling_oracle_50M | 699.7 MiB | ceiling | pass |
| ceiling_native_10M_onegroup | 620.9 MiB | ceiling | pass |

All 20 bars pass.

## Size ratchets

`_pipeline.py` 679, `_planner.py` 595, `_chunked_input.py` 417 (ratchet figures from the build handback; `wc -l` on the final tree gives 679, 595 and 423 lines, so `_chunked_input.py` is 6 lines over the stated figure).
