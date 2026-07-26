# Mutation grading: `transforms/date_shift.py` -- LIVE SURFACE LOGIC-100% (rest dead)

TQ step-4 pass, graded 2026-07-26. **Scoped grade.** The active engine-v2 S9
date_shift is `execution/_strategies/_date_shift.DateShiftStrategyHandler`; the V1
`DateShiftStrategy` class in this module (`apply`, `_column_key`, `__init__`,
`validate_rule`) plus `_parse_date`, `_shift_for_value_md5`,
`_shift_for_value_keyed` are **dead legacy code** -- not instantiated or called
anywhere in `src/` (tq-findings #11). The ONLY live export is `_detect_format`,
reused by the engine-v2 handler, `bucket_perturb`, and the out-of-core path. This
ledger grades `_detect_format` to LOGIC-100% and EXCLUDES the dead class (authoring
tests for code slated for removal would lock it in).

**Grade scope: FOCUSED selection** (`test_date_shift_key_failure.py` +
`test_dateshift_formula.py`).

## Numbers

Whole module: 265 mutants, 117 killed, 38 no-tests, 110 survived. Of the survivors
+ no-tests, **141 are inside the dead V1 class / dead helpers** (excluded, finding
#11); the remaining **7 are `_detect_format` equivalents**.

`_detect_format` (live): **18 survived -> 11 LOGIC killed, 7 EQUIVALENT.**
LOGIC-mutant score 100% for the live surface. 0 timeouts.

## LOGIC (11): killed by new tests

All in `tests/unit/transforms/test_date_shift_key_failure.py::TestDetectFormat`.

| Mutants | Mutation | Killed by |
|---|---|---|
| `10`, `11`, `19` | `ok = True` -> None/False; `candidates.append(fmt)` -> `append(None)` (a valid format is never detected / None returned) | `test_detect_iso_format` (a single-format column must be detected) |
| `17`, `18` | parse-fail `ok = False` -> `True` (accept-on-fail); `break` -> `return` (bail at first miss) | `test_detect_non_first_candidate_format` (first candidate fails; must skip to the real one, not accept the failing one or return None) |
| `22` | ambiguity threshold `len(candidates) > 1` -> `> 2` (no warn on 2 candidates) | `test_ambiguous_column_warns` |
| `21` | `> 1` -> `>= 1` (warn on a single candidate) | `test_unambiguous_column_does_not_warn` |
| `28` | warning first-literal upper-cased | `test_ambiguous_column_warns` (`match="multiple formats"` fails on the upper-cased text) |
| `2`, `8` | sample cap `head(min(200, n))` -> `head(None)` / `head(min(201, n))` (samples beyond the 200-row cap) | `test_sample_cap_limits_detection_to_200_rows` (200 valid + 1 breaker: the cap keeps detection on the first 200) |

## EQUIVALENT (7)

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `16` | parse-fail `ok = False` -> `None` | consumed only by `if ok:`; `None` and `False` are both falsy, so the format is rejected either way. |
| `26`, `32` | `stacklevel=2` dropped / `=3` | only shifts the warning's reported source line; the warning + return value are unchanged. |
| `27`, `30`, `31` | warning message literals XX-wrapped / upper-cased (second literal) | prose only; the return value is unchanged and the asserted `match="multiple formats"` substring is preserved. |
| `29` | warning f-string `candidates[0]` -> `candidates[1]` | only the displayed candidate in the message text; the RETURN is still `candidates[0]`. |

## Dead code (excluded, finding #11)

141 survivors/no-tests in `DateShiftStrategy.{apply,_column_key,__init__,validate_rule}`
and `_parse_date` / `_shift_for_value_md5` / `_shift_for_value_keyed`. Not graded:
the V1 class is superseded by the engine-v2 handler and not instantiated anywhere.
Flagged for removal (tq-findings #11), not for test authoring.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/date_shift.py`, selection to
`test_date_shift_key_failure.py` + `test_dateshift_formula.py`, then
`rm -rf mutants && python -m mutmut run`. Only the `_detect_format` survivors are
in scope; the rest are the dead V1 class.
