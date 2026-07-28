# Mutation grading: `execution/_distribution_behavior.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`). `_distribution_behavior.py` is
the per-strategy distribution-behavior metadata registry: a module-level constant
`_STATIC_BEHAVIOR` (24 strategy -> label entries) plus `distribution_behavior_for()`,
which resolves `categorical` dynamically from its provider config (weights /
from_profile) and everything else via the static map. Not crypto/RI, so the bar
is 75%.

## Numbers

**Re-graded (`scripts/tq_mutate.py`): 30/30 killed = 100% LOGIC, 0 unresolved.**
Baseline was 28/30 (93.33%); this sweep killed the 2 remaining survivors in
`distribution_behavior_for` and pinned the static constant per-value (finding #9).

| Function | Mutants | Killed | Survived |
|---|---|---|---|
| `distribution_behavior_for` | 30 | 30 | 0 |

## The 2 killed survivors (dynamic-config logic)

- **mut_14** `if from_profile is True` -> `is False`: differs only for
  `from_profile=False` (the bool singleton), where the mutant wrongly promotes the
  explicit-off case to `preserves_all`. The pre-existing suite tested
  `from_profile` True and int-1 and int-0, but not the `False` bool. Killed by
  `test_categorical_from_profile_bool_false_destroys_frequency`.
- **mut_25** `len(weights) > 0` -> `> 1`: differs at exactly one weight, where the
  mutant drops the single-weight source-weighted case to `destroys_frequency`. The
  suite tested 3-element and empty weights, not length 1. Killed by
  `test_categorical_single_weight_still_preserves_all`.

## Finding #9 (constant coverage)

`_STATIC_BEHAVIOR` is not mutated by mutmut. The pre-existing golden pinned only 12
of its entries per-value. Extended `test_static_strategy_labels` to pin all 23
statically-resolved entries (`categorical` is resolved dynamically before the
static lookup, covered by `TestCategoricalDynamicResolution`), plus a drift guard
`test_static_parametrize_covers_the_whole_static_map` that fails if a future entry
is added to the constant without a golden row.

## Tests

`tests/unit/providers_v2/test_distribution_behavior_metadata.py` (extended): 40
tests green on real code; ruff format + check clean.

## EQUIVALENT / survivors

None. All 30 LOGIC mutants killed; the constant is fully pinned per-value.

## Candidate findings

None. 0 product bugs.

## Regenerate

`[tool.mutmut]` `only_mutate = ["src/decoy_engine/execution/_distribution_behavior.py"]`,
`pytest_add_cli_args_test_selection = ["tests/unit/providers_v2/test_distribution_behavior_metadata.py"]`,
then `python scripts/tq_mutate.py --run`.
