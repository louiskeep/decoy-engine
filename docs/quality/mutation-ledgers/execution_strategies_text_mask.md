# Mutation grading: `execution/_strategies/_text_mask.py` -- non-NER layer LOGIC-100%, NER paths deferred

TQ crown-jewels pass, graded 2026-07-26. `_text_mask.py` is a thin V2
StrategyHandler that resolves config, applies an optional spaCy NER
version-mismatch guard, iterates the non-null cells, and delegates each to
`transforms.text_mask.mask_cell`. It has two layers with different gradeability:

- The **non-NER handler layer** (detector/per-detector/policy/token/date-bound
  resolution, the extension-dtype boxing, the per-cell loop and str-coercion,
  the `mask_cell` argument forwarding, and the output-Series assembly) reads only
  public config and runs in any shell with no spaCy pipeline. **Graded to
  LOGIC-100% here.**
- The **NER path** (`ner_cfg`/`ner_model`/`ner_entities` resolution, the
  `ner_model_version_mismatch` guard and its `StrategyError` fields, and the
  per-cell `iter_ner_spans` call) is only meaningfully reachable with the spaCy
  extra installed: off the extra, `installed_model_version` returns None so the
  guard's raise never fires and `iter_ner_spans` never runs. Its survivors are
  **deferred to the NER-enabled env** (mirrors `quality_dp.md`'s cert-gated
  mechanism). Not classified equivalent; they are simply not gradeable here.

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`_text_mask.py` with the test selection restricted to
`tests/unit/execution/test_text_mask_ner.py` (~0.36s). Integration and pipeline
suites that also exercise `text_mask` were NOT in the selection, so the survivor
count is a conservative lower bound: some mutants counted "survived" here may be
killed by tests outside this file.

## Numbers

**128 mutants: 63 killed (49% baseline), 65 survived.** The 65 survivors split:

- **50 non-NER-layer survivors, all LOGIC**, killed with **12 new tests** this
  pass (final: 112/128 killed). 0 LOGIC mutants survive in the non-NER layer.
  (`run__mutmut_10`, the no-detectors `detector_ids = None -> ""` else-branch, was
  missed by the list-branch test in the first pass and killed on re-verify by
  `test_absent_detectors_forwards_none`.)
- **3 non-NER-layer survivors, EQUIVALENT** (redundant `dtype=object`; the
  invisible extension-boxing branch).
- **13 NER-path survivors, DEFERRED** (uncovered off the spaCy extra).

## LOGIC (49): killed by new tests in this pass

All killing tests live in `tests/unit/execution/test_text_mask_ner.py`. Most use
a `_CaptureMask` spy monkeypatched over the module-level `mask_cell`, driven with
no `ner` config, and assert the forwarded arg. Asserting a forwarded kwarg kills
both the value mutants (wrong key / wrong default / case) and the kwarg-drop
mutants at the call site: a dropped kwarg is absent from `**kwargs`, so the
assertion raises KeyError.

### Detector resolution + forwarding (11)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_3`, `4`, `5`, `6` | `detectors_raw` = None / `cfg.get(None)` / wrong key -> resolves to None | `TestArgForwarding::test_forwards_detector_list` |
| `run__mutmut_7`, `8` | `detector_ids = None` / `[...] and None` -> None | same |
| `run__mutmut_9` | `[str(None) for ...]` -> `["None"]` | same |
| `run__mutmut_10` | no-detectors else branch `detector_ids = None` -> `""` | `test_absent_detectors_forwards_none` (the list-branch test does not reach the else branch) |
| `run__mutmut_106`, `114` | call site `detector_ids=None` / kwarg dropped | `test_forwards_detector_list` |

### Per-detector strategy map + forwarding (7)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_11`, `14`, `15`, `16` | `per_detector` = None / `cfg.get(None)` / wrong key -> None or `{}` | `TestArgForwarding::test_forwards_per_detector_strategy_map` |
| `run__mutmut_108`, `120` | call site `strategy_map=None` / `per_detector and None` | same |
| `run__mutmut_116` | call site `strategy_map=` kwarg dropped (-> KeyError) | same |

### Unmatched-span policy default (4)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_20`, `22` | policy default `None` / kwarg default dropped -> `"None"` | `TestArgForwarding::test_forwards_default_unmatched_policy` |
| `run__mutmut_25`, `26` | policy default `"XXredactXX"` / `"REDACT"` | same |

### Replacement token (7 default + 3 custom)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_28`, `30`, `31`, `32` | `str(None)` / default None / wrong key single-arg / dropped default -> `"None"` | `TestArgForwarding::test_forwards_default_token` |
| `run__mutmut_35`, `36` | default `"XX[REDACTED]XX"` / `"[redacted]"` | same |
| `run__mutmut_118` | call site `token=` kwarg dropped (-> KeyError) | same |
| `run__mutmut_29`, `33`, `34` | `cfg.get(None, ...)` / wrong key `"XXtokenXX"` / `"TOKEN"` -> ignores a configured token | `TestArgForwarding::test_forwards_custom_token` |

### Date-bound extra dict + cfg forwarding (9)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_37` | `extra = None` -> `None[key] =` TypeError when a bound is set | `TestArgForwarding::test_forwards_date_bounds_as_cfg` |
| `run__mutmut_38`, `39` | loop key `"XXmin_daysXX"` / `"MIN_DAYS"` -> min_days dropped | same |
| `run__mutmut_40`, `41` | loop key `"XXmax_daysXX"` / `"MAX_DAYS"` -> max_days dropped | same |
| `run__mutmut_43` | `extra[key] = None` -> both bounds nulled | same |
| `run__mutmut_111`, `121` | call site `cfg=None` / `extra and None` | same |
| `run__mutmut_119` | call site `cfg=` kwarg dropped (-> KeyError) | same |

### Cell iteration (5)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_90` | null-cell `continue` -> `break` (truncates the loop; later cells never masked) | `TestCellIteration::test_null_cell_is_skipped_and_later_cells_still_masked` |
| `run__mutmut_91`, `92`, `93` | str-coerce guard inverted / `value = None` / `value = str(None)` -> non-str cell reaches mask_cell as the raw int / None / `"None"` | `TestCellIteration::test_coerces_non_str_cell_to_str` |
| `run__mutmut_94` | `ner_spans = ""` init -> forwarded as `extra_spans=""` (runs for every cell, no NER needed) | `TestArgForwarding::test_extra_spans_none_without_ner` |

### Extension-dtype boxing (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_84` | `col = None` in the extension branch -> `None.isna()` AttributeError | `TestCellIteration::test_extension_dtype_column_masks_without_error` |
| `run__mutmut_85` | `col.astype(None)` -> ValueError on a `string`-dtype column | same |

### Output-Series index alignment (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_124`, `127` | `pd.Series(..., index=None)` / index arg dropped -> RangeIndex misaligns a non-default index and blanks every row to NaN | `TestOutputSeries::test_output_series_aligned_to_non_default_index` |

## EQUIVALENT (3)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_83` | `is_extension_array_dtype(col.dtype)` -> `is_extension_array_dtype(None)` (always False) | `None` makes the guard always take the `else` (`col.copy()`) and skip `astype(object)`. But `col` is consumed only via `col.isna().to_list()` and `col.to_list()`, both dtype-agnostic, and the output is rebuilt from a plain list with an explicit `dtype=object`. Verified byte-identical across `string`, `Int64`, and `category` extension columns (null masks, non-null python scalars, and output dtype all match). No input distinguishes it. |
| `run__mutmut_125` | `pd.Series(..., dtype=object)` -> `dtype=None` | `mask_cell` always returns a string (the handler pre-coerces every non-null cell via the `str(value)` step, and nulls are skipped), so `col_values` is uniformly str-or-None and pandas infers `object` whether or not the dtype is stated. Confirmed on mixed string+null and empty columns. |
| `run__mutmut_128` | `pd.Series(..., dtype=object)` -> dtype arg dropped | Same as `run__mutmut_125`: the explicit `object` dtype is redundant given a uniformly str-or-None `col_values`. |

## NER path (13): deferred to the NER-enabled env

Off the spaCy extra these survivors are UNCOVERED, not equivalent. The
`ner_model` resolution branch runs only for a dict `ner` config; the guard's
raise is unreachable because unpatched `installed_model_version` returns None;
`iter_ner_spans` is imported and called only when `ner_model is not None`. Grade
them on the NER-enabled profile (`spacy_installed()` and
`model_installed(DEFAULT_NER_MODEL)`), where the `@needs_ner` end-to-end cells
execute and the resolution/guard/iter path is fully exercised.

| Site | Mutants |
|---|---|
| `ner_entities` init (`= ""`) | `run__mutmut_49` |
| dict `ner_model` resolution (`model` key: `and` / `get(None)` / `"XXmodelXX"` / `"MODEL"`) | `run__mutmut_52`, `53`, `54`, `55` |
| entities guard (`isinstance(...) or raw_entities`) | `run__mutmut_60` |
| version guard `installed_model_version(None)` | `run__mutmut_68` |
| version-guard `StrategyError` fields (`strategy=None` / `message=None` / `message=` dropped / `"XXtext_maskXX"` / `"TEXT_MASK"`) | `run__mutmut_73`, `74`, `77`, `80`, `81` |
| per-cell `iter_ner_spans(None, ...)` | `run__mutmut_97` |

To reproduce on the NER env:

```
# with spacy + en_core_web_sm installed (ner extra)
# pyproject [tool.mutmut]: only_mutate=["src/decoy_engine/execution/_strategies/_text_mask.py"],
# test selection = tests/unit/execution/test_text_mask_ner.py
rm -rf mutants && python -m mutmut run
```

Then classify + kill the reachable NER LOGIC survivors and extend this ledger.

## Regenerate (non-NER layer, any shell)

Repoint `[tool.mutmut]` `only_mutate` to this module + test selection
`tests/unit/execution/test_text_mask_ner.py`, then
`rm -rf mutants && python -m mutmut run`.
