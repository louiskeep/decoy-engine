# Mutation grading: `out_of_core/_external_sort.py` -- logic 0 unresolved

`BoundedExternalSorter` (P4-A.2): a memory-capped external merge sort (Knuth v3
§5.4), ported from OOC-B and key-generalized. Graded via
`scripts/native-testing/python_mutation_pilot.py` (mutmut generation +
standalone-pytest-per-mutant readjudication), selection
`tests/unit/execution/test_ooc_external_sort.py` +
`tests/unit/execution/test_ooc_external_sort_generic.py`.

## Numbers

**456 mutants: 341 killed, 109 survived, 6 true-timeout** (76.1% logic:
(341 + 6) / 456, counting a hang-detected mutant as a kill). The
standalone-pytest readjudication confirmed mutmut's raw counts verbatim -- every
survivor still survived and every timeout was a true (not merely slow) hang
under a per-mutant standalone run, so no false-survivor came from test-ordering.

The count rose from the previous 441 mutants because the Codex-round-2 fix added
two new fail-closed guards (the narrow-row index guard in `write`, the
zero-per-head-cap guard in `__init__`), each carrying an `ExecutionError` whose
message prose mutates into a handful of accepted survivors (the `code` is
pinned; the message is advisory -- see below). The logic percentage is slightly
lower for the same reason (more advisory-prose mutants), not from any new
unresolved logic: the metric that governs the gate is "0 unresolved
correctness-critical logic survivors", which holds.

**0 unresolved-LOGIC survivors on the correctness-critical functions.** Diffing
every survivor in `write` / `_flush` / `_validate_key` / `__init__` /
`_merge_heads_into` against the original leaves only message prose, attribute
seeds, cosmetic naming, redundant-guard-equivalent boundaries, and accepted
over-rejections -- itemized below. Every condition that decides accept/reject,
sort order, or a memory bound is killed. The 6 true-timeouts are infinite-loop
mutations in the merge/refill logic; a mutation that hangs the tests is DETECTED
(killed by timeout), not an escape.

## The Codex-round-2 fix, mutation-verified

The round-2 blocker was that the resident cap did not actually hold for narrow
keys: `sort_by` allocates an 8-byte `uint64` index per row, so a run of rows
under 8 bytes each makes that index dwarf the data the byte cap counts, in both
the flush sort and the merge sort (the merge also carried a 4-byte int32
source-tag column, itself unbounded relative to a narrow key). The fix, and the
mutants that pin it:

- **`write` narrow-row guard** (`num_rows * INDEX_BYTES > row_bytes` ->
  `out_of_core_sort_row_index_unbounded`). The condition and its `code` are
  killed: `test_reject_narrow_rows_index_unbounded` (a bare int8 key, 1 byte/row)
  and `test_reject_narrow_key_only_int32` (int32 key, 4 bytes/row) redden any
  mutation that flips the direction or drops the raise; only the message prose
  (`write__mutmut_40`,`42`,`45`-`53`) survives, `code`-pinned.
- **`__init__` zero-per-head-cap guard** (`per_head_cap_bytes < 1` raises
  `out_of_core_reorder_budget_too_small`). Killed and boundary-pinned by
  `test_reject_cap_too_small_for_per_head` (cap=3, fan_in=2 -> per-head 0, must
  raise) plus `test_minimal_viable_cap_and_fan_in_construct` (cap=4 -> per-head
  1, must construct): together they fix the `< 1` boundary exactly (a `< 2`
  reddens the construct test, a dropped guard reddens the too-small test).
- **tag-free merge** (`_merge_heads_into` splits each already-sorted head at the
  cutoff, no source-tag column). Correctness across a forced multi-pass merge is
  pinned by the value+schema parity tests (`test_string_key_multipass_parity`,
  `test_timestamp_ns_key_multipass_does_not_hang`,
  `test_exact_8_byte_rows_accepted_and_sorted`); the 6 true-timeouts are hangs
  injected into this loop, all detected.
- **honest merge peak metric** (`peak_merge_resident_bytes` now measures
  `resident + combined.nbytes`, the co-loaded heads plus the emit gather held
  concurrently, not just the pre-split heads). See the one accepted
  instrumentation survivor below.

## Correctness-critical logic: fully pinned

- `__init__` budget guards -- `run_bytes_cap <= 0`, `merge_fan_in < 2`, and the
  new per-head-cap-rounds-to-zero guard all raise
  `out_of_core_reorder_budget_too_small`
  (`test_reject_nonpositive_run_bytes_cap`, `test_reject_merge_fan_in_below_two`,
  `test_reject_cap_too_small_for_per_head`,
  `test_minimal_viable_cap_and_fan_in_construct`).
- `write()` after `finish()` and `iter_ordered()` before `finish()` raise
  `out_of_core_sort_invalid_state` (`test_write_after_finish_raises`,
  `test_iter_ordered_before_finish_raises`).
- The empty-batch guard (`num_rows == 0`) -- a one-row batch is not dropped
  (`test_single_row_batch_is_not_dropped`).
- Key contract (`_validate_key`): missing / null / float / unsupported /
  drifting keys each raise their own `code` (the reject tests); only the message
  prose survives.

## Accepted survivors (adjudicated, not unresolved logic)

- **Message prose (~55):** an `ExecutionError` / assert `message=` mutated to
  `None`, XX-wrapped, or re-cased across `write`, `__init__`, `_validate_key`,
  and the budget helper. The machine-consumed `code` is pinned by the rejection
  tests; the message is advisory.
- **Redundant-guard-equivalent boundaries:** `__init__`'s `run_bytes_cap <= 0`
  mutated to `< 0` or `<= 1` (`__init__mutmut_3`,`4`) still rejects the same
  inputs, because the new zero-per-head-cap guard is a backstop that rejects
  cap<=0 (and cap=1) with the SAME `code` -- no observable difference. The
  `//`->`/` mutation (`__init__mutmut_25`) makes the per-head cap a float that is
  numerically equal on every integer byte count, so no row's accept/reject
  changes.
- **Accepted over-rejections (fail-closed, never under-bound):** `write`'s
  `row_bytes > _per_head_cap_bytes` -> `>=` (`write__mutmut_21`) rejects a row of
  EXACTLY the per-head cap. This is NOT output-equivalent -- the original
  correctly ACCEPTS an exact-cap row as bounded-mergeable and the mutant
  over-rejects it -- but it only ever fails closed, never under-bounds memory, so
  it is an accepted survivor, not an escape. (This corrects the earlier ledger's
  looser "output-equivalent" wording, per Codex round-2.) The flush threshold
  `_buffered_bytes + row_bytes > run_bytes_cap` -> `>=` (`write__mutmut_56`)
  flushes one row earlier at the exact boundary; the sorted output is identical
  and still cap-bounded, so that one is genuinely output-equivalent. The
  exact-boundary ACCEPT side of the new index guard is pinned positively by
  `test_exact_8_byte_rows_accepted_and_sorted` (a bare int64 key, exactly 8
  bytes/row, must sort through a multi-pass merge), so the guard is proven to
  over-reject nothing at its own boundary.
- **Instrumentation / attribute seeds:** the `__init__` peak/counter seeds
  (`= 0` -> `= 1`) and the merge peak update `resident + combined.nbytes` ->
  `resident - combined.nbytes` (`_merge_heads_into__mutmut_47`). The merge peak
  is instrumentation, not the enforced envelope: the memory-cap tests bracket it
  (`0 < peak_merge_resident_bytes <= run_bytes_cap`) and the first per-round
  `max(peak, resident)` update already holds the metric at `resident`, so the
  mutated second update only reverts the metric to the (less honest) heads-only
  value without lowering it below `resident` or changing a sorted row. The TRUE
  end-to-end memory bound -- including sort_by's own un-instrumentable index
  array -- is proved by the subprocess RSS test
  (`tests/perf/test_ooc_external_sort_memory.py`), not by this nbytes counter.
- **Cosmetic naming:** `_run_counter` seed / increment and merge-file prefix
  mutations only change run-file NAMES; uniqueness (and correctness) is
  preserved.
- **`finish` / seed no-ops:** `_finished = False` -> `None` (both falsy),
  `_final_run_path = None` -> `""` (overwritten by `finish()` before any
  `iter_ordered` None-check is reachable), the size-1 merge-group
  `continue`/`break` (only ever the LAST group), `combined.slice(0, 0)` ->
  `slice(1, 0)` (both empty).
- **`mkdir` argument robustness:** `_spill_dir.mkdir(parents=True,
  exist_ok=True)` with `parents`/`exist_ok` mutated to `False`/`None`/absent
  (`__init__mutmut_60`-`65`). These survive because every test spills into a
  fresh directory whose parent already exists; they change behavior only for a
  pre-existing spill dir or a missing intermediate parent, neither a
  sort-correctness path.
- **Bisection internals:** `_materialize` / `_iter_bounded_views` /
  `_bounded_batches` mutations that change how a batch is split but not the
  sorted output (verified equivalent by the value+schema parity tests).

## The dennis P1 (found by review, not mutation) stays fixed + regression-tested

Mutation could not catch the `timestamp[ns]` merge hang because a per-type test
used `timestamp("us")` only. dennis found it; the fix keeps the cutoff a pyarrow
scalar (no `.as_py()` truncation). `test_timestamp_ns_key_multipass_does_not_hang`
(SIGALRM-guarded) is the direct hang repro. Lesson: a parametrized-type test
must cover every unit/subtype of a claimed capability.

## Candidate findings

None. Every surviving mutation is message prose, an instrumentation/attribute
seed, cosmetic naming, a redundant-guard-equivalent boundary, an accepted
fail-closed over-rejection, or an output-equivalent no-op; none exposes a wrong
sort order, a dropped/duplicated row, a leaked spill file, a broken guard, or an
under-bounded memory envelope.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/out_of_core/_external_sort.py \
  --tests tests/unit/execution/test_ooc_external_sort.py \
          tests/unit/execution/test_ooc_external_sort_generic.py \
  --timeout 45
```
