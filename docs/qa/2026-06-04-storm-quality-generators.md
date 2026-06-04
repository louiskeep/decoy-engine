# QA Review: storm/profiler · quality/ · generators/derivation

**Date:** 2026-06-04  
**Reviewer:** QA agent (claude-sonnet-4-6)  
**Scope:** `src/decoy_engine/storm/profiler.py`, `src/decoy_engine/quality/{snapshot,fidelity,policy}.py`, `src/decoy_engine/generators/derivation.py`  
**Branch avoidance:** checked against all 2026-06-04 engine QA branches (expressions/formula/textredact, hash/dateshift/categorical/sampler, pandas/fpe/compile, polars/relationships, synthesize/determinism/connectors) — no overlap.

---

## 1. Summary

The quality subsystem (`snapshot`, `fidelity`, `policy`) is well-designed and internally consistent — the snapshot's explicit determinism sort, the TVD-based comparators, and the per-strategy expectation tables are all sound. The main concerns are in `storm/profiler.py` (hardcoded sampling seed, vectorization gaps, a divergence from the snapshot module's determinism practice) and a semantic mismatch in `quality/policy.py` where violation `severity` fields are emitted but never consulted when computing the final verdict. `generators/derivation.py` has a 1-bit entropy leak in its unkeyed fallback.

**Most important issue:** `quality/policy.py::_verdict_for` ignores the `severity` field it documents — every violation in `fail` mode drives the verdict to `"fail"` even if all violations are tagged `severity="warn"`. The violation schema promises two severity levels; the verdict logic uses zero.

---

## 2. Findings

### F-1 · High · Correctness · `quality/policy.py`
**`_verdict_for` ignores violation severity — documented contract is broken.**

The module docstring states violations carry `"severity": "warn"|"fail"` and the verdict should reflect them. Every check emits `"severity": "fail"` today, but the verdict calculation never reads this field:

```python
def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "pass"
    if mode == "report":
        return "pass"
    if mode == "warn":
        return "warn"      # always "warn" regardless of severity
    return "fail"           # always "fail" regardless of severity
```

In `fail` mode, if a caller ever emits `severity="warn"` violations, the verdict will still be `"fail"`. The contract says `mode="fail"` + all-warn-severity should presumably yield `"warn"`, not `"fail"`. This is fine today because no check currently emits `severity="warn"`, but it is a latent bug waiting for the first caller that does.

**Fix:**
```python
def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "pass"
    if mode == "report":
        return "pass"
    has_fail = any(v.get("severity") == "fail" for v in violations)
    if mode == "warn":
        return "warn"
    # mode == "fail"
    return "fail" if has_fail else "warn"
```
Or, if the intent truly is that `mode` overrides severity, document that and delete the `severity` field from the schema to avoid confusion.

---

### F-2 · High · Correctness · `quality/policy.py`
**`_check_overall` / `_check_marginal` / `_check_pairwise` treat `actual is None` as a threshold violation.**

```python
def _check_overall(report, thresholds, violations):
    ...
    if actual is None or float(actual) < float(minimum):
        violations.append({"check": "overall", "severity": "fail",
                           "expected": float(minimum), "actual": actual,
                           "detail": f"overall_score {actual} below minimum {minimum}"})
```

When a report has no comparable columns (every column is empty or kind-mismatched), `overall_score` is `None`. The current code fires a `"fail"` violation with `"actual": None` and a message claiming `"None below minimum X"`. This is misleading — `None` means "not measurable", not "zero similarity". Operators who turn on `mode=fail` + `thresholds.overall.min` would see their jobs fail on empty tables with a confusing message.

**Fix:** treat `None` as a separate, lower-severity case:
```python
if actual is None:
    violations.append({"check": "overall", "severity": "warn",
                       "actual": None, "expected": float(minimum),
                       "detail": "overall_score is None (no comparable columns)"})
elif float(actual) < float(minimum):
    violations.append({"check": "overall", "severity": "fail",
                       "actual": float(actual), "expected": float(minimum),
                       "detail": f"overall_score {actual} below minimum {minimum}"})
```
Apply the same pattern to `_check_marginal` and `_check_pairwise`.

---

### F-3 · High · Determinism · `storm/profiler.py`
**`_top_values` tie-breaking is insertion-order-dependent; diverges from `snapshot._categorical_stats`.**

```python
# profiler.py
def _top_values(series, total_rows, n=5):
    vc = series.value_counts(dropna=False).head(n)
    for val, cnt in vc.items(): ...
```

`pandas.value_counts()` orders by count descending, but ties in count are broken by first-occurrence order (insertion order in the underlying dict), which depends on data traversal order and can vary across pandas versions. Compare with `snapshot._categorical_stats`, which explicitly re-sorts for determinism:

```python
# snapshot.py — the right approach
sorted_items = sorted(
    ((str(val), int(cnt)) for val, cnt in counts.items()),
    key=lambda kv: (-kv[1], kv[0]),
)
```

A StormProfile's `top_values` list can change order between runs on the same data if two values share a count and the DataFrame's internal encoding changes. The fix is to apply the same explicit sort in `_top_values`.

**Fix:**
```python
def _top_values(series: pd.Series, total_rows: int, n: int = 5) -> list[TopValue]:
    vc = series.value_counts(dropna=False)
    sorted_items = sorted(
        vc.items(),
        key=lambda kv: (-int(kv[1]), "" if _is_null_like(kv[0]) else str(kv[0])),
    )[:n]
    out = []
    for val, cnt in sorted_items:
        out.append(TopValue(
            value="(null)" if _is_null_like(val) else str(val),
            count=int(cnt),
            pct=round(cnt / total_rows * 100, 1) if total_rows > 0 else 0.0,
        ))
    return out
```

---

### F-4 · Medium · Determinism / Design · `storm/profiler.py`
**Sampling uses hardcoded `random_state=42`, not the engine's seed protocol.**

```python
# run_storm()
df = df.sample(n=sample_row_cap, random_state=42).reset_index(drop=True)
```

The seed value `42` is static — it's not derived from the job master key or any caller-supplied seed. Every STORM scan of the same `sample_row_cap` on the same data produces the same sample (good for profile reproducibility), but it's impossible to vary the sample by changing the pipeline seed. This undocumented coupling means operators cannot use seed-controlled sampling to get a different profile view, and CI runs that pass different seeds get the same STORM result.

For a pure-analysis tool this is defensible, but it should be explicit. The function signature should accept an optional `sample_seed: int = 42` parameter and document that it defaults to a fixed value rather than the pipeline seed.

**Recommended fix:**
```python
def run_storm(
    df: pd.DataFrame,
    source_label: str,
    *,
    sample_strategy: str = "full",
    sample_row_cap: int | None = None,
    sample_seed: int = 42,          # add this
    custom_detectors: list[CustomDetectorSpec] | None = None,
    extra_name_hints: dict[str, list[str]] | None = None,
    ctx: ExecutionContext | None = None,
) -> StormProfile:
    ...
    df = df.sample(n=sample_row_cap, random_state=sample_seed).reset_index(drop=True)
```
Caller (platform) can pass `sample_seed=derive_key("storm:sample")[:4]` if seed-tied sampling is wanted.

---

### F-5 · Medium · Performance · `storm/profiler.py`
**Money detection in `_classify_numeric_range` uses a Python-level loop; vectorizable.**

```python
sample = numeric.head(_B2_ALPHABET_SAMPLE)  # up to 200 values
two_dp_hits = 0
has_fractional = False
total = 0
for v in sample:               # Python loop over a pandas Series
    total += 1
    fv = float(v)
    if abs(fv - round(fv, 2)) < 1e-9:
        two_dp_hits += 1
    if abs(fv - round(fv)) >= 1e-9:
        has_fractional = True
```

200 iterations is cheap but the pattern is inconsistent with the rest of the profiler (which uses vectorized pandas ops throughout). On pathologically wide tables with many float columns this adds up.

**Fix (vectorized):**
```python
if inferred_type == "float":
    sample = numeric.head(_B2_ALPHABET_SAMPLE).to_numpy(dtype=float)
    rounded_2dp = np.round(sample, 2)
    two_dp_hits = int(np.sum(np.abs(sample - rounded_2dp) < 1e-9))
    has_fractional = bool(np.any(np.abs(sample - np.round(sample)) >= 1e-9))
    if len(sample) > 0 and has_fractional and two_dp_hits / len(sample) >= 0.7:
        return "decimal_money"
    return "decimal_other"
```

---

### F-6 · Medium · Correctness · `quality/snapshot.py`
**`_joint_snapshot` builds full crosstab matrix then iterates; memory blowup on high-cardinality pairs.**

```python
ct = pd.crosstab(a_vals, b_vals)
cells = []
for a_val in ct.index:
    for b_val in ct.columns:
        count = int(ct.at[a_val, b_val])
        if count == 0:
            continue
        cells.append({...})
```

`joint_columns` is caller-supplied and is not restricted to low-cardinality columns. If the caller passes two string columns with 1000 distinct values each, `pd.crosstab` allocates a 1000×1000 integer matrix (1M cells, ~8MB), and the double for-loop iterates all 1M entries to filter zeros. This is O(D_a × D_b) in both time and memory, where D_a and D_b are column cardinalities.

`pd.crosstab` returns a DataFrame; `stack(dropna=False)` or `reset_index` gives the non-zero cells directly. Alternatively, use `groupby` on the raw pairs:

**Fix:**
```python
pairs = pd.DataFrame({"a": a_vals.values, "b": b_vals.values})
counted = pairs.groupby(["a", "b"], sort=False).size().reset_index(name="count")
cells = [
    {"key": [str(row.a), str(row.b)], "count": int(row["count"])}
    for row in counted.itertuples()
]
# then sort + cap as before
```

This is O(N log N) instead of O(D_a × D_b) and never materializes the full sparse matrix.

---

### F-7 · Medium · Correctness · `storm/profiler.py`
**`_compute_mode` uses bare `except Exception` — swallows MemoryError.**

```python
try:
    vc = non_null.value_counts(dropna=True)
except Exception:
    return None, 0.0
```

`value_counts` on an object-typed column can only realistically raise `TypeError` (unhashable values). `MemoryError`, `KeyboardInterrupt`, and `SystemExit` should not be caught here.

**Fix:** `except (TypeError, ValueError):`

---

### F-8 · Medium · Determinism · `generators/derivation.py`
**Unkeyed fallback discards the high bit — 31 bits of entropy instead of 32.**

```python
return (int(fallback_seed) ^ name_int) & 0x7FFFFFFF
```

`0x7FFFFFFF` is `2**31 - 1`, which zeros the most-significant bit and limits the seed space to 2 billion values. `numpy.random.RandomState` and `numpy.random.default_rng` both accept seeds up to `2**32 - 1`; the mask unnecessarily halves the available seed space.

If the concern is avoiding `numpy` dtype overflow for signed-int callers, use `& 0xFFFFFFFF` (32 bits) or `% (2**31 - 1)` (keep fully positive without losing the high bit).

**Fix:** `return (int(fallback_seed) ^ name_int) & 0xFFFFFFFF`

---

### F-9 · Low · Design · `quality/snapshot.py`
**`_freetext_stats` uses plain `round()` for bin edges; inconsistent with `_round()` helper.**

```python
# _freetext_stats:
bin_edges = [round(float(e)) for e in edges]   # plain round

# _numeric_stats:
bin_edges = [_round(float(e)) for e in edges]  # uses _FLOAT_PRECISION
```

For freetext length edges (always integer-valued after `np.histogram` over integer lengths), this produces the same result as `int(e)`. The inconsistency is confusing to maintainers and will silently change behavior if edge types ever include fractional lengths. Use `_round` everywhere or cast explicitly to `int`.

**Fix:** `bin_edges = [int(round(float(e))) for e in edges]` (integer lengths, so int cast is correct here)

---

### F-10 · Low · Design · `storm/profiler.py`
**`_run_storm_inner` is a module-level private function with an unclean split.**

The split was done to avoid deep indentation under the `name_hint_extras()` context manager, as noted in its docstring. But this means `_run_storm_inner` is now an importable name despite being implementation-only. Its signature includes mutable `logger: Any` without type annotation. Consider either nesting it inside `run_storm` (valid since Python 3.x supports inner functions) or giving it a typed signature and adding it to `__all__` explicitly as excluded.

---

### F-11 · Low · Design · `generators/derivation.py`
**`synthetic_column_seed` 32-bit truncation (`_bytes_to_seed`) worth documenting for NumPy 2.x.**

NumPy 2.x's `np.random.default_rng()` accepts a `SeedSequence` with up to 128 bits of entropy. Truncating `derive_key()` output to 32 bits (`int.from_bytes(b[:4], "big")`) discards 28 bytes of the HKDF-derived key material. For today's use (seeding per-column Faker/random generators) this is more than sufficient, but if downstream callers ever pass the seed to `np.random.default_rng` and want full entropy, document that `_bytes_to_seed` is intentionally lossy and why.

---

## 3. Performance Notes

| Area | Bottleneck | Note |
|---|---|---|
| `storm/profiler.py` | CPU-bound per column | `_profile_column` is O(N) per column; the profiler is serial (columns scanned in a loop). For a 200-column, 1M-row table this is the dominant cost. No parallelism today. Consider `concurrent.futures.ThreadPoolExecutor` per column once GIL analysis confirms pandas releases it in the hot paths (generally yes for numeric ops). |
| `storm/profiler.py :: _classify_numeric_range` | Python loop (minor) | See F-5. Vectorize the money detection inner loop. |
| `quality/snapshot.py :: _joint_snapshot` | Memory + CPU | See F-6. Full crosstab is O(D_a × D_b); groupby approach is O(N log N). |
| `storm/profiler.py :: _compute_k_anonymity` | CPU | C(10,2)+C(10,3)=165 groupby calls on the candidate sub-DataFrame. Each is O(N log N). For a 1M-row table with 10 candidates this is ~165M comparisons. Profile with `cProfile` targeting `_compute_k_anonymity` for any table above 100k rows. Consider capping at `sample_row_cap` if one is already applied. |
| `quality/fidelity.py` | Pure Python dict iteration | All comparators iterate over small dicts (top-K values, quantile grids). Negligible cost. |
| `quality/policy.py` | Pure dict traversal | No loops of concern. |

**Profiling command for the STORM hot path:**
```bash
python -m cProfile -s cumulative -m pytest tests/unit/test_storm_profiler.py -k large_table 2>&1 | head -40
```
Or with `scalene` for wall + memory in one pass:
```bash
scalene --cpu --memory -- pytest tests/unit/test_storm_profiler.py -k large_table
```

---

## 4. Suggested Tests

### Determinism regressions
```python
# Verifies _top_values is stable across shuffles of equal-count values
def test_top_values_deterministic_on_tie():
    import pandas as pd, random
    data = ["a"] * 10 + ["b"] * 10 + ["c"] * 5
    s = pd.Series(data)
    r1 = [tv.value for tv in _top_values(s, len(s))]
    random.shuffle(data)
    r2 = [tv.value for tv in _top_values(pd.Series(data), len(data))]
    assert r1 == r2, "_top_values must be stable under reordering of equal-count values"

# Two-run determinism check for run_storm with explicit sample_seed
def test_run_storm_same_seed_same_profile(sample_df):
    p1 = run_storm(sample_df, "test", sample_strategy="head", sample_row_cap=100, sample_seed=7)
    p2 = run_storm(sample_df, "test", sample_strategy="head", sample_row_cap=100, sample_seed=7)
    import dataclasses, json
    assert json.dumps(dataclasses.asdict(p1), sort_keys=True) == \
           json.dumps(dataclasses.asdict(p2), sort_keys=True)
```

### Policy verdict correctness
```python
# Ensures that a future severity="warn" violation in fail mode yields "warn", not "fail"
def test_policy_warn_severity_does_not_fail_in_fail_mode():
    violations = [{"check": "dummy", "severity": "warn", "expected": 0.9, "actual": 0.8, "detail": "test"}]
    verdict = _verdict_for("fail", violations)
    assert verdict == "warn"  # currently fails — this test documents the desired behavior

# None actual_score should not fire as a hard fail
def test_check_overall_none_score_is_not_hard_fail():
    empty_report = {"overall_score": None, "marginal": {"score": None}, "pairwise": {"score": None}}
    violations = []
    _check_overall(empty_report, {"overall": {"min": 0.8}}, violations)
    # Should produce a warn, not a fail
    assert not violations or violations[0]["severity"] == "warn"
```

### Snapshot determinism (cross-platform)
```python
# Use Hypothesis to stress the categorical sort stability
from hypothesis import given, settings
from hypothesis import strategies as st

@given(st.lists(st.integers(min_value=0, max_value=9), min_size=10))
def test_categorical_stats_deterministic(values):
    import pandas as pd, json
    s = pd.Series(values).astype(str)
    s1 = _categorical_stats(s, top_k=5)
    s2 = _categorical_stats(pd.Series(values[::-1]).astype(str), top_k=5)
    # Same multiset of values -> same top_values order
    assert [v["value"] for v in s1["top_values"]] == [v["value"] for v in s2["top_values"]]
```

### k-anonymity edge cases
```python
# Single-row table: k_anonymity should be None, not 1
def test_k_anonymity_single_row_is_none():
    df = pd.DataFrame({"age": [25], "zip": ["10001"]})
    fields = [_profile_column(df[c], 1) for c in df.columns]
    k, groups = _compute_k_anonymity(df, fields)
    assert k is None

# All-unique column excluded from QI candidates
def test_k_anonymity_excludes_pk_columns():
    df = pd.DataFrame({"id": range(100), "gender": ["M", "F"] * 50})
    fields = [_profile_column(df[c], 100) for c in df.columns]
    k, groups = _compute_k_anonymity(df, fields)
    for group in groups:
        assert "id" not in group, "unique-rate=1.0 column must not appear in QI groups"
```

### Joint snapshot performance / correctness
```python
# Large-cardinality pair should not OOM or time out
import time
def test_joint_snapshot_large_cardinality():
    import pandas as pd, numpy as np
    np.random.seed(0)
    df = pd.DataFrame({"a": np.random.randint(0, 500, 10_000).astype(str),
                       "b": np.random.randint(0, 500, 10_000).astype(str)})
    t = time.perf_counter()
    snap = compute_distribution_snapshot(df, joint_columns=[("a", "b")])
    assert time.perf_counter() - t < 2.0, "joint snapshot on 500x500 pairs should complete < 2s"
    assert snap["joints"][0]["cell_count"] > 0
```

---

## 5. What's Good

- **`quality/snapshot.py` determinism discipline is excellent.** The explicit `sorted(..., key=lambda kv: (-kv[1], kv[0]))` in `_categorical_stats`, the `_round()` helper with a documented `_FLOAT_PRECISION` pin, and the timezone-strip before `isoformat()` in `_datetime_stats` are exactly what a data-processing engine needs for byte-stable JSON across machines. The k-anonymity constant-range fallback for zero-width histograms is also well-handled.

- **`generators/derivation.py` seed isolation is solid.** `_EXCLUDED_FROM_FINGERPRINT` correctly severs display-name coupling. The `"fresh"` determinism escape hatch (admin-gated, documented) and the explicit `raise RuntimeError` on `derive_key` failure (instead of silent fallback) both reflect the right crypto-degradation posture.

- **`storm/profiler.py` F-3, F-6, F-7, F-9, F-10 prior fixes are well-commented.** The inlined `# F-N fix:` comments explain non-obvious decisions (double coerce elimination in F-9, vectorized casing in F-4, `pd.NA` handling in F-3). These make regression prevention tractable.

- **`quality/policy.py` extensibility model is clean.** The `strategy_expectations` override dict, the `column_overrides` resolution priority (explicit > strategy > skip), and the parallel D5b shape-fidelity checks are all additive without forking the logic. The D5a re-calibration comments are honest about why defaults dropped.
