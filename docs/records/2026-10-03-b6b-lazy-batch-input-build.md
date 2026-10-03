# B6b LazySource batch input: build record (partial)

Status: record
Date: 2026-10-03. Branch `feat/b6b-lazy-input`. Plan: `docs/plans/2026-10-02-b6b-lazy-batch-input.md` (revision 2.1). Builder: Sonnet. Nothing is pushed or merged. The build is stopped for an owner decision, see "Stop".

## Commits

- `816cbc43` tests first (acceptance tests 1 to 11, 13, 14 and the plan-named existing-test edits).
- `bf0ec80c` implementation, docs, sentry lists, and the authorized edit to `test_b6a_output.py::test_live_chunks_stay_within_one_row_group_plus_one`.
- `d544d707` benchmark harness.
- A later commit with three authorized existing-test edits (below).
- The commit holding this record and the partial benchmark artifacts.

## Red before

On `816cbc43` with no implementation: the six smaller b6b files gave 117 failed, 12 passed; `test_b6b_equality.py` gave 679 failed, 1 passed (660 of them the lazy run's sink calls differing from the twin's). Failure reasons were the missing module and classes, the lazy refusal text, and spills into a read-only `TMPDIR`. Tests that already passed before the change: the planner no-data-page test, the JSON-safe payload test, the zero-spill streaming test, missing-source-at-routing, transform-once, the small worker job and the low-cap completion.

## Existing tests changed, each forced by the plan

Plan-named: the dispatcher lazy-routing test, the split lazy test, the planner lazy-source test (plus a sibling for a missing footer null count), the physical-plan lazy marker, the four B6a hold-back spill tests with `_b6a_support.patched_tempdir` removed.

Authorized in session:
- `test_b6a_output.py::test_live_chunks_stay_within_one_row_group_plus_one`: iterated per-chunk results, which the online accumulator no longer keeps. It now checks only the chunk count. The no-output-retained guarantee moved to the weakref and live-object checks in `test_b6b_sink.py`; it is not weakened.
- `test_multi_table_run.py` evidence tests: `"input"` added to the exact key set (guarantee 5).
- `test_multi_table_when.py[lazy_source]`: `expect_dispatched=True` (guarantee 1 reverses the old refusal).
- `test_isolated_run.py::TestMemCapOom`: the OOM test now runs with `auto_chunk=False` so the job stays resident and still classifies `oom_killed`; a sibling pins that the same job on auto-chunk streams and completes under the same 768 MiB cap.

## Counts

- Full suite, once, native venv, before the three last test edits: 5 failed, 21328 passed, 168 skipped, 21 deselected, 59 xfailed in 29:40. The failures were the known gcs test and the four ids those edits cover. The three edited files then passed (77).
- Companion absent (plain `.venv`, pyarrow 24.0.0): the seven b6b files plus three B6a files, 620 passed, 418 skipped. The pre_buffer omission test passes on both pyarrow versions.
- ruff check and format clean; mypy shows the same 6 errors in 2 files (opendp stubs, `decoy_engine.internal.logger`) as main `649f8fc1`.

## Coverage

Line and branch, `.venv-decoy` (no companion), b6b plus B6a plus multi-table run tests: `_chunked_input` 93%, `_chunked_output_sink` 98%, `_planner` 94%, `_pipeline_auto_chunk` 91%, `_pipeline_multi_table` 92%, `_pipeline_generate_mask` 94%, `_pipeline_chunk_route` 95%. Two b6b tests that need the companion (unified-slice evidence) failed under the coverage wrapper because the conftest skip did not trigger there; they skip in a normal companion-absent run and pass with the companion. Mutation: not run. mutmut cannot grade these modules (see `[tool.mutmut]` in `pyproject.toml`); the hand-mutation substitute is still to do.

## Benchmark (partial) and the stop

Harness: `scripts/bench-auto-chunk/bench_lazy.py` and `bench_worker_lazy.py`. Raw trials: `docs/records/b6b-bench-2026-10-03/results.jsonl`.

| Cell | config | n | wall p50 / p95 (s) | increment p50 / p95 (MiB) | lifetime peak max (MiB) |
|---|---|---|---|---|---|
| native 2M | baseline | 10 | 5.79 / 6.64 | 495 / 498 | 666 |
| native 2M | candidate | 10 | 4.68 / 5.06 | 402 / 402 | 570 |
| native 10M | baseline | 10 | 31.54 / 35.43 | 1186 / 1191 | 1358 |
| native 10M | candidate | 10 | 21.63 / 23.37 | 468 / 472 | 640 |
| oracle 2M | baseline | 5 | 45.88 / 46.44 | 583 / 585 | 753 |
| oracle 2M | candidate | 5 | 44.36 / 44.50 | 472 / 476 | 644 |

Every computed bar passes on these cells (M-base, M1 native, M2 native 10M, W, ceilings). Oracle 10M, the one-row-group cell and both 50M cells did not run.

Stop: the plan says no trial in either configuration may run the routing probe, and a probe is a plan finding. At native 10M all ten baseline (resident) trials ran the routing probe (`probe_ran` true); candidate trials never did. This is plan Owner question 2 materializing: the baseline pays for the probe on today's resident path, so its wall time at 10M includes wasted probe time and the wall ratios (0.69 and 0.66) are flattered. The memory bars are unaffected. The driver did not stop the cell on its own; the 10M result must be read with that caveat or rerun after the probe is addressed. The benchmark was killed during oracle 10M.
