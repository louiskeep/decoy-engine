# ENGINE-PAR P2 build record: run the engine suite in parallel with xdist in gates.sh

Status: record

Branch `feat/engine-par-p2-xdist`, based on engine main after P1 (`79f229d0`, tree identical to
`f3c17c50`). Plan: `docs/plans/2026-11-06-engine-par.md` on `origin/plan/engine-par`, section 8 P2.
Test nodes are GCP `e2-standard-4` VMs through `~/bin/vm-run` (Python 3.10.20, the lock venv
`dev+lint+vault+cloud`). Nothing ran on the devbox.

## What P2 adds

- `pytest-xdist>=3.5` in the `dev` extra, lock updated by hand to the two lines `uv lock` adds (a
  newer uv rewrote unrelated markers, so that output was discarded). The `xdist` extra stays as an
  alias. DP re-pin: the dev profile is now 79 distributions and its fingerprint is
  `4e494aff...5246` (read on a fresh VM venv built by `bootstrap.sh`). It is added to
  `_CERTIFIED_STACKS`; the two older engine rows stay, since they certify snapshots fit earlier.
- `scripts/dev/_gate_workers.py` (platform port; also resolves to 1 when xdist is missing), reached
  through `_gates_report.py --workers` so `gates.sh` keeps only reporter calls (the P1 guard).
- `gates.sh`: `GATES_WORKERS`, the parallel pass (`-n N --dist loadfile`, `(expr) and not serial`),
  the serial pass (`(expr) and serial`) that always runs, `combine_rc`, the `workers:` header line,
  `--order-from`, the order map handed to the suite, a baseline tree without the scrubber stays
  serial and says so on its steps line.
- `_gates_report.py`: `_xmls` makes every `tests` read cover both JUnit files; run mode derives
  `dist` from the worker count and the order label from the baseline's own `order_seconds`.
  The baseline model moved to `_gates_baseline.py` (the module-size sentry caps scripts at 600
  lines and allows no new census keys).
- `tests/_xdist_safe_reports.py`, `short_tmp_path`, `SERIAL_DIRS = ("tests/perf",)`, the order hook
  (`DECOY_TEST_ORDER_FILE`, one stable sort by file), and the dead data-dir fixtures removed from
  `tests/conftest.py` (301 to 144 lines).
- The `serial` marker in `pyproject.toml`; marks on the tests below.
- Sentry `tests/sentry/parallel/` (scan, required-serial registry, allowlist baseline
  `serial-resources.json`), registered in `SENTRIES`, `sentry_registry.toml` and `_baseline_sources.py`.
- Docs: gates help text, `CONTRIBUTING.md`, the `pyproject.toml` xdist comment, a `ci.yml` comment.

## Marks

38 test functions or classes carry `serial` in this change, plus all of `tests/perf` through the
hook. The scan found 25 functions that read a real clock or memory source directly; 5 are in
`tests/perf` and 1 sits under the `benchmark` marker, so the gates expression never collects it.
The other 19 are marked. Further marks from the plan's inventory:
`test_gcs_timeouts.py` (20 cases), `TestRealChild` in `test_isolated_memwatch_run.py` (4), the speedup
ratio test in `test_gil_release.py`, the real-subprocess governor class, isolated-run RSS tests, the
spill-reader RSS test, four `test_fk_scale_infra.py` tests, three `test_bench_compare_harness.py`
tests, `test_unified_slice_performance.py`, and the sampler pacing test.

Deviation from the plan, stated plainly: the name vocabulary in section 6 flags 236 test functions
outside `tests/perf`, most of them byte-estimator arithmetic (`budget_bytes`, `peak_bytes`) or
recorded timing fields. Of these, 38 were confirmed and marked. The other 192 are on the allowlist,
each with a category reason (estimator arithmetic, scripted memory-pressure flag, timing field
that is only checked for presence). The reasons come from reading the compared names and sampling
files, not from reading every test. The allowlist can only shrink.

## Acceptance tests

| AT | Test | Result |
|---|---|---|
| 1, 2, 4 | `test_gates_parallel.py` worker cells | pass |
| 3 | `test_the_two_passes_partition_the_real_selection` (takes the two `-m` expressions from the argv gates.sh runs) | pass |
| 5 | reporter cells, one file against two | pass |
| 6 | `test_a_worker_crash_is_a_failing_id_of_that_test` (real xdist, `os.abort()`) | pass |
| 7 | `test_a_failing_test_that_logs_a_surrogate_runs_under_xdist` | pass |
| 8 | mode cells (existing P1 cells, adapted) plus `test_order_round_trip_...`, P1-format at one worker | pass |
| 10 | `test_a_baseline_tree_without_the_scrubber_runs_serial_and_says_so` | pass |
| 11 | sentry and seeded spellings; scan-alone run reports all 20 GCS ids | pass |
| 12 | `test_the_socket_test_passes_under_xdist_with_a_very_long_basetemp` | pass |
| 15 | order tests; mutations below | pass |
| 16, 17 | P1 cells in `test_gates_baseline_file.py` (unchanged by P2) | pass |
| 18 | `test_every_perf_item_carries_serial_at_collection`; conftest `pytestmark` cell in the sentry | pass |
| 13 | three parallel full runs, see below | see below |

Adapted P1 cells (premise changed, not weakened): "a baseline ordered by a map is refused until gates
can order" became "is compared in that order"; the order-label-mismatch cell now expects
`run mode differs in order`, which is the wording AT-8 asks for.

T1 on the final head (VM): `tests/unit/scripts`, all of `tests/sentry`, the DP row test and
`tests/fileaccess/test_policy_rooted.py`: 4399 passed, 2 skipped.

## Red mutations (VM, each restored after)

| Mutation | Went red |
|---|---|
| parallel pass without `and not serial` | `test_the_two_passes_partition_the_real_selection` |
| `junit_failing_ids` reads only the first JUnit file | `test_reporter_counts_...`, `test_write_baseline_stores_ids_from_both_passes` |
| drop the run-mode check | `test_a_parallel_baseline_records_workers_and_dist` |
| compare orders by `file_seconds` | `test_a_compare_orders_by_order_seconds_not_file_seconds` |
| sort tests (by node id) instead of files | `test_collection_puts_the_longest_files_first_...` |
| drop `tests/perf` from `SERIAL_DIRS` | `test_every_perf_item_carries_serial_at_collection` |
| remove `serial` from `test_gcs_timeouts.py` | `test_the_required_serial_registry_is_marked` |
| remove `serial` from `TestRealChild` | `test_the_required_serial_registry_is_marked` |
| misspell a registry node id | `test_the_required_serial_registry_is_marked` |
| remove the GCS marks and their registry rows | `test_every_host_resource_test_is_serial_or_allowlisted` (all 20 ids) |

The first run of the last mutation passed. Cause: the sentry named the defining file from the
function's module name, which is a bare file name where a test directory has no `__init__.py`, so
it scanned nothing. The sentry now takes the file from `inspect.getsourcefile` and asserts a source
file exists. Fix commit `7135220b`.

The stale-baseline and shrink mutations (AT-16, AT-17) belong to P1 and were not re-run.

## Speed and failing ids

Three full parallel runs of `GATES_WORKERS=4 CI=true bash scripts/dev/gates.sh --keep-going --steps tests tests`
on `e2-standard-4` VMs, same selection each time:

| Run | Head | Wall | Passed | Failed | Skipped | Failing ids |
|---|---|---|---|---|---|---|
| 1 | `8fe060f6` | 34:07 (2047 s) | 35694 | 0 | 5299 | none |
| 2 | later head | 28:58 (1738 s) | 35694 | 0 | 5299 | none |
| 3 | later head | 25:54 (1554 s) | 35694 | 0 | 5299 | none |

Run 1 shared the VM pool with a concurrent job, so it is the slowest. No same-mode baseline file
existed (`--write-baseline` needs HEAD equal to origin/main), so "no new failing id" is shown as zero
failing ids in all three runs.

The serial comparison run (`GATES_WORKERS=1`, same selection) did not finish: its process on the
test VM was gone after more than 2.5 h of contended wall time, with only an unrelated baseline job
left running. No serial wall time or serial failing-id set was measured. The plan's expected serial
time (about 2:01) is not confirmed here. AT-14 speed numbers are a P3 matter.
