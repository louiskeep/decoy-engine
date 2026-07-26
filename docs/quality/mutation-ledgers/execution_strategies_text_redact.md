# Mutation grading: `execution/_strategies/_text_redact.py` -- LOGIC-100%

TQ crown-jewels pass, graded 2026-07-26. `_text_redact.py` is the span-level PII
redaction StrategyHandler: it resolves config (detectors / token / label_token),
resolves an optional spaCy NER config, applies the NER version-mismatch guard,
iterates the non-null cells (coercing non-str values, calling `iter_ner_spans`
per cell when NER is on, then `iter_spans`), and splices span replacements via
`_splice`.

- The **non-NER handler layer** (token / label_token / detector resolution, the
  extension-dtype boxing, the per-cell loop, null-skip and str-coercion, the
  `iter_spans` `extra_spans` forwarding, the output-Series assembly, and the
  `_splice` cursor/index math) reads only public config. Graded to LOGIC-100%.
- The **NER path** (`ner_cfg` / `ner_model` / `ner_entities` resolution, the
  `ner_model_version_mismatch` guard and its `StrategyError` fields, and the
  per-cell `iter_ner_spans` call) is graded here too. `installed_model_version`
  and `iter_ner_spans` are BOUNDARIES the tests monkeypatch (as
  `test_version_mismatch_raises` already did), so the handler's NER logic
  (model/entities resolution, the version guard, the call-site args) is fully
  gradeable off-spaCy. Only mutations INSIDE the real `storm.ner` primitives
  would need a real model, and those live in that module, not here. Mirrors the
  sibling `_text_mask.py` grade (batch-3 dennis P2: these are not "spaCy-gated").

**Grade scope: FOCUSED selection only.** mutmut ran against `_text_redact.py`
with the test selection restricted to `tests/unit/execution/test_text_redact.py`.
Integration and pipeline suites that also exercise `text_redact` were NOT in the
selection, so the survivor count is a conservative lower bound: some mutants
counted "survived" here may be killed by tests outside this file.

## Numbers

**125 mutants: 81 killed (65% baseline), 44 survived -> 118 killed after this
pass, 7 EQUIVALENT.** LOGIC-mutant score 100%.

(Batch-7 dennis P2 correction: `mutmut_110`/`113` (`dtype=object` -> `None`/dropped)
were first filed equivalent but are LOGIC -- an all-null FLOAT column infers
float64 without the explicit `object`, an observable output-dtype change. Killed
below. The sibling `_text_mask`/`_nested` ledgers carry the same now-superseded
"uniformly str-or-null" rationale for their dtype=object survivors; see
tq-findings.md #7.)

- **37 LOGIC survivors killed** with **11 new tests** (plus a strengthened
  `.strategy` assertion on the existing version-guard test). 0 real bugs found.
- **7 EQUIVALENT survivors** left alive (2 label_token default no-ops, 2
  NER-guard message-prose, 1 extension-boxing no-op, 2 `_splice`
  symmetric-boundary no-ops). All verified behavior-preserving.
- **0 deferred.** No mutant needs a real spaCy model to grade.

## LOGIC (37): killed by new tests in this pass

All killing tests live in `tests/unit/execution/test_text_redact.py`. The NER and
`iter_spans` forwarding tests use spies monkeypatched over the `storm.ner`
boundaries (`iter_ner_spans`, `installed_model_version`) and the module-level
`iter_spans`, capturing the forwarded args. Asserting a forwarded value kills both
the value mutants (wrong key / forced default / None) and the kwarg-drop mutants
(a dropped kwarg is absent from `**kwargs`, so the captured value is None or the
positional index is missing).

### NER dict model + entities resolution (14)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_30`, `31`, `32`, `33`, `34`, `35` | dict `ner_model` = None / `str(None)` / `... and DEFAULT` / `get(None)` / `"XXmodelXX"` / `"MODEL"` -> None or forced DEFAULT | `TestNerConfigResolution::test_ner_dict_model_and_entities_forwarded` (non-default model string; DEFAULT is only the `or` fallback). `mutmut_30` (`ner_model = None`) additionally skips NER entirely, so the spy is never called -> assert `called is True` |
| `run__mutmut_36`, `37`, `38`, `39` | `raw_entities` = None / `get(None)` / `"XXentitiesXX"` / `"ENTITIES"` -> configured entities dropped | `test_ner_dict_model_and_entities_forwarded` |
| `run__mutmut_41`, `42` | `ner_entities = None` / `[str(None) for ...]` -> entities nulled / `["None", ...]` | same |
| `run__mutmut_29` | `ner_entities` init `= None -> ""` (only visible when no entities key) | `test_ner_dict_without_entities_forwards_none` |
| `run__mutmut_40` | entities guard `and -> or raw_entities` -> empty list forwarded as `[]` | `test_ner_dict_empty_entities_forwards_none` (empty list is falsy; `or` lets it through) |

### NER version guard (4)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_48` | `installed_model_version(ner_model) -> (None)` | `TestNerCallSiteForwarding::test_version_guard_checks_the_resolved_model` (only the resolved model reports the drifting version; `None` reports absent -> guard silently stops firing) |
| `run__mutmut_53`, `60`, `61` | `StrategyError.strategy` = None / `"XXtext_redactXX"` / `"TEXT_REDACT"` | `TestF14bNerVersionGuard::test_version_mismatch_raises` (strengthened: `assert exc.value.strategy == "text_redact"`) |

### Cell loop control + str-coercion (5)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_75` | null-skip `continue -> break` (truncates the loop; later PII never redacted) | `TestLoopControl::test_null_cell_does_not_truncate_later_cells` (`[None, PII]`) |
| `run__mutmut_97` | no-match `continue -> break` (same truncation after a passthrough cell) | `TestLoopControl::test_no_match_cell_does_not_truncate_later_cells` (`[no-match, PII]`) |
| `run__mutmut_76`, `77`, `78` | str-coerce guard inverted / `text = None` / `text = str(None)` -> non-str cell reaches `iter_spans` as the raw int / None / `"None"` | `TestNonStringCoercion::test_non_string_non_null_cell_coerced_to_str` (`42 -> "42"`) |

### NER + iter_spans call-site forwarding (10)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_79` | `extra` init `= None -> ""` (forwarded as `extra_spans=""`; output-invisible but wrong) | `TestExtraSpansForwarding::test_extra_spans_none_without_ner` (asserts `extra_spans is None`) |
| `run__mutmut_82`, `85` | `iter_ner_spans(text, ...)` text arg -> None / positional dropped | `TestNerCallSiteForwarding::test_ner_result_and_call_args_forwarded` (asserts `ner_text == cell`) |
| `run__mutmut_83`, `86` | `iter_ner_spans(..., model=ner_model, ...)` -> `model=None` / kwarg dropped | same (asserts `ner_model`) + `test_ner_dict_model_and_entities_forwarded` |
| `run__mutmut_84`, `87` | `iter_ner_spans(..., entities=ner_entities)` -> `entities=None` / kwarg dropped | same (asserts `ner_entities`) + `test_ner_dict_model_and_entities_forwarded` |
| `run__mutmut_81`, `91`, `94` | NER result dropped (`extra = None`) / `iter_spans(..., extra_spans=None)` / `extra_spans` kwarg dropped | `test_ner_result_and_call_args_forwarded` (asserts the NER `Span` reaches `iter_spans` as `extra_spans`) |

### Output-Series index alignment (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_109`, `112` | `pd.Series(..., index=df.index, ...)` -> `index=None` / index arg dropped -> RangeIndex misaligns a non-default index and blanks every row to NaN | `TestOutputIndexAlignment::test_output_aligned_to_non_default_index` (index `[10, 20]`) |

### Output-Series dtype (2)

| Mutants | Mutation | Killed by |
|---|---|---|
| `run__mutmut_110`, `113` | `pd.Series(..., dtype=object)` -> `dtype=None` / dtype arg dropped | `test_all_null_float_column_output_is_object_dtype`: an all-null FLOAT column infers `float64` without the explicit `object`, so the output dtype (a deliberate Arrow/concat-boundary contract) changes observably. Batch-7 dennis P2. |

(Defense-in-depth, no new mutant: `test_empty_detectors_list_redacts_all_not_nothing`
pins the S5c F2 anti-PHI-leak invariant -- `detectors: []` means "all detectors",
never "redact nothing".)

## EQUIVALENT (7)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `run__mutmut_17` | `bool(cfg.get("label_token", False))` -> `..., None)` | The default only applies when the key is absent, and `bool(None) == bool(False) == False`. The `bool()` wrapper collapses both to the same value for every config. |
| `run__mutmut_19` | `bool(cfg.get("label_token", False))` -> default dropped | `cfg.get("label_token")` returns None when absent; `bool(None) == False`. Same collapse as `mutmut_17`. |
| `run__mutmut_54` | NER version-guard `message=None` | Consumed only as `StrategyError.message`; the guard's `code`/`strategy` are asserted (`test_version_mismatch_raises`), so a message-only change is invisible. |
| `run__mutmut_57` | NER version-guard `message=` kwarg dropped | `StrategyError.message` defaults to `""` (only `code`/`strategy` are required); the raise still carries the right machine fields. |
| `run__mutmut_68` | `is_extension_array_dtype(col.dtype)` -> `is_extension_array_dtype(None)` (always False) | `None` forces the `else` (`col.copy()`) and skips `astype(object)`. But `col` is consumed only via `col.isna().to_list()` and `col.to_list()`, both dtype-agnostic, and the output is rebuilt from a plain list with an explicit `dtype=object`. Verified byte-identical across `string[pyarrow]`, `Int64`, and `category` columns (non-null values and null markers match). No input distinguishes it. Same as `_text_mask` `mutmut_83`. |
| `_splice__mutmut_4` | `if s.start > cursor:` -> `if s.start >= cursor:` | The added case is `s.start == cursor`, which appends `text[cursor:s.start]` = `text[cursor:cursor]` = `""`. Appending an empty string never changes the joined output. Verified on adjacent spans, a span at offset 0, and a span ending at `len(text)`. Symmetric-boundary no-op. |
| `_splice__mutmut_9` | `if cursor < len(text):` -> `if cursor <= len(text):` | `cursor` never exceeds `len(text)` (`cursor = s.end` and spans stay in-bounds), so the added case is `cursor == len(text)`, which appends `text[cursor:]` = `text[len(text):]` = `""`. No observable difference. Symmetric-boundary no-op. |

## Regenerate (any shell, no spaCy needed)

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_strategies/_text_redact.py` and the test selection to
`tests/unit/execution/test_text_redact.py`, then `rm -rf mutants && python -m
mutmut run`. `source_paths` MUST stay at the package root (see the playbook's
copy-per-module note).
