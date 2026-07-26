# Mutation grading: `execution/_strategies/_derived.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-26. `_derived` is a thin per-row handler: it iterates the
frame and calls `transforms.derived.apply_derived(config, row_context, column=,
row_index=)` for each row (the closed-expression evaluation lives in the transforms
module). Graded with the FOCUSED selection `tests/unit/execution/test_derived_strategy.py`
(~0.3s). Conservative lower bound.

**19 mutants: 13 killed baseline (68%) -> 18 killed, 1 equivalent.** LOGIC-mutant
score 100%.

## LOGIC killed this pass (2 new tests)

| Mutants | Mutation | Killed by |
|---|---|---|
| `itertuples(index=False)` -> `index=True` | with `index=True` the row tuple carries the frame Index as a field named `Index`, so a source column literally named `Index` is renamed and `row_context["Index"]` becomes the row number -- the derived expression then computes on the row number, not the column value | `test_row_context_is_columns_not_the_frame_index` (column `Index`, expr `Index * 2`) |
| `apply_derived(..., column=column, row_index=row_idx)` -> `column=None` / `column` dropped / `row_index=None` / `row_index` dropped (4) | `apply_derived` names the column + row index in its evaluation-error message (operator diagnostics); a None/dropped arg drops them from the message | `test_eval_error_names_the_column_and_row_index` (a divide-by-zero at row 3; asserts `'c'` and `row 3` in the message) |

## EQUIVALENT (1)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `itertuples(index=False)` -> `index=None` | pandas treats the `index` param as boolean; `None` is falsy, so `index=None` behaves identically to `index=False` (verified: same `_asdict()` output, no `Index` field). No input distinguishes them. |

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + selection
`tests/unit/execution/test_derived_strategy.py`, then `rm -rf mutants && python -m mutmut run`.
