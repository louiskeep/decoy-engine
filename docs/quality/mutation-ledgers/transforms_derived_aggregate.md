# Mutation grading: `transforms/derived_aggregate.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-26. `derived_aggregate` computes a scalar aggregate
(sum / mean / min / max / count) over a source column, with NULLs excluded per
the SQL set-function rule. Graded with the FOCUSED selection
`tests/unit/transforms/test_derived_aggregate.py` (~0.3s). Conservative lower
bound.

**58 mutants: 46 killed baseline (79%) -> 50 killed, 8 equivalent.** LOGIC-mutant
score 100%. (Batch-6 dennis P1/P2 correction: the `col.get("op"/"column", "XXXX")`
mutants were first filed equivalent but are reachable LOGIC -- killed below.)

## LOGIC killed this pass (4 new tests)

| Mutants | Mutation | Killed by |
|---|---|---|
| `series.min(skipna=True)` -> `skipna=False`, `series.max(skipna=True)` -> `skipna=False` | min/max would return NaN once any null is present, breaking the SQL null-exclusion contract (sum's skipna was already tested; min/max were not) | `test_min_excludes_nulls`, `test_max_excludes_nulls` (min/max over `[3, None, 1, 2]`) |
| `col.get("column", "")` -> `"XXXX"` | a gen config with no `column` reaches runtime; the real `""` default fails closed (`derived_aggregate_column_missing`), but `"XXXX"` -> `generated.get("XXXX", []) == []` -> silently aggregates an empty series to 0 (fail-closed -> silent-wrong-output) | `TestGenerateModeMissingKeysFailClosed::test_missing_column_fails_closed_not_zero` |
| `col.get("op", "")` -> `"XXXX"` | a gen config with no `op`: real `""` -> `derived_aggregate_op_missing`, `"XXXX"` -> `op_invalid` (different operator-facing code) | `..::test_missing_op_fails_closed_with_missing_not_invalid` |

## EQUIVALENT (8)

Falsy gen-mode defaults that collapse to the same coded error, or a value-invariant
dtype.

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `col.get("op", "")` -> `None` / dropped (2) | the gen-mode cfg builder's `op` default | the default IS consulted (a generate-mode config with no `op` reaches `generate_derived_aggregate_column` -- there is no `_type_params_present` branch and `check_derived_aggregate_refs` short-circuits on an absent column). But an absent `op` feeds a falsy default (`None`/dropped -> `""`) into `from_dict`, which raises `derived_aggregate_op_missing` for every falsy value alike -- indistinguishable. (The `"XXXX"` variant is NOT equivalent: it raises `op_invalid` instead; killed -- see LOGIC.) |
| `col.get("column", "")` -> `None` / dropped (2) | the `column` default | same: a falsy `column` default raises `derived_aggregate_column_missing`; `None`/dropped are indistinguishable from `""`. (The `"XXXX"` variant is LOGIC -- it silently aggregates an empty series to 0; killed.) |
| `generated.get(config.column, [])` -> `None` / dropped (2) | source-values lookup default | `config.column` is a validated, declared-earlier sibling column, so it is always a key in `generated`; the `[]` default is never used. |
| `pd.Series(source_values, dtype=object)` -> `dtype=None` / dropped (2) | output-Series dtype | the aggregate value is dtype-invariant for the covered ops (an object-dtype Series and an inferred-dtype Series produce the same sum/mean/min/max/count scalar); no covered input distinguishes them. |

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + selection
`tests/unit/transforms/test_derived_aggregate.py`, then `rm -rf mutants && python -m mutmut run`.
