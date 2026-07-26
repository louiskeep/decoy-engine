# Mutation grading: `execution/_strategies/_formula.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_formula.py` (38 LOC) is a thin handler for
the user-defined safe-eval transform: it reads `formula` from provider_config,
builds a `{"formula": ..., "column": column}` rule, and delegates to the reused V1
`transforms.formula.FormulaStrategy.apply` (the simpleeval sandbox itself is
graded with that module, not here).

**Grade scope: FOCUSED selection only** (`tests/unit/execution/test_dateshift_formula.py`).

## Numbers

**19 mutants: 14 killed (74% baseline), 5 survived -> 17 killed after this pass,
2 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **3 LOGIC survivors killed** with 2 new tests. 0 product bugs.
- **2 EQUIVALENT survivors** (`formula` default None vs ""). Both falsy, both
  passthrough.

## LOGIC (3): killed by new tests

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_12` | `cfg.get("formula", "")` -> default `"XXXX"` (an absent formula would eval the bogus expression `"XXXX"` and raise, instead of passing through) | `test_missing_formula_config_passes_through_unchanged` (no `formula` config; asserts the column is returned unchanged, where the mutant raises `NameNotDefined`) |
| `run__mutmut_13`, `14` | rule key `"column"` -> `"XXcolumnXX"` / `"COLUMN"` (the wrong key makes `apply` read `rule.get("column", "unnamed")` as `"unnamed"`, so the per-formula RNG seed no longer folds in the real column name) | `test_random_formula_is_seeded_by_column_name` (the same random expression on two differently-named columns yields different output; the mutant seeds both as `"unnamed"` -> identical output) |

## EQUIVALENT (2)

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_7`, `9` | `cfg.get("formula", "")` -> default `None` / dropped | `FormulaStrategy.apply` treats a falsy `formula` (`if not expr`) as passthrough, and `None` is falsy exactly like `""`, so an absent formula behaves identically either way (verified: both return the column unchanged). |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `_formula.py`, selection to
`test_dateshift_formula.py`, then `rm -rf mutants && python -m mutmut run`.
