Status: record (build STOPPED at the test gate; not ready to merge)

Plan: `docs/plans/2026-10-07-c8-iii-b-numeric-refs.md` (rev 3).

# C8-iii-b build record (partial)

## What was built

- `planner_relaxed_when_columns` relaxes to the section 2a allow-list through `_stable_when_reference`. Integer and bool references need `facts_for(...).null_count(name) == 0`; `None` never relaxes. The target stays `pa.string()`.
- Docs: CHANGELOG entry and the `when:` section of `docs/strategies.md`.
- Tests: `tests/native/test_c8_iii_b_numeric_refs.py`, written first.

## Red-before proof

The test file was committed on top of the unchanged source (e4e5252e), exported with `git archive` and run from a scratch tree: 159 failed, 32 passed, 8 skipped. The failures are `when_predicate_not_chunk_stable` (the auto run stays full-frame) plus the allow-list unit pins. The 32 passes are the still-declined cases and the both-raise cases, which hold on the old code too.

## Old pin changes

- `test_c8_i_when_auto_route.py::test_a_numeric_reference_stays_full_frame` became `test_a_numeric_reference_with_unstable_chunks_stays_full_frame`: the reference is now a bool column with nulls, so the full-frame expectation and its reason assertion are unchanged. Module docstring updated.
- `test_planner_mutation_kills.py` and `test_c8_i_when_declines.py:321` needed no change (they call `_whole_column_state_rejections` directly and the reason set did not change).

## Stop: mismatch in the plan's byte-for-byte comparison

With the implementation in place, every mask and every `when` target column equals the whole-frame run (all admitted cases, resident and lazy, chunk sizes 7 and 5). The full-output comparison still fails for three PASSTHROUGH reference columns, and each difference reproduces on the unchanged source with no `when:` at all, so it belongs to the existing auto-chunk path, not to this relaxation:

| Reference | auto-chunked output | whole-frame output |
|---|---|---|
| float64 holding NaN (and nulls) | NaN stays NaN | NaN becomes null |
| large_string | `large_string` | `string` |
| date64 | `date64[ms]` | `date32[day]` |

Per the plan's rules the tests are not narrowed and the mismatch is not called benign. Open decision: fix the whole-frame/auto-chunk output representation at its source (a separate slice), or amend the plan's comparison for these types. Nothing past the test gate (sentries, full suite, testflight, mutation) was run.
