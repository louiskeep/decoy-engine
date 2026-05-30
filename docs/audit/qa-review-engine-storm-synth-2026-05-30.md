# QA Review — STORM Profiler · Detectors · Synthesize Pipeline

**Date:** 2026-05-30  
**Reviewer:** Claude Sonnet 4.6 (QA session)  
**Branch:** `claude/gallant-curie-8HZA2`  
**Scope:** `storm/profiler.py`, `storm/detectors.py`, `generation/synthesize.py`, `context.py` (post-Q11)  
**Excluded (reviewed in earlier sessions today):** `execution/_transforms.py`, `config/_transforms.py`, `execution/_pandas_adapter.py`, `generation/pool/_sampler.py`

---

## 1. Summary

The STORM profiler and detector layer are structurally sound and well-thought-out — the k-anonymity algorithm, ContextVar-backed per-scan extras, and Luhn/IBAN/NPI validators are the right tools. The most critical finding is that `run_storm` accepts `sample_row_cap` and `sample_strategy` but **never uses them** — the profiler always scans the entire DataFrame regardless of what the caller passes. For large tables this silently violates the expected performance contract. The second critical finding is that `synthesize.py` mutates the **global** `random` module state on every column generator call, making concurrent generation jobs nondeterministic on a multi-threaded/multi-process worker — a core invariant violation for a determinism-first system.

---

## 2. Findings

### F-1 · **Critical** · Correctness — `sample_row_cap` / `sample_strategy` silently ignored

**File:** `storm/profiler.py` — `run_storm()` → `_run_storm_inner()`

`run_storm` accepts `sample_strategy: str = "full"` and `sample_row_cap: int | None = None` and passes them straight to `_run_storm_inner`. Inside `_run_storm_inner` neither parameter is ever read — the function calls `_profile_column(df[col], total, ...)` for every column of the full `df` unconditionally.

The parameters appear in `StormProfile` output (correctly recording *what was requested*) but the sampling itself is never applied. Callers that pass e.g. `sample_row_cap=50_000` on a 5M-row DataFrame get a full scan, not a 50k-row sample — O(100×) more work than requested, with a corresponding scan time that may exceed the caller's timeout budget.

**Impact:** Silent performance contract violation; platform scan budgets and CLI `--sample` flags do nothing. For wide high-cardinality tables this could cause OOM or timeout on the platform job worker.

**Verify:** `run_storm(df_5m_rows, "test", sample_row_cap=1000); assert profile.row_count == 1000` — this assertion will fail today.

**Fix:** Apply sampling at the entry point before passing `df` to `_run_storm_inner`:

```python
def run_storm(df, source_label, *, sample_strategy="full",
              sample_row_cap=None, ...):
    if sample_strategy != "full" and sample_row_cap and len(df) > sample_row_cap:
        # Deterministic sample: fixed seed so two scans of the same data
        # with the same cap agree on which rows were profiled.
        df = df.sample(n=sample_row_cap, random_state=42)
    ...
```

For the platform caller the sample seed should match the pipeline seed. A `random_state` parameter threaded from `run_storm` → sampling step keeps it deterministic.

---

### F-2 · **Critical** · Determinism — `random.seed()` global mutation in `synthesize.py`

**File:** `generation/synthesize.py` — `_categorical`, `_faker`, `_reference`, `_apply_null_probability`

Every generator calls `random.seed(col_seed)` or `random.seed(col_seed + i)`, mutating the **module-global** `random` state. This is correct in a single-threaded, single-process context (output is deterministic within that process). It breaks in any of these real scenarios:

1. Platform runs two pipeline jobs in the same process using `concurrent.futures.ThreadPoolExecutor` — threads share the global RNG; one thread's `random.seed()` overwrites another's mid-stream.
2. Any code path (including Faker internals, numpy, or other libraries) calls `random.random()` / `random.seed()` between two `_generate_column` calls in the same pipeline — the documented per-row seed guarantee is silently violated.
3. The formula generator delegates to `ColumnGenerator._eval_formula_inline` which itself uses global `random` — two layers of global mutation.

**Impact:** Nondeterministic generation output when jobs run concurrently; violates the core system invariant. Not caught by single-threaded parity tests.

**Verify:** Run `generate_tables` twice from two threads simultaneously with the same config + seed and diff the outputs — they will diverge.

**Fix:** Replace all global `random` calls with a local `random.Random(seed)` instance:

```python
# _categorical (and all other generators) — BEFORE
random.seed(col_seed)
return random.choices(cats, weights=weights, k=n)

# AFTER
rng = random.Random(col_seed)
return rng.choices(cats, weights=weights, k=n)
```

For `_faker`, the per-row seed pattern becomes:
```python
rng = random.Random(col_seed)
for i in range(n):
    row_seed = col_seed + i
    rng.seed(row_seed)          # instance-local, not global
    faker_inst.seed_instance(row_seed)
    out.append(provider_func(**faker_kwargs))
```

Note: V1 uses global `random.seed()` as well (by design, the parity freeze locks this). The fix here intentionally diverges from V1's implementation detail — output is byte-identical (same seed → same sequence from `random.Random`) but the concurrency hazard is resolved. The parity tests run single-threaded so they will still pass.

---

### F-3 · **High** · Data — `pd.NA` not caught by `_top_values` null check

**File:** `storm/profiler.py` — `_top_values()`

```python
is_na = val is None or (isinstance(val, float) and pd.isna(val))
```

`pd.NA` (the NA scalar used in nullable integer `Int64`, nullable string `StringDtype`, and boolean `BooleanDtype` columns) satisfies neither `val is None` nor `isinstance(val, float)`. So rows with `pd.NA` values get `str(pd.NA)` = `"<NA>"` as their label instead of `"(null)"`. The downstream UI will show `"<NA>"` as if it were a real data value.

This matters in practice because nullable dtypes are increasingly common — SQLAlchemy 2.x + pandas 2.x round-trip nullable integers as `Int64` by default.

**Fix:**

```python
def _is_null_like(val) -> bool:
    if val is None:
        return True
    try:
        return bool(pd.isna(val))
    except (TypeError, ValueError):
        return False

# In _top_values:
out.append(
    TopValue(
        value="(null)" if _is_null_like(val) else str(val),
        ...
    )
)
```

`pd.isna(pd.NA)` returns `True`; `pd.isna("hello")` returns `False` (no exception). The `try/except` covers unhashable or user-defined NA-like objects.

---

### F-4 · **High** · Performance — Pure-Python character iteration in `_classify_alphabet` and `_detect_casing`

**File:** `storm/profiler.py` — `_classify_alphabet()` and `_detect_casing()`

Both functions iterate through the `.astype(str)` representation of up to 200 rows, and inside each row they iterate character-by-character in pure Python:

```python
for v in non_null.astype(str):      # Python loop over rows
    for c in s:                      # Python loop over characters
        if c.isdigit(): ...
```

Bottleneck: pure Python at O(sample_size × avg_char_length) per column. On a 200-sample cap this is at most ~200×50 = 10k iterations, but `_profile_column` calls both functions for **every** string column. A table with 100 string columns runs 200k–400k Python-level character checks per scan pass.

Measure: `python -m scalene` on a wide-string scan will show both functions as hot in the Python-layer.

**Fix:** Replace with vectorized regex tests:

```python
# _classify_alphabet — vectorized replacement
def _classify_alphabet(series: pd.Series) -> str | None:
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    if len(non_null) == 0:
        return None
    if len(non_null) > _B2_ALPHABET_SAMPLE:
        non_null = non_null.iloc[:_B2_ALPHABET_SAMPLE]
    is_digits   = non_null.str.fullmatch(r"\d+")
    is_alpha    = non_null.str.fullmatch(r"[A-Za-z]+")
    is_alphanum = non_null.str.fullmatch(r"[A-Za-z0-9]+")
    n = len(non_null)
    threshold = 0.8
    if is_digits.mean() >= threshold:   return "digits"
    if is_alpha.mean() >= threshold:    return "alpha"
    if is_alphanum.mean() >= threshold: return "alphanum"
    return "mixed"
```

Similar approach applies to `_detect_casing` — use `series.str.isupper()`, `series.str.islower()`, `series.str.istitle()` which are vectorized.

---

### F-5 · **High** · Performance — `Faker()` re-instantiated per column in `_faker`

**File:** `generation/synthesize.py` — `_faker()`

When no `locale` and no `instance_default_locale` is set:

```python
faker_inst = Faker()
faker_inst.seed_instance(seed)
```

A fresh `Faker()` is constructed **every call to `_faker`**, i.e., once per faker-type column per table generation. `Faker()` is non-trivial to instantiate — it loads locale data and registers ~200 providers. For a table with 20 faker columns, that's 20 expensive instantiations.

Bottleneck: I/O-bound (file loading) + object construction cost. Measurable with `timeit.timeit(lambda: Faker(), number=100)` — typically 50–200ms per instance on cold import.

**Fix:** Cache the no-locale instance at module level and clone per-call (or use `seed_instance` to re-seed the shared instance):

```python
_FAKER_DEFAULT: Faker | None = None

def _get_default_faker(seed: int) -> Faker:
    global _FAKER_DEFAULT
    if _FAKER_DEFAULT is None:
        _FAKER_DEFAULT = Faker()
    inst = _FAKER_DEFAULT  # reuse; per-row seed_instance overrides the sequence anyway
    inst.seed_instance(seed)
    return inst
```

Since the per-row loop calls `faker_inst.seed_instance(row_seed)` on every iteration, the initial instance seed is overridden immediately and the shared instance is safe. Note: if F-2 (global `random`) is fixed with instance-local `rng`, the Faker instance sharing still works because `seed_instance` operates on Faker's own internal Generator, not on `random` module state.

---

### F-6 · **Medium** · Reliability — Silent exception swallow in `_compute_k_anonymity` groupby

**File:** `storm/profiler.py` — `_compute_k_anonymity()`

```python
try:
    sub = df.loc[:, list(combo)].dropna()
    k = int(sub.groupby(list(combo), dropna=False).size().min())
except Exception:
    continue
```

The bare `except Exception` was intended to handle unhashable column values (documented in comment). In practice it also silently swallows:
- `MemoryError` on a pathological combo with very many groups
- Future pandas API changes that raise unexpected exceptions
- Real bugs in the calling code (wrong column type passed to `_compute_k_anonymity`)

A `MemoryError` swallowed here means the k-anonymity result returns `None` for a column combination that could have been computed if it had been handled differently — the STORM scan completes successfully but with an incomplete risk picture.

**Fix:** Narrow to only the known-safe categories:

```python
except (TypeError, ValueError):
    # Unhashable column value (dict/list cell from JSON-typed source).
    # Log at debug level so a developer can trace the skipped combo.
    continue
```

Let `MemoryError` propagate — the outer `_run_storm_inner` try/except will catch it, log it, and surface a proper error.

---

### F-7 · **Medium** · Correctness — `_topo_sort` silently tolerates invalid `reference_table`

**File:** `generation/synthesize.py` — `_topo_sort()` and `generate_tables()`

The DFS has `if n not in deps: return` which silently skips any node not in the `generate_by_name` dict. This covers the case where `reference_table` points to a mask table (correct skip). But it also silently covers the case where `reference_table` is a typo or refers to a table name that doesn't exist at all — the reference column then gets silently deferred to `_reference` which calls `pools[ref_table]` and raises a `KeyError`.

The `KeyError` surfaces later as an uncontrolled exception mid-generation rather than a pre-flight validation error. The validator `_reference_graph_valid` is supposed to catch this, but it's called at `PipelineConfig` parse time; if `generate_tables` is called with an unvalidated dict (the docs say callers can do this), the defensive fallback is absent.

**Fix:** In `generate_tables`, before calling `_topo_sort`, validate that every `reference_table` either resolves to a generate table or a known mask table, and raise a descriptive error if not:

```python
for name, t in generate_by_name.items():
    for col in t["generate_columns"]:
        if col.get("type") == "reference":
            ref = col["reference_table"]
            if ref not in generate_by_name:
                raise ValueError(
                    f"Table {name!r} column {col['name']!r}: "
                    f"reference_table {ref!r} is not a generate table"
                )
```

---

### F-8 · **Medium** · Correctness — `_dominant_variant` regex list not deduped; all variants precompiled at load time

**File:** `storm/detectors.py` — `_SSN_VARIANTS`, `_US_PHONE_VARIANTS`, etc.

All variant lists store `(label, re.Pattern)` tuples where the label string and the compiled regex are separate objects. For `_SSN_VARIANTS`:

```python
_SSN_VARIANTS = [
    (r"\d{3}-\d{2}-\d{4}", re.compile(r"\d{3}-\d{2}-\d{4}")),
    (r"\d{9}",              re.compile(r"\d{9}")),
]
```

The label and the pattern source are **the same string**, so they're compiled twice — once as a string stored in the tuple, once as a `re.Pattern`. This is low-cost (module load only) but confusing and a future-bug risk if someone edits the label string without updating the compiled pattern.

**Fix:** Derive the `re.Pattern` from the label string at definition time:

```python
_SSN_VARIANTS = [
    (label := r"\d{3}-\d{2}-\d{4}", re.compile(label)),
    (label := r"\d{9}",              re.compile(label)),
]
```

Or better, define a `_variant(label)` helper:

```python
def _variant(label: str) -> tuple[str, re.Pattern[str]]:
    return label, re.compile(label)

_SSN_VARIANTS = [_variant(r"\d{3}-\d{2}-\d{4}"), _variant(r"\d{9}")]
```

This is a nit in terms of impact but a real maintenance hazard.

---

### F-9 · **Low** · Performance — `pd.to_datetime` called twice for date-shaped string columns

**File:** `storm/profiler.py` — `_profile_column()`

```python
coerced_sample = pd.to_datetime(non_null.head(sample_size), errors="coerce")
parse_rate = coerced_sample.notna().sum() / sample_size if sample_size else 0
if parse_rate >= 0.7:
    all_coerced = pd.to_datetime(non_null, errors="coerce")  # full series — second call
```

When `parse_rate >= 0.7`, the sample result is discarded and `pd.to_datetime` runs again over the full series. For a 100k-row date column with 200-row sample the overhead is ~500× the sample work.

Bottleneck: CPU-bound (dateutil parsing is expensive per-value). Measure with `python -m cProfile -s cumulative` on a scan with a large date column — `pd.to_datetime` will dominate.

**Fix:** Extend the sample to the full series in a single call and use the sample slice of the result for the `parse_rate` check:

```python
all_coerced = pd.to_datetime(non_null, errors="coerce")
parse_rate = all_coerced.head(sample_size).notna().sum() / sample_size if sample_size else 0
if parse_rate >= 0.7:
    valid = all_coerced.dropna()
    ...
```

This makes a single full-series call and uses the head slice for the threshold check — identical semantics, ~half the work.

---

### F-10 · **Low** · Reliability — `fields`/`total`/`pii_count` defined inside try, referenced outside

**File:** `storm/profiler.py` — `_run_storm_inner()`

```python
try:
    total = len(df)
    fields: list = []
    pii_count = 0
    ...
except Exception as exc:
    ...
    raise                          # always re-raises — safe today
if logger is not None:
    logger.info(f"... {pii_count} ...")  # NameError if except path ever returns
emit_step(..., rows_out=len(fields))      # same risk
```

Currently safe because the except block always re-raises. But if a future change adds a "continue on error" mode that swallows the exception, this becomes a `NameError` or references stale data.

**Fix:** Initialize `fields`, `total`, `pii_count` before the try block:

```python
total = len(df)
fields: list[FieldStats] = []
pii_count = 0
detector_hits: dict[str, int] = {}
try:
    for col in df.columns:
        ...
```

---

### F-11 · **Nit** · Design — `_hits_name_hint` vs `_hits_custom_name_hint` use different match strategies

**File:** `storm/detectors.py`

Built-in hints use `pat.fullmatch(target)` (anchored to whole string via the `_hint()` regex's `^...$` structure).  
Custom hints use `pat.search(col_name)` (unanchored substring search).

The difference is subtle: a built-in term `"ssn"` won't match column name `"session"` because neither the prefix `(.*[._-])?` nor the suffix `([._-].*)?` allows bare letters. A custom hint term `"ss"` WOULD match `"session"` via `search`. The asymmetry is undocumented and could surprise custom-detector authors.

Document the difference in `name_hint_extras()` docstring or unify on a single strategy.

---

## 3. Performance Notes

| Subsystem | Bottleneck Class | What to Measure |
|---|---|---|
| `run_storm` full-column scan | CPU (regex matching) | `scalene src/decoy_engine/storm/profiler.py` — look for `str.fullmatch` density in `_evaluate` |
| `_classify_alphabet` + `_detect_casing` | CPU (pure Python char loop) | `cProfile` on a 200-column all-string table; both functions will cluster near the top |
| `pd.to_datetime` on date columns | CPU (dateutil) | `%timeit pd.to_datetime(series_100k, errors='coerce')` — typically 1–5s per 100k rows |
| `Faker()` instantiation in `_faker` | I/O (provider load) | `timeit.timeit(lambda: Faker(), number=20)` — 0.5–2s total for 20 columns |
| k-anonymity `groupby` | CPU + memory | `%timeit df.groupby(['col1','col2']).size()` on the full (unsampled) DataFrame |

Algorithmic complexity of `_profile_column`: O(N × D) where N = rows, D = detectors (26 built-in). Each detector runs a vectorized `str.fullmatch` = O(N) per detector. For 100 columns × 26 detectors × 100k rows this is ~260M regex comparisons — acceptable with vectorized execution, but depends critically on F-1 being fixed so sampling actually fires.

---

## 4. Suggested Tests

### Determinism / reproducibility

```python
# Verify same seed → identical generate_tables output across calls.
def test_generate_tables_deterministic():
    config = {"global_settings": {"seed": 7}, "tables": [...]}
    out1 = generate_tables(config)
    out2 = generate_tables(config)
    for name in out1:
        assert out1[name].equals(out2[name])

# Verify concurrent generation jobs don't cross-contaminate RNG.
def test_generate_tables_concurrent_deterministic():
    import concurrent.futures
    config = {"global_settings": {"seed": 7}, "tables": [...]}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(generate_tables, config) for _ in range(4)]
        results = [f.result() for f in futures]
    ref = results[0]
    for res in results[1:]:
        for name in ref:
            assert ref[name].equals(res[name])
```

### STORM sampling contract

```python
def test_run_storm_respects_sample_row_cap():
    df = pd.DataFrame({"a": range(100_000)})
    profile = run_storm(df, "test", sample_strategy="random", sample_row_cap=1_000)
    assert profile.row_count == 1_000
```

### Nullable dtype null display

```python
def test_top_values_nullable_int_na_label():
    s = pd.array([1, 2, None, None, 3], dtype="Int64")
    series = pd.Series(s, name="col")
    tvs = _top_values(series, len(series))
    null_tv = next((t for t in tvs if t.count == 2), None)
    assert null_tv is not None
    assert null_tv.value == "(null)", f"Got {null_tv.value!r} for pd.NA"
```

### k-anonymity with unhashable column values

```python
def test_k_anonymity_skips_unhashable_column():
    # Dict-valued column from JSON source — groupby would raise TypeError.
    df = pd.DataFrame({
        "status": ["A", "B", "A", "B"],
        "meta":   [{"x": 1}, {"x": 2}, {"x": 1}, {"x": 2}],
    })
    fields = [
        FieldStats(name="status", inferred_type="string", distinct_count=2,
                   unique_rate=0.5, ...),
        FieldStats(name="meta", inferred_type="mixed", distinct_count=2,
                   unique_rate=0.5, ...),
    ]
    # Should not raise; should return a result (possibly None).
    k, groups = _compute_k_anonymity(df, fields)
    # Status column alone can't form a combo; meta is unhashable → skipped.
    # One candidate → k=None.
    assert k is None
```

### Detector edge cases

```python
# pd.NA in a string column should not crash run_all_detectors.
def test_detectors_nullable_string_column():
    s = pd.array(["john@example.com", None, "jane@example.com"], dtype="string")
    series = pd.Series(s, name="email")
    matches = run_all_detectors(series, "email")
    assert any(m.detector_id == "email" for m in matches)

# IBAN detector should not false-positive on a random uppercase + digit string.
def test_iban_no_false_positive_random_alphanumeric():
    s = pd.Series(["GB99INVALID123", "DE00BADINPUT456"], name="ref")
    match = detect_iban(s, "ref")
    assert match is None

# Compact ISO date validator should reject pure 8-digit IDs.
def test_iso_date_compact_rejects_phone_number():
    from decoy_engine.storm.detectors import _iso_date_valid
    assert not _iso_date_valid("99991399")   # month 13 invalid
    assert not _iso_date_valid("20260132")   # day 32 invalid
    assert _iso_date_valid("20260101")        # valid
    assert _iso_date_valid("2026-01-01")      # dashed always valid
```

### `_topo_sort` / reference validation

```python
def test_generate_tables_raises_on_unknown_reference_table():
    config = {
        "global_settings": {"seed": 1},
        "tables": [{
            "name": "child",
            "row_count": 10,
            "generate_columns": [{
                "name": "parent_id",
                "type": "reference",
                "reference_table": "nonexistent_parent",
                "reference_column": "id",
            }],
        }],
    }
    with pytest.raises(ValueError, match="reference_table"):
        generate_tables(config)
```

---

## 5. What's Good

- **k-anonymity implementation** is thoughtfully designed. The combo-sweep approach correctly avoids the HIPAA-trio-only trap; the deterministic sort (`-distinct_count, name`) guarantees identical output across Python versions.

- **ContextVar-backed `name_hint_extras`** correctly scopes per-scan extras to prevent concurrent scan cross-contamination — this is the right primitive and the implementation is clean.

- **Validator chain (Luhn / mod-97 / NPI CMS check digit / ICD-10 chapter range)** meaningfully reduces false-positive rates without adding a heavy dependency. The test comments include verified reference vectors.

- **HKDF canonical routing (Q11 fix)** is correctly done — `context._hkdf_sha256` now delegates to `determinism._hkdf.hkdf_sha256` which is a clean RFC 5869 implementation with a length ceiling and reference-vector tests. The old one-round shortcut is fully removed.

- **`_run_custom_detector` catches `re.error`** so a malformed admin-configured pattern doesn't kill the whole scan — good defensive boundary.

- **Parity-freeze comments in `synthesize.py`** are explicit and include V1 function/line references. A future developer removing V1 knows exactly which behaviors are frozen and why.

- **`_distribute_pattern`** correctly sizes the distribution to the non-null population (`n = len(non_null)`) rather than `total_rows`, avoiding a misleading "100% of rows match" display when there are many nulls.

---

## 6. Files NOT Reviewed (Previous Sessions)

The following were reviewed in earlier sessions today — do not re-examine without checking those reports first:

| File | Session |
|---|---|
| `execution/_transforms.py`, `config/_transforms.py` | `qa/s14-s17-review-2026-05-30` |
| `config/_sources.py`, `config/_targets.py` | `qa/s14-s17-review-2026-05-30` |
| `execution/_pandas_adapter.py` (FK resolver, Q7) | `qa/s14-s17-review-2026-05-30` |
| `generation/pool/_sampler.py` (Q6 fix) | `qa/s14-s17-review-2026-05-30` |
| `context._hkdf_sha256` (Q11 fix) | `qa/s14-s17-review-2026-05-30` |
