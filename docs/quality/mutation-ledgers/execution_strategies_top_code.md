# Equivalent-mutant ledger: `execution/_strategies/_top_code.py`

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`_top_code.py` with the test selection restricted to
`tests/unit/execution/test_top_code.py`. Integration and pipeline tests that
also exercise top-coding (the chunked-route suites, end-to-end masking runs)
were NOT in the selection, so the survivor count and the resulting score are a
conservative lower bound on the module's real coverage: some mutants counted
"survived" here are in fact killed by tests outside this file.

TQ crown-jewels pass, 2026-07-25. A mutmut run against
`execution/_strategies/_top_code.py` (HC-3b top-coding / bottom-coding
generalization, e.g. HIPAA age>89 -> "90+") produced **415 mutants, 271 killed
(65% baseline), 144 survived**. Every survivor was classified LOGIC or
EQUIVALENT per `docs/quality/module-test-quality-playbook.md` ("Scope the score
to LOGIC, not error-message wording"). **83 LOGIC survivors** were killed with
**37 new tests** in `tests/unit/execution/test_top_code.py` (final: 354/415
killed); **61 survive and are equivalent** (message prose, reason prose, or a
true behavioral no-op), tabled below with the one-line argument for why no input
can distinguish them from the original.

Verification note: the two `in_range` mask index mutants (`mutmut_115`/`117`)
initially survived the general index-alignment test because a misaligned mask
collapses `result.where(~in_range_mask, in_range_str)` to use `in_range_str`
everywhere, which is correct at in-range rows (over/under rows are overwritten
afterward). They are only observable through an UNCOERCIBLE cell at a non-default
index, which must pass through unchanged but gets nulled under the misaligned
mask; `test_in_range_mask_alignment_observed_via_uncoercible_passthrough` pins
exactly that.

Bugs found in `_top_code.py`: none introduced or newly exposed by this pass.

## LOGIC (83): killed by new tests in this pass

All killing tests live in `tests/unit/execution/test_top_code.py`. Grouped by
the method the mutant lands in.

### `_is_usable_bound` (4) -- `TestIsUsableBoundContract`

| Mutant | Mutation | Killed by |
|---|---|---|
| `x__is_usable_bound__mutmut_4` | `isinstance(value, float) and not isfinite` -> `... or not isfinite` (a huge Python int now hits `math.isfinite`, raising `OverflowError` instead of classifying by magnitude) | `test_huge_python_int_rejected_without_overflow` |
| `x__is_usable_bound__mutmut_5` | `and not math.isfinite(value)` -> `and math.isfinite(value)` (a finite float is wrongly rejected) | `test_finite_float_is_usable` |
| `x__is_usable_bound__mutmut_6` | `math.isfinite(value)` -> `math.isfinite(None)` (`TypeError` on any float) | `test_finite_float_is_usable` |
| `x__is_usable_bound__mutmut_7` | non-finite branch `return False` -> `return True` (inf/nan wrongly accepted as usable) | `test_infinite_float_rejected`, `test_nan_float_rejected` |

### `_parse_exact` (23) -- `TestParseExactContract`

The parametrized `test_parse_exact_tuple` asserts the exact classification
tuple for every branch, killing all tag-literal, tag-value, and returned-int
mutations.

| Mutant | Mutation |
|---|---|
| `x__parse_exact__mutmut_1` | bool -> `("XXbadXX", 0)` |
| `x__parse_exact__mutmut_2` | bool -> `("BAD", 0)` |
| `x__parse_exact__mutmut_3` | bool -> `("bad", 1)` |
| `x__parse_exact__mutmut_4` | int -> `("XXintXX", value)` |
| `x__parse_exact__mutmut_5` | int -> `("INT", value)` |
| `x__parse_exact__mutmut_6` | float: `if not math.isfinite(value)` -> `if math.isfinite(value)` |
| `x__parse_exact__mutmut_7` | float: `math.isfinite(value)` -> `math.isfinite(None)` |
| `x__parse_exact__mutmut_8` | non-finite float -> `("XXbadXX", 0)` |
| `x__parse_exact__mutmut_9` | non-finite float -> `("BAD", 0)` |
| `x__parse_exact__mutmut_10` | non-finite float -> `("bad", 1)` |
| `x__parse_exact__mutmut_11` | integral float -> `("XXintXX", ...)` |
| `x__parse_exact__mutmut_12` | integral float -> `("INT", ...)` |
| `x__parse_exact__mutmut_13` | integral float -> `("int", int(None))` (`TypeError`) |
| `x__parse_exact__mutmut_14` | fractional float -> `("XXfracXX", 0)` |
| `x__parse_exact__mutmut_15` | fractional float -> `("FRAC", 0)` |
| `x__parse_exact__mutmut_16` | fractional float -> `("frac", 1)` |
| `x__parse_exact__mutmut_22` | non-parseable string -> `("bad", 1)` |
| `x__parse_exact__mutmut_24` | non-finite Decimal -> `("XXbadXX", 0)` |
| `x__parse_exact__mutmut_25` | non-finite Decimal -> `("BAD", 0)` |
| `x__parse_exact__mutmut_26` | non-finite Decimal -> `("bad", 1)` |
| `x__parse_exact__mutmut_27` | integral Decimal -> `("XXintXX", ...)` |
| `x__parse_exact__mutmut_28` | integral Decimal -> `("INT", ...)` |
| `x__parse_exact__mutmut_33` | fractional Decimal -> `("frac", 1)` |

### `run` (19)

| Mutant | Mutation | Killed by |
|---|---|---|
| `xǁTopCodeStrategyHandlerǁrun__mutmut_7` | unresolvable-bound error: `strategy="top_code"` -> `strategy=None` | `TestErrorMachineFields::test_unresolvable_bound_strategy_and_code` |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_14` | same error: `strategy="XXtop_codeXX"` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_15` | same error: `strategy="TOP_CODE"` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_33` | object path: `_classify_object_column(..., cap, None, under_label, ...)` (floor dropped, bottom-coding silently disabled) | `TestObjectBottomCoding::test_object_bottom_coding_boundaries` |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_34` | object path: `_classify_object_column(..., cap, floor, None, ...)` (under_label dropped) | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_46` | numeric path: `pd.to_numeric(col, errors="coerce")` -> `pd.to_numeric(col)` (default `errors="raise"` aborts instead of quarantining) | `TestNumericPathRowError::test_uncoercible_cell_is_quarantined_not_raised` |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_51` | `np.flatnonzero(...to_numpy())` -> `np.flatnonzero(None)` (no RowError recorded) | `TestNumericPathRowError::test_row_error_machine_fields` |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_52` | `row_errors.append(RowError(...))` -> `append(None)` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_53` | RowError `column=column` -> `column=None` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_54` | RowError `row_index=int(i)` -> `row_index=None` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_55` | RowError `trigger="format_error"` -> `trigger=None` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_57` | RowError `column=` kwarg dropped (required field -> `TypeError`) | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_58` | RowError `row_index=` kwarg dropped (`TypeError`) | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_59` | RowError `trigger=` kwarg dropped (`TypeError`) | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_60` | RowError `reason=` kwarg dropped (required field -> `TypeError`) | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_61` | RowError `row_index=int(i)` -> `int(None)` (`TypeError`) | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_62` | RowError `trigger="XXformat_errorXX"` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_63` | RowError `trigger="FORMAT_ERROR"` | same |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_132` | evidence warning gate `len(over) or len(under)` -> `... and ...` (over-only tail no longer warns) | `TestWarningEmittedForOverOnlyTail::test_over_only_still_emits_warning` |

### `_classify_object_column` (23)

| Mutant | Mutation | Killed by |
|---|---|---|
| `xǁ...ǁ_classify_object_column__mutmut_1` | `do_under = floor is not None and under_label is not None` -> `do_under = None` | `TestObjectBottomCoding::test_object_bottom_coding_boundaries` |
| `xǁ...ǁ_classify_object_column__mutmut_3` | `do_under = floor is None and under_label is not None` | same |
| `xǁ...ǁ_classify_object_column__mutmut_4` | `do_under = floor is not None and under_label is None` | same |
| `xǁ...ǁ_classify_object_column__mutmut_12` | `under = np.zeros(n, dtype=bool)` -> `np.zeros(None, dtype=bool)` (0-d array -> `IndexError` on assignment) | same |
| `xǁ...ǁ_classify_object_column__mutmut_96` | `elif do_under and floor is not None and exact < floor` -> `elif do_under or ...` (every non-over cell becomes under) | same |
| `xǁ...ǁ_classify_object_column__mutmut_97` | same elif: `floor is not None` -> `floor is None` (below-floor cell no longer generalized) | same |
| `xǁ...ǁ_classify_object_column__mutmut_98` | same elif: `exact < floor` -> `exact <= floor` (value AT the floor wrongly generalized) | same |
| `xǁ...ǁ_classify_object_column__mutmut_99` | `under[pos] = True` -> `under[pos] = None` (coerced to False) | same |
| `xǁ...ǁ_classify_object_column__mutmut_100` | `under[pos] = True` -> `under[pos] = False` | same |
| `xǁ...ǁ_classify_object_column__mutmut_105` | `index = col.index` -> `index = None` (all returned Series lose the source index) | `TestObjectSeriesIndexAlignment::test_non_default_index_preserved_and_aligned` |
| `xǁ...ǁ_classify_object_column__mutmut_107` | `pd.Series(over, index=index)` -> `index=None` | same |
| `xǁ...ǁ_classify_object_column__mutmut_109` | `pd.Series(over, index=index)` -> `index=` dropped | same |
| `xǁ...ǁ_classify_object_column__mutmut_111` | `pd.Series(under, index=index)` -> `index=None` | same |
| `xǁ...ǁ_classify_object_column__mutmut_113` | `pd.Series(under, index=index)` -> `index=` dropped | same |
| `xǁ...ǁ_classify_object_column__mutmut_115` | `pd.Series(in_range, index=index)` -> `index=None` | `TestObjectSeriesIndexAlignment::test_in_range_mask_alignment_observed_via_uncoercible_passthrough` |
| `xǁ...ǁ_classify_object_column__mutmut_117` | `pd.Series(in_range, index=index)` -> `index=` dropped | same |
| `xǁ...ǁ_classify_object_column__mutmut_119` | `pd.Series(render, index=index, dtype=object)` -> `index=None` | same |
| `xǁ...ǁ_classify_object_column__mutmut_122` | `pd.Series(render, index=index, dtype=object)` -> `index=` dropped | same |
| `xǁ...ǁ_classify_object_column__mutmut_29` | non-scalar RowError `column=column` -> `column=None` | `TestObjectRowErrorColumnField::test_array_like_cell_row_error_names_column` |
| `xǁ...ǁ_classify_object_column__mutmut_43` | null-cell `continue` -> `break` (truncates the loop, later tail cells leak) | `TestObjectNullBreak::test_tail_after_null_still_generalized` |
| `xǁ...ǁ_classify_object_column__mutmut_69` | fractional-column error: `strategy="top_code"` -> `strategy=None` | `TestFractionalObjectStrategyField::test_fractional_column_error_attributes_strategy` |
| `xǁ...ǁ_classify_object_column__mutmut_76` | same error: `strategy="XXtop_codeXX"` | same |
| `xǁ...ǁ_classify_object_column__mutmut_77` | same error: `strategy="TOP_CODE"` | same |

### `preflight` (3) -- `TestErrorMachineFields::test_preflight_strategy_field`

| Mutant | Mutation |
|---|---|
| `xǁTopCodeStrategyHandlerǁpreflight__mutmut_2` | `strategy="top_code"` -> `strategy=None` |
| `xǁTopCodeStrategyHandlerǁpreflight__mutmut_9` | `strategy="XXtop_codeXX"` |
| `xǁTopCodeStrategyHandlerǁpreflight__mutmut_10` | `strategy="TOP_CODE"` |

### `_resolve_top_bound` (1) -- `TestResolveMachineFields::test_resolve_top_empty_over_label_is_unresolvable`

| Mutant | Mutation |
|---|---|
| `xǁ...ǁ_resolve_top_bound__mutmut_24` | over_label guard `not isinstance(...) or not over_label` -> `... and ...` (empty over_label wrongly accepted) |

### `_resolve_bottom_bound` (10) -- `TestResolveMachineFields`

| Mutant | Mutation | Killed by |
|---|---|---|
| `xǁ...ǁ_resolve_bottom_bound__mutmut_11` | invalid-floor error: `strategy="top_code"` -> `strategy=None` | `test_invalid_floor_strategy_field` |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_18` | invalid-floor error: `strategy="XXtop_codeXX"` | same |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_19` | invalid-floor error: `strategy="TOP_CODE"` | same |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_29` | under_label guard `not isinstance(...) or not under_label` -> `... and ...` (empty under_label wrongly accepted) | `test_empty_under_label_rejected` |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_32` | missing-under_label error: `code="top_code_bounds_unresolvable"` -> `code=None` | `test_floor_without_under_label_code_and_strategy` |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_33` | same error: `strategy="top_code"` -> `strategy=None` | same |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_38` | same error: `code="XXtop_code_bounds_unresolvableXX"` | same |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_39` | same error: `code="TOP_CODE_BOUNDS_UNRESOLVABLE"` | same |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_40` | same error: `strategy="XXtop_codeXX"` | same |
| `xǁ...ǁ_resolve_bottom_bound__mutmut_41` | same error: `strategy="TOP_CODE"` | same |

## EQUIVALENT (61)

### Message prose (48)

mutmut sets `message=None`, drops the `message=` kwarg (`ExecutionError.message`
defaults to `""`, so a dropped kwarg is a safe no-op), wraps a fragment in
`XX...XX`, or changes its case, and also mutates the `cfg.get('preset')` /
`cfg.get('cap')` lookups that only feed the f-string. In every case the literal
is consumed only inside a raised error's human `message`; it never becomes the
exception's `code`/`strategy`, a return value, or a comparison target. Tests
assert `code`/`strategy`, so only pure-wording variants survive.

| Method | Mutants |
|---|---|
| `run` (unresolvable-bound error) | `mutmut_8` (`message=None`), `mutmut_11` (kwarg dropped), `mutmut_16`, `mutmut_17`, `mutmut_18` (preset lookup / prose), `mutmut_19`, `mutmut_20`, `mutmut_21` (cap lookup / prose), `mutmut_22`, `mutmut_23` (prose) |
| `_classify_object_column` (fractional-column error) | `mutmut_70` (`message=None`), `mutmut_73` (kwarg dropped), `mutmut_78`–`mutmut_89` (prose fragments: `XX...XX` / case) |
| `preflight` (when-gate error) | `mutmut_3` (`message=None`), `mutmut_6` (kwarg dropped), `mutmut_11`–`mutmut_21` (prose fragments) |
| `_resolve_bottom_bound` (invalid-floor error) | `mutmut_12` (`message=None`), `mutmut_15` (kwarg dropped), `mutmut_20`–`mutmut_24` (prose fragments) |
| `_resolve_bottom_bound` (missing-under_label error) | `mutmut_34` (`message=None`), `mutmut_37` (kwarg dropped), `mutmut_42`, `mutmut_43` (prose fragments) |

### Reason prose (8)

The RowError `reason` is human-readable prose (per `_row_errors.py`: "never
embeds the cell value", travels into logs/manifests). No machine consumer reads
it; the only test touching `reason` is the trap-T3 negative check that a raw
value is absent, which a mutated string still satisfies. Setting `reason=None`
or mutating its text is indistinguishable. (Note: DROPPING the `reason=` kwarg
is a different mutant -- it is LOGIC, since `reason` is a required dataclass
field and its omission raises `TypeError`; see `run` `mutmut_60` above.)

| Method | Mutants |
|---|---|
| `run` (numeric-path RowError) | `mutmut_56` (`reason=None`), `mutmut_64` (`XX...XX`), `mutmut_65` (uppercased) |
| `_classify_object_column` (non-scalar RowError) | `mutmut_32` (`reason=None`), `mutmut_39` (`XX...XX`), `mutmut_40` (uppercased) |
| `_classify_object_column` (bad-parse RowError) | `mutmut_61` (`XX...XX`), `mutmut_62` (uppercased) |

### Behavioral no-op (5)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `xǁTopCodeStrategyHandlerǁrun__mutmut_69` | `if floor is not None and under_label is not None` -> `... or ...` | `_resolve_bottom_bound` returns floor and under_label as both-None or both-set (it raises on any mismatch), so on this line the two operands always share their truth value; `and` and `or` are indistinguishable for every reachable input. |
| `xǁTopCodeStrategyHandlerǁrun__mutmut_90` | `nums.astype("float64")` -> `nums.astype(None)` | `np.dtype(None)` resolves to `float64`, so `astype(None)` is byte-identical to `astype("float64")` (verified: `pd.Series([...],"float64").astype(None).dtype == float64`). |
| `xǁ...ǁ_classify_object_column__mutmut_2` | `do_under = floor is not None and under_label is not None` -> `... or ...` | Same both-None-or-both-set contract from `_resolve_bottom_bound` as `run` `mutmut_69`; the two operands always agree. |
| `xǁ...ǁ_classify_object_column__mutmut_120` | `pd.Series(render, index=index, dtype=object)` -> `dtype=None` | `render` is a `list[str | None]`; pandas infers `object` for it either way (a string, or an all-`None` list, both infer `object`). The Series is also consumed ONLY where `in_range_mask` is True, and every such slot holds a rendered string, so even a hypothetical dtype divergence on an all-tail column is unreachable through the mask. |
| `xǁ...ǁ_classify_object_column__mutmut_123` | `pd.Series(render, index=index, dtype=object)` -> `dtype=` dropped | Same as `mutmut_120`: the default inference yields `object` for this `render` list. |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
