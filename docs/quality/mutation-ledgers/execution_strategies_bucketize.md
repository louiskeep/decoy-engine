# Mutation grading: `execution/_strategies/_bucketize.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_bucketize.py` (137 LOC) is the numeric
generalization handler: it resolves a bucket `width` (from provider_config
`width`, or a `preset` such as `by_decade`), fails closed when the width is
unresolvable, floors each value to its bucket, and renders per `format` (lower /
range / midpoint). It is the engine path behind the HIPAA age>89 generalization,
so the bucket-edge math is correctness-critical.

**Grade scope: FOCUSED selection only** (`tests/unit/execution/test_hash_bucketize.py`).

## Numbers

**132 mutants: 72 killed (55% baseline), 60 survived -> 117 killed after this
pass, 15 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **45 LOGIC survivors killed** with 11 new tests (`TestBucketizeSurvivors`). 0 bugs.
- **15 EQUIVALENT survivors** (prose message / f-string key mutations, and
  format-default no-ops that reset to `"lower"`). All verified behavior-preserving.

## LOGIC (45): killed by new tests

All in `tests/unit/execution/test_hash_bucketize.py::TestBucketizeSurvivors`.

| Test | Kills | Pins |
|---|---|---|
| `test_unresolvable_width_error_carries_code_and_strategy` | run_7, 14, 15 | `StrategyError.code == "bucketize_width_unresolvable"` and `.strategy == "bucketize"` (width=0) |
| `test_bool_width_fails_closed` | _resolve_width_11 | `or`->`and` would let `width=True` resolve to 1 instead of failing closed |
| `test_width_one_is_resolvable` | _resolve_width_14 | `raw > 0`->`raw > 1` would reject a valid width of 1 |
| `test_unknown_format_falls_back_to_lower` | run_34, 35, 36 | a bad `format` resets to "lower", not the midpoint branch |
| `test_float_width_lower_keeps_fractional` | run_50, 60 | `and`->`or` sends a float width down the int path; `lower=None` |
| `test_float_width_range_upper_edge` | run_61, 62 | `upper_excl=None`; `lower+width`->`lower-width` |
| `test_midpoint_even_int_width` | run_80-92, 94, 95, 96 (15) | wrong midpoint value, float-instead-of-int render, or crash |
| `test_midpoint_odd_int_width_keeps_fractional` | run_84 | `and`->`or` truncates the .5 midpoint |
| `test_midpoint_preserves_null` | run_93 | `"Int64"`->`"int64"` rejects NaN (IntCastingNaNError) |
| `test_non_numeric_cell_records_row_error_and_keeps_original` | run_42, 98, 99, 100-103, 105-111, 116, 118 | `errors="coerce"` drop; `&`->`|`; `flatnonzero`; RowError machine fields; `where` other-arg |

## EQUIVALENT (15)

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `run_8`, `11` | width-error `message=None` / dropped | `StrategyError.message` defaults `""`, prose; `code`/`strategy` asserted. |
| `run_16`-`21` | `cfg.get(...)` keys inside the error-message f-string | only alter displayed prose; `dict.get(None)` / changed key strings do not change the machine fields. |
| `run_26`, `28` | `format` default `None` / dropped | absent format -> `str(None).lower()="none"` not in `_FORMATS` -> resets to `"lower"`; identical output. |
| `run_31`, `32` | format default `"XXlowerXX"` / `"LOWER"` | when format absent both collapse back to `"lower"` (via reset or `.lower()`); identical output. |
| `run_104`, `112`, `113` | `RowError.reason` None / string variants | `reason` is diagnostic prose (docstring: human-readable); `trigger="format_error"` is the asserted machine field. |

## Gate

Dennis batch gate (_hash + _bucketize): **PASS**, 0 P0 / 0 P1 / 0 P2. All
EQUIVALENT classifications verified behavior-preserving against source; all kills
confirmed genuine.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `_bucketize.py`, selection to
`test_hash_bucketize.py`, then `rm -rf mutants && python -m mutmut run`.
