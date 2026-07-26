# Mutation grading: `transforms/formula.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `transforms/formula.py` (57 LOC) is the V1
`FormulaStrategy`: `apply` reads the `formula` expression from the rule, passes an
empty/absent formula through unchanged, derives a per-formula RNG seed from
`sha256(f"{column}|{formula}")[:16]` (base 16) so a random expression is
deterministic per (column, formula) but does not share module-global RNG state,
and evaluates the expression per non-null cell via the `expressions.safe_eval`
sandbox. The sandbox primitives themselves (`safe_eval`, `make_mask_globals`) live
in `decoy_engine.expressions` and are tested there; this grade covers `apply`.

**Grade scope: FOCUSED selection** (`tests/unit/execution/test_dateshift_formula.py`,
via the strategy adapter plus direct `FormulaStrategy.apply` tests).

## Numbers

**51 mutants: 31 killed (61% baseline), 20 survived -> 40 killed after this pass,
11 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **9 LOGIC survivors killed** with 3 new direct-apply tests
  (`TestFormulaTransformDirect`) plus the existing adapter-path formula tests.
- **11 EQUIVALENT survivors** (formula None default + warning-message prose).

## LOGIC (9): killed by new tests

| Mutants | Mutation | Killed by |
|---|---|---|
| `apply_29`, `38` | `formula_seed = None` / `random.Random(None)` (non-deterministic RNG) | `test_random_output_is_deterministic_and_seed_pinned` (two runs must match; a nulled seed draws from system entropy) |
| `apply_35`, `36` | seed `hexdigest()[:17]` / `int(..., 17)` (a different seed) | same (exact-output KAT: the changed seed yields different values) |
| `apply_8` | `rule.get("formula", "XXXX")` (a bogus non-empty default that would eval and raise on an absent formula) | `test_missing_formula_key_passes_through` (direct `apply` with no `formula` key must pass through, not raise) |
| `apply_21`, `23`, `26`, `27` | `col_name = rule.get("column", None/dropped/"XXunnamedXX"/"UNNAMED")` (a wrong default changes the seed's name component) | `test_missing_column_key_defaults_to_unnamed_seed` (an absent `column` must seed as `"unnamed"`, matching an explicit `column="unnamed"`) |

## EQUIVALENT (11)

| Mutants | Category | Why equivalent |
|---|---|---|
| `3`, `5` | `rule.get("formula", None)` / dropped | `None` is falsy exactly like the `""` default, so `if not expr` still passes through -- identical behavior for an absent formula. |
| `10`, `11`, `12`, `13`, `14`, `15`, `16`, `17`, `18` | warning-message f-string (message `None`, and `rule.get(...)` key/default variants INSIDE the `logger.warning` string) | these only alter the human-readable warning text emitted when the formula is absent; the return value (`column.copy()`) is unchanged and the log string is never machine-consumed. |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/formula.py`, selection to
`tests/unit/execution/test_dateshift_formula.py`, then
`rm -rf mutants && python -m mutmut run`.
