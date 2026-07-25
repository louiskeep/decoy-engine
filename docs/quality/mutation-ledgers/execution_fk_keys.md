# Equivalent-mutant ledger: `_fk_keys.py`

TQ crown-jewels pass, 2026-07-25. A mutmut run against `execution/_fk_keys.py`
(runtime FK key preservation, the RI crown jewel) left **51 survivors**. Every
survivor was classified LOGIC or EQUIVALENT per
`docs/quality/module-test-quality-playbook.md` ("Scope the score to LOGIC, not
error-message wording"). **19 LOGIC survivors** were killed with new tests in
`tests/property/test_fk_keys_invariants.py`; **32 survive and are equivalent**
(message-prose-only, a `code`/`message` default-value no-op, or a true no-op),
listed below with the one-line argument for why no input can distinguish them
from the original.

Covering tests: `tests/unit/execution/test_de10_fk_lossless_typing.py` +
`tests/unit/execution/test_de10_chunked_fk_declared_dtype.py` +
`tests/unit/execution/test_de10_chunked_fk_passthrough.py` +
`tests/unit/kernel/test_kernel_contracts.py` (example-based) +
`tests/property/test_fk_keys_invariants.py` (this module's oracle layer --
RI PRESERVATION, DETERMINISM, CONSISTENCY, NAMESPACE ISOLATION, and
ORPHAN/FAIL-CLOSED properties, plus the 16 targeted tests added in this pass,
two of which each kill two mutants).

Bugs found in `_fk_keys.py`: none introduced or newly exposed by this pass.
One pre-existing OBSERVATION, not a bug: `_decimal_join_token`'s zero-sign
canonicalization (`if canonical.is_zero(): canonical = abs(canonical)`) is
dead code via the function's only production call site. `fk_join_key` only
calls `_decimal_join_token` on a Decimal that `fk_key_value` did NOT fold to
a plain `int`, and `fk_key_value` folds every int-equal Decimal (any zero,
regardless of scale or sign, is always int-equal to `0`) to `int` before
`fk_join_key` ever checks `isinstance(normalized, Decimal)`. So a zero
Decimal can never reach `_decimal_join_token` through `fk_join_key` in
today's wiring; the zero-canonicalization branch only matters if some future
caller invokes `_decimal_join_token` directly, or if the upstream folding in
`fk_key_value` ever changes. Not a correctness defect (no observable output
differs), so not reported as a finding to fix -- flagged here so a future
change to `fk_key_value`'s folding does not silently un-dead this branch
without re-verifying it.

## WORDING (28): error-message prose only

mutmut lowercases/uppercases a message fragment, wraps it in `XX...XX`, sets
`message=None`, or drops the `message=` kwarg entirely (`ExecutionError`'s
`message` parameter defaults to `""`, so a dropped kwarg is a safe no-op,
unlike a dropped `code=` kwarg -- see the LOGIC table). In every case the
literal is consumed only inside a raised `ExecutionError`'s human `message`;
it never becomes the exception's `code`, a return value, or a comparison
target. Tests assert the `code`, so only pure-wording variants survive.

| Mutant | Mutation |
|---|---|
| `x_lossless_fk_int_values__mutmut_28` | fail-closed error: `message=(...)` -> `message=None` |
| `x_lossless_fk_int_values__mutmut_30` | fail-closed error: `message=` kwarg dropped |
| `x_lossless_fk_int_values__mutmut_31` | fail-closed error, sentence 1: wrapped in `XX...XX` |
| `x_lossless_fk_int_values__mutmut_32` | fail-closed error, sentence 1: lowercased |
| `x_lossless_fk_int_values__mutmut_33` | fail-closed error, sentence 1: uppercased |
| `x_lossless_fk_int_values__mutmut_34` | fail-closed error, sentence 2: wrapped in `XX...XX` |
| `x_lossless_fk_int_values__mutmut_35` | fail-closed error, sentence 2: uppercased |
| `x_lossless_fk_int_values__mutmut_36` | fail-closed error, sentence 3: wrapped in `XX...XX` |
| `x_lossless_fk_int_values__mutmut_37` | fail-closed error, sentence 3: uppercased |
| `x_lossless_fk_int_values__mutmut_38` | fail-closed error, sentence 4: wrapped in `XX...XX` |
| `x_lossless_fk_int_values__mutmut_39` | fail-closed error, sentence 4: uppercased |
| `x_lossless_fk_int_values__mutmut_40` | fail-closed error, sentence 5: wrapped in `XX...XX` |
| `x_lossless_fk_int_values__mutmut_41` | fail-closed error, sentence 5: uppercased |
| `x_fk_nullable_int_array__mutmut_8` | UInt64-overflow error: `message=(...)` -> `message=None` |
| `x_fk_nullable_int_array__mutmut_10` | UInt64-overflow error: `message=` kwarg dropped |
| `x_fk_nullable_int_array__mutmut_11` | UInt64-overflow error text: wrapped in `XX...XX` |
| `x_fk_nullable_int_array__mutmut_12` | UInt64-overflow error text: `"UInt64's"` -> `"uint64's"` |
| `x_fk_nullable_int_array__mutmut_13` | UInt64-overflow error text: uppercased |
| `x_fk_nullable_int_array__mutmut_18` | Int64-underflow error: `message=(...)` -> `message=None` |
| `x_fk_nullable_int_array__mutmut_20` | Int64-underflow error: `message=` kwarg dropped |
| `x_fk_nullable_int_array__mutmut_21` | Int64-underflow error text: wrapped in `XX...XX` |
| `x_fk_nullable_int_array__mutmut_22` | Int64-underflow error text: uppercased |
| `x_fk_nullable_int_array__mutmut_35` | pandas-construction-failure error: `message=(...)` -> `message=None` |
| `x_fk_nullable_int_array__mutmut_37` | pandas-construction-failure error: `message=` kwarg dropped |
| `x_to_pandas_fk_safe__mutmut_14` | cast-failure error: `message=(...)` -> `message=None` |
| `x_to_pandas_fk_safe__mutmut_16` | cast-failure error: `message=` kwarg dropped (`from exc` untouched -- verified via `mutmut show`, not a chaining mutation) |
| `x_to_pandas_fk_safe__mutmut_17` | cast-failure error text: wrapped in `XX...XX` |
| `x_to_pandas_fk_safe__mutmut_18` | cast-failure error text: uppercased |

## NO-OP (4): a default value only ever read as a truthy/falsy flag

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `x_lossless_fk_int_values__mutmut_2` | `saw_non_int = False` -> `saw_non_int = None` | Only ever read as `if not saw_non_int:` and reassigned to `True` on the one branch that sets it; `None` and `False` are both falsy and neither is ever compared by identity or type, so the two initial values are behaviorally indistinguishable. |
| `x_lossless_fk_int_values__mutmut_4` | `saw_unrepresentable_int = False` -> `saw_unrepresentable_int = None` | Same reasoning: only ever read as `if saw_unrepresentable_int:` and reassigned to `True` on one branch. |
| `x_lossless_fk_int_values__mutmut_11` | except-branch `is_null = False` -> `is_null = None` | Only ever read as `if is_null:` immediately after; both are falsy, so this initial value in the rare array-like-`pd.isna`-result fallback is indistinguishable from the real default (contrast with `mutmut_12` below, which flips it to `True` -- that one is a real behavior change and is in the LOGIC table). |
| `x_fk_nullable_int_array__mutmut_1` | `needs_uint64 = False` -> `needs_uint64 = None` | Only ever read as `"UInt64" if needs_uint64 else "Int64"` and reassigned to `True` on one branch; `None` and `False` pick the identical `"Int64"` branch of the ternary. |

## LOGIC (19): killed by new tests in this pass

All in `tests/property/test_fk_keys_invariants.py`, appended after the
existing property suite under the "TQ crown-jewels mutation-kill pass"
heading. Two entries below are each killed by the SAME new test because the
test asserts on the boundary/coded-error contract both mutants break.

| Mutant | Mutation | Killed by |
|---|---|---|
| `x_fk_key_value__mutmut_6` | `is_nan = value != value` -> `is_nan = None` (falsy, so a NaN `numbers.Number` like `Decimal("NaN")` is never recognized as null) | `test_fk_key_value_number_nan_collapses_to_null_fk_key` |
| `x__decimal_join_token__mutmut_2` | `.normalize(context=_DECIMAL_JOIN_CONTEXT)` -> `context=None` (falls back to the ambient default's 28-digit precision, silently rounding a higher-precision Decimal before the token is minted) | `test_decimal_join_token_preserves_precision_beyond_default_context` |
| `x__decimal_join_token__mutmut_3` | zero-sign branch: `canonical = abs(canonical)` -> `canonical = None` | `test_decimal_join_token_zero_sign_is_canonicalized_to_non_negative` |
| `x__decimal_join_token__mutmut_4` | zero-sign branch: `canonical = abs(canonical)` -> `canonical = abs(None)` (raises `TypeError`) | same |
| `x__decimal_join_token__mutmut_5` | `return repr(canonical)` -> `return repr(None)` (every Decimal mints the identical token) | `test_decimal_join_token_reflects_the_actual_value` |
| `x_fk_join_key__mutmut_7` | OBJ: tag: `type(normalized).__qualname__` -> `type(None).__qualname__` (always `"NoneType"`, regardless of the value's real type) | `test_fk_join_key_obj_tag_uses_the_value_s_actual_type_name` |
| `x_fk_join_key_tuple__mutmut_4` | `"".join(...)` -> `"XXXX".join(...)` (splices a literal separator into the token, part of the RETURNED join key, not message prose) | `test_fk_join_key_tuple_uses_no_separator_between_components` |
| `x__is_exactly_float_representable__mutmut_2` | `-BOUND <= value <= BOUND` -> `-BOUND < value <= BOUND` (rejects the exact `-2**53` boundary, which IS exactly representable) | `test_lossless_fk_int_values_exact_float_bound_is_inclusive_on_both_sides` |
| `x__is_exactly_float_representable__mutmut_3` | same call: `-BOUND <= value <= BOUND` -> `-BOUND <= value < BOUND` (rejects the exact `+2**53` boundary) | same |
| `x_lossless_fk_int_values__mutmut_8` | `is_null = bool(pd.isna(value))` -> `is_null = None` (falsy; a `pd.NA` masked value is no longer recognized as null) | `test_lossless_fk_int_values_pd_na_is_classified_as_a_null_slot` |
| `x_lossless_fk_int_values__mutmut_9` | same call: `bool(pd.isna(value))` -> `bool(None)` (also always falsy) | same |
| `x_lossless_fk_int_values__mutmut_12` | except-branch `is_null = False` -> `is_null = True` (an array-like `pd.isna` result is misclassified as a null slot instead of falling to the non-int bucket) | `test_lossless_fk_int_values_array_like_element_falls_to_non_int_bucket` |
| `x_lossless_fk_int_values__mutmut_13` | null-slot `continue` -> `break` (silently truncates the classified column at the first null) | `test_lossless_fk_int_values_processes_every_value_after_a_null` |
| `x_fk_nullable_int_array__mutmut_6` | `if value > _UINT64_MAX:` -> `if value >= _UINT64_MAX:` (wrongly rejects the exactly-representable `_UINT64_MAX` boundary) | `test_fk_nullable_int_array_uint64_max_is_a_valid_boundary_not_an_overflow` |
| `x_fk_nullable_int_array__mutmut_16` | `elif value < _INT64_MIN:` -> `elif value <= _INT64_MIN:` (wrongly rejects the exactly-representable `_INT64_MIN` boundary) | `test_fk_nullable_int_array_int64_min_is_a_valid_boundary_not_an_overflow` |
| `x_to_pandas_fk_safe__mutmut_3` | absent-column `continue` -> `break` (abandons the loop, un-protecting every FK column listed after the first absent one) | `test_to_pandas_fk_safe_skips_an_absent_fk_column_and_still_protects_the_rest` |
| `x_to_pandas_fk_safe__mutmut_8` | non-integer-column `continue` -> `break` (abandons the loop, un-protecting every FK column listed after the first non-integer one) | `test_to_pandas_fk_safe_skips_a_non_integer_fk_column_and_still_protects_the_rest` |
| `x_to_pandas_fk_safe__mutmut_13` | cast-failure error: `code=FK_KEY_DTYPE_UNSUPPORTED_CODE` -> `code=None` | `test_to_pandas_fk_safe_cast_failure_raises_the_coded_execution_error` |
| `x_to_pandas_fk_safe__mutmut_15` | cast-failure error: `code=` kwarg dropped entirely (required kwarg-only param on `ExecutionError.__init__` -> raises bare `TypeError`, not `ExecutionError`) | same |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
