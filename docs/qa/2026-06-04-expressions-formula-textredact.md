# Engine QA Review — 2026-06-04

Reviewer: QA/performance pass (automated session)  
Scope: `src/decoy_engine/expressions.py`, `src/decoy_engine/transforms/formula.py`,
`src/decoy_engine/execution/_strategies/_formula.py`,
`src/decoy_engine/execution/_strategies/_text_redact.py`  
Context: SEC.1 C1 (simpleeval sandbox, 2026-06-03), S5c (text_redact strategy, 2026-05-31 / wired 2026-06-03)

Previous QA branches checked (to avoid duplication):
- `qa/2026-06-03-engine-review` — covered pool sampler, FPE, formula eval()
  RCE (F5), generators, HKDF. That F5 is now fixed by SEC.1 C1.
- `qa/review-2026-06-03-strategies-instrumentation` — execution strategies
- `qa/review-2026-06-03-when-gate-pipeline-postmask` — when-gate, plan, post-mask

This review focuses on the code those branches did NOT touch: the
replacement simpleeval evaluator, `FormulaStrategy`'s RNG and hot-path
behaviour, the V2 formula strategy handler, and the `TextRedactHandler`.

---

## Summary

The simpleeval migration (SEC.1 C1) closes the previous `eval()` RCE correctly:
`_SafeRe` is a well-designed module-reference firewall, and simpleeval's
`_`-prefix block forecloses the class-traversal escape. The two most important
new issues are: (1) `FormulaStrategy.apply()` re-parses the expression AST on
every row via a new `EvalWithCompoundTypes` construction — at 1M rows this is
~5-10 s of avoidable overhead; (2) `TextRedactHandler` silently returns an
unmodified dataframe with no `QualityWarning` when `token` is not a string,
which means a `token: null` YAML misconfiguration leaves PHI in place without
any diagnostic signal.

---

## Findings

### F1 — HIGH | Performance | `safe_eval` re-parses AST on every row

**File:** `transforms/formula.py:60-65`, `expressions.py:118-130`

```python
# transforms/formula.py — called once per row via column.apply()
return column.apply(
    lambda v: v if pd.isna(v) else safe_eval(expr, scope, {"value": v})
)

# expressions.py — inside safe_eval
return EvalWithCompoundTypes(names=scope, functions=functions).eval(expr)
```

`EvalWithCompoundTypes(...)` constructs a fresh evaluator object on every call.
Inside `.eval(expr)`, simpleeval calls `ast.parse(expr)`. `ast.parse` on a
short formula (~50 chars) costs approximately 5-10 µs. At 1 M rows that is
5-10 s of pure AST-parse overhead before any value processing. At 100 K rows
it is 500 ms-1 s, noticeable on interactive mask previews. The bottleneck is
CPU / Python object allocation (not I/O).

**Verify:** `python -m timeit -n 1000 -r 5 "import ast; ast.parse('str(value).upper()')"` —
measure µs per parse. Multiply by row count.

**Fix:** Construct the `EvalWithCompoundTypes` once per `apply()` call and
rebind only the `value` name before each row:

```python
def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
    expr = rule.get("formula", "")
    if not expr:
        ...
        return column.copy()
    col_name = rule.get("column", "unnamed")
    seed_material = f"{col_name}|{expr}".encode("utf-8")
    formula_seed = int(hashlib.sha256(seed_material).hexdigest()[:16], 16)
    rng = random.Random(formula_seed)
    scope = make_mask_globals(rng)
    scope_no_builtins = {k: v for k, v in scope.items() if k != "__builtins__"}
    functions = {k: v for k, v in scope_no_builtins.items() if callable(v)}
    evaluator = EvalWithCompoundTypes(names=scope_no_builtins, functions=functions)

    def _eval_row(v):
        if pd.isna(v):
            return v
        evaluator.names["value"] = v          # rebind, no re-parse
        if callable(v):
            evaluator.functions["value"] = v  # keep functions in sync
        return evaluator.eval(expr)

    return column.apply(_eval_row)
```

Note: `evaluator.names` is the dict simpleeval reads at `eval`-time, so
mutating it before each call is safe and avoids a new object per row.

---

### F2 — HIGH | Correctness / Determinism | Formula RNG seed excludes job seed

**File:** `transforms/formula.py:42-55`

```python
seed_material = f"{col_name}|{expr}".encode("utf-8")
formula_seed = int(hashlib.sha256(seed_material).hexdigest()[:16], 16)
rng = random.Random(formula_seed)
```

The per-formula RNG seed is derived exclusively from the column name and
expression text — no job-level seed component. Two separate mask runs that
share the same table name and formula expression (even with different
`job_seed` values set in `PipelineConfig.global_settings.seed`) produce
byte-identical output from any `randint`/`choice`/`random` call. This
breaks the property that two distinct jobs can generate independently
pseudonymized output for the same column (important when masking the same
table for different partner deliveries or audit trails).

The fix depends on the intended semantics:

- If the formula is always a deterministic function of the cell value (no RNG),
  this is intentional and correct; the docstring should say so explicitly.
- If formula RNG should be job-keyed (the common case for `randint`-using
  formulas), include the job seed in the derivation:

```python
# The job_seed (int) must be threaded into apply() from the execution context
seed_material = f"{job_seed}|{col_name}|{expr}".encode("utf-8")
```

The V2 `FormulaStrategyHandler.run()` has access to `StrategyContext` (the
`ctx` parameter); add `ctx.job_seed` or an equivalent and pass it through.

**Verify:** Run two mask jobs with the same YAML but different seeds, both
containing `formula: "randint(0, 999)"`; assert outputs differ.

---

### F3 — HIGH | Data | Non-string `token` silently bypasses text_redact

**File:** `execution/_strategies/_text_redact.py:68-70`

```python
if not isinstance(token, str):
    return df, []
```

If the operator configures `token: null` or `token: 42` in the pipeline
YAML (both valid YAML scalars), `cfg.get("token", _DEFAULT_TOKEN)` returns
`None` or `42`. The `isinstance(token, str)` check then causes the handler to
return the dataframe unchanged — all PHI spans in the `clinical_notes` column
would be left in place — with an empty warnings list. No log message is
emitted, and no `QualityWarning` is surfaced to the job's quality report.

The docstring says this is intentional ("a misconfigured plan never crashes
the run"), but silently passing through PHI is a worse outcome than crashing.

**Fix:** emit a `QualityWarning` and fall back to the default token rather
than suppressing redaction entirely:

```python
warnings: list[QualityWarning] = []
if not isinstance(token, str):
    warnings.append(
        QualityWarning(
            column=column,
            message=(
                f"text_redact 'token' must be a string; got "
                f"{type(token).__name__!r}. Falling back to \"{_DEFAULT_TOKEN}\"."
            ),
        )
    )
    token = _DEFAULT_TOKEN
```

**Test:** configure `token: null` in a text_redact rule; assert the column
still has PII redacted (fall-back token used) and a `QualityWarning` is returned.

---

### F4 — MEDIUM | Correctness | Blank detector IDs bypass the fail-safe

**File:** `execution/_strategies/_text_redact.py:76-82`

```python
detector_ids = [str(d) for d in detectors_cfg] or None
```

The S5c F2 fail-safe converts an empty list to `None` (all detectors) via
`[] or None`. However, a list of blank strings such as `detectors: [""]` is
Truthy, so it passes through as `[""]`. When `iter_spans` receives
`detector_ids=[" "]`, it looks up each id in `_SPAN_DETECTORS` and silently
skips unknowns. The result is that every detector is skipped and zero PII
spans are found — the column is returned unchanged with no warning. A
whitespace-corrupted YAML config (or a UI that emits blank entries when the
user clears the detector list) silently disables redaction.

**Fix:** strip and filter blank entries:

```python
detector_ids: list[str] | None
if detectors_cfg is None:
    detector_ids = None
elif isinstance(detectors_cfg, (list, tuple)):
    cleaned = [str(d).strip() for d in detectors_cfg if str(d).strip()]
    detector_ids = cleaned or None   # empty-after-strip → all detectors
else:
    return df, []
```

**Test:** `detectors: ["", " "]` with a cell containing a known SSN;
assert the SSN is redacted (all-detectors fallback applies).

---

### F5 — LOW | Design | V2 FormulaStrategyHandler discards QualityWarnings from V1 path

**File:** `execution/_strategies/_formula.py:29-32`

```python
df[column] = _V1_FORMULA.apply(df[column], rule)
return df, []
```

`FormulaStrategy.apply()` emits a `self.logger.warning(...)` when the
formula field is missing or empty, but returns `column.copy()` silently.
The V2 handler wraps this with `return df, []` — the empty warnings list
means the V2 pipeline's quality-event collector never sees a structured
warning. Operators debugging formula misconfiguration must grep logs rather
than reading the job's quality report.

The handler should at minimum check for an empty `formula` and return a
`QualityWarning` directly (rather than delegating to V1's logger):

```python
if not cfg.get("formula", ""):
    return df, [
        QualityWarning(
            column=column,
            message=(
                f"formula strategy on {column!r} has no 'formula' field;"
                " column left unchanged"
            ),
        )
    ]
```

---

### F6 — NIT | Design | `MASK_GLOBALS` retains vestigial `__builtins__: {}`

**File:** `expressions.py:74-75`

```python
MASK_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    ...
}
```

`safe_eval` strips `__builtins__` before constructing the simpleeval scope
(`{k: v for k, v in globals_.items() if k != "__builtins__"}`). The key is
never read by simpleeval and has no effect. The comment correctly labels it
"vestigial" — remove it from `MASK_GLOBALS` and `BASE_GLOBALS` to avoid
confusing future readers who might think it has security significance.

---

## Performance Notes

| Path | Bottleneck | Complexity | Measurement |
|---|---|---|---|
| `FormulaStrategy.apply()` (pre-fix) | CPU / `ast.parse` per row | O(n × parse_cost) | `python -m timeit -n 10000 "ast.parse('str(value).upper()')"` |
| `FormulaStrategy.apply()` (post-fix) | CPU / simpleeval eval | O(n × eval_cost) | `cProfile` on 100K-row formula column |
| `TextRedactHandler.run()` | CPU / n_detectors × regex scans | O(rows × detectors × text_len) | `py-spy top` on a 10K-row clinical_notes column |
| `iter_spans` overlap dedup | CPU / sort | O(k log k) per cell (k = raw span count) | negligible for typical PHI density |

For `TextRedactHandler` on large text columns (multi-paragraph clinical notes),
the per-cell `iter_spans` cost dominates. Profile with:
```
python -m cProfile -s cumulative your_mask_job.py | grep text_redact
```
The `regex.finditer` calls per active detector will dominate. If text lengths
exceed ~10 KB per cell, consider chunking or limiting the active detector set.

---

## Suggested Tests

1. **F1 regression benchmark:** `FormulaStrategy.apply()` on a 1 M-row Series
   with formula `str(value).upper()` — assert wall time < 5 s after the
   per-apply evaluator construction fix (currently ~10-15 s).

2. **F2 job-seed isolation test:** Run two mask jobs on the same data with
   formulas `randint(0, 9999)`, using different `job_seed` values. Assert
   the outputs differ byte-for-byte after fix.

3. **F3 token=null fallback test:** Configure `text_redact` with
   `token: null`; assert (a) the column's PHI spans ARE redacted using
   `"[REDACTED]"`, and (b) at least one `QualityWarning` is returned.

4. **F4 blank detector test:** `detectors: [" "]`; assert redaction still
   applies all built-in detectors. Currently would pass through PHI
   unredacted.

5. **F5 missing-formula warning test:** `FormulaStrategyHandler.run()` with
   an empty `formula` field; assert `warnings` list contains at least one
   entry. Currently returns `[]`.

6. **`_splice` overlap guard test (existing, should pass):** Manually construct
   overlapping raw spans; assert `iter_spans` returns only non-overlapping,
   leftmost-then-longest spans.

7. **`_splice` trailing-text test:** Cell `"prefix ssn@123-45-6789 suffix"` with
   SSN detector; assert result is `"prefix ssn@[REDACTED] suffix"` — trailing
   ` suffix` preserved. Verify cursor-past-end path.

---

## What's Good

- **`_SafeRe` proxy design:** Exposing `re` methods as an ordinary object's
  attributes (not the module itself) is the correct way to block the
  `re.__builtins__` → escape surface. simpleeval would reject a module in
  scope; the proxy sidesteps that rejection cleanly.

- **simpleeval choice:** Using an established restricted-eval library (per the
  engineering-best-practices established-methodology rule) is the right call.
  The `_`-prefix blocking in simpleeval closes the class-traversal RCE without
  rolling a custom AST visitor.

- **`EvalWithCompoundTypes` selection:** Choosing the compound-type variant is
  correct given that formula operators write list comprehensions. The simpler
  `SimpleEval` would reject those. The F1 issue is about construction placement,
  not the class choice.

- **`col.to_list()` batch pattern in `TextRedactHandler`:** The QA-3 fix (batch
  list + single Series assignment vs. per-row `col.at[idx] = ...`) is correctly
  applied here. The duplicate-index issue mentioned in the QA-3 F2 note is
  also avoided by using positional enumeration over `to_list()`.

- **Fail-safe empty-detector coercion:** `[] or None` → all detectors is exactly
  the right default for a PHI-redaction strategy — the safe side of "not sure"
  is more redaction, not less.

- **`_splice` algorithm:** The cursor-based span stitching is correct,
  clear, and O(k) in the number of spans. Non-overlapping + sorted
  precondition from `iter_spans` is correctly relied on.

- **`iter_spans` overlap deduplication:** leftmost-then-longest is the standard
  convention for non-overlapping span extraction (same as spaCy's
  `util.filter_spans`). Sorting by `(start, -length)` correctly implements this.
