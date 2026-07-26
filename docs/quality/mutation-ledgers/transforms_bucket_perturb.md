# Mutation grading: `transforms/bucket_perturb.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-26. `bucket_perturb` snaps each date to a deterministic
position within its ISO week / calendar month / calendar quarter. The bucket
boundary comes from the input date; the in-bucket offset is
`int.from_bytes(derive(job_seed, namespace, value)[:8], "big") % bucket_size`.
This is the CORE compute; the fail-closed strategy handler is graded separately
(`execution_strategies_bucket_perturb.md`).

Graded with the FOCUSED selection `tests/unit/execution/test_bucket_perturb.py`
(28 tests, ~0.4s). Conservative lower bound: only this selection is counted, and
the new KAT layer drives the core functions directly
(`_bucket_start_and_size`, `_perturb_date`, `apply_bucket_perturb`,
`validate_bucket_perturb_config`) rather than only through the handler.

**140 mutants: 118 killed, 22 survived (84% baseline).** This pass killed the 18
LOGIC survivors and left 4 equivalent. LOGIC-mutant score 100%.

## LOGIC killed this pass (11 new tests)

All new tests live in `test_bucket_perturb.py`.

### `_bucket_start_and_size` (6)

| Mutant | Mutation | Killed by |
|---|---|---|
| `mutmut_31` | `q_idx = (date.month - 1) // 4` (should be `// 3`) | `TestBucketStartAndSizeCore::test_quarter_start_is_first_day_of_the_quarters_first_month` -- Apr/Jul/Oct land in the prior quarter under `// 4` (Apr->Jan, Jul->Apr, Oct->Jul); the KAT asserts each returns its own quarter's first-of-month |
| `mutmut_40` | quarter start `datetime.date(y, q_start_month, 2)` (day 1->2) | same test -- start is asserted `== date(y, m, 1)`, so day 2 fails |
| `mutmut_57` | `size = (end - start).days - 1` | `test_quarter_size_is_the_exact_day_count` -- Q1 2024 is 91 days; `- 1` reports 89 |
| `mutmut_59` | `size = (end - start).days + 2` | same test -- `+ 2` reports 92 |
| `mutmut_60` | `raise ValueError(None)` (message nulled) | `test_unrecognized_bucket_raises_valueerror` -- `match="unrecognized bucket 'garbage'"` fails against `str(None)` |
| `mutmut_61` | `f"...{sorted(None)}."` | same test -- `sorted(None)` raises `TypeError`, not `ValueError`, so `pytest.raises(ValueError)` is not satisfied |

### `_perturb_date` (1)

| Mutant | Mutation | Killed by |
|---|---|---|
| `mutmut_20` | offset slice `digest[:8]` -> `digest[:9]` | `TestPerturbDateOffsetKnownAnswer::test_perturb_date_is_the_pinned_known_answer` -- fixed seed+namespace+value pin `month -> 2024-06-09`, `quarter -> 2024-06-03`; the wider slice yields a different offset (verified `off8 != off9` for both pins) |

### `apply_bucket_perturb` (9)

| Mutant | Mutation | Killed by |
|---|---|---|
| `mutmut_2` | `series = None` (in the extension-dtype branch) | `TestApplyBucketPerturbCore::test_extension_array_dtype_input_is_processed` -- a `string`-dtype input reaches the branch; `None.isna()` then raises `AttributeError` |
| `mutmut_3` | `series = series.astype(None)` | same test -- `astype(None)` coerces to float64 and raises `ValueError` on a date string |
| `mutmut_4` | `fmt = None` | `test_valid_value_is_bucketed_to_its_known_answer` -- forces the warn+passthrough path, returning the input unchanged instead of the pinned `2024-06-09` |
| `mutmut_6` | `_detect_format(None)` (autodetect arg nulled) | `test_autodetect_used_when_date_format_is_none` -- with `date_format=None` the `or` no longer short-circuits, and `_detect_format(None)` raises `AttributeError` |
| `mutmut_7` | `if fmt is not None:` (guard inverted) | `test_valid_value_is_bucketed_to_its_known_answer` -- a valid format now enters the passthrough branch, returning the input unchanged |
| `mutmut_17` | `pd.to_datetime(..., )` drops `errors="coerce"` | `test_unparseable_value_passes_through_unchanged` -- without coerce the `"not-a-date"` cell raises instead of becoming NaT |
| `mutmut_22` | `parse_failed = parsed.isna() | ~null_mask` (`&`->`|`) | `test_valid_value_is_bucketed_to_its_known_answer` -- every non-null valid cell is now flagged parse-failed and skipped, so the value is returned unchanged |
| `mutmut_23` | `parse_failed = parsed.isna() & null_mask` (drops `~`) | `test_unparseable_value_passes_through_unchanged` -- the unparseable non-null cell is no longer skipped, so it is processed (NaT) instead of preserved verbatim |
| `mutmut_30` | `continue` -> `break` in the null/unparseable guard | `test_value_after_a_null_is_still_bucketed` -- `break` stops the loop at the null, leaving the following value unbucketed; the KAT pins that trailing value's output |

### `validate_bucket_perturb_config` (2)

| Mutant | Mutation | Killed by |
|---|---|---|
| `mutmut_6` | `raise ValueError(None)` (message nulled) | `TestValidateConfigCore::test_missing_bucket_raises_valueerror` -- `match="'bucket' is required"` fails against `str(None)` |
| `mutmut_7` | `f"...{sorted(None)}."` | same test -- `sorted(None)` raises `TypeError`, not `ValueError` |

## EQUIVALENT (4)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `apply_bucket_perturb__mutmut_1` | `is_extension_array_dtype(None)` (was `series.dtype`) | `is_extension_array_dtype(None)` is `False`, so the branch is skipped. The branch only does `series = series.astype(object)`; downstream `result = series.astype(object).copy()` materializes the same object-dtype column and assigns the same string values regardless, so the output column is byte-identical. Same pattern as the `_shuffle` / `_text_mask` ledgers. |
| `apply_bucket_perturb__mutmut_8` | `_LOG.warning(None)` | Log-line prose only. Reached solely when `fmt is None` (undetectable format), where the function returns `series.copy()` either way; the argument to `warning()` changes no returned value or control flow. |
| `apply_bucket_perturb__mutmut_9` | log message wrapped `XX...XX` | Same log line as above; string-literal wording only. |
| `apply_bucket_perturb__mutmut_10` | log message uppercased | Same log line; string-literal wording only. |

## FOCUSED-selection caveat

The 84% baseline and the kill accounting are scoped to
`tests/unit/execution/test_bucket_perturb.py` only, with `[tool.mutmut]`
`only_mutate` pointed at `src/decoy_engine/transforms/bucket_perturb.py`. Broader
suites (integration, pipeline goldens) exercise this module too but are not
counted here, so the true score is at least this. The LOGIC-100% claim applies to
behavior mutants; the four equivalents are string-only (one dtype no-op, three
log-message wordings).

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/transforms/bucket_perturb.py` and
`pytest_add_cli_args_test_selection` to
`tests/unit/execution/test_bucket_perturb.py`, then
`rm -rf mutants && python -m mutmut run`.
