# Mutation grading: `execution/_strategies/_nested.py` -- LOGIC-100%

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`_nested.py` with the test selection restricted to
`tests/unit/execution/test_nested_strategy.py` (36 tests after this pass, ~0.5s).
Integration and pipeline suites that also drive the `nested` strategy (and its
polars port + when-gate preflight route) were NOT in the selection, so the
survivor count and the resulting score are a conservative lower bound: some
mutants counted "survived" here are in fact killed by tests outside this file.

TQ crown-jewels pass, 2026-07-26. `nested` wraps a child strategy and applies it
at a JSONPath target inside each JSON cell: parse -> locate leaves (`jsonpath_ng`)
-> collect into one Series -> run the child once (batch) -> write each masked
value back at its own path -> re-serialize. Malformed JSON emits a
`QualityWarning` and passes through; a no-match path is left unchanged with no
warning (sparse paths are valid); overlapping matches (recursive/wildcard
selectors) are ordered deepest-first and flagged with a typed warning.

A mutmut run produced **303 mutants, 188 killed + 37 timeout (62% baseline),
78 survived**. Every survivor was classified LOGIC or EQUIVALENT per
`docs/quality/module-test-quality-playbook.md` ("Scope the score to LOGIC, not
error-message wording"). **58 LOGIC survivors** were killed with **15 new tests**
(plus two `.strategy` assertions added to existing rejection tests);
**20 survive and are EQUIVALENT** (error-message prose, cosmetic message-suffix
arg, a proven-identical branch, consistent internal renames, and inferred-vs-
explicit dtype), tabled below with the one-line argument for why no input
distinguishes each from the original. The 37 timeouts are counted as caught (a
mutant that hangs the suite is detected), so they are not in the survivor set.

Verification note: `StrategyError(code, strategy, message)` defaults `message=""`,
so `message=None` / a dropped `message=` kwarg still raises successfully -- those
land in EQUIVALENT. `code` and `strategy` are required and machine-load-bearing;
every error test pins the exact `.code`/`.strategy` attributes. The two
`QualityWarning`s carry `provider`, `column`, and a `detail` dict whose keys and
values flow into the manifest quality summary, so those are pinned exactly
(dict keys/values written to output are LOGIC per the playbook).

Bugs found in `_nested.py`: none introduced or newly exposed by this pass.

Survivor spread by function (raw / LOGIC-killed / EQUIVALENT):

| Function | Survived | LOGIC killed | EQUIVALENT |
|---|---|---|---|
| `run` | 71 | 53 | 18 |
| `_has_prefix_overlap` | 5 | 3 | 2 |
| `_path_segments` | 2 | 2 | 0 |
| **Total** | **78** | **58** | **20** |

## LOGIC (58): killed by new tests in this pass

All killing tests live in `tests/unit/execution/test_nested_strategy.py`.

### `run` -- fail-closed error `strategy` field (6)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_11`, `18`, `19` | `nested_target_unset` `strategy=None` / `"XXnestedXX"` / `"NESTED"` | `TestRejections::test_nested_target_empty_raises` (now asserts `.strategy`) |
| `run__mutmut_34`, `41`, `42` | `nested_jsonpath_parse_error` `strategy=None` / `"XXnestedXX"` / `"NESTED"` | `TestJsonPathParseErrorFields::test_jsonpath_parse_error_strategy_field` |

### `run` -- extension-array dtype branch (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_45` | `col = col.astype(object)` -> `col = None` (`None.to_list()` AttributeError) | `TestExtensionArrayColumn::test_extension_array_dtype_column_is_masked` |
| `run__mutmut_46` | `col.astype(object)` -> `col.astype(None)` (ValueError on a string column) | same |

### `run` -- per-row skip branches (3)

Each mutant turns a per-cell `continue` into `break`, halting the scan; killed by
placing the skipped row BEFORE a maskable row and asserting the later row is
still masked.

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_54` | null-cell `continue` -> `break` | `TestRowOrderingBranches::test_null_cell_before_valid_row_does_not_halt` |
| `run__mutmut_73` | parse-error `continue` -> `break` | `test_parse_error_cell_before_valid_row_does_not_halt` |
| `run__mutmut_78` | no-match `continue` -> `break` | `test_no_match_cell_before_valid_row_does_not_halt` |

### `run` -- parse-error `QualityWarning` fields (10)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_59`, `68`, `69` | `provider=None` / `"XXnestedXX"` / `"NESTED"` | `TestWarningMachineFields::test_parse_error_warning_fields` |
| `run__mutmut_60`, `64` | `column=None` / `column=` dropped (defaults None) | same |
| `run__mutmut_61`, `65` | `detail=None` / `detail=` dropped (defaults `{}`) | same |
| `run__mutmut_70`, `71` | detail KEY `"row_pos"` -> `"XXrow_posXX"` / `"ROW_POS"` | same |
| `run__mutmut_72` | detail VALUE `str(pos)` -> `str(None)` | same |

### `run` -- overlap `QualityWarning` fields (15)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_84`, `93`, `94` | `provider=None` / `"XXnestedXX"` / `"NESTED"` | `TestWarningMachineFields::test_overlap_warning_fields` |
| `run__mutmut_85`, `89` | `column=None` / `column=` dropped | same |
| `run__mutmut_86`, `90` | `detail=None` / `detail=` dropped | same |
| `run__mutmut_95`, `96` | detail KEY `"row_pos"` -> `"XXrow_posXX"` / `"ROW_POS"` | same |
| `run__mutmut_97` | detail VALUE `str(pos)` -> `str(None)` | same |
| `run__mutmut_98`, `99` | detail KEY `"target"` -> `"XXtargetXX"` / `"TARGET"` | same |
| `run__mutmut_100`, `101` | detail KEY `"match_count"` -> `"XXmatch_countXX"` / `"MATCH_COUNT"` | same |
| `run__mutmut_102` | detail VALUE `str(len(ordered_matches))` -> `str(None)` | same |

### `run` -- outer-column ctx save / stamp / restore (13)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_111` | `prior_outer_column = None` | `TestOuterColumnCtxLifecycle::test_outer_column_restored_to_default_when_absent` + `test_outer_column_restored_to_prior_value` |
| `run__mutmut_112` | `getattr(None, "nested_outer_column", "")` | `test_outer_column_restored_to_prior_value` |
| `run__mutmut_114` | save default `""` -> `None` | `test_outer_column_restored_to_default_when_absent` |
| `run__mutmut_118`, `119` | save reads `"XXnested_outer_columnXX"` / `"NESTED_OUTER_COLUMN"` | `test_outer_column_restored_to_prior_value` |
| `run__mutmut_120` | save default `""` -> `"XXXX"` | `test_outer_column_restored_to_default_when_absent` |
| `run__mutmut_123` | stamp `object.__setattr__(ctx, ..., column)` -> `..., None` | `test_child_sees_real_outer_column_during_dispatch` |
| `run__mutmut_127`, `128` | stamp writes `"XXnested_outer_columnXX"` / `"NESTED_OUTER_COLUMN"` | same |
| `run__mutmut_133` | child called with `ctx` -> `None` | same |
| `run__mutmut_140` | finally restores `None` instead of prior | both restore tests |
| `run__mutmut_144`, `145` | finally writes `"XXnested_outer_columnXX"` / `"NESTED_OUTER_COLUMN"` | both restore tests |

### `run` -- child batch write-back mapping (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_151` | `cursor += 1` -> `cursor = 1` | `TestChildBatchMapping::test_each_leaf_maps_to_its_own_masked_value` |
| `run__mutmut_152` | `cursor += 1` -> `cursor -= 1` | same |

### `run` -- write-back index alignment (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_162` | `pd.Series(..., index=df.index, ...)` -> `index=None` | `TestWritebackIndexAlignment::test_custom_index_preserved_on_writeback` |
| `run__mutmut_165` | `index=df.index` arg dropped (defaults RangeIndex) | same |

### `_path_segments` (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `_path_segments__mutmut_3` | paren guard `and` -> `or` (a single leading paren is wrongly stripped) | `TestPathSegments::test_unbalanced_leading_paren_is_not_stripped` |
| `_path_segments__mutmut_11` | strip `raw[1:-1]` -> `raw[1:-2]` (drops an extra trailing char) | `test_balanced_parens_strip_one_char_each_end` |

### `_has_prefix_overlap` (3)

| Mutants | Mutation | Killed by |
|---|---|---|
| `_has_prefix_overlap__mutmut_11` | inner `range(i + 1, n)` -> `range(i + 2, n)` (skips the adjacent neighbour, missing the only overlap) | `TestHasPrefixOverlap::test_adjacent_prefix_pair_is_detected` |
| `_has_prefix_overlap__mutmut_13` | `len(shallower) < len(deeper) and ...` -> `or ...` (a shorter non-prefix is wrongly flagged) | `test_shorter_non_prefix_is_not_flagged` |
| `_has_prefix_overlap__mutmut_14` | `<` -> `<=` (equal-length identical paths are wrongly flagged) | `test_equal_length_identical_paths_are_not_flagged` |

## EQUIVALENT (20)

### Error-message prose (12)

Each literal / arg below flows only into the human `message` of a raised
`StrategyError`; the machine `code`/`strategy` and the raise condition are
unchanged, and the tests assert `.code`/`.strategy` (not the rendered string), so
no input distinguishes them.

| Site | Mutants | Note |
|---|---|---|
| `run` `nested_target_unset` | `run__mutmut_12` (`message=None`), `15` (kwarg dropped -> defaults `""`), `20`-`25` (prose fragments / case) | 8 |
| `run` `nested_jsonpath_parse_error` | `run__mutmut_35` (`message=None`), `38` (kwarg dropped) | 2 |
| `run` `_resolve_child(plan, column)` column arg | `run__mutmut_28` (`column` -> `None`), `30` (arg dropped -> `None`) | 2 |

`run__mutmut_28`/`30`: `column` reaches `_resolve_child` only to build the
`" (column=...)"` SUFFIX of the child-resolution error messages
(`nested_strategy_unset` / `nested_recursive_nested_rejected` /
`nested_child_strategy_unknown`); the module docstring designates it "optional and
cosmetic only". Passing `None` drops the suffix and changes no `code`/`strategy`
and no masking outcome. (Dataflow traced: `column` -> `column_suffix` -> f-string
`message` only.)

### Consistent internal rename -- scratch column (3)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_106` | `temp_col = "_nested_leaves"` -> `None` | `temp_col` is used symmetrically to build the scratch frame (`pd.DataFrame({temp_col: ...})`), pass it to the child (`child_handler.run(temp_df, temp_col, ...)`), and read the result (`temp_df[temp_col]`). Its literal value never escapes to output; the child reads by the same handle and evidence keying uses `nested_outer_column`, not this name. A `None` / renamed label round-trips identically (empirically confirmed: `df[None]` is a valid pandas column and these mutants survived every covering test, which they could not if the value escaped or crashed). |
| `run__mutmut_107` | `-> "XX_nested_leavesXX"` | same |
| `run__mutmut_108` | `-> "_NESTED_LEAVES"` | same |

### Proven-identical control flow (1)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_44` | `is_extension_array_dtype(col.dtype)` -> `is_extension_array_dtype(None)` (always `False`, forces the `else: col = col.copy()` branch) | `col` is immediately materialised via `col.to_list()` into plain Python objects, and the column is rebuilt at the end with an explicit `dtype=object`; on a no-match cell the frame is returned untouched. So `astype(object)` vs `copy()` on the local `col` never reaches output for any extension-array column of JSON text. Confirmed: with a `string`-dtype column, both branches yield an identical `to_list()`, and the mutant survives the extension-array test that kills `_45`/`_46`. |

### Loop-bound mutants under the deepest-first precondition (2)

`_has_prefix_overlap`'s only call site passes `_order_matches_deepest_first(...)`,
so `seg_lists` is always non-increasing in length. Under that documented
precondition (see the function docstring) these two loop-bound changes add only
iterations whose `len(shallower) < len(deeper)` guard can never fire, or that
duplicate an existing comparison. No input the real code can produce distinguishes
them (only a contract-violating unsorted list could).

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_has_prefix_overlap__mutmut_8` | `range(i + 1, n)` -> `range(n)` | the added `j <= i` iterations compare a deeper-or-equal element as `shallower`, so `len(shallower) < len(deeper)` is always `False` -> no new match; the `j > i` comparisons are unchanged. |
| `_has_prefix_overlap__mutmut_10` | `range(i + 1, n)` -> `range(i - 1, n)` | for `i >= 1` the extra `j = i - 1` is a deeper-or-equal element (guard never fires); for `i = 0` the extra `j = -1` (== `n-1`) and `j = 0` (self) are a redundant comparison and a self-skip. Identical result on any sorted input. |

### Inferred-vs-explicit dtype (2)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_163` | `pd.Series(..., dtype=object)` -> `dtype=None` | the reachable `col_values` are always `str`-or-`None` (JSON text / `json.dumps` output; the all-null case returns early before this line), for which pandas infers `object`, identical to the explicit `dtype=object`. |
| `run__mutmut_166` | `dtype=object` arg dropped | same (dropping the arg is the same as `dtype=None`). |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_strategies/_nested.py` with test selection
`tests/unit/execution/test_nested_strategy.py`, then:

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```

Run the covering tests directly (fast, no mutmut) with:

```
python -m pytest tests/unit/execution/test_nested_strategy.py -q
```
