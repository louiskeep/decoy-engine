# Mutation grading: `execution/_probe_scale.py` -- pure down-scaling math

TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`) by
`scripts/tq_mutate.py`. Like `_mem_estimate`, this module was on finding #15's
"un-gradeable subprocess substrate" list but is PURE and in-process (zero
subprocess references): it computes the down-scaled config / sources / row_counts a
memory probe runs against, all plain-Python dict/table transforms. Every mutant is
gradeable in-process with the existing tool. Public API: `scale_row_count`,
`downscale_config`, `downscale_job` (returns a `DownscaledJob` with `config`,
`sources`, `row_counts`). Not crypto/RI, so the bar is the measure-first substrate
bar; the re-grade is authoritative.

## Numbers

Baseline (existing `test_probe_scale.py`): 99/109 = 90.83% LOGIC, 0 unresolved,
10 survivors. Full triage: **all 10 survivors are LOGIC and are KILLED** by 9 tests
in a new `tests/unit/execution/test_probe_scale_kills.py` (one test kills both
`scale_row_count` boundary mutants). 0 proven-equivalent, 0 accepted non-contract,
0 residual. Re-grade with the kill file in the selection: **109/109 = 100.00% LOGIC
(tool-native, 0 unresolved, 0 survivors).**

| Function | Survivors triaged | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `scale_row_count` | 2 | 2 | 0 | 0 |
| `downscale_config` | 3 | 3 | 0 | 0 |
| `downscale_job` | 5 | 5 | 0 | 0 |
| **total** | **10** | **10** | **0** | **0** |

## Kills added by the full triage (10 via 9 tests)

Each asserts the exact machine field the mutation flips (a scaled row count, a
skip-vs-abort loop outcome, a forwarded floor, or a row_counts membership).

| mut | mutation | killed by (machine field) |
|---|---|---|
| `scale_row_count` 1 | `< 0` -> `<= 0` (row-count validation) | `TestScaleRowCountZeroBoundary` -- 0 rows is a legal input (scales to 0), not a validation error; the mutant rejects 0. |
| `scale_row_count` 2 | `< 0` -> `< 1` | same test -- also moves the guard to reject 0. |
| `downscale_config` 9 | `continue` -> `break` on the non-dict entry guard | `test_non_dict_entry_is_skipped_not_a_loop_abort` -- a junk entry is stepped over; a later generate table still scales (1M -> 10k). `break` would drop it. |
| `downscale_config` 14 | `continue` -> `break` on the no-`generate_columns` guard | `test_mask_table_is_skipped_not_a_loop_abort` -- a mask table before a generate table must not stop the loop. |
| `downscale_config` 27 | drops `floor_rows=floor_rows` (callee default 2000) | `test_floor_rows_is_forwarded_to_scale_row_count` -- with floor_rows=0 a sub-floor scale lands on its raw value (100), not the default floor. |
| `downscale_job` 7 | config call drops `floor_rows` | `test_config_floor_rows_is_forwarded` -- sub-floor generate table lands on 100, not 2000. |
| `downscale_job` 14 | sources call drops `floor_rows` | `test_sources_floor_rows_is_forwarded` -- a resident source is sliced to 100 rows, not lifted to 2000. |
| `downscale_job` 21 | `or` -> `and` in the row_counts skip guard | `test_only_generate_tables_contribute_to_row_counts` -- a mask table with a stray row_count must be excluded; `and` would admit its 777. |
| `downscale_job` 27 | `continue` -> `break` in the row_counts loop | `test_mask_table_does_not_abort_the_row_counts_loop` -- a mask table before a generate table must not stop row_counts collection. |
| `downscale_job` 36 | `and` -> `or` in the isinstance guard | `test_row_counts_requires_both_a_count_and_a_name` -- recording needs BOTH an int row_count and a str name; `or` would write `{name: None}`. |

## Candidate findings

None. No mutation exposed a wrong scaled row count, a wrong skip/abort loop
decision, a dropped floor, or a wrong row_counts membership.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_probe_scale.py`
and the test selection to `tests/unit/execution/test_probe_scale_kills.py` and
`tests/unit/execution/test_probe_scale.py`; then `rm -rf mutants && python
scripts/tq_mutate.py --run --jobs 6`. `source_paths` stays at the package root.
