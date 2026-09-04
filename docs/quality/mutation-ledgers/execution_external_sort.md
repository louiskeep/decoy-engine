# Mutation grading: `out_of_core/_external_sort.py` -- logic 0 unresolved

`BoundedExternalSorter` (P4-A.2): a memory-capped external merge sort (Knuth v3
§5.4), ported from OOC-B and key-generalized. Graded via
`scripts/native-testing/python_mutation_pilot.py` (mutmut generation +
standalone-pytest-per-mutant readjudication), selection
`tests/unit/execution/test_ooc_external_sort.py` +
`tests/unit/execution/test_ooc_external_sort_generic.py`.

## Numbers

**499 mutants: 367 killed, 119 survived, 13 true-timeout** (76.2% logic:
(367 + 13) / 499, counting a hang-detected mutant as a kill). The
standalone-pytest readjudication confirmed mutmut's raw counts verbatim -- every
survivor still survived and every timeout was a true (not merely slow) hang
under a per-mutant standalone run, so no false-survivor came from test-ordering.

The count grew across the Codex remediation rounds (441 -> 456 -> 499) because
each fix added fail-closed guards and a new pure helper (`_min_row_bytes`), whose
`ExecutionError` message prose mutates into accepted survivors (the `code` is
pinned; the message is advisory -- see below). The logic percentage tracks that,
not any new unresolved logic: the gate metric is "0 unresolved
correctness-critical logic survivors", which holds.

**0 unresolved-LOGIC survivors on the correctness-critical functions.** Diffing
every survivor in `write` / `_flush` / `_validate_key` / `__init__` /
`_merge_heads_into` / `_min_row_bytes` against the original leaves only message
prose, attribute seeds, cosmetic naming, boundary/float-equivalents, safe
under-count mutations (which only ever over-reject, never under-bound), and
accepted over-rejections -- itemized below. Every condition that decides
accept/reject, sort order, or a memory bound is killed. The 13 true-timeouts are
infinite-loop mutations in the merge/refill/slice logic; a mutation that hangs
the tests is DETECTED (killed by timeout), not an escape.

## The Codex remediation (rounds 2-4), mutation-verified

The original blocker was that the resident cap did not hold for narrow keys:
`sort_by` allocates an 8-byte `uint64` index per row, so rows under 8 bytes make
that index dwarf the data the byte cap counts, in both the flush sort and the
merge sort. Round 2 added a per-batch AVERAGE-width guard + dropped the merge's
int32 source tag; round 3 found the average guard unsound (the flush sort
reorders rows, so a schema mixing narrow and wide rows can cluster narrow rows
into a post-sort run batch that a per-batch average never sees) and the merge's
`filter`-based split wasteful. The final fix, and the mutants that pin it:

- **`write` SCHEMA guard** (`_min_row_bytes(schema) < INDEX_BYTES` ->
  `out_of_core_sort_row_index_unbounded`), checked once on the first batch.
  `_min_row_bytes` is a LOWER bound over every possible row (each column's
  unavoidable per-row cost: fixed width, or a variable column's offset entry),
  so `>= INDEX_BYTES` proves every row -- and hence every reordered run batch and
  every merge emit subset -- carries at least an index's worth of data. The
  condition and its `code` are killed by three reject tests
  (`test_reject_narrow_rows_index_unbounded` int8=1B,
  `test_reject_narrow_key_only_int32` int32=4B,
  `test_reject_mixed_narrow_schema_that_could_cluster` int8+binary=5B, whose
  average is >> 8 yet is still rejected) plus two accept tests at the boundary
  (`test_exact_8_byte_rows_accepted_and_sorted` int64=8B,
  `test_accept_int32_key_with_binary_payload` int32+binary=8B). Any mutation that
  flips the `<` direction reddens an accept test; any that drops the raise
  reddens a reject test. Only the message prose survives.
- **`__init__` zero-per-head-cap guard** (`per_head_cap_bytes < 1` raises
  `out_of_core_reorder_budget_too_small`). Boundary-pinned by
  `test_reject_cap_too_small_for_per_head` (cap=3 -> per-head 0, must raise) plus
  `test_minimal_viable_cap_and_fan_in_construct` (cap=4 -> per-head 1, must
  construct).
- **tag-free, zero-copy-slice merge** (`_merge_heads_into` splits each
  already-sorted head at the cutoff with `slice`, no source-tag column, no
  `filter` copy). Correctness across a forced multi-pass merge is pinned by the
  parity tests (`test_string_key_multipass_parity`,
  `test_timestamp_ns_key_multipass_does_not_hang`, the two boundary-accept tests
  above); the 13 true-timeouts are hangs injected into this slice/refill loop,
  all detected.
- **honest merge peak metric** (`peak_merge_resident_bytes` is a coarse
  DATA-resident witness: heads + emit gather). It is NOT the enforced envelope --
  sort_by's index array is un-instrumentable through `nbytes`, so the true merge
  peak is bounded by `run_bytes_cap * SORT_OVERHEAD_FACTOR` (the same envelope
  the flush lives under, per the `// 2` per-head cap) and proved by the RSS test.
  See the accepted instrumentation survivor below.

### `_min_row_bytes` survivors are all safe (never under-bound)

For SOUNDNESS `_min_row_bytes` must be a lower bound (never over-estimate a
row's true width). Its 10 survivors are all safe: under-count mutations (`+=`
-> `=`/`-=`, `continue` -> `break`, `is_list(t)` -> `is_list(None)`) only shrink
the estimate, which can over-reject but never accept a too-narrow schema;
`// 8` -> `/ 8` is float-equivalent on every 8-divisible bit width; and the two
`+1` over-estimates (`4` -> `5` on the int32 offset, `8` -> `9` on the int64
offset, `total = 0` -> `1`) at worst accept a schema whose true minimum row is 7
bytes -- an index/data ratio of 8/7 ~= 1.14, far inside `SORT_OVERHEAD_FACTOR`.
The dangerous class (an over-estimate large enough to accept a sub-4-byte-row
schema) does NOT survive: the three reject tests hold int8 (<= 2 mutated),
int32 (<= 5), and int8+binary (<= 6) all under 8, so no survivor accepts them.

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
  `row_bytes > _per_head_cap_bytes` -> `>=` (`write__mutmut_39`) rejects a row of
  EXACTLY the per-head cap. This is NOT output-equivalent -- the original
  correctly ACCEPTS an exact-cap row as bounded-mergeable and the mutant
  over-rejects it -- but it only ever fails closed, never under-bounds memory, so
  it is an accepted survivor, not an escape. The flush threshold
  `_buffered_bytes + row_bytes > run_bytes_cap` -> `>=` (`write__mutmut_57`)
  flushes one row earlier at the exact boundary; the sorted output is identical
  and still cap-bounded, so that one is genuinely output-equivalent. The
  exact-boundary ACCEPT side of the schema guard is pinned positively by
  `test_exact_8_byte_rows_accepted_and_sorted` (a bare int64 key, exactly 8
  bytes/row) and `test_accept_int32_key_with_binary_payload` (schema min exactly
  8), both through a multi-pass merge, so the guard over-rejects nothing at its
  boundary.
- **Instrumentation / attribute seeds:** the `__init__` peak/counter seeds
  (`= 0` -> `= 1`) and the merge peak update `resident + combined.nbytes` ->
  `resident - combined.nbytes` (`_merge_heads_into__mutmut_54`). The merge peak
  is a coarse data-resident witness, not the enforced envelope (see above): the
  first per-round `max(peak, resident)` update already holds the metric at
  `resident`, so the mutated second update only reverts it to the heads-only
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

## Path note (P4 HIGH-1 decomposition, 2026-09-03)

`_min_row_bytes`, `_is_supported_key_type`, `_materialize`, `_iter_bounded_views`,
and `_bounded_batches` moved to the sibling `_external_sort_bounding.py`, and
`_RunHead` to `_external_sort_run.py` -- pure moves (module-size decomposition
only), no logic change, so the grading above still holds. Regrading the split
would need one `--module` invocation per sibling.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/out_of_core/_external_sort.py \
  --tests tests/unit/execution/test_ooc_external_sort.py \
          tests/unit/execution/test_ooc_external_sort_generic.py \
  --timeout 45
```
