# Mutation grading: bounded-parent split-dedup (`_relation.py::_build_relation`)

Scope: the CHANGED unit for the split-dedup slice is `_build_relation` in
`src/decoy_engine/execution/out_of_core/_relation.py` (the three-statement
STAGE -> `max(row_nr) GROUP BY` winners -> JOIN-BACK form that replaced the one
combined query). The rest of `_relation.py` (`build_parent_key_relation*`,
`_parent_key_batches`, `_relation_staging_batches`, `_AlignedMaskedCursor`, the
scalar helpers) is pre-existing and unchanged by this slice; its survivors are
out of this slice's mutation scope.

Graded via `scripts/native-testing/python_mutation_pilot.py` (mutmut generation +
standalone-pytest-per-mutant readjudication), selection
`test_out_of_core_relation_split_dedup.py` + `test_out_of_core_relation.py`.

## Numbers

**485 mutants: 333 killed, 152 survived, 0 true-timeout** (readjudication
confirmed mutmut's raw counts). The whole module is mutated, so most survivors
fall in the pre-existing functions the fast selection does not fully exercise.
**`_build_relation` (the changed unit): 5 survivors, 0 unresolved
correctness-critical logic.**

## `_build_relation` survivor adjudication (all accepted)

- **mutant 51** `memory_limit=memory_limit` -> `memory_limit=None`, and
  **mutant 53** drops the `memory_limit=` kwarg. `memory_limit` is DuckDB's
  spill-threshold tuning knob: it changes WHEN/WHETHER DuckDB spills, never an
  output VALUE. The value + structural (EXPLAIN) suite produces byte-identical
  output with or without it at the tested row counts, so these are
  output-equivalent here. The bounding EFFECT of `memory_limit` is what the
  opt-in `benchmark` RSS proof measures, not the correctness suite.
- **mutant 59** `conn.register("parent_keys", ...)` ->
  `conn.register("PARENT_KEYS", ...)`. Equivalent mutant: DuckDB folds unquoted
  identifiers case-insensitively, so the later `COPY parent_keys TO ...`
  resolves to the registered view regardless of case.
- **mutants 82, 83** `staged_path.unlink(missing_ok=True)` -> `None` / `False`
  in the `finally` cleanup. `missing_ok` only changes behavior when the scratch
  file is ABSENT at cleanup (an early-abort before the staging COPY writes it);
  in every tested path the file is present, so the guard is unobservable. This
  is the same defensive cleanup surface dennis accepted as LOW-2 (loud-fail on a
  post-success IO error is pre-existing and defensible), and matches the
  external-sorter ledger's accepted "missing_ok on always-present files" class.

No survivor changes a masked value, a dedup winner, the fail-closed
partial-output removal, or the independent scratch-cleanup guards; those are all
killed.

## Regenerate

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/out_of_core/_relation.py \
  --tests tests/unit/execution/test_out_of_core_relation_split_dedup.py \
          tests/unit/execution/test_out_of_core_relation.py \
  --timeout 45
```
