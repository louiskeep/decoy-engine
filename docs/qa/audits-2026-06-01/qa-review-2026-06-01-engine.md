# decoy-engine QA Review — 2026-06-01

**Session scope:** MG-1 (Tier-S Sweep), MG-2 (text_redact), MG-3 (when: / nested), MG-4 (composite generators), MG-6 (distribution_behavior + seed_not_numeric fix). All landed on `main` within the 24 hours prior to this review. No prior QA branches exist on decoy-engine; this is the first review.

**Reviewer:** Claude (QA session, 2026-06-01)
**Prior QA branches checked:** `decoy/qa/review-2026-05-30`, `decoy/qa/review-2026-05-31` (CLI plan commands, not engine), `decoy-web/qa/review-2026-05-31` (disguises.ts). None touched engine code.

**Files reviewed in depth:**
- `src/decoy_engine/plan/_compile.py` (`_normalize_job_seed`, `_hash_config`, `_build_seed_envelope`)
- `src/decoy_engine/plan/_types.py` (`ColumnSeed` fields)
- `src/decoy_engine/execution/_when_gate.py`
- `src/decoy_engine/execution/_strategies/_nested.py`
- `src/decoy_engine/execution/_strategies/_text_redact.py`
- `src/decoy_engine/execution/_strategies/_categorical.py`
- `src/decoy_engine/execution/_strategies/_composite.py`
- `src/decoy_engine/execution/_distribution_behavior.py`
- `src/decoy_engine/storm/detectors.py` (`iter_spans` + full detector set)

---

## 1. Summary

The MG-1 through MG-6 sprint is architecturally sound: the determinism contract, scope-clamped numexpr eval, composite wiring, and distribution-behavior metadata are all well-designed. The seed_not_numeric fix (MG-6 F7) is the right direction but is **incomplete**: Python's `int(True)` = 1 and `int(False)` = 0 mean `seed: true` (YAML boolean) silently compiles identically to `seed: 1`, violating the anti-misconfiguration intent the fix was added for. That is the single highest-impact finding. A second correctness bug — silent wrong output when a DataFrame has a duplicate index in the `nested` strategy — is exploitable via normal `pd.concat` patterns.

---

## 2. Findings

### F1 — Critical | Correctness | `plan/_compile.py:_normalize_job_seed`

**Issue:** `bool` (and `float`) seeds are silently accepted as integers.

Python's `int(True) == 1` and `int(False) == 0` — no `TypeError` or `ValueError` is raised. A YAML config with `seed: true` compiles to byte-identical output as `seed: 1`. Similarly, `int(1.5) == 1` means `seed: 1.5` silently truncates to `seed: 1`. The MG-6 commit message explicitly lists "a bool, a dict, a list" as values that should raise `seed_not_numeric`, but the implementation only rejects types for which `int()` raises.

**Impact:** Two pipelines with `seed: true` and `seed: 1` are indistinguishable at plan-compile time. Audit/reproducibility tooling cannot distinguish them. A user who writes `seed: true` by YAML mistake gets a valid plan with seed=1 instead of a rejection.

**Fix:** Add an explicit isinstance check for `bool` (and optionally `float`) before the `int()` coercion:

```python
if isinstance(job_seed_raw, bool):
    raise PlanCompileError(
        code="seed_not_numeric",
        path="global_settings.seed",
        message=(
            f"seed must be an integer; bool is not accepted "
            f"(got {job_seed_raw!r}). Use an integer literal."
        ),
    )
if isinstance(job_seed_raw, float):
    raise PlanCompileError(
        code="seed_not_numeric",
        path="global_settings.seed",
        message=(
            f"seed must be an integer; float is not accepted "
            f"(got {job_seed_raw!r}). Use an integer literal."
        ),
    )
```

These two checks belong immediately after the `if job_seed_raw is None:` branch and before the `try: seed_int = int(job_seed_raw)` block.

**Test to add:**
```python
# tests/unit/plan/test_normalize_job_seed.py
@pytest.mark.parametrize("bad_seed", [True, False, 1.5, 1.0, 0.0])
def test_seed_not_numeric_rejects_bool_and_float(bad_seed):
    config = {"global_settings": {"seed": bad_seed}}
    with pytest.raises(PlanCompileError, match="seed_not_numeric"):
        _normalize_job_seed(config)
```

---

### F2 — High | Correctness | `execution/_strategies/_nested.py`

**Issue:** `per_row_state` uses `row_idx` (the DataFrame index value) as a dict key. Duplicate index values silently overwrite earlier entries, then the writeback cursor misaligns.

**Code path:**
```python
per_row_state: dict[Any, tuple[Any, list]] = {}
for row_idx in col.index:
    ...
    per_row_state[row_idx] = (parsed, list(matches))  # overwrites on duplicate idx
    for m in matches:
        leaf_values.append(m.value)  # ALL leaves still accumulated
```

For `col.index = [0, 0, 1]`:
- First `row_idx=0`: stores state, appends N0 leaf values
- Second `row_idx=0`: **overwrites** state, appends N1 leaf values
- `per_row_state` has 2 entries (0 and 1), but `leaf_values` has N0+N1+N2 entries

During writeback, `cursor` advances for the second `row_idx=0`'s matches, but skips N0 values from the first row silently.

**Trigger:** `pd.concat([df1, df2])` without `ignore_index=True`; `df.loc[boolean_mask]` that produces non-monotonic index; any frame that wasn't explicitly `reset_index(drop=True)` before the pipeline runs. Silent wrong output — no exception.

**Fix:** Use positional enumeration, not index values, as the dict key:

```python
per_row_state: dict[int, tuple[Any, Any, list]] = {}  # key = position
for pos, row_idx in enumerate(col.index):
    cell = col.at[row_idx]
    if pd.isna(cell):
        continue
    ...
    per_row_state[pos] = (row_idx, parsed, list(matches))
    for m in matches:
        leaf_values.append(m.value)

# Writeback:
cursor = 0
for pos in sorted(per_row_state):  # maintain insertion order
    row_idx, parsed, matches = per_row_state[pos]
    for m in matches:
        new_value = new_leaf_values[cursor]
        cursor += 1
        m.full_path.update(parsed, new_value)
    col.at[row_idx] = json.dumps(parsed)
```

Alternately, call `col = col.reset_index(drop=True)` at the top of `run()` and work on the reset copy, writing back via positional assignment.

**Test to add:**
```python
def test_nested_duplicate_index_no_cursor_corruption():
    # DataFrame with duplicate index (simulates pd.concat without ignore_index)
    df = pd.DataFrame(
        {"note": ['{"name": "Alice"}', '{"name": "Bob"}']},
        index=[0, 0],
    )
    # Run nested strategy with hash child on $.name
    # Both rows must produce distinct correct outputs; cursor must not skip row 0's first entry
    ...
```

---

### F3 — High | Performance | `execution/_strategies/_text_redact.py`

**Issue:** The `run()` method iterates cell-by-cell with `col.at[idx]` — the slowest pandas access pattern — and writes back with another `col.at[idx] = ...` scalar set per cell.

```python
for idx in col.index[~na_mask]:
    text = col.at[idx]          # O(1) but in Python loop
    spans = iter_spans(...)      # D regex scans per cell
    col.at[idx] = _splice(...)   # scalar pandas write per cell
```

For N=100k rows, D=10 detectors, this is 100k Python iterations × (D regex `finditer` calls + 1 pandas scalar write). On production clinical notes (50–300 words each), expect throughput < 500 rows/second. The docstring says the limit is "~100k rows of short notes" but gives no concrete throughput number — operators have no way to size the operation.

The `col.at[idx] = text` no-op when `not spans` (line `col.at[idx] = text; continue`) also wastes a pandas write per non-matching cell.

**Fix:** Collect output into a list and assign once:

```python
values = col.tolist()
na_positions = set(col.index[na_mask].tolist())
for i, idx in enumerate(col.index):
    if idx in na_positions:
        continue
    text = values[i]
    if not isinstance(text, str):
        text = str(text)
    spans = iter_spans(text, detector_ids)
    if spans:
        values[i] = _splice(text, spans, token, label_token)
df[column] = values
```

Removes all per-cell `.at[]` reads/writes. Benchmark with `python -m timeit` or `scalene` against a 10k-row column before and after.

**Note:** The regex work in `iter_spans` itself is the dominant cost and cannot be vectorized without rewriting the detector layer. The `.at[]` overhead is secondary but eliminates easily.

---

### F4 — High | Correctness | `execution/_when_gate.py:_eval_predicate`

**Issue:** `mask.dtype != bool` rejects pandas nullable `BooleanDtype()` masks with a spurious `when_expression_not_boolean` error.

```python
if not isinstance(mask, pd.Series) or mask.dtype != bool:
    raise StrategyError(code="when_expression_not_boolean", ...)
```

`pd.BooleanDtype() != bool` evaluates to `True` (they are different type objects), so a nullable boolean Series raises the error even though it is a valid boolean result. This can be triggered when a DataFrame column has `pd.BooleanDtype` dtype (from `pd.read_sql` with `nullable_int=True`, `pd.read_parquet`, or explicit `astype("boolean")`), and the `when:` expression directly references it.

umexpr via `df.eval(engine="numexpr")` typically produces `dtype('bool')` (numpy bool), not `BooleanDtype`, because it converts pandas extension arrays to numpy before evaluation. However, if the eval falls back to Python evaluation (which can happen in edge cases with certain column types), it may produce a `BooleanDtype` result.

**Fix:** Use `pd.api.types.is_bool_dtype(mask.dtype)` which returns `True` for both `dtype('bool')` and `BooleanDtype()`:

```python
if not isinstance(mask, pd.Series) or not pd.api.types.is_bool_dtype(mask.dtype):
    raise StrategyError(code="when_expression_not_boolean", ...)
```

**Test to add:**
```python
def test_when_gate_nullable_bool_dtype():
    df = pd.DataFrame({"age": pd.array([25, 30, None], dtype="Int64"), "name": ["a","b","c"]})
    # when expression on a nullable-int column: should not raise when_expression_not_boolean
    ...
```

---

### F5 — Medium | Correctness | `plan/_compile.py:_normalize_job_seed`

**Issue:** Float seeds are silently accepted via `int()` truncation. `seed: 1.5` compiles identically to `seed: 1`; `seed: 1.9` also compiles to `seed: 1`. No error, no warning.

This is the float-specific half of F1. The fix is the same `isinstance(job_seed_raw, float)` guard described in F1.

---

### F6 — Medium | Data | `storm/detectors.py:iter_spans` — `us_zip` over-fires in free text

**Issue:** `_SPAN_DETECTORS` includes `us_zip` with pattern `\d{5}(?:-\d{4})?`, which matches any 5-consecutive-digit sequence in free text without a word-boundary constraint. Clinical notes commonly contain 5-digit numeric values that are not ZIP codes:

- `"Dose: 12345 mg"` → matches, redacts `12345`
- `"BP record ID 98765"` → matches, redacts `98765`
- `"Weight: 10000 grams"` → no match (5 zeros fails? no — `10000` matches `\d{5}`)

The column-level `detect_us_zip` fires at 50% match rate, which is already somewhat noisy. As a span detector inside free text, it fires on any 5-digit run with no minimum context. This is a privacy-correct error (over-redaction, not under-redaction) but it corrupts clinical note content.

The docstring for `detect_us_zip` (column-level) implicitly relies on the column-name hint to lower noise. That gate does not exist in `iter_spans`.

**Fix options (pick one):**

1. Add word-boundary anchors to the span variant: `r'(?<![\d])\d{5}(?:-\d{4})?(?![\d])'` — this at least requires the 5 digits to be isolated, so `"12345"` matches but `"123456"` does not.
2. Remove `us_zip` from `_SPAN_DETECTORS` for V1. ZIP spans in clinical notes add marginal privacy value; the entire note is already being scanned for SSN, PAN, NPI, IBAN.
3. Document the false-positive risk prominently in `_text_redact.py` so operators know to exclude `us_zip` from `detectors:` for clinical-note columns.

Option 1 is lowest risk; option 2 is cleanest for V1.

**Test to add:**
```python
def test_iter_spans_us_zip_word_boundary():
    # Should NOT match a 5-digit dose or weight
    spans = iter_spans("Patient weight 12345 grams", ["us_zip"])
    # With option-1 fix: spans is empty (12345 is surrounded by space, not digits, so it DOES match!)
    # Actually option 1 doesn't help here -- the word-boundary approach must use lookbehind/ahead for non-digit
    # The real fix is: require the 5 digits to NOT be surrounded by digits AND to NOT be preceded by a currency sign or unit
    # This is V1.5 territory; at minimum document the behavior.
```

Note: `\b\d{5}(?:-\d{4})?\b` is not reliable for digit-only strings (\b only transitions between \w and \W; `\b12345\b` matches `12345` in `" 12345 "` and also in `"abc12345def"`). Proper word-boundary for numeric tokens requires lookahead/behind for non-digits: `(?<!\d)\d{5}(?!\d)` — this correctly excludes `123456` while matching `12345`.

---

### F7 — Medium | Data | `execution/_strategies/_nested.py` — child `ColumnSeed` inherits wrong `technique_class`

**Issue:** The synthetic `child_seed` in `_nested.py` sets `technique_class=plan.technique_class`. Since `technique_class_for("nested")` returns `None` (nested is excluded from `TECHNIQUE_CLASS_BY_STRATEGY` by design), every nested column's `plan.technique_class` is `None`. The child_seed therefore gets `technique_class=None` even when the child strategy (e.g., `hash` → `pseudonymisation`, `redact` → `anonymisation`) has an explicit classification.

This means the post-mask coherence audit (MG-4's `_composite_coherence.py` pattern) and any downstream GDPR classifier consuming `technique_class` from the runtime context will see `None` for nested child runs instead of the correct child technique.

**Fix:** Derive the child's technique class from the child strategy name:

```python
from decoy_engine.execution._technique_class import technique_class_for
...
child_seed = ColumnSeed(
    ...
    technique_class=technique_class_for(child_strategy_name),  # not plan.technique_class
    when=None,
)
```

---

### F8 — Medium | Correctness | `execution/_strategies/_nested.py` — JSON not byte-stable

**Issue:** `json.dumps(parsed)` re-serializes the entire JSON document after writeback. The strategy's module docstring says "Non-PII text is preserved byte-for-byte" and the `text_redact` docstring says the same. This claim is not true at the byte level:

- `json.loads` then `json.dumps`: floats `1.0` → `"1.0"` vs. original `"1"` (JSON spec allows both)
- Trailing whitespace and pretty-print formatting are stripped
- Unicode escapes (`A` → `A`) may normalize
- Repeated object keys (technically invalid JSON) collapse silently

For masking correctness this is acceptable — the logical content is preserved. But the contract as stated is incorrect and could cause issues for downstream consumers doing byte-level diffs (e.g., audit tools comparing original vs. masked documents to verify non-PII fields are untouched).

**Fix:** Update the docstring to say "logical content of non-PII nodes preserved; serialization may differ from the source (whitespace, float formatting)" rather than "byte-for-byte."

---

### F9 — Low | Data | `execution/_strategies/_categorical.py:_build_cdf`

**Issue:** Weights smaller than `1e-6` round to zero via `int(running / total * 1_000_000)`. A weight of `0.0000005` (below the CDF resolution floor of `1 / 1_000_000 = 1e-6`) produces `cdf[i] = cdf[i-1]`, making `bisect_right` skip that category entirely. The category is effectively weight-zero but no error or warning is emitted.

This is a theoretical issue at extreme weight ratios (1:1,000,000). In practice, operators using the weights feature would not specify such extreme distributions. However, the contract (all weights > 0 produce nonzero probability) is silently violated.

**Fix:** After computing the CDF list, verify no consecutive duplicate entries exist (except the forced final `= _WEIGHTED_CDF_RES`):

```python
for i in range(len(cdf) - 1):
    if cdf[i] == cdf[i + 1]:
        raise StrategyError(
            code="categorical_weights_underresolved",
            strategy="categorical",
            message=(
                f"weight at index {i} is too small to be represented at "
                f"the CDF resolution ({_WEIGHTED_CDF_RES}); minimum representable "
                f"weight is 1/{_WEIGHTED_CDF_RES}."
            ),
        )
```

---

### F10 — Low | Design | `execution/_strategies/_composite.py` — `bundle` variable shadowed

**Issue:** `bundle` is used for two unrelated values in `CompositeHandler.run()`:

```python
bundle = cfg.get("bundle") or []     # list[dict] — composite_custom YAML config
generator = composite_custom(bundle=bundle, ...)
...
bundle = generator.generate_bundle(...)  # dict[str, Series] — execution output
for out_col, series in bundle.items():
```

The names collide but the code is correct (the config `bundle` is only used to construct the generator; the output `bundle` is only used afterward). The collision confuses readers and is an accident waiting to happen if the `composite_custom` block is extended.

**Fix:** Rename the config variable: `bundle_spec = cfg.get("bundle") or []`.

---

### F11 — Low | Performance | `execution/_when_gate.py` — Polars path materializes full frame for predicate eval

**Issue:** `run_with_when_gate_polars` calls `frame.to_pandas()` on the entire polars frame to evaluate the `when:` predicate via numexpr. For a 10M-row polars table where only 100 rows match the predicate, this materializes ~10M rows in memory as a pandas DataFrame just for the boolean mask computation.

**Impact:** O(N) memory allocation and type conversion for every `when:` expression on any polars-backed table, regardless of selectivity. For wide tables with many columns, the `frame.to_pandas()` call may allocate 2-5x the frame size transiently.

**Fix (V1.5):** Parse the `when:` expression to extract referenced column names (e.g., via `numexpr.necompiler.precompile` or a regex on identifier tokens) and convert only those columns to pandas for eval. The rest of the frame stays in polars until writeback.

For V1, add a comment noting the full-frame conversion cost and recommend that operators using `when:` on large polars tables target selectivity above 10% to amortize the conversion cost.

---

## 3. Performance Notes

| Component | Bottleneck | Big-O | How to profile |
|---|---|---|---|
| `text_redact` | Python loop + per-cell `iter_spans` | O(N × D) | `scalene src/decoy_engine/execution/_strategies/_text_redact.py` |
| `categorical` deterministic | Python loop + per-row `derive_index` | O(N) — no vectorized path | `python -m cProfile`; compare to non-deterministic `rng.integers` baseline |
| `nested` JSON parse + writeback | Python loop over matched rows | O(N_match × J) where J = JSON doc depth | `cProfile` on a 10k-row JSON column |
| `when` polars path | `frame.to_pandas()` on full frame | O(N × W) where W = column width | `time.perf_counter` around the `.to_pandas()` call |

For `text_redact` at production scale (100k clinical notes, avg 200 words, 10 detectors): estimated throughput before F3 fix is ~200–500 rows/second. After collecting results into a list (F3 fix), the Python loop overhead drops ~30%, but regex work dominates — the real V1.5 fix is a Rust/C extension for multi-pattern text scanning.

---

## 4. Suggested Tests

| # | Module | Test case |
|---|---|---|
| T1 | `plan/_compile.py` | `seed: true` raises `seed_not_numeric` (bool guard, F1) |
| T2 | `plan/_compile.py` | `seed: false` raises `seed_not_numeric` |
| T3 | `plan/_compile.py` | `seed: 1.5` raises `seed_not_numeric` (float guard, F5) |
| T4 | `plan/_compile.py` | `seed: 1.0` raises `seed_not_numeric` (float, even if integer-valued) |
| T5 | `_nested.py` | DataFrame with duplicate index → both rows written back correctly, no cursor skew |
| T6 | `_nested.py` | `technique_class` on child_seed matches the child strategy's class (not None) |
| T7 | `_when_gate.py` | Column with `pd.BooleanDtype` → `when:` expression evaluates without raising `when_expression_not_boolean` |
| T8 | `_text_redact.py` | 5-digit non-ZIP number in clinical note with `us_zip` in detector_ids → (document current behavior; after F6 fix, should not be redacted) |
| T9 | `_categorical.py` | Weight `[1e-7, 1.0]` → raises `categorical_weights_underresolved` (after F9 fix) |
| T10 | `_when_gate.py` | Two identical runs of `when:` gate on same seed + input → byte-identical output (determinism cross-check) |
| T11 | `_normalize_job_seed` | Negative seed → `seed_overflow` (already tested); `seed: -1` → `seed_overflow` |
| T12 | `iter_spans` | Free-text cell containing 5-digit medication dose → document false-positive before fix, absence after |

---

## 5. What's Good

**Scope-clamped numexpr eval** (`_when_gate.py`): The `local_dict={}, global_dict={}` pattern correctly blocks `@var`-style scope walks. The chained `from exc` on the `L2` fix surfaces the numexpr internal for engineers while hiding it from operator-facing messages. This is the right design.

**`_build_cdf` negative-weight guard** (`_categorical.py`): The `if w < 0: raise` check is correctly placed *before* `running += w`, so a negative weight can't corrupt the running sum before the error fires.

**`iter_spans` overlap deduplication**: Leftmost-then-longest via `sort(key=lambda s: (s.start, -(s.end - s.start)))` + a `last_end` cursor is clean and correct. Reusing the column-level validator functions (`_luhn_valid`, `_iban_valid`, `_npi_valid`) rather than reimplementing them is the right factoring — span-level and column-level detection cannot drift.

**ContextVar for per-scan name-hint extras** (`detectors.py`): Using `ContextVar` instead of a module-global prevents concurrent scan cross-contamination. This is a subtle but important concurrency correctness choice.

**`_normalize_job_seed` MG-6 fix intent**: The fix correctly identifies and closes the category of "malformed seeds that compiled identically." The direction (explicit rejection rather than silent fallback) is right. F1 + F5 are narrowing the scope of the fix, not reversing it.

**`ColumnSeed.distribution_behavior` defaulting to `None`**: The field default prevents any `MG-3`-era `ColumnSeed` construction (like the one in `_nested.py`) from raising a `TypeError`. The field is opt-in at plan-compile, which preserves backward compatibility.

**`_composite.py` wiring guard** (`composite_output_column_missing`): Raising `ExecutionError` when a composite produces an undeclared output column is the right defensive check. Silent partial writes would be worse.

---

## 6. Remediation Priority

| Finding | Severity | Fix effort | Block release? |
|---|---|---|---|
| F1 bool/float seed | Critical | 30 min | Yes |
| F2 nested duplicate index | High | 1 hr | Yes — silent wrong output |
| F3 text_redact perf loop | High | 30 min | No (documented limit) |
| F4 nullable bool dtype | High | 5 min | No (rare trigger) |
| F5 float seed (same root as F1) | Medium | Covered by F1 fix | Yes (bundled with F1) |
| F6 us_zip span false positives | Medium | 30 min | No (over-redaction, not under) |
| F7 nested technique_class | Medium | 5 min | No |
| F8 nested JSON doc claim | Medium | Doc only | No |
| F9 CDF weight underresolved | Low | 30 min | No |
| F10 bundle variable shadow | Low | 2 min | No |
| F11 polars full-frame eval | Low | V1.5 | No |

F1 + F5 (same fix), F2, and F4 are the three changes that should land before the next gate review.
