# Mutation grading: `execution/_strategies/_categorical.py` -- LOGIC-100%

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`_categorical.py` with the test selection restricted to
`tests/unit/execution/test_categorical_weighted.py` (28 tests, ~0.4s). Integration
and pipeline suites that also exercise the categorical strategy were NOT in the
selection, so the survivor count and the resulting score are a conservative lower
bound: some mutants counted "survived" here are in fact killed by tests outside
this file.

TQ crown-jewels pass, 2026-07-25. `_categorical` remaps a column onto a category
pool (uniform, or weighted through a CDF over a fixed integer resolution;
deterministic keyed by `derive_index`, or non-deterministic via an unseeded rng).
A mutmut run produced **238 mutants, 123 killed (52% baseline), 115 survived**.
Every survivor was classified LOGIC or EQUIVALENT per
`docs/quality/module-test-quality-playbook.md` ("Scope the score to LOGIC, not
error-message wording"). **71 LOGIC survivors** were killed with **13 new tests**;
**44 survive and are equivalent** (error-message prose, message-only arithmetic,
or an unreachable defensive clamp), tabled below with the one-line argument for
why no input distinguishes them from the original.

Verification note: `str(StrategyError)` embeds `code`, `strategy` AND `message`
(`f"[{code}] strategy={strategy!r}: {message}"`), so the pre-existing
`pytest.raises(match=...)` assertions could not distinguish a mutated `code` or
`strategy` from the original whenever the mutation kept the matched substring (an
`XX`-wrap of a code still contains the code). Every new error-field test pins the
exact `.code` and `.strategy` attributes instead of matching the rendered string.

Bugs found in `_categorical.py`: none introduced or newly exposed by this pass.

## LOGIC (71): killed by new tests in this pass

All killing tests live in `tests/unit/execution/test_categorical_weighted.py`.
Grouped by the method the mutant lands in.

### `_build_cdf` (18)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_6`, `13`, `14` | sum<=0 error: `strategy=None` / `"XXcategoricalXX"` / `"CATEGORICAL"` | `TestCdfErrorFields::test_nonpositive_error_fields` |
| `__mutmut_11` | sum<=0 error: `code="XXcategorical_weights_nonpositiveXX"` | same |
| `__mutmut_25`, `31`, `32` | negative-weight error: `code=None` / `code` XX-wrap / uppercased | `test_negative_error_fields` |
| `__mutmut_26`, `33`, `34` | negative-weight error: `strategy=None` / XX-wrap / uppercased | same |
| `__mutmut_45`, `51`, `52` | below-resolution error: `code=None` / XX-wrap / uppercased | `test_below_resolution_error_fields` |
| `__mutmut_46`, `53`, `54` | below-resolution error: `strategy=None` / XX-wrap / uppercased | same |
| `__mutmut_20`, `21` | `prev_threshold = 0` -> `None` / `1` (an index-0 weight that rounds to threshold 0 slips past the zero-width guard) | `test_first_weight_below_resolution_raises` |

### `run` -- config validation (33)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_3`, `4`, `5`, `6` | `raw_categories = cfg.get("categories")` -> `None` / `cfg.get(None)` / wrong key (guard reads nothing, string slips through) | `TestRunConfigErrorFields::test_not_list_categories_raises` |
| `run__mutmut_9` | not-list guard `not cfg.get("from_profile")` -> `cfg.get("from_profile")` (guard inverted, skips) | same |
| `run__mutmut_13` | not-list guard `raw_categories is not None` -> `is None` (guard skips for a present value) | same |
| `run__mutmut_15`, `21`, `22` | not-list error: `code=None` / XX-wrap / uppercased | same |
| `run__mutmut_16`, `23`, `24` | not-list error: `strategy=None` / XX-wrap / uppercased | same |
| `run__mutmut_18`, `19` | not-list error: `code=` / `strategy=` kwarg dropped (both required -> `TypeError`) | same |
| `run__mutmut_10`, `11`, `12` | from_profile bypass `not cfg.get("from_profile")` -> `not cfg.get(None)` / wrong key (bypass never fires, non-list wrongly rejected) | `test_from_profile_bypasses_not_list_guard` |
| `run__mutmut_34`, `36` | `list(cfg.get("categories", []))` -> default `None` / dropped (`list(None)` -> `TypeError` when key absent) | `test_absent_categories_raises_requires` |
| `run__mutmut_40`, `46`, `47` | no-categories error: `code=None` / XX-wrap / uppercased | same |
| `run__mutmut_41`, `48`, `49` | no-categories error: `strategy=None` / XX-wrap / uppercased | same |
| `run__mutmut_43`, `44` | no-categories error: `code=` / `strategy=` kwarg dropped (`TypeError`) | same |
| `run__mutmut_60`, `67`, `68` | weights-shape error: `strategy=None` / XX-wrap / uppercased | `test_weights_shape_error_fields` |
| `run__mutmut_65` | weights-shape error: `code="XXcategorical_weights_shapeXX"` | same |
| `run__mutmut_84`, `90`, `91` | no-namespace error: `code=None` / XX-wrap / uppercased | `test_requires_namespace_error_fields` |
| `run__mutmut_85`, `92`, `93` | no-namespace error: `strategy=None` / XX-wrap / uppercased | same |
| `run__mutmut_87`, `88` | no-namespace error: `code=` / `strategy=` kwarg dropped (`TypeError`) | same |
| `run__mutmut_148`, `155`, `156` | non-det sum<=0 error: `strategy=None` / XX-wrap / uppercased | `test_non_deterministic_nonpositive_error_fields` |
| `run__mutmut_153` | non-det sum<=0 error: `code="XXcategorical_weights_nonpositiveXX"` | same |

### `run` -- null handling + picks (20)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_97` | uniform deterministic null loop `continue` -> `break` (truncates output, length desyncs on df assign) | `TestNullPreservation::test_nulls_pass_through_in_deterministic_uniform` |
| `run__mutmut_135` | `picks = rng.integers(0, len(categories), n)` -> `picks = None` (`None[i]` -> `TypeError`) | `TestNonDeterministicUniform::test_uniform_picks_are_valid_indices` |
| `run__mutmut_136` | `rng.integers(None, len, n)` (`TypeError`) | same |
| `run__mutmut_137` | `rng.integers(0, None, n)` (`high<=0` -> `ValueError`) | same |
| `run__mutmut_138` | `rng.integers(0, len, None)` (scalar -> `picks[i]` `TypeError`) | same |
| `run__mutmut_139`, `140`, `141` | dropped arg -> two-positional `integers` returns a scalar (`TypeError` on `picks[i]`) | same |
| `run__mutmut_142` | `rng.integers(1, len, n)` (low shifted; first category index never drawn) | same |
| `run__mutmut_160` | non-det weighted `normalized = [w / total ...]` -> `[w * total ...]` (probabilities sum to total**2, numpy rejects) | `TestNonDeterministicWeightedNormalization::test_unnormalized_weights_normalized_by_total` |

## EQUIVALENT (44)

### Error-message prose (37)

`StrategyError` carries a machine `code` + `strategy` (both asserted) and a human
`message`. These mutants set `message=None`, drop the `message=` kwarg (it
defaults to `""`, so the raise still succeeds -- contrast `code=`/`strategy=`,
which are required and land in the LOGIC table), wrap a message fragment in
`XX...XX`, change its case, or mutate an f-string interpolation
(`type(x).__name__` -> `type(None).__name__`, `hasattr(weights_raw, ...)` ->
`hasattr(None, ...)`). Every one flows only into the human `message`; tests assert
`.code`/`.strategy`, so no input distinguishes them.

| Raise site | Mutants |
|---|---|
| `_build_cdf` sum<=0 | `__mutmut_7` (`message=None`), `10` (kwarg dropped), `15`, `16` (prose) |
| `_build_cdf` negative weight | `__mutmut_27` (`message=None`), `30` (kwarg dropped) |
| `_build_cdf` below resolution | `__mutmut_47` (`message=None`), `50` (kwarg dropped), `55`-`59` (prose fragments) |
| `run` not-list | `run__mutmut_17` (`message=None`), `20` (kwarg dropped), `25` (`type(None).__name__`), `26`-`30` (prose) |
| `run` no categories | `run__mutmut_42` (`message=None`), `45` (kwarg dropped) |
| `run` weights shape | `run__mutmut_61` (`message=None`), `64` (kwarg dropped), `69` (`type(None).__name__`), `70` (`hasattr(None, ...)`), `74`-`77` (prose) |
| `run` no namespace | `run__mutmut_86` (`message=None`), `89` (kwarg dropped) |
| `run` non-det sum<=0 | `run__mutmut_149` (`message=None`), `152` (kwarg dropped), `157`, `158` (prose) |

### Message-only arithmetic (3)

The below-resolution error message ends with a suggested minimum weight,
`f"{1.0 / _WEIGHTED_CDF_RES * total:.2e}"`. These mutate that expression
(`* total` -> `/ total`, `1.0 /` -> `1.0 *`, `1.0` -> `2.0`). The result is
formatted only into the human message; the `.code`/`.strategy` and the raise
condition are unchanged, so no input distinguishes them.

| Raise site | Mutants |
|---|---|
| `_build_cdf` below resolution | `__mutmut_60`, `61`, `62` |

### Unreachable defensive clamp (4)

The weighted deterministic path derives `bucket` with `pool_size =
_WEIGHTED_CDF_RES`, so `bucket` is always in `[0, _WEIGHTED_CDF_RES)`. The CDF's
last entry is exactly `_WEIGHTED_CDF_RES`, and it is strictly greater than any
`bucket`, so `bisect_right(cdf, bucket)` always returns an index `<=
len(categories) - 1`. The guard `if cat_idx >= len(categories)` can therefore
never be true, and every mutation of the guard or its clamp body is dead code.

| Mutant | Mutation | Why unreachable |
|---|---|---|
| `run__mutmut_128` | `cat_idx >= len(categories)` -> `> len(categories)` | guard never true for either operator |
| `run__mutmut_129` | `cat_idx = len(categories) - 1` -> `= None` | clamp body never executes |
| `run__mutmut_130` | clamp `- 1` -> `+ 1` | never executes |
| `run__mutmut_131` | clamp `- 1` -> `- 2` | never executes |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to this module + test selection
`tests/unit/execution/test_categorical_weighted.py`, then
`rm -rf mutants && python -m mutmut run`.
