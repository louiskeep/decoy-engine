# Mutation grading: `transforms/derived_aggregate.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-26. `derived_aggregate` computes a scalar aggregate
(sum / mean / min / max / count) over a source column, with NULLs excluded per
the SQL set-function rule. Graded with the FOCUSED selection
`tests/unit/transforms/test_derived_aggregate.py` (~0.3s). Conservative lower
bound.

**58 mutants: 46 killed baseline (79%) -> 48 killed, 10 equivalent.** LOGIC-mutant
score 100%.

## LOGIC killed this pass (2 new tests)

| Mutants | Mutation | Killed by |
|---|---|---|
| `series.min(skipna=True)` -> `skipna=False`, `series.max(skipna=True)` -> `skipna=False` | min/max would return NaN once any null is present, breaking the SQL null-exclusion contract (sum's skipna was already tested; min/max were not) | `test_min_excludes_nulls`, `test_max_excludes_nulls` (min/max over `[3, None, 1, 2]`) |

## EQUIVALENT (10)

All are gen-mode default values that are unreachable given the surrounding
validation, or a value-invariant dtype.

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `col.get("op", "")` -> `None` / dropped / `"XXXX"` (3) | the gen-mode cfg builder's `op` default | `DerivedAggregateConfig.from_dict` requires `op` (raises `PlanCompileError` if missing) and rejects any `op` not in `AGGREGATE_OPS`, and the plan compiler validates it before runtime, so `col` always carries a valid `op` here -- the default is never consulted. |
| `col.get("column", "")` -> `None` / dropped / `"XXXX"` (3) | the `column` default | same: `from_dict` requires `column`; it is always present at runtime, default unreached. |
| `generated.get(config.column, [])` -> `None` / dropped (2) | source-values lookup default | `config.column` is a validated, declared-earlier sibling column, so it is always a key in `generated`; the `[]` default is never used. |
| `pd.Series(source_values, dtype=object)` -> `dtype=None` / dropped (2) | output-Series dtype | the aggregate value is dtype-invariant for the covered ops (an object-dtype Series and an inferred-dtype Series produce the same sum/mean/min/max/count scalar); no covered input distinguishes them. |

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + selection
`tests/unit/transforms/test_derived_aggregate.py`, then `rm -rf mutants && python -m mutmut run`.
