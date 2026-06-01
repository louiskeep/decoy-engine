# QA Review — decoy-engine: MG-2, MG-3, MG-4, MG-6

**Date:** 2026-06-01  
**Reviewer:** QA / senior performance engineer  
**Branch:** `qa/review-2026-06-01-mg2-mg3-mg4`  
**Scope:** New engine code not covered by prior QA sessions (`qa/review-2026-06-01`, `qa/review-2026-06-01-storm-hardening`).  
Specifically: `storm/detectors.iter_spans` (MG-2), `execution/_strategies/_text_redact.py` (MG-2), `execution/_when_gate.py` (MG-3 M3), `execution/_strategies/_nested.py` (MG-3 M2), `execution/_strategies/_composite.py` (MG-4), `execution/_distribution_behavior.py` (MG-6 D1), `plan/_compile.py` additions.

---

## 1. Summary

The new feature surface is well-structured and the security hardening from prior sessions is correctly applied throughout. The single most important issue is in `iter_spans`: including `us_zip` in `_SPAN_DETECTORS` (the span-level registry used by `text_redact`) causes every 5-digit number in a free-text clinical note to be treated as a ZIP code and redacted — a massive false-positive rate that undercuts the HIPAA killer-feature narrative for `text_redact`. Three other HIGH findings cover the `nested` strategy silently passing PII through on config errors, a fragile positional writeback in the polars `when:` gate, and a dict-key collision risk in `nested` when the DataFrame carries a non-unique index.

---

## 2. Findings

### F1 — HIGH | Data / Correctness
**`iter_spans`: `us_zip` in `_SPAN_DETECTORS` causes pervasive false positives in free text**

`_US_ZIP_RE = re.compile(r"\d{5}(?:-\d{4})?")` is included in `_SPAN_DETECTORS` with no contextual validator. In column-level detection, `_evaluate()` calls `str.fullmatch()` against the whole cell — a cell whose entire value is `"12345"` is plausibly a ZIP. In `iter_spans`, `regex.finditer()` runs against free text, so **any 5-digit substring** matches: patient IDs, lab values (`WBC 10500`... wait, that's 5 digits), protocol numbers, measurement values. Clinical notes are the stated killer use case for `text_redact`; in practice `us_zip` will redact large swaths of legitimate numeric content.

```python
# storm/detectors.py — _SPAN_DETECTORS
"us_zip": (_US_ZIP_RE, None),  # ← no validator; any \d{5} substring matches
```

Affected path: `storm/detectors.py` → `_SPAN_DETECTORS` → `iter_spans()` → `_text_redact.py` → `TextRedactHandler.run()`.

**Impact:** `text_redact` on clinical notes produces over-redacted output. Operator enables `us_zip` detector (or leaves `detectors=None` for all), and notes like `"BP 120/80, MRN 12345, admitted 30167 protocol"` have `12345` and `30167` both replaced with `[REDACTED:us_zip]`, destroying legitimate numeric content.

**Fix:** Add `us_zip` to a `_HINT_ONLY_SPAN_DETECTORS` set (excluded from `iter_spans` by default unless the caller explicitly opts in), OR require a contextual wrapper (e.g., ZIP must be preceded/followed by a city/state token). At minimum, document prominently that `us_zip` should be excluded from `text_redact` configs via `detectors:` list — the default `detectors=None` (all detectors) is unsafe for clinical notes. A short-term safe fix:

```python
# Exclude high-false-positive detectors from the span registry by default.
# Operators who know their text contains ZIPs can pass detector_ids=["us_zip", ...] explicitly.
_SPAN_DETECTORS: dict[str, ...] = {
    "email": (_EMAIL_RE, None),
    "ssn": (_SSN_RE, None),
    "us_phone": (_US_PHONE_RE, None),
    # "us_zip": REMOVED from default set; opt-in only
    "pan": (_PAN_RE, _luhn_valid),
    "iban": (_IBAN_RE, _iban_valid),
    "ipv4": (_IPV4_RE, _ipv4_valid),
    "icd10": (_ICD10_RE, _icd10_valid),
    "npi": (_NPI_RE, _npi_valid),
    "url": (_URL_RE, None),
}
```

**Verify:** `python -c "from decoy_engine.storm.detectors import iter_spans; print(iter_spans('Patient BMI 23456 kg admitted to floor 3'))"` — should produce zero spans after fix; currently produces one `us_zip` span at `'23456'`.

---

### F2 — HIGH | Correctness / Data Integrity
**`_nested.py`: dict-key collision when DataFrame index is non-unique corrupts masked output silently**

`NestedStrategyHandler.run` builds `per_row_state: dict[Any, tuple[Any, list]] = {}` keyed by `row_idx` (the DataFrame index label). If two rows share the same index label (possible after `pd.concat`, SQL reads without explicit sort, or `reset_index(drop=False)` on a previously-joined frame), the second row's state overwrites the first. `leaf_values` still contains entries for both rows, so the `cursor` in the writeback loop is now out of sync with `per_row_state`: later JSON cells receive values that were computed for earlier cells.

```python
# execution/_strategies/_nested.py
for row_idx in col.index:           # ← row_idx could repeat
    ...
    per_row_state[row_idx] = (parsed, list(matches))  # ← second entry overwrites first
    for m in matches:
        leaf_values.append(m.value)  # ← both rows' values accumulate
```

The writeback then assigns wrong masked values to wrong cells — silent data corruption with no exception raised.

**Impact:** Rows that shared an index label have their JSON cells incorrectly masked; in the worst case a later cell receives masked values computed for an earlier cell. Determinism is also broken (the output depends on row order when index labels repeat).

**Fix:** Guard against non-unique index, or key `per_row_state` by positional integer instead of label:

```python
for pos, row_idx in enumerate(col.index):
    cell = col.iat[pos]   # positional access; immune to duplicate labels
    ...
    per_row_state[pos] = (row_idx, parsed, list(matches))  # keyed by position
    ...

# writeback:
for pos, (row_idx, parsed, matches) in per_row_state.items():
    for m in matches:
        new_value = new_leaf_values[cursor]
        cursor += 1
        m.full_path.update(parsed, new_value)
    col.iat[pos] = json.dumps(parsed)  # positional write
```

**Verify:** `pd.concat([df, df]).reset_index(drop=False)` produces duplicate index labels; run `NestedStrategyHandler.run` against that frame and assert the output is byte-identical to running against each half separately.

---

### F3 — HIGH | Correctness / Security
**`_nested.py`: config errors silently pass PII through rather than raising**

When `target_path` is absent, the child strategy is unknown, or the JSONPath fails to parse, `NestedStrategyHandler.run` returns `(df, [QualityWarning(...)])` — the **column is not masked**. A misconfigured `nested` plan is indistinguishable at the data level from a correctly-masked one unless the operator inspects QualityWarnings.

```python
if not isinstance(target_path, str) or not target_path:
    return df, [QualityWarning(code="nested_target_unset", ...)]
    # ← df is returned unchanged; column contains PII
```

**Impact:** In a masking context this is a critical safety property: a mis-typed `target: $.patientName` (e.g., `target: $.patient_name`) silently produces unmasked output. The QualityWarning is only surfaced in the Storm report, not at job completion.

**Fix options (pick one, escalate to PO):**
1. **Preferred:** Promote config errors to `StrategyError` (which the runner treats as a hard failure): the masking job fails rather than producing unmasked output. Sparse paths (no JSONPath matches in a cell) remain a silent passthrough per spec.
2. **Alternative:** Add a pre-run compile-time validation pass that catches empty `target` and unknown `child_strategy` before the job starts.

At minimum, the QualityWarning severity for `nested_target_unset` and `nested_child_strategy_unknown` should be elevated so the runner flags the job as `warnings=CRITICAL` rather than `warnings=info`.

---

### F4 — MEDIUM | Correctness
**`run_with_when_gate_polars`: positional writeback via `.values` is fragile if handler reorders rows**

```python
# execution/_when_gate.py
sub_pdf = sub_frame.to_pandas()
pdf.loc[mask, column] = sub_pdf[column].values   # ← positional, no index alignment
```

`sub_frame` is a polars filter result passed to `handler.run`. If the polars child handler sorts, filters NaN, or otherwise reorders the subset, `sub_pdf[column].values` is a numpy array that no longer corresponds positionally to `pdf.loc[mask]`. The mismatch is silent — pandas assigns values by position, not by any key — and the wrong masked values land in the wrong rows.

The **pandas path** (`run_with_when_gate`) is correct: it uses `.loc[mask, column] = sub_df[column]` with label alignment, so row reordering in the child handler is safely neutralized by the index. The polars path lacks this safety.

**Impact:** Any polars handler that sorts its subset (currently none, but not enforced by contract) produces a subtly wrong result. This is a latent defect that will be triggered if a polars strategy is ever refactored to sort internally.

**Fix:** Replace positional assignment with index-aligned assignment:

```python
# After sub_frame is processed:
sub_pdf = sub_frame.to_pandas()
# Attach the original pandas index so .loc can align by label:
sub_pdf.index = pdf.index[mask]  # pandas boolean mask gives the same index positions
pdf.loc[mask, column] = sub_pdf[column]  # now label-aligned, not positional
```

**Verify:** Write a test handler that sorts its polars subset by the column value in reverse. Run `run_with_when_gate_polars` and assert the output assigns values to the correct rows.

---

### F5 — MEDIUM | Correctness
**`_eval_predicate`: `mask.dtype != bool` incorrectly rejects nullable boolean Series**

```python
# execution/_when_gate.py
if not isinstance(mask, pd.Series) or mask.dtype != bool:
    raise StrategyError(code="when_expression_not_boolean", ...)
```

`pd.DataFrame.eval()` with `engine="numexpr"` returns numpy-backed `bool` dtype in standard cases. However, if the DataFrame contains nullable-typed columns (`pd.BooleanDtype`, `pd.Int64Dtype`, etc.) and the expression references them, pandas can return a nullable boolean Series with `dtype=pd.BooleanDtype()`. `pd.BooleanDtype() != bool` evaluates to `True`, so a perfectly valid boolean `when:` expression on a nullable-typed column raises a false-positive error.

**Impact:** Operators who store boolean flags in nullable columns (common in read-from-postgres flows where `NOT NULL` columns may still be read as nullable by `pd.read_sql`) hit an opaque `when_expression_not_boolean` error with no workaround except to cast their column.

**Fix:**
```python
import pandas as pd

def _is_bool_mask(s: pd.Series) -> bool:
    return s.dtype == bool or isinstance(s.dtype, pd.BooleanDtype)

if not isinstance(mask, pd.Series) or not _is_bool_mask(mask):
    raise StrategyError(code="when_expression_not_boolean", ...)
```

---

### F6 — MEDIUM | Performance
**`run_with_when_gate_polars`: three polars↔pandas conversions per gated column**

For a column with `plan.when` set, the polars path converts:
1. `frame.to_pandas()` (full frame, for predicate eval)
2. `sub_frame.to_pandas()` (filtered subset, for writeback)
3. `pl.from_pandas(pdf[[column]])` (single column, back to polars)

Each conversion is O(rows × dtype_width). For a 1M-row table with 10 gated columns, this is 30 full-frame conversions. The pandas path pays one copy (`df.loc[mask].copy()`) and one assignment — about 6× cheaper.

**Impact:** Measurable throughput regression on the polars path for tables with `when:` gates. Bottleneck is memory bandwidth, not CPU.

**Measure:** `timeit` a 500K-row frame with a `when:` gate on 5 columns, comparing polars vs pandas adapters. Profile with `scalene` to confirm the polars↔pandas round-trips dominate.

**Fix (medium effort):** Evaluate the `when:` predicate in native polars using `pl.Expr` if the expression is simple enough (column comparisons), falling back to the numexpr path only when the expression can't be parsed as a polars expression. This removes the first `to_pandas()` for the common case.

---

### F7 — MEDIUM | Design / Security
**`_nested.py`: `jsonpath_ng.update()` on overlapping paths causes silent partial masking**

When a JSONPath expression matches nested paths in the same object (e.g., `$..name` matches both `root.name` and `root.address.name`), `leaf_values` contains entries for both. The writeback loop calls `m.full_path.update(parsed, new_value)` for each match in collected order. For jsonpath-ng, `update()` mutates `parsed` in place. If the first update changes the structure referenced by the second match (e.g., replacing an object with a scalar), the second `m.full_path.update(parsed, ...)` silently fails or writes to the wrong location — some PII leaves are unmasked.

**Impact:** Recursive/wildcard JSONPath expressions (`$..ssn`, `$.*.name`) that hit structurally nested paths can produce partially-masked output without any error or warning.

**Verify:** `iter_spans` is not involved here. Write a test with JSONPath `$..id` on `{"id": "123", "sub": {"id": "456"}}`. Assert both IDs are masked.

**Fix:** After `jsonpath_expr.find(parsed)`, check that no match's path is a prefix of another's before proceeding. If overlapping paths are detected, emit a `QualityWarning(code="nested_overlapping_paths")` and fall back to masking in leaf-to-root order (deepest first) so parent-path updates don't invalidate child references.

---

### F8 — LOW | Correctness
**`_normalize_job_seed`: `bool` seed silently coerces to 0 or 1**

```python
seed_int = int(job_seed_raw)  # int(True) == 1, int(False) == 0
```

A YAML with `seed: true` (intended to mean "enable seeding") compiles successfully to `seed_int = 1`. `seed: false` compiles to `seed_int = 0` — same as omitting the key. Neither triggers `seed_not_numeric`. The fix for F7 in the MG-6 triage (Dennis QA 2026-05-31) was aimed at strings like `"dev"`, not booleans.

**Fix:** Add `bool` to the rejection branch:
```python
if isinstance(job_seed_raw, bool):
    raise PlanCompileError(
        code="seed_not_numeric",
        path="global_settings.seed",
        message=f"seed must be an integer; got bool {job_seed_raw!r}. Did you mean seed: 0 or seed: 1?",
    )
```

---

### F9 — LOW | Reliability
**`_distribution_behavior.py`: `from_profile` check requires exact `True` sentinel; `1` or `"true"` would miss it**

```python
if cfg.get("from_profile") is True:
    return "preserves_all"
```

PyYAML parses `from_profile: yes` and `from_profile: true` both as Python `True`, so those cases are fine. But `from_profile: 1` (integer) is parsed as `1`, which is not `True` by identity, and returns `"destroys_frequency"` instead of `"preserves_all"`. This is a low-impact metadata classification error (FE drift badge is wrong, masking is unaffected).

**Fix:** `if cfg.get("from_profile") is True or cfg.get("from_profile") == 1:`  — or simply use `if cfg.get("from_profile"):` (truthy check).

---

### F10 — LOW | Performance
**`TextRedactHandler.run`: `col.at[idx] = text` write on no-spans path is a no-op**

```python
spans = iter_spans(text, detector_ids)
if not spans:
    col.at[idx] = text   # ← unnecessary; `col` already has `text` at this position
    continue
```

This writes `text` back to the position it was just read from. The `col.at[idx] = text` call still triggers pandas' internal setitem path, including dtype checks and potential copy-on-write triggers. For columns with many cells that have no PII spans, this is meaningless overhead.

**Fix:** Remove the assignment:
```python
if not spans:
    continue
```

---

### F11 — NIT | Design
**`_composite.py`: `bundle` variable shadows config-dict then output-dict in the same scope**

```python
bundle = cfg.get("bundle") or []          # config: list of {column, provider} dicts
...
generator = composite_custom(bundle=bundle, ...)  # config bundle → CompositeGenerator
...
bundle = generator.generate_bundle(...)   # ← now bundle is the generated output dict
for out_col, series in bundle.items():    # confusingly reads "the config bundle"
```

Rename the generated output to `output_bundle` or `generated` to eliminate the shadow.

---

### F12 — NIT | Reliability
**`_normalize_job_seed` error message includes sprint-internal references**

```python
message=(
    "...This rejection (Dennis QA triage 2026-05-31 / engine "
    "session 2 F7) makes such configs fail loud."
)
```

This audit trail is valuable internally but operators reading the error message have no context for "Dennis QA triage" or "session 2 F7". Remove or move it to a comment.

---

## 3. Performance Notes

**`TextRedactHandler.run` — CPU-bound, O(n_cells × n_detectors × cell_length)**  
Each non-null cell runs `iter_spans`, which executes up to 10 `finditer` calls (one per detector). For a 100K-row clinical-notes column with average 500-character cells, this is ~100K × 10 × 500 = 500M character operations. numexpr is not in the path; this is pure Python regex. Benchmark with `timeit` + `cProfile`. If throughput is under ~5K rows/sec on typical hardware, consider:  
- Aho-Corasick (via `pyahocorasick`) for keyword-style detectors  
- Anchored alternation regex (one compiled `re.compile("email_pat|ssn_pat|...")`) to run a single `finditer` pass  

**`_nested.py` writeback — O(n_matches) in Python**  
The leaf-collection + writeback loop is Python-level per-cell. For JSON arrays with 100 elements each matching the JSONPath, this is 100 Python loop iterations per cell. Not a bottleneck at typical cardinality (most JSON cells have O(10) leaves), but worth noting if `nested` is applied to high-cardinality arrays.

**`run_with_when_gate_polars` — see F6 above.** Primary bottleneck: memory bandwidth from polars↔pandas conversions.

---

## 4. Suggested Tests

| Area | Test case |
|------|-----------|
| `iter_spans` / `us_zip` | `iter_spans("lab value 23456 units", ["us_zip"])` should return a span; `iter_spans("lab value 23456 units")` (all detectors, default) should NOT (after fix). |
| `iter_spans` / `icd10` | `iter_spans("Chapter T52 refers to organic solvents")` — assert `T52` does NOT fire as ICD-10 (no valid subcategory, or add context guard). |
| `nested` / duplicate index | Create `pd.concat([df, df])` (duplicate index), run `NestedStrategyHandler.run`, assert `ValueError` (explicit guard) or correct per-row output (after positional-key fix). |
| `nested` / config error + PII passthrough | Set `target: ""`, run handler, assert the output column is UNCHANGED but a `QualityWarning` is emitted (documents current behavior) — OR assert a `StrategyError` is raised (desired behavior). |
| `nested` / overlapping JSONPath | `{"id": "SSN1", "sub": {"id": "SSN2"}}` with `target: $..id` — assert BOTH are masked. |
| `when` gate / pandas | A handler that internally sorts its subset (monkey-patched in test) — assert output is still correct after `run_with_when_gate`. |
| `when` gate / polars | Same but for `run_with_when_gate_polars` — after fix, assert correct despite handler reordering. |
| `when` gate / nullable bool | DataFrame with `pd.BooleanDtype()` column, `when: "flag == True"` — assert no `when_expression_not_boolean` after fix. |
| `_normalize_job_seed` / bool | `config = {"global_settings": {"seed": True}}` → assert `PlanCompileError(code="seed_not_numeric")` after fix; currently raises no error. |
| `text_redact` / no-op | Column with no PII → `handler.run` returns identical df; assert no `.at` writes touch the column (track with mock). |
| `distribution_behavior_for` / bool from_profile | `provider_config=(('from_profile', 1),)` → assert `"preserves_all"` after fix; currently returns `"destroys_frequency"`. |
| `composite_custom` / bundle shadowing | Integration test that exercises the `composite_custom` path and asserts `bundle` config dict is not mutated by `generate_bundle`. |

---

## 5. What's Good

- **`_eval_predicate` security posture is correct.** `engine="numexpr"`, `local_dict={}`, `global_dict={}` — the Dennis C1 scope-clamp is consistently applied and the L2 gate (Dennis MG-3) correctly chains the original exception via `from exc` while surfacing a stable operator-facing message. This is the right pattern.
- **`_splice` is clean and correct.** The span-based text replacement handles cursor tracking, non-overlapping span guarantee, and the `label_token` flag without any off-by-one risk.
- **`_SPAN_DETECTORS` design is sound.** Deliberately excluding name-hint-only detectors (`person_name`, `address`, `biometric_id`, etc.) from span detection is the correct call — those detectors require a column-name signal that's unavailable in free text. The `F1` finding is a tuning gap, not a design flaw.
- **`_normalize_job_seed` fix is correct and well-motivated.** Rejecting non-numeric seeds with a typed error code closes the silent-fallback-to-zero footgun identified in Dennis's triage. The `bool` gap (F8) is a minor omission, not a design miss.
- **`NestedStrategyHandler` lazy import of `SCALAR_HANDLERS` correctly breaks the cycle** and the comment explains why. The single-batch delegation to the child handler preserves vectorization — the right design for deterministic parity with direct strategy invocation.
- **`CompositeHandler` wiring error detection is solid.** The `composite_output_column_missing` check prevents silent partial-writes; raising `ExecutionError` (not a warning) is the correct severity for a wiring bug.
