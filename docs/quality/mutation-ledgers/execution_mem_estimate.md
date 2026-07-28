# Mutation grading: `execution/_mem_estimate.py` -- pure schema-derived estimator

TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`) by
`scripts/tq_mutate.py`. This module was on finding #15's "un-gradeable subprocess
substrate" list but is in fact PURE and in-process (zero subprocess references):
plain-Python byte arithmetic over the normalized `TableSizeSpec` / `ColumnSizeSpec`
dataclasses, so every mutant is gradeable in-process with the existing tool -- no
standalone-per-mutant runner is needed. This is the first module graded under the
2026-07-28 FRAME finding that finding #15's list was largely a misclassification
(see tq-findings #15). Public API: `raw_data_bytes`, `estimate_peak_bytes`,
`estimator_basis_bytes`, `fits`, `default_fk_key_size_bytes`, `route_intercept_bytes`,
`route_slope`, `is_fixed_width_dtype`, plus the `*SizeSpec` / `*Estimate` /
`RawBytesResult` / `FkCardinalityInput` result types. Not crypto/RI, so the bar is
the measure-first substrate bar; the re-grade is authoritative.

## Numbers

Baseline (existing tests: `test_mem_estimate.py` + `test_byte_estimate_routing.py`
+ `test_mem_calibration.py`): 100/119 = 84.03% LOGIC, 0 unresolved, 19 survivors.
Full triage of the 19 adds **8 kills** (8 tests in a new
`tests/unit/execution/test_mem_estimate_kills.py`) and adjudicates the remaining
**11 as PROVEN EQUIVALENT** (0 accepted non-contract, 0 residual). Re-grade with the
kill file in the selection: **108/119 = 90.76% LOGIC (tool-native, 0 unresolved)**;
the 8 LOGIC targets are all absent from the survivor set. 108/108 = 100% of the
KILLABLE mutants are killed -- the 11 equivalents are unreachable-branch message
prose, unkillable by any test.

| Function | Survivors triaged | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `raw_data_bytes` | 1 | 1 | 0 | 0 |
| `default_fk_key_size_bytes` | 1 | 1 | 0 | 0 |
| `_column_bytes` | 4 | 0 | 4 | 0 |
| `estimator_basis_bytes` | 2 | 2 | 0 | 0 |
| `fits` | 11 | 4 | 7 | 0 |
| **total** | **19** | **8** | **11** | **0** |

## Kills added by the full triage (8)

Each asserts the exact machine field the mutation breaks (byte count / verdict),
verified standalone (`MUTANT_UNDER_TEST` set, orig rc 0 / mutant rc 1).

| mut | mutation | killed by (machine field) |
|---|---|---|
| `raw_data_bytes` 11 | `continue` -> `break` after appending an unpriceable column | `TestRawDataBytesContinuesPastUnpriceable` -- an unpriceable column FIRST then an int64: `break` abandons the trailing column, dropping `priceable_bytes` 8000 -> 0. |
| `default_fk_key_size_bytes` 2 | `+ _HASH_ENTRY_OVERHEAD_BYTES` -> `- ...` | `TestDefaultFkKeySizeBytesExactSum` -- a zero-width key costs 57 (object) + 32 (hash slot) = 89 exactly; subtraction gives 25. |
| `estimator_basis_bytes` 33 | sequential `fk_bytes = 0` -> `= 1` | `TestEstimatorBasisSequentialFkTerm::test_sequential_basis_without_fk...` -- no fk_cardinality means the basis is the working set alone (8000); `+1` makes it 8001. |
| `estimator_basis_bytes` 36 | sequential `distinct_key_count * key_size_bytes` -> `/` | `TestEstimatorBasisSequentialFkTerm::test_sequential_fk_term_multiplies...` -- 10 keys * 64 bytes = 640 added to 8000 = 8640; division gives 8000.15625. |
| `fits` 1 | `error_band < 0` -> `<= 0` | `TestFitsBoundaries::test_error_band_of_zero_is_accepted...` -- zero is the tightest VALID band; the mutant rejects it with ValueError instead of returning a verdict. |
| `fits` 10 | `estimate_peak_bytes(..., fk_cardinality=fk_cardinality)` -> `=None` | `TestFitsBoundaries::test_fk_cardinality_is_forwarded...` -- budget placed between the with-fk and fk-ignored margins on the sequential path: dropping fk flips the verdict. |
| `fits` 13 | drops the `fk_cardinality=` argument (callee default None) | same test as `fits` 10 (identical observable effect: fk ignored). |
| `fits` 26 | final `estimated * (1+error_band) < budget` -> `<=` | `TestFitsBoundaries::test_margin_equal_to_budget_does_not_fit` -- with `error_band=0.0` the margin equals the estimate; at `budget == estimate` the strict under-fit rule returns False, `<=` returns True. |

## Proven equivalent (11): unreachable defensive-branch message prose

All 11 survivors mutate the message of an `AssertionError` inside a branch that no
input can reach, so no test -- not even brittle full-message-equality -- can kill
them. Each verified to survive standalone (rc 0).

- **`_column_bytes` 7, 8, 9, 10**: the `if width is None: raise AssertionError(...)`
  guard for a variable-width column. `ColumnSizeSpec.__post_init__` already rejects
  a variable-width column with no `string_width_bytes` and no `unpriceable=True`, so
  by the time `_column_bytes` runs, a non-unpriceable variable-width column always
  has a width. The raise is dead. (7 nulls the message; 8/9/10 case/wrapper-mutate it.)
- **`fits` 16, 17, 18, 19, 20, 21, 22**: the `if estimated_bytes is None: raise
  AssertionError(...)` guard AFTER `if estimate.unpriceable: return None`.
  `PeakEstimate.__post_init__` forbids `estimated_bytes=None` unless
  `unpriceable_columns` is non-empty (i.e. `unpriceable` is True), which the earlier
  return already handled, so `estimated_bytes` is never None here. The raise is dead.
  (16 nulls the message; 17-22 case/wrapper-mutate its two fragments.)

## Candidate findings

None. No mutation exposed a wrong byte count, a wrong fit/no-fit verdict, a wrong
priceable/unpriceable classification, or a wrong FK-dedup term.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_mem_estimate.py`
and the test selection to `tests/unit/execution/test_mem_estimate_kills.py`,
`tests/unit/execution/test_mem_estimate.py`, `tests/unit/execution/test_byte_estimate_routing.py`,
and `tests/unit/execution/test_mem_calibration.py`; then `rm -rf mutants && python
scripts/tq_mutate.py --run --jobs 6`. `source_paths` stays at the package root.

Standalone single-mutant check (activation caveat): the repo `pyproject.toml`
`pythonpath = ["src"]` is injected ahead of the env `PYTHONPATH=src` when pytest's
rootdir resolves to the repo, so a per-mutant command targeting `../tests/...` must
append `-o pythonpath=` to clear the ini injection and let `mutants/src` load the
mutant. The authoritative `tq_mutate.py --run` (cwd=mutants) is unaffected.
