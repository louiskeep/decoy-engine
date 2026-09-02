# Mutation grading: P4-A.3 order-restore (the `_stream_join.py` reorder consumer)

Scope: the P4-A.3 CHANGED units in
`src/decoy_engine/execution/out_of_core/_stream_join.py` (Tasks B/C/D + the
dennis and Codex-final remediations): the unordered join + plan guard, the
`run_ordered_join` consumer, the `_OrderedJoinRows` owning iterator + contiguity
guard, and the EXPLAIN handling. The rest of `_stream_join.py` is the Task-A
mechanical reland of the streaming-join scaffolding, exercised (and gated) by the
byte-parity suite `tests/parity/test_out_of_core_fk_parity.py`, not by this
slice's fast unit selection.

Graded via `scripts/native-testing/python_mutation_pilot.py` (mutmut generation +
standalone-pytest-per-mutant readjudication), selection
`test_out_of_core_stream_join_reorder.py` + `_unordered.py` + `_stream_join.py`.

## Numbers

**879 mutants: 588 killed, 290 survived, 1 true-timeout** (readjudication
confirmed mutmut's raw counts). The whole module is mutated, so most survivors
fall in the relanded scaffolding the fast selection does not exercise (it is
parity-covered). **0 unresolved correctness-critical logic on the P4-A.3 changed
units.**

## Correctness-critical new logic: fully pinned (0 survivors each)

- **`_verify_unordered_plan_or_raise`** (forced-parent-build + no-global-sort
  guard, run on every real drain): 0 survivors. Pinned by the plan-structure
  tests -- interposed-projection accepted, flipped build rejected, a build
  subtree that also holds a child scan rejected (EXCLUSIVE placement), an unknown
  sort operator rejected.
- **`_is_global_sort_operator`** (substring sort detection): 0 survivors.
- **`_subtree_scan_names`** (descend to scan leaves): 0 survivors.
- **`_guarded_reorder_iter`** (the 0..N-1 contiguity guard against the
  independent child-stage count, incl. the explicit null-`row_nr` reject): 0
  survivors. Pinned by lost-suffix, mid-gap, within-2-row-batch duplicate/gap,
  and null-row_nr tests -- the within-batch `if n > 1` kill was spot-confirmed by
  applying the `n > 2` mutant.
- **`_release_reorder`** (the abandonment finalizer): 0 survivors. Pinned by the
  drop-without-close cleanup test and the FD-release-on-close test.

## Accepted survivors on the changed peripheral funcs

All message prose (an `ExecutionError` `message=` mutated to `None` / paren
dropped, with the machine-consumed `code` preserved and pinned by the reject
tests), the `run_ordered_join` `merge_fan_in` default (16 -> 17; callers pass an
explicit value), and a cleanup `unregister("child_keys")` string mutation (the
per-edge connection is closed after the drain, so a failed unregister is a
no-op). None changes a sort order, a dropped/duplicated row, a leaked resource,
or a fail-closed guard. The round-1 `_run_explain_json` `code=None` survivors are
now killed by the direct malformed-EXPLAIN arity/shape tests.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/out_of_core/_stream_join.py \
  --tests tests/unit/execution/test_out_of_core_stream_join_reorder.py \
          tests/unit/execution/test_out_of_core_stream_join_unordered.py \
          tests/unit/execution/test_out_of_core_stream_join.py \
  --timeout 45
```
