# Mutation grading: `transforms/derived.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `transforms/derived.py` (293 LOC) computes a
derived column from a closed expression over the row context: `DerivedConfig`
parses/compiles the expression, `_coerce_context` applies the null-propagation
mode (explicit_null / sentinel / default), `apply_derived` evaluates it per row
via the `expressions` sandbox, and `_apply_bounds` clips the result to
[min, max] preserving int type. Pure function (no RNG).

**Grade scope: FOCUSED selection** (`tests/unit/transforms/test_derived.py`, 80 tests).

## Numbers

**87 mutants: 81 killed (93% baseline), 6 survived -> 84 killed after this pass,
3 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts. (High baseline: the SP-10
TDD suite already covered operators, null-propagation, bounds, and determinism.)

- **3 LOGIC survivors killed** with 3 new direct tests
  (`TestDerivedNullAndBoundsInternals`). 0 product bugs.
- **3 EQUIVALENT survivors** (error-message context prose).

## LOGIC (3): killed by new tests

| Mutants | Mutation | Killed by |
|---|---|---|
| `_is_null_4` | `math.isnan(value)` -> `math.isnan(None)` (raises TypeError on any float, since the `and` only reaches it when value is a float) | `test_is_null_on_a_plain_float_is_false_not_error` (`_is_null(1.5)` must be False, not raise; `nan`/`None` still True) |
| `_apply_bounds_27` | int-preservation `isinstance(value, int) and result == int(result)` -> `... and result != int(result)` (an int value with a whole result returns float instead of int) | `test_apply_bounds_preserves_int_type` (`_apply_bounds(5, {"min": 0})` stays `int`) |
| `_apply_bounds_26` | `isinstance(value, int) and ...` -> `isinstance(value, int) or ...` (a float value with a whole result is coerced to int) | `test_apply_bounds_keeps_float_value_as_float` (`_apply_bounds(5.0, {"min": 0})` stays `float`) |

## EQUIVALENT (3)

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `apply_derived_23`, `24`, `25` | error-context `", ".join` -> `"XX, XX".join`; fallback `"unknown column"` -> `"XXunknown columnXX"` / `"UNKNOWN COLUMN"` | all only alter the human-readable `ctx_str` inside the raised `ValueError` message on an eval failure; `ValueError` has no machine field and the message is not asserted/consumed. Per the TQ string-mutant policy, equivalent. |

## Gate

Dennis batch gate: **PASS**, 0 P0 / 0 P1 / 0 P2. The 3 equivalents confirmed
message-only (ValueError, no machine field, no caller parses it); the 3 kills
confirmed machine-observable. Finding #9 check: the module-level constants
`_SENTINEL_VALUE`/`_DEFAULT_VALUE`/`_NULL_PROPAGATION_MODES` are pinned by
value-level oracles, so no constant-driven blind spot.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/derived.py`, selection to
`tests/unit/transforms/test_derived.py`, then `rm -rf mutants && python -m mutmut run`.
