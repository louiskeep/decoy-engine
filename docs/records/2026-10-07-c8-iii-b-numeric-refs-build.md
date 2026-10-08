Status: record (build complete; awaiting the review gates and Cam's merge call)

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

## Rev 4 (test oracle follows the auto-chunk output contract)

Plan rev 4 (1ac61943) corrects test 1: the `when` mask and the masked column `s` equal the whole-frame run; names, order and row count equal the whole frame; every passthrough column, each `when` reference included, equals the SOURCE column (Arrow field with metadata, plus values). `_assert_output_contract` implements exactly that, with no column excluded. The float NaN, large_string and date64 differences above are therefore the approved contract and are no longer failures.

Test setup corrections made on the way: a uint64 literal above int64 is outside the closed grammar, so the planner declines it and both runs raise (the case carries `in_grammar=False`; generated uint64 predicates use in-range literals).

## Pin changes (final list)

- `test_c8_i_when_auto_route.py::test_a_numeric_reference_stays_full_frame` renamed `..._with_unstable_chunks_stays_full_frame`, reference is now a bool with nulls.
- `tests/unit/execution/test_auto_chunk_routing.py::TestChunkStateGates` (`_when_bearing_config`): `amount` was a null-free int with `amount > 30`, which now auto-chunks; it is now a bool with nulls and `amount == True`, so both tests keep their intent (still full-frame, still rejected by the planner).
- `tests/unit/execution/test_multi_table_when.py::_when_case`: same change (bool with nulls, `amount == True`).
- `test_planner_mutation_kills.py` and `test_c8_i_when_declines.py` unchanged.

## Checks

Finished:
- New file plus old pins: 268 passed, 8 skipped (Python 3.11 native venv, `~/bin/pytest-one`).
- Sentries (after commit): 2451 passed, 1 skipped. `_when_admission.py` is 261 LOC, under the 600 census threshold.
- Full `tests/` on Python 3.11: 25565 passed, 4 failed, 180 skipped. Three failures were the pins fixed above (re-run after the fix: 90 passed across the two files). The fourth, `test_v2_cloud_sources.py::test_profile_gcs_source_via_mocked_client`, fails with `ModuleNotFoundError: No module named 'google'` (the native venv lacks the cloud extra); not caused by this change, not re-run on main.
- Testflight (check mode): 53/53 invariant checks passed, FINGERPRINTS 5/5 match golden.
- ruff check and format: clean. mypy on changed files: no errors in them (5 pyarrow-stub errors in untouched files, 3.10 venv).

Mutation (manual harness, since mutmut is not installed; scratch script, not committed). 24 mutants over `_stable_when_reference` and the `all(...)` line, each run against the new file plus the old pins through `~/bin/pytest-one`:
- null-count comparison (4), int/bool branch (4), each allowed type dropped (6), time, duration, decimal, dictionary, nested, binary and null types added (7), reference-not-in-schema check ignored (1): all 22 KILLED.
- `all` to `any`: SURVIVED at first. That exposed a missing test (one unstable reference beside a stable one). Added `test_one_unstable_reference_declines_even_beside_a_stable_one`; the mutant is now KILLED.
- Dropping the `refs and` guard: SURVIVES, and is equivalent. A natively admitted `when:` column has a parsed predicate (rule 3) and the closed grammar needs a column in every comparison, so `refs` is never empty on this path.
- Score: 23 of 24 killed, 1 equivalent survivor (23/23 on non-equivalent mutants).

The cloud-source failure (`test_profile_gcs_source_via_mocked_client`) fails identically on main 4a08f570 in the same venv (`ModuleNotFoundError: No module named 'google'`, from a `git archive` export), so it is an environment gap and not this change.
