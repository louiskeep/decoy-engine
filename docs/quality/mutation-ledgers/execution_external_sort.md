# Mutation grading: `out_of_core/_external_sort.py` -- logic 0 unresolved

`BoundedExternalSorter` (P4-A.2): a memory-capped external merge sort (Knuth v3
§5.4), ported from OOC-B and key-generalized. Graded via
`scripts/native-testing/python_mutation_pilot.py` (mutmut generation +
standalone-pytest-per-mutant readjudication), selection
`tests/unit/execution/test_ooc_external_sort.py` +
`tests/unit/execution/test_ooc_external_sort_generic.py`.

## Numbers

**441 mutants: 343 killed, 92 survived, 6 true-timeout** (78.85% logic on the
final run, after the Codex-final remediation). Rounds: the first run
(322 killed / 110 survived) exposed error guards no test triggered; pinning them
(below) reached 341/92; the Codex-final fixes (the per-head-cap row guard and the
bracketed peak asserts) then reached 343/92.

**0 unresolved-LOGIC survivors on the correctness-critical functions.** Diffing
every survivor in `write` / `_flush` / `_validate_key` / `__init__` /
`_merge_heads_into` against the original leaves exactly two surviving CONDITION
mutations, both a `>` -> `>=` boundary in `write` and both accepted:
`_buffered_bytes + row_bytes > run_bytes_cap` -> `>=` (a flush fires one row
earlier at the exact boundary -- the sorted output is identical, partition-
invariant, and still cap-bounded) and `row_bytes > _per_head_cap_bytes` -> `>=`
(differs only for a row of EXACTLY the per-head cap, which the original correctly
accepts as bounded-mergeable; the mutant only over-rejects it, never under-bounds
memory). Every other critical mutant -- codes, conditions on the key guards, the
budget checks, the empty-batch guard, the sort/cutoff logic -- is killed. The 6
true-timeouts are infinite-loop mutations in the merge/finish/refill logic; a
mutation that hangs the tests is DETECTED (killed by timeout), not an escape.

## Correctness-critical logic: fully pinned (the mutation-found gaps)

The first run's survivors traced to error paths NO test exercised -- their
machine-consumed `code` field survived as `code=None`. All now pinned (commit
`48650fd4` + the cap-boundary above):

- `__init__` budget guards -- `run_bytes_cap <= 0` and `merge_fan_in < 2` raise
  `out_of_core_reorder_budget_too_small`, and a positive minimum constructs
  (`test_reject_nonpositive_run_bytes_cap`, `test_reject_merge_fan_in_below_two`,
  `test_minimal_positive_cap_and_fan_in_construct`). The `resolve_reorder_budgets`
  helper tests never hit the sorter's OWN `__init__` validation -- that was the
  gap.
- `write()` after `finish()` and `iter_ordered()` before `finish()` raise
  `out_of_core_sort_invalid_state` (`test_write_after_finish_raises`,
  `test_iter_ordered_before_finish_raises`).
- The empty-batch guard (`num_rows == 0`) -- a one-row batch is not dropped
  (`test_single_row_batch_is_not_dropped`; kills the `== 1` mutation).

After pinning, no `code=None` survives on any guard and no surviving mutation
changes a `_validate_key` condition, a `write` guard condition, or a budget
condition (verified by diffing every critical-function survivor against the
original).

## Accepted survivors (adjudicated, not unresolved logic)

Per the chunked-lane ledger policy (coded/consumed fields pinned, everything else
adjudicated):

- **Message prose (20):** the `ExecutionError` `message=` value mutated to `None`
  or XX-wrapped/uppercased/re-cased in `_validate_key`, `write`, and `__init__`.
  The `code` (the machine-consumed field) is pinned by the rejection tests; the
  message is advisory.
- **Instrumentation / attribute init:** the `__init__` attribute seeds and the
  `peak_*` `max(...)` updates. The memory-cap tests now BRACKET the peaks
  (`run_bytes_cap // 2 <= peak_pre_sort_buffer_bytes <= cap`;
  `0 < peak_merge_resident_bytes <= cap`;
  `peak_pre_sort <= peak_buffered <= cap * SORT_OVERHEAD_FACTOR`), so a mutation
  that undercounts a peak toward zero now reddens the lower bound -- the earlier
  "`<= cap` is vacuous under undercount" gap (Codex final MEDIUM) is closed.
  Attribute-seed survivors that do not feed a peak or the sort output remain
  accepted (they change neither the sorted result nor the enforced envelope).
- **Cosmetic naming:** `_next_run_path`'s `_run_counter += 1` (-> `-= 1` / `+= 2`)
  and `finish`'s `pass_no` / merge-file prefix mutations only change run-file
  NAMES; uniqueness (and therefore correctness) is preserved.
- **Output-equivalent boundary / no-op:** `>` vs `>=` on the flush and
  row-too-wide thresholds (output identical + still cap-safe, partition-invariant);
  `combined.slice(0, 0)` vs `slice(1, 0)` (both empty); `missing_ok=True` vs
  `False` on files that always exist at unlink time; `continue` vs `break` on the
  size-1 merge group (only ever the LAST group, so equivalent); `_RunHead`
  `OSFile` default mode; `close()` setting `_run_paths`/`_final_run_path` to a
  different post-terminal sentinel (unused after close).
- **Bisection internals:** `_materialize` / `_iter_bounded_views` /
  `_bounded_batches` mutations that change how a batch is split but not the sorted
  output (verified equivalent by the value+schema parity tests).

## The dennis P1 (found by review, not mutation) is fixed + regression-tested

Mutation could not catch the `timestamp[ns]` merge hang because the per-type test
used `timestamp("us")` only. dennis found it; the fix keeps the cutoff a pyarrow
scalar (no `.as_py()` truncation). The per-type parity test now covers `date64`
and all four timestamp units, plus a ns key through a forced multi-pass merge
(`test_timestamp_ns_key_multipass_does_not_hang`) -- the direct hang repro,
hang-guarded. Lesson: a parametrized-type test must cover every unit/subtype of a
claimed capability.

## Candidate findings

None. Every surviving mutation is message prose, memory instrumentation, cosmetic
naming, or output-equivalent; none exposes a wrong sort order, a dropped/duplicated
row, a leaked spill file, or a broken guard.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/out_of_core/_external_sort.py \
  --tests tests/unit/execution/test_ooc_external_sort.py \
          tests/unit/execution/test_ooc_external_sort_generic.py \
  --timeout 45
```
