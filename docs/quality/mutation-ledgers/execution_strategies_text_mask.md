# Mutation grading: `execution/_strategies/_text_mask.py` -- LOGIC-100%

TQ crown-jewels pass, graded 2026-07-26. `_text_mask.py` is a thin V2
StrategyHandler that resolves config, applies an optional spaCy NER
version-mismatch guard, iterates the non-null cells, and delegates each to
`transforms.text_mask.mask_cell`.

- The **non-NER handler layer** (detector/per-detector/policy/token/date-bound
  resolution, the extension-dtype boxing, the per-cell loop and str-coercion,
  the `mask_cell` argument forwarding, and the output-Series assembly) reads only
  public config. Graded to LOGIC-100%.
- The **NER path** (`ner_cfg`/`ner_model`/`ner_entities` resolution, the
  `ner_model_version_mismatch` guard and its `StrategyError` fields, and the
  per-cell `iter_ner_spans` call) is also graded here: although a real spaCy
  model is not installed, the guard's `installed_model_version` and the
  `iter_ner_spans` call are BOUNDARIES that the existing tests monkeypatch, so the
  handler's NER LOGIC (model/entities resolution, the version guard, the call-site
  args) is fully mockable off-spaCy. (Batch-3 dennis P2 correction: the first pass
  wrongly deferred these as "spaCy-gated"; they are not.) Only mutations INSIDE
  the real `storm.ner` primitives would need spaCy, and those live in that module,
  not here.

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`_text_mask.py` with the test selection restricted to
`tests/unit/execution/test_text_mask_ner.py` (~0.36s). Integration and pipeline
suites that also exercise `text_mask` were NOT in the selection, so the survivor
count is a conservative lower bound: some mutants counted "survived" here may be
killed by tests outside this file.

## Numbers

**128 mutants: 63 killed (49% baseline), 65 survived -> 123 killed after this
pass, 5 EQUIVALENT.** LOGIC-mutant score 100%.

- **61 LOGIC survivors killed** with **18 new tests** (50 non-NER + 11 NER-path:
  the model/entities resolution `mutmut_49/52-55/60`, the version-guard `.strategy`
  fields `mutmut_73/80/81`, the `installed_model_version(ner_model)` arg
  `mutmut_68`, and the `iter_ner_spans(value, ...)` text arg `mutmut_97` -- all
  killed off-spaCy via the `iter_ner_spans`/`installed_model_version` monkeypatch
  boundary). `run__mutmut_10` (no-detectors else-branch) was missed by the first
  pass and killed on re-verify by `test_absent_detectors_forwards_none`.
- **5 EQUIVALENT survivors:** 3 non-NER (`mutmut_83` invisible extension-boxing
  branch; `mutmut_125`/`128` redundant `dtype=object`) + 2 NER-guard message prose
  (`mutmut_74` `message=None`, `mutmut_77` `message=` kwarg drop; the guard's
  `code`/`strategy` are asserted, so message-only variants survive).
- **0 deferred.**

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

## EQUIVALENT (5)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_74` | NER version-guard `message=None` | consumed only as `StrategyError.message`; the guard's `code`/`strategy` are asserted (`test_version_mismatch_raises`), so a message-only change is invisible. |
| `run__mutmut_77` | NER version-guard `message=` kwarg dropped | `StrategyError.message` defaults to `""` (only `code`/`strategy` are required); the raise still carries the right machine fields. |
| `run__mutmut_83` | `is_extension_array_dtype(col.dtype)` -> `is_extension_array_dtype(None)` (always False) | `None` makes the guard always take the `else` (`col.copy()`) and skip `astype(object)`. But `col` is consumed only via `col.isna().to_list()` and `col.to_list()`, both dtype-agnostic, and the output is rebuilt from a plain list with an explicit `dtype=object`. Verified byte-identical across `string`, `Int64`, and `category` extension columns (null masks, non-null python scalars, and output dtype all match). No input distinguishes it. |
| `run__mutmut_125` | `pd.Series(..., dtype=object)` -> `dtype=None` | `mask_cell` always returns a string (the handler pre-coerces every non-null cell via the `str(value)` step, and nulls are skipped), so `col_values` is uniformly str-or-None and pandas infers `object` whether or not the dtype is stated. Confirmed on mixed string+null and empty columns. |
| `run__mutmut_128` | `pd.Series(..., dtype=object)` -> dtype arg dropped | Same as `run__mutmut_125`: the explicit `object` dtype is redundant given a uniformly str-or-None `col_values`. |

## NER path (11 LOGIC): killed off-spaCy via the mock boundary

These were wrongly deferred in the first pass. `installed_model_version` and
`iter_ner_spans` are boundaries the tests monkeypatch, so the handler's NER LOGIC
is fully gradeable without a real spaCy model (batch-3 dennis P2). All killed in
`tests/unit/execution/test_text_mask_ner.py`.

| Site | Mutants | Killed by |
|---|---|---|
| dict `ner_model` resolution (`and` / `get(None)` / `"XXmodelXX"` / `"MODEL"` -> forces DEFAULT) | `run__mutmut_52`, `53`, `54`, `55` | `test_ner_dict_nondefault_model_is_forwarded` (a non-default model string; the DEFAULT is only the `or` fallback) |
| `ner_entities` init `= None -> ""` | `run__mutmut_49` | `test_ner_dict_without_entities_forwards_none` |
| entities guard `and -> or raw_entities` | `run__mutmut_60` | `test_ner_dict_empty_entities_forwards_none` (empty list is falsy) |
| version guard `installed_model_version(ner_model) -> (None)` | `run__mutmut_68` | `test_version_guard_checks_the_resolved_model` (only the resolved model reports drift) |
| version-guard `StrategyError.strategy` (`None` / `"XXtext_maskXX"` / `"TEXT_MASK"`) | `run__mutmut_73`, `80`, `81` | `test_version_mismatch_raises` (strengthened: `assert exc.value.strategy == "text_mask"`) |
| per-cell `iter_ner_spans(value, ...) -> (None, ...)` | `run__mutmut_97` | `test_ner_spans_receive_the_cell_text` |

(The 2 remaining NER-guard survivors `mutmut_74`/`77` are message prose -> EQUIVALENT table above.)

## Regenerate (any shell, no spaCy needed)

Repoint `[tool.mutmut]` `only_mutate` to this module + test selection
`tests/unit/execution/test_text_mask_ner.py`, then
`rm -rf mutants && python -m mutmut run`.
