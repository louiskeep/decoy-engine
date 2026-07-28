# Mutation grading: `execution/_technique_class.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`). `_technique_class.py` is the
GDPR technique-classification registry: a module-level constant
`TECHNIQUE_CLASS_BY_STRATEGY` (23 strategy -> class entries) plus one small
function `technique_class_for(strategy)`. Not crypto/RI, so the bar is 75%.

## Numbers

**Re-graded (`scripts/tq_mutate.py`): 2/2 killed = 100% LOGIC, 0 unresolved.**
mutmut generates only 2 mutants for this module -- both inside
`technique_class_for`, both already killed by the pre-existing suite. There is
no new mutmut work here.

| Function | Mutants | Killed | Survived |
|---|---|---|---|
| `technique_class_for` | 2 | 2 | 0 |

## The real gap this sweep closed (finding #9)

mutmut's 2/2 is MISLEADING on its own: the module's actual content is the
23-entry `TECHNIQUE_CLASS_BY_STRATEGY` constant, and mutmut does not mutate
module-level constants (tq-findings #9). So the strategy -> class MAPPING was not
mutation-covered. The pre-existing parametrized golden
(`test_classification_matches_industry_taxonomy`) pinned only 11 of the 23
entries per-value; `test_every_shipped_strategy_is_classified` checks only that
every strategy IS classified, not that it maps to the CORRECT class. A wrong
class on any of the other 12 entries would have passed silently.

Closed by:
- Extending the parametrized golden to pin ALL 23 entries per-value (hardcoded
  strategy -> class literals, NOT recomputed from the constant -- no
  self-referential oracle).
- Adding `test_taxonomy_match_parametrize_covers_the_whole_taxonomy`: a drift
  guard asserting the pinned set equals `set(TECHNIQUE_CLASS_BY_STRATEGY)`, so a
  future strategy added to the constant without a golden row fails the suite.

## Tests

`tests/unit/execution/test_technique_class.py` (extended): 35 tests green on real
code; ruff format + check clean. Existing structural tests retained
(`test_four_user_visible_classes_only`, the unknown/None/empty guards, the
ColumnSeed round-trip cells).

## EQUIVALENT / survivors

None. Both mutmut LOGIC mutants are killed; the constant is now fully pinned by
the per-value golden.

## Candidate findings

None. Every taxonomy entry classifies to the industry-standard class its
docstring documents; the extended golden pins the current mapping as the
regression contract.

## Regenerate

`[tool.mutmut]` `only_mutate = ["src/decoy_engine/execution/_technique_class.py"]`,
`pytest_add_cli_args_test_selection = ["tests/unit/execution/test_technique_class.py"]`,
then `python scripts/tq_mutate.py --run`.
