Status: record

# C8-iii-a build record: `when:` for text_redact, bucket_perturb and date_shift

Plan: `docs/plans/2026-10-07-c8-iii-a-when-operators.md` (rev 3, Codex plan gate GO). Branch `feat/c8-iii-a-when-operators` off engine main `2ed9eb4c`. Not pushed, not merged.

## What was built

- **3a, admission.** `ADMITTED_WHEN_STRATEGIES` (`native/_when_admission.py`) gained text_redact, bucket_perturb and date_shift. Each still passes its own config gate, so NER text_redact, implicit-format bucket_perturb and date_shift, date_shift with `group_by` and windowed_date keep declining with their codes. Both routes read the one verdict, so the unified route and the auto-chunk planner follow without further change.
- **3a-ii, the source fix.** `bucket_perturb_config_rejection` returns `bucket_perturb_special_date_format:<col>` for `date_format` `mixed` and `ISO8601`, masked or not. The constant already existed for date_shift and moved above the function.
- **3b, row errors.** `run_kernel_step_masked` maps the kernel's subset positions to chunk or batch positions with the selected-row index (`np.flatnonzero(mask)[p]`) and returns them in `StepResult`. The chunked loop keeps them chunk-local. The unified coordinator rebases them once through `rebase_row_errors`. No change was needed in either consumer.
- **3c, degenerate outputs.** No code change. The intermediate Arrow `null` retype in `_chunk_masking` stays, and the emitted chunked schema is `string` through `when_pinned_columns`. The unified reconstruction already replays the write-back.
- **One more rejection site, `reject_bucket_perturb_when`** (`_chunked_bucket_perturb.py`). It is called from `check_chunked_compatibility`, which `run_mask_chunked` runs on every call before routing, so it blocked every bucket_perturb `when:` column before admission was consulted. It now skips a column whose predicate parses under the closed grammar and whose config passes `bucket_perturb_config_rejection`, which is exactly the set the native route admits. Every other bucket_perturb `when:` column still raises `chunked_bucket_perturb_when_not_supported`, including special formats (zero-match predicates too), implicit formats, a missing namespace and a raw predicate outside the grammar. This also lets the chunked oracle leg run an admitted column, which the differential tests need as their lane-off comparator. Rejection sites touched: this one only; `_chunked.py` still calls it unchanged.

## Judgment calls

1. **Gate exemption by config, not by data.** The exemption sees only config (no schema). A non-string source on an exempted column is still refused by the existing chunk-schema gate, and a closed-grammar predicate has no whole-column reduction, which was the hazard the gate named.
2. **Unified row errors with no quarantine.** `finalize_validators_and_quarantine` raises on any row error and the lane reroutes, so a successful lane run never returns row errors, and a `quarantine:` config declines the lane. Test 2 on the unified route therefore spies the coordinator's `row_errors` (table-global, offset added once) and checks them against the oracle's records on `RowErrorsFailedError`, as `test_shadow_date_shift` does. The unselected-row case is a normal differential.
3. **Special-format decline code.** The chunked entry's reroute reason is `fallback_policy_not_native:<col>:python_only`, which hides the gate code. Tests assert the code through `native_route_eligibility(...).rejections` and the config gate directly.
4. **Golden for jobs that succeeded natively on main.** `tests/native/_c8_iii_a_main_goldens.json` holds main's native output for the unmasked special-format jobs (ordinary dates, uniform offset, all-null, empty; both formats; both routes), recorded before any source change. Tests compare the new oracle-route output to it, schema type, values and pandas metadata included.
5. **Equivalent mutant.** See Mutation.

## Tests

- `tests/native/test_c8_iii_a_when_chunked.py` and `tests/physical/test_c8_iii_a_when_unified.py`: 311 tests, written first. Against the unmodified source 269 failed for the right reasons (oracle ran where native was required, or the lane was active where the new gate must decline) and 44 passed (decline guards and unmasked special formats that main already sent to the oracle by exception). A first red run was invalid because pytest's `pythonpath` puts the worktree `src` first; the red run was redone with the implementation patch reverted.
- Test 6 runs twelve decline configs (NER, implicit formats, `group_by`, positional categorical and Faker, deterministic Faker, group_key, text_mask, windowed_date, top_code, code_set); each outcome equals the oracle's, and nine are exceptions with their existing codes.

### Test 7: old decline tests changed

Each changed only where its config is now admitted; each decline it pinned is kept on a config that still declines.

| Test | Change |
|---|---|
| `test_c6c_i_text_redact_chunked.py::test_a_when_predicate_keeps_text_redact_off_the_native_route` | Config is text_redact with `token: 7` (the config gate declines it); same assertions. |
| same file, `test_a_text_redact_column_with_a_when_predicate_is_not_string_pinned` | Same `token: 7` config; added `test_an_admitted_text_redact_column_with_a_when_predicate_is_string_pinned`. |
| `test_c8_i_when_declines.py::test_a_non_admitted_strategy_or_config_declines_with_the_legacy_code[text_redact]` | Param is text_redact with `token: 7` (id `text_redact_non_string_token`). |
| `test_chunked_bucket_perturb_admission.py::test_a_when_predicate_is_rejected_with_its_exact_code` | Now parametrized over five configs that still decline (mixed, ISO8601, implicit format, no namespace, raw predicate); added a test that an admitted config passes the gate. |
| `test_chunked_date_shift_admission.py::test_a_when_predicate_keeps_the_table_off_the_native_route_and_off_auto_chunk` | Config is implicit-format date_shift; same assertions. |
| `unit/execution/test_bucket_perturb_chunked.py::_when_bearing_bucket_perturb_cfg` (feeds `test_when_rejected_manual_entry` and `test_when_rejected_direct_unit`, plus the auto-route test) | Helper uses `date_format="mixed"`. |
| `test_planner_mutation_kills.py:148` | No change needed; it passes. |
| `test_c8_ii_unified_when.py:733, 740` | No change needed; both pass. |

Two failures outside the plan's list had the same cause and got the same treatment:

| Test | Change |
|---|---|
| `test_chunked_date_shift_types_errors.py::test_a_non_admissible_date_shift_is_not_pinned[when]` | Predicate is `d.notnull()` (outside the grammar, still declined); id `when_outside_grammar`. |
| `test_c6c_i_text_redact_unified.py::test_a_when_predicate_declines_the_lane_and_leaves_output_unchanged` | Config is `token: 7`; the assertion that the column is unchanged now covers every row. |

## Mutation (hand, against the two new files plus the bucket admission, c8-i decline and bucket unit files)

| Mutant | Result |
|---|---|
| admit: drop text_redact / bucket_perturb / date_shift (three) | killed (matrix) |
| remap: positions left subset-relative | killed (test 2 chunk-local) |
| remap: positions dropped | killed (test 2) |
| remap: off by one | killed (test 2) |
| gate: special formats admitted | killed (test 4) |
| gate: only `mixed` rejected | killed (test 4, ISO8601) |
| chunked gate: exemption always on | killed (test 4 masked special) |
| chunked gate: exemption never | killed (matrix) |
| chunked gate: grammar check dropped | killed (admission unit) |
| retype: all-null bucket chunk not retyped | survived the masked tests, killed by 70 existing unmasked C2 tests |

The retype mutant is equivalent for a `when:` column: `normalize_chunk` pins the emitted type to `string` either way. It matters only for an unmasked column, where the existing C2 parity tests catch it.

## Counts and gates

Python 3.11 with the Rust companion (`pytest-one`, `-p no:randomly`), on the code tree of the last source commit (later commits touch only the sentry allowlist entry, tests and docs, and the sentries were re-run after the final commit):

| Suite | Result |
|---|---|
| tests/native | 6186 passed, 1 skipped |
| tests/physical | 1991 passed, 1 skipped |
| tests/unit/execution | 6513 passed, 5 skipped |
| tests/parity | 352 passed, 6 skipped, 59 xfailed |
| tests/perf | 15 passed, 10 deselected |
| tests/sentry (3.11) | 2442 passed, 1 skipped |
| tests/sentry (3.10) | 2442 passed, 1 skipped |

`ruff check src tests`, `ruff format --check src tests` and `mypy src` (3.10 mirror venv) are clean. The testflight (`scripts/test_flight.py`, check only) passed 53 of 53 checks and `FINGERPRINTS: 5/5 match golden`. The physical-seam sentry needed one permitted entry, `execution/_chunked_bucket_perturb.py`. No module needed a census entry.

## Perf record (not a gate)

1M rows, `when: k == 'x'` at 10% selectivity, one column per operator, best of two, native companion. Output is byte-equal across the three arms (values compared).

| Operator | pandas (`auto_chunk=False`, lane off) | native chunked (auto-chunk) | unified (`auto_chunk=False`) |
|---|---|---|---|
| text_redact | 2.74 s | 2.71 s | 4.05 s |
| bucket_perturb | 8.42 s | 0.45 s | 0.67 s |
| date_shift | 0.71 s | 0.39 s | 0.64 s |

text_redact gains nothing on the chunked route (the span scan is pure Python in both) and is slower on the unified route, where the Arrow-to-pandas reconstruction costs more than the saved work. That is a pre-existing cost of the unified route for text_redact, not something `when:` adds.

## Not verified

- No mixed generate+mask run with a `when:` date_shift or bucket_perturb column.
- The unified route's row-error path is checked through the coordinator spy; a successful lane run cannot carry row errors (see judgment call 2).
- The planner's auto-chunk decision for the three operators is covered by the shared verdict and the existing auto-route tests, with no new planner test.

## dennis gate and remediation

dennis returned NO-GO with 0 BLOCKER, 1 HIGH, 2 MEDIUM and 4 LOW. The block was a wrong claim in the release notes, not a code defect: every code probe matched.

- **HIGH (CHANGELOG):** the Fixed entry said special-format bucket_perturb jobs "now succeed". They run on the pandas route with output identical to main. But on every route, bucket_perturb writes the literal format name (`mixed` or `ISO8601`) in place of every date it parses, a pre-existing defect. The entry now says exactly that, flags the defect, and names the follow-up. Cam then chose "reject with a clear error" (2026-10-07), tracked as its own slice `fix/bucket-perturb-format-guard`.
- **MEDIUM (`transforms/bucket_perturb.py:170`, the pre-existing literal-format defect):** fixed at its source in that follow-up slice, not here. This slice's output equals main's.
- **MEDIUM (text_redact unified cost):** dennis could not reproduce the 4.05 s. Best of two: pandas 2.53 s, chunked 2.71 s, unified 2.92 s. In the profile, `iter_spans` (pure Python) is 4.16 of 5.39 s. The same gap exists without `when`. The admission is kept to match unmasked admission. The carry-forward is the Rust span kernel (C6c-ii).
- **LOW, docstring:** `_when_column_is_chunk_safe` no longer claims per-chunk equals whole-frame. It cites C8-iii-b.
- **LOW, decline tests:** three test-7 edits had lost their `when`-specific purpose. They now use an ADMITTED config with an out-of-grammar chained-comparison predicate:
  - `test_c6c_i_text_redact_chunked.py`, the not-string-pinned test;
  - `test_c6c_i_text_redact_unified.py`, the decline test;
  - `test_bucket_perturb_chunked.py`, the `_when_bearing_bucket_perturb_cfg` helper.
- **LOW, line length:** the over-long docstring line in `_operator_step.py` is wrapped.
- **LOW, rebase:** rebased onto main with #222, keeping both CHANGELOG entries. Sentries rerun after the rebase.

After remediation, the touched suites passed: 452.
