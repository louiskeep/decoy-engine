# QA Review — Storm Post-Mask Module + Hardening Batch A/B

**Date:** 2026-06-01  
**Reviewer:** QA/Performance agent  
**Branch reviewed:** `post-reframe-d-batch-b-2026-06-01` (engine tip: `3051550d`)  
**Platform branch reviewed:** `post-reframe-d-batch-b-2026-06-01` (tip: `1b0c90a3`)  
**Prior QA avoided:** `qa/review-2026-06-01-engine.md` (MG-1 through MG-6; plan/execution/providers_v2)  
**Scope:** `storm/postmask/` package (Reframe-A), hardening Batch A (`H2`, `H3`) + Batch B (`H4`, `M11`, `M12`, `M13`, `M23`), platform-side security hardening (`B1`, `H5`, `M10`, `H1`, `M22`, `M24`)

---

## 1. Summary

The `storm/postmask` package is structurally sound: best-effort orchestration, typed finding dataclasses, and correct severity semantics. The Batch A/B fixes address real bugs and are correctly implemented in the production code. **The single most important issue** is that `text_redact` — a MG-2 masking strategy whose explicit purpose is to destroy PII patterns in free-text fields — is absent from `_DESTROYS_PATTERN` in `residual_pii.py`. A text column masked with `text_redact` that still contains a detector hit after masking will be classified `warning` (ambiguous) instead of `fail` (mask didn't work), silently misreporting a PHI leakage event. A secondary issue is that the M11 regression test (`test_comparison_failure_emits_error_severity`) never actually exercises the exception path it names — the fix is real but untested.

---

## 2. Findings

### F1 — HIGH | Correctness | `residual_pii.py`
**`text_redact` absent from `_DESTROYS_PATTERN` — PHI leak misclassified as warning**

`_DESTROYS_PATTERN` contains `{"hash", "redact", "bucketize"}`. `text_redact` (MG-2, `storm/detectors`-backed span-scrubber for clinical notes) destroys PII patterns by construction — any surviving detector hit means the mask missed a span.

```python
# residual_pii.py lines ~50-57
_DESTROYS_PATTERN: frozenset[str] = frozenset({
    "hash",
    "redact",
    "bucketize",
    # text_redact is MISSING here
})
```

Because `text_redact` falls into neither `_DESTROYS_PATTERN` nor `_PRODUCES_PII_LIKE_VALUES` nor `_NO_OP_BY_DESIGN`, it hits the `else` branch and classifies surviving hits as `warning` ("does not have a documented residual-PII expectation"). For a HIPAA use-case clinical-note column the intended outcome is `fail` — the whole point of the strategy is to scrub those patterns.

**Impact:** A failing `text_redact` mask (e.g. a custom detector that misses a phone number format) surfaces in the Storm tab as a yellow warning rather than a red fail. Operators tuned to triage only fail-severity findings will miss it.

**Fix:**
```python
_DESTROYS_PATTERN: frozenset[str] = frozenset({
    "hash",
    "redact",
    "bucketize",
    "text_redact",  # MG-2: span-scrubber; surviving hits = mask failure
})
```

Also add `text_redact` to the test matrix in `test_postmask_batch_b.py` — the passthrough test shows the pattern.

---

### F2 — HIGH | Reliability | `post_mask_hook.py`
**`generated_at` missing from the `TypeError` fallback report dict**

When `run_storm_post_mask` raises `TypeError` (wrong argument shape), the hook catches it and constructs a fallback report dict manually:

```python
# post_mask_hook.py lines ~130-142
report = {
    "schema_version": SCHEMA_VERSION,
    "residual_pii": [],
    "fk_preservation": [],
    "policy_validation": [],
    "pass_count": 0,
    "warning_count": 0,
    "fail_count": 0,
    "error_count": 1,
    "pass_failed_with": type(exc).__name__,
    # generated_at is ABSENT
}
```

The M23 fix (Batch B, commit `3051550d`) added `generated_at` to the normal report path specifically because "the FE + JobStormReport row agree on the field's presence". The fallback dict is never updated. The `JobStormReport.generated_at` DB column will be `NULL` for jobs that hit the TypeError path, while the FE TypeScript union (`JobStormReportReason`) expects the field.

**Fix:** Add `generated_at` to the fallback dict:
```python
from datetime import datetime, timezone
report = {
    "schema_version": SCHEMA_VERSION,
    ...,
    "pass_failed_with": type(exc).__name__,
    "generated_at": datetime.now(timezone.utc).isoformat(),
}
```

---

### F3 — MEDIUM | Performance | `fk_preservation.py:_check_composite_fk`
**Pure-Python orphan-count loop for composite FKs — O(n) per-row dispatch**

The single-column path (`_check_one_fk`) uses vectorized pandas:
```python
orphan_mask = ~child_fks.isin(parent_set)
orphan_count = int(orphan_mask.sum())
```

The composite path (`_check_composite_fk`) uses a Python generator loop:
```python
parent_tuples = set(map(tuple, parent_tuples_df.itertuples(index=False, name=None)))
child_tuple_iter = child_tuples_df.itertuples(index=False, name=None)
orphan_count = sum(1 for t in child_tuple_iter if tuple(t) not in parent_tuples)
```

`itertuples` is the fastest row-iteration method in pandas, but it is still pure Python. At 1 M child rows × 2 columns, this is ~0.5–2 s on a warm interpreter vs. ~10–50 ms for a vectorized alternative.

**Bottleneck:** CPU-bound; Python per-row dispatch overhead.

**Recommended fix** (MultiIndex containment check, C-level):
```python
child_mi = pd.MultiIndex.from_frame(child_tuples_df)
parent_mi = pd.MultiIndex.from_frame(parent_tuples_df.drop_duplicates())
orphan_count = int((~child_mi.isin(parent_mi)).sum())
```
`MultiIndex.isin` is implemented in Cython and runs 10–30× faster than the iterator loop for tables over 100K rows. `pd.MultiIndex.from_frame` is available from pandas 0.24.

**To profile:** `python -m timeit -s 'import pandas as pd; N=1_000_000; df=pd.DataFrame({"a":range(N),"b":range(N)})'` against both implementations.

---

### F4 — MEDIUM | Reliability | `test_postmask_batch_b.py`
**M11 regression test does not exercise the exception path it names**

`test_comparison_failure_emits_error_severity` is the designated regression cell for M11 ("comparison failure surfaces as error not info"). The test defines two helper classes (`_ExplodingSeries`, `_NotComparable`) that are never instantiated, then pivots to testing the *success* case (different outputs → info severity):

```python
# test_postmask_batch_b.py lines ~95-118
class _ExplodingSeries:       # defined but never used
    ...
class _NotComparable:         # defined but never used
    ...
# The actual assertion:
out_diff = pd.DataFrame({"col_a": [10, 20, 30]})
findings = check_policy_validation({"t": src}, {"t": out_diff}, cfg)
assert findings[0].severity == "info"   # success path, not exception path
```

The M11 fix (the `except Exception` block in `policy_validation.py`) is correct code but has zero test coverage. The fix is reachable in production when ArrowDtype-backed columns (e.g. from a pyarrow-backed pandas DataFrame) fail `.astype(object)` with a `TypeError`.

**Impact:** A regression in the exception handler (e.g. re-introducing the silent info fallback) will not be caught by CI.

**Recommended test addition:**
```python
def test_comparison_failure_surfaces_as_error(monkeypatch):
    import decoy_engine.storm.postmask.policy_validation as pv
    src = pd.DataFrame({"col": [1, 2, 3]})
    out = pd.DataFrame({"col": [4, 5, 6]})
    original_check = pv._check_one_column
    def _force_raise(*, table, column, strategy, src_df, out_df):
        # Bypass the normal path and force the astype() exception.
        raise TypeError("simulated ArrowDtype mismatch")
    # Monkeypatch the inner helper's astype call instead:
    # easier via a real ArrowDtype column.
    src2 = pd.DataFrame({"col": pd.array([1, 2, 3], dtype="int64[pyarrow]")})
    out2 = pd.DataFrame({"col": [4, 5, 6]})  # different dtype
    cfg = {"tables": [{"name": "t", "columns": [{"name": "col", "strategy": "hash"}]}]}
    findings = pv.check_policy_validation({"t": src2}, {"t": out2}, cfg)
    # If pyarrow isn't installed, mark the test xfail. Otherwise assert
    # either info (success) or error (exception path).
    assert any(f.severity in ("info", "error") for f in findings)
    # Stronger: force the exception with a mock.
    import unittest.mock as mock
    with mock.patch.object(pd.Series, "astype", side_effect=TypeError("boom")):
        findings = pv.check_policy_validation({"t": src}, {"t": out}, cfg)
    assert findings[0].severity == "error"
    assert "policy validation did not conclude" in findings[0].message
```

---

### F5 — MEDIUM | Performance | `worker.py:_count_rows`
**New SQLAlchemy engine created per poll invocation — connection churn**

```python
def _count_rows(connector_id, schema, table, db) -> int | None:
    ...
    engine = create_engine(decrypt(conn_rec.encrypted_dsn), pool_size=1, max_overflow=0)
    try:
        with engine.connect() as conn:
            ...
    finally:
        engine.dispose()   # pool torn down every call
```

For a db_change trigger with `poll_minutes=1` on 20 schedules, this is 20 engine creations per minute, each of which decrypts the DSN, calls the DB driver's connect() (TCP handshake + SSL + auth), runs `SELECT COUNT(*)`, then disposes the engine. On Postgres or Snowflake, the connect sequence alone is 50–500 ms.

**Bottleneck:** Network I/O; the `finally: engine.dispose()` prevents connection pool reuse across polls.

**Fix:** Cache lightweight connection pools keyed by `connector_id`, e.g.:
```python
_ENGINE_CACHE: dict[int, Engine] = {}

def _get_or_create_engine(connector_id: int, encrypted_dsn: str) -> Engine:
    if connector_id not in _ENGINE_CACHE:
        _ENGINE_CACHE[connector_id] = create_engine(
            decrypt(encrypted_dsn),
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
        )
    return _ENGINE_CACHE[connector_id]
```
The `pool_pre_ping=True` already in the current code handles stale connections; removing `engine.dispose()` and reusing the pool would cut per-poll latency by 50–500 ms. Add a cache eviction on `remove_schedule`.

---

### F6 — MEDIUM | Correctness | `policy_validation.py`
**Index alignment is assumed but never validated; fragile for non-CSV DataFrames**

The code comment at the byte-comparison says: *"Frame index is assumed aligned."*

```python
bytes_identical = bool(
    len(src_col) == len(out_col)
    and src_col.astype(object).equals(out_col.astype(object))
)
```

`Series.equals()` compares element-by-element by **position**, not by index label. For the current hook (CSV source + CSV output via `pd.read_csv`), both series will have `RangeIndex(0, n)` and positional alignment is correct. However, if future callers pass DB-sourced DataFrames (e.g., from a `pd.read_sql` with a primary-key index), source row 5 and output row 5 might have the same index label but represent different records, or source and output might have different indexes entirely after a sort.

**Risk:** No immediate production impact (hook is CSV-only today), but a dangerous silent assumption as the hook is extended to cover DB/cloud sources in V2.

**Recommended guard:**
```python
# Reset index before comparison to ensure positional alignment
src_col = src_df[column].reset_index(drop=True)
out_col = out_df[column].reset_index(drop=True)
```
Or, document explicitly that callers must pass DataFrames with a reset integer index.

---

### F7 — LOW | Reliability | `post_mask_hook.py`
**Single-table assumption silently disables FK check for multi-table pipelines**

```python
# post_mask_hook.py lines ~112-119
table_name = _first_source_table_name(cfg) or "default"
source_frames = {table_name: src_df}
output_frames = {table_name: out_df}
```

For a two-table pipeline where `relationships` declares `parent_table: orders` and `child_table: order_items`, the FK check in `check_fk_preservation` walks the config relationships but skips both edges because neither table is in the one-entry `output_frames` dict (the guard `if child_table not in output_frames: continue` fires). The FK check returns `[]` with no findings and no log line explaining why it ran zero checks.

The V2.0 limitation is acknowledged in the code comment and the spec. The issue is the **silent skip** — the Storm tab will show zero FK findings, which an operator may mistake for "all FK relationships verified" rather than "FK check did not run."

**Fix:** Emit a `[storm.fk_skip]` log line when the relationship graph has non-empty entries but zero findings were produced:
```python
if config.get("relationships") and not report.get("fk_preservation"):
    _safe_log(job, db, "[storm.fk_skip] FK check: multi-table pipelines not yet supported in V2.0")
```

---

### F8 — LOW | Correctness | `fk_preservation.py:_check_one_fk`
**No cap on parent set size — unbounded memory for large reference tables**

```python
parent_set = set(parent_pks.tolist())
```

For a reference table with 10M UUID primary keys (~90 bytes per UUID as Python object), this set is ~900 MB of heap. The storm runner is best-effort and will catch the `MemoryError` and record it as an error-severity finding, but the OOM will have already fired and may affect other workers in the same process.

**Recommended:** Cap the parent set with a warning finding:
```python
_PARENT_SET_CAP = 5_000_000  # ~450 MB for UUID-sized keys
if len(parent_pks) > _PARENT_SET_CAP:
    return FKPreservationFinding(
        ...,
        severity="warning",
        message=f"parent table {parent_table!r} has {len(parent_pks)} rows; "
                f"FK check skipped (exceeds {_PARENT_SET_CAP:,} row cap).",
    )
```

---

### F9 — NIT | Reliability | `date_shift.py`
**`_column_key` docstring claims "same as HashStrategy._column_key" — not obviously true**

The comment says *"Same as HashStrategy._column_key — instance-master-only, no per-column tagging. `column_name` is kept for log context only."* The docstring is technically accurate but misleading: it implies these two strategies share identical key-derivation logic, when in fact the important invariant is that both pass `"mask"` as a fixed tag to `derive_key` (no per-column specialisation). If a future engineer changes HashStrategy to pass `column_name` as the tag, date_shift would silently diverge (producing different keys for the same master key + column). Consider an assertion or a shared `_derive_mask_key` helper to couple the behaviour explicitly.

---

## 3. Performance Notes

| Subsystem | Bottleneck | Complexity | What to measure |
|---|---|---|---|
| `_check_composite_fk` | CPU (Python loop) | O(n_child) per-row dispatch | `timeit` the iterator loop vs. `MultiIndex.isin` at 100K/1M/10M child rows |
| `_check_one_fk` (parent_set) | Memory | O(n_parent) heap | `memory_profiler` on a 10M-PK parent table |
| `_count_rows` scheduler poll | Network I/O | O(1) but with per-call TCP handshake | `cProfile` `_count_rows` under a 1-min poll for 20 schedules; measure `create_engine` + `dispose` overhead |
| `run_all_detectors` in residual_pii | CPU (regex) | O(rows × detectors) | Already capped by `_CSV_ROW_CAP=100_000`; acceptably bounded |
| `_detect_format` (date_shift) | CPU (strptime) | O(min(200, n) × formats) | Acceptable; sample is bounded to 200 rows |

The `_check_composite_fk` loop is the most actionable bottleneck. Switching to `MultiIndex.isin` is a one-line change with no semantic risk.

---

## 4. Suggested Tests

| Priority | Module | Test case |
|---|---|---|
| Must-have | `residual_pii.py` | `text_redact` column with surviving email detector → severity `fail` |
| Must-have | `policy_validation.py` | ArrowDtype mismatch between source and output forces the `except Exception` path → severity `error`, message contains "did not conclude" |
| Must-have | `post_mask_hook.py` | TypeError fallback report dict includes `generated_at` field |
| Should-have | `fk_preservation.py` | `_check_composite_fk` with 500K child rows + 100K parent rows; assert orphan count is correct + runtime < 1 s |
| Should-have | `fk_preservation.py` | Parent table with 0 non-null PKs → empty finding (not crash) |
| Should-have | `worker.py` | `_count_rows` called twice for same connector_id; assert `create_engine` called once (after engine caching fix) |
| Nice-to-have | `policy_validation.py` | Source and output DataFrames with non-default integer index (e.g. start=10) → bytes_identical check still correct |
| Nice-to-have | `post_mask_hook.py` | Pipeline with two tables + a relationship; assert `[storm.fk_skip]` appears in job.log (after F7 fix) |

---

## 5. What's Good

- **H3 (exception text leak):** The three catch sites in `runner.py` correctly emit `{type(exc).__name__}` only, with a "see job log" pointer. The full exception lands in the worker log via the catch-site `logger`. Clean separation of FE-visible and operator-log-visible error text.

- **H4 (composite FK tuple-wise check):** The root cause (per-column zip walk passing cross-product orphans) was correctly identified and the fix (set-of-tuples containment) is semantically correct. The regression test (`test_composite_orphan_tuple_flagged_as_fail`) pins the canonical (1,99) orphan case precisely.

- **B1 (path traversal jail):** Both `resolve_v2_source_paths` (V2 config choke-point) and `preflight._check_source_file` (V1 graph path) were updated together. The `.resolve().relative_to()` pattern is the correct OS-level jail implementation and is consistent with the existing target-side jail in `resolve_v2_target_paths`.

- **H5 (SQL identifier validation):** `_validate_sql_identifier` uses a conservative `^[A-Za-z_][A-Za-z0-9_]*$` regex before interpolation. The double-quoting of the validated identifier is conservative (identifiers that pass the regex don't need quoting, but quoting doesn't hurt). The null-short-circuit design (return `None` → skip poll silently with a log line) is the right fail-safe posture for a scheduler.

- **M10 (preflight deep-copy):** `copy.deepcopy(cfg)` before `resolve_in_config` is the correct fix. The comment documents why (resolver mutates in place; idempotent today but fragile under future resolver changes).

- **M23 (generated_at field):** The engine emits a timezone-aware UTC ISO-8601 string at report-construction time. The `Optional[datetime]` type on `JobStormReportOut.generated_at` is correct — FastAPI/Pydantic v2 coerces the ISO string to `datetime` on the platform side and serializes it back to ISO-8601 in the JSON response.

- **`types.py`:** All finding dataclasses are `frozen=True`. This prevents accidental mutation of findings by the caller and makes the dataclass hashable. Good practice for an immutable report payload.

- **Best-effort orchestration:** The `run_storm_post_mask` try/except pattern ensures one broken detector subsystem doesn't kill the whole report. Error findings carry the exception type name (not the full message), which is the right information density for an FE-rendered report.
