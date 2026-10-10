Status: record

# Out-of-core external-reorder lane: liveness reconciliation

Written for the R3 slice (`docs/plans/2026-10-10-r3-run-context-route-executors.md` section 5.3). It states whether the out-of-core external-reorder lane is live code on current main, what evidence says so, and how it differs from the reverted OOC-B work. R3 deletes nothing on the strength of this record. Phase D consumes it before any hard delete of legacy FK paths.

Revision examined: `refactor/r3-run-context` at `7373cee9`, based on `origin/main` @ `ac6a0e8e`. The code under `src/decoy_engine/execution/out_of_core/` and `_pipeline_route_exec.py` is byte-identical to `ac6a0e8e` in that tree.

## Verdict

The lane is live. A public `run_pipeline` call reaches `_stream_driver.stream_table` whenever the out-of-core route runs with a sink, a resolvable memory budget, a resolvable disk reading, acceptable fan-in and per-edge width, and a deduplicated, null-filtered parent-key count at or above the reorder threshold. It is the P4-A Task 6 driver wired by Task 7 and merged in #126. It is a re-adaptation of the salvage design from the OOC-B branch, not a surviving piece of the reverted #107 code (details under Provenance).

## Reachability, traced by hand

Automated "zero importer" scans gave false positives here and were not used. The chain, with line numbers on the revision above:

1. `run_pipeline` (`execution/_pipeline.py`) selects the route via `resolve_execution_route` and, for `route == "out_of_core"` on a job with a mask table, returns `_route_exec.run_out_of_core_route(...)` (`_pipeline.py:526`).
2. `run_out_of_core_route` (`execution/_pipeline_route_exec.py`) resolves the memory budget (`resolve_ooc_memory_limit`), sets `temp_disk_budget_bytes` to 0.9 of `shutil.disk_usage(default_ooc_temp_root()).free` (left `None` on `OSError`), runs the memory preflight, then calls `run_fk_out_of_core(...)` with `out_of_core_reorder_threshold_rows` (`_pipeline_route_exec.py:408`).
3. `run_fk_out_of_core` (`out_of_core/_runner.py:114`, exported from `out_of_core/__init__.py`) loops over tables. For each sink-path table it calls `_route_policy.decide_route(...)` (`_runner.py:225`).
4. `decide_route` (`out_of_core/_route_policy.py`) returns `use_reorder=True` with `ReorderCaps` only when all hold: a sink; at least one incoming edge; a memory budget; a disk budget; incoming edges not above `2 * merge_fan_in`; the largest incoming relation's parent-key count at or above `threshold_rows`; and, for every incoming edge, `max_sort_payload_row_bytes` below `run_bytes_cap // (2 * merge_fan_in)`. The key count is the footer `num_rows` of the relation file, which is already deduplicated and null-filtered (`_parent_key_count`), not the source row count. The default threshold is `REORDER_PARENT_KEY_THRESHOLD = 2_000_000`, overridable per run through `out_of_core_reorder_threshold_rows`.
5. When `route.reorder_caps is not None`, `_runner.py:258` calls `stream_table(...)` (`out_of_core/_stream_driver.py:120`), which builds a `StreamFkJoiner` per incoming edge (`_stream_driver.py:560`). Otherwise `_runner.py:265` calls the batch route `_stream_table`, which uses `ChildFkBatchJoiner`.

The root table of an FK job has no incoming edge and never reorders.

## Execution witnesses

`tests/unit/execution/test_r3_ooc_reorder_reachability.py` (added by R3) drives the real public `run_pipeline` with `execution_mode="out_of_core"`, a `ParquetTransactionalSink`, a 1 GiB `out_of_core_budget_bytes`, a fixed disk reading, and spies that wrap (not replace) the real functions:

- `test_ooc_reorder_lane_reachable`: 20 distinct parent keys against a threshold of 10. `stream_table` runs for `child` only, `_stream_table` runs for `parent` only, `StreamFkJoiner` is constructed, `ChildFkBatchJoiner` is never constructed, and the sink output equals the batch route's output.
- Boundary: threshold 19 and 20 reorder, 21 does not (20 distinct keys). With 20 parent rows holding 12 distinct non-null keys, threshold 12 reorders and 13 does not, which pins the deduplicated, null-filtered decision key.
- Negatives, each keeping the batch route: the default threshold; no sink; a relation width at the per-head cap (one byte under the cap still reorders); an unresolvable memory budget (RAM detection failure); an unresolvable disk reading (`OSError`).

The over-wide case injects the width at the decision, because the pipeline's own masked keys are narrow. The real wide-key shapes are covered at the policy level in `test_route_policy_wide_key_fallback.py` (see below).

Each test was shown to fail when the behavior it guards is broken (probes applied to the source, then restored): lane made unreachable (5 tests fail), threshold `<` to `<=` (2), decision key replaced by a raw row count (1), sink check dropped (1), width admission dropped (1), disk-`None` check dropped (1).

## Cited parity and perf runs

All run on 2026-10-10 against the revision above in the companion venv (`decoy-c6a-venv`, companion ABI 3), one runner at a time:

| Suite | What it establishes | Result |
|---|---|---|
| `tests/parity/test_out_of_core_route_seam_parity.py` | Forced reorder equals `_batch_join` exactly on the sink path: orphan policies, sink plus `LazySource`, `code_set_corpora`, warning order and content, projection warn and fail, keyed mask, exact Arrow schema and metadata, composite and overlapping edges, empty/null/NaN columns, every admitted payload and parent-key strategy. | pass |
| `tests/parity/test_stream_driver_reorder.py` | Driver-level reorder parity. | pass |
| `tests/unit/execution/test_route_policy.py`, `_wide_key_fallback.py`, `_disk_posture.py` | Selection predicate (T1-T13): threshold, missing budgets, root table, resident path, fan-in boundary, deduplicated key count, wide-key fallback, invalid overrides. | pass |
| `tests/unit/execution/test_stream_driver_reorder_caps.py`, `test_stream_driver_lifecycle.py`, `test_ooc_reorder_budget.py`, `test_slim_sort_reorder_acceptance.py` | Caps, lifecycle and abort, budget arithmetic, slim-sort acceptance. | pass |
| `tests/perf/test_out_of_core_reorder_memory.py`, `_multi_edge_memory.py`, `_wide_raw_memory.py` | Real-RSS proofs in fresh subprocesses: peak RSS within 1.35x of the process ceiling while spilling, reorder selected and the batch joiner never built, at one edge and at the admitted maximum fan-in. | pass |

Combined run: 137 passed in 220 s (this set plus the new reachability file).

Limitations. These runs use small and moderate fixtures and a 1 GiB budget; the perf tests prove a bounded envelope for the reorder step, not a 100M-row end-to-end result. The memory proof is scoped to the reorder (child and join-output) step; its own docstring says the parent-relation dedup is a different, separately bounded thing. The suites run on the box's disk and RAM, so the perf envelopes are a statement about this host class. They do not exercise a real object-store sink. The over-wide admission is exercised by injection at the public level and by real wide keys only at the policy level.

## Provenance: P4-A versus the reverted OOC-B

- OOC-B fix#1, PR #107 (`d790ac10`, merged 2026-07-21), added a single-streaming-join out-of-core FK path including `_stream_driver.py`, `_stream_join.py` and `_payload_store.py`. PR #108 (`ed148156`, 2026-07-22) reverted it over a memory regression and deleted those files. The salvage lives on the branch `fix/ooc-b-memory-streaming-join`.
- P4-A re-landed the idea under a different design. The plans `docs/plans/2026-09-01-p4a2-bounded-external-sorter.md` and `2026-09-01-p4a1-fk4a-join-free-out-of-core.md` state that the 21 GB peak "was a different, reverted architecture" and that re-landing a whole-stream external reorder is P4-A.3. Task 6 (`docs/plans/2026-09-02-p4-task6-reorder-driver.md`) builds the driver on `StreamFkJoiner.run_ordered_join` over a `BoundedExternalSorter`, and Task 7 (`docs/plans/2026-09-03-p4-task7-route-seam.md`) adds `_route_policy.py` and the route seam. All of it merged in #126 (`cd6e993d`, 2026-09-07), which is where the current `_stream_driver.py`, `_route_policy.py` and `_stream_join.py` come from.
- The current `_stream_driver.py` says in its docstring that it adapts the salvage driver's structure and keeps its single-source-read shape, so the file name and the three-phase outline are shared with the reverted work. What differs is the join and ordering machinery (bounded external sort and order restore in place of a DuckDB `ORDER BY` global sort) and the route policy that keeps `_batch_join` as the default.
- Since #126, `git log` on `_stream_driver.py` shows only `9916b2c5` (an unrelated text_mask warning).

## What this record does not claim

It does not say the lane should be kept, and it deletes nothing. It does not claim the over-wide payload path is covered end to end at pipeline level, or that the perf envelopes hold at 100M rows. The decision Phase D owns is whether this evidence is enough to hard-delete the legacy FK paths that Phase D replaces; this record is the input to that decision.
