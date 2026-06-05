# QA Review: composite generators, pool subsystem, postmask checks, expressions, pipeline entry

**Date:** 2026-06-05
**Branch:** `qa/review-2026-06-05-composite-pool-postmask`
**Reviewer:** QA/Performance agent
**Scope:** First review of the following modules (not touched by any prior QA branch):

- `src/decoy_engine/generation/composite/_person.py`, `_address.py`, `_generator.py`
- `src/decoy_engine/generation/pool/_value_pool.py`, `_builder.py`, `_cache.py`, `_pool_adapter.py`, `_canonicalize.py`
- `src/decoy_engine/storm/postmask/fk_preservation.py`, `residual_pii.py`, `policy_validation.py`
- `src/decoy_engine/expressions.py`
- `src/decoy_engine/execution/_pipeline.py`, `_guards.py`
- `src/decoy_engine/profile/_hash.py`, `_pii.py`, `_serialize.py`
- `src/decoy_engine/providers_v2/mimesis/_adapter.py`
- `src/decoy_engine/connectors/gcs.py`
- `src/decoy_engine/context.py`

---

## 1. Summary

The pool and determinism infrastructure is well-designed: HKDF-derived pool seeds, frozen numpy arrays, cache-before-build identity pattern, and `_canonicalize_source` with per-dtype hard errors are all sound. The critical gap is that `_canonicalize_source` misses `numpy.float32` scalars (which do NOT inherit from Python `float`), silently encoding them as strings instead of hard-erroring -- a determinism contract violation for float32 Arrow columns. The second most important finding is that `_generate_deterministic` in both composite generators loops over rows with `.iloc[i]`, the same O(n) Python-loop bottleneck flagged for four other strategies in the 06-04 reviews, and neither output series carries the source index, which causes NaN-fill misalignment for any non-default-indexed frame. The postmask checks (`fk_preservation`, `policy_validation`) have a documented size-cap asymmetry and a docstring/code disagreement on FPE self-mapping severity.

---

## 2. Findings

### F1 -- HIGH | Correctness / Determinism

**File:** `src/decoy_engine/generation/pool/_canonicalize.py:87-96`

**Issue:** `isinstance(value, float)` does not catch `numpy.float32` scalars. `numpy.float64` inherits from Python `float`; `numpy.float32` does not -- it only registers with `numbers.Real`. A PyArrow column of type `float32` loaded via `to_pandas()` produces `numpy.float32` scalars from `.iloc[i]`. These fall through to the string fallback (`str(value).encode("utf-8")`), silently canonicalizing as `b"1.0"` instead of hard-erroring. Two consequences: (a) a float32 source column bypasses the PO-locked hard-error policy; (b) a float32 value of 1.0 and a string "1.0" canonicalize identically, breaking injectivity and creating cross-type collision in the deterministic index.

**Why it matters:** The determinism contract states float sources are always rejected at canonicalization. Under PyArrow float32 -> pandas, that rejection silently does not fire.

**Verify:** `python -c "import numpy as np; print(isinstance(np.float32(1.0), float))"` returns `False`. Check with `isinstance(np.float32(1.0), numbers.Real)` -> `True`.

**Fix:** Add a `numbers.Real` check after `numbers.Integral` and before the string fallback:

```python
# After the Integral check, before the float hard-error:
if isinstance(value, numbers.Real):  # catches np.float32, np.float16, etc.
    raise GenerationError(
        code="float_canonicalization_unsupported",
        message=(
            f"Float source value {value!r} (type {type(value).__name__}) cannot "
            "be canonicalized. IEEE-754 representation drifts across platforms; "
            "route through a stringified or integer column upstream."
        ),
    )
```

This replaces the current `isinstance(value, float)` check. `numbers.Integral` is already a broader check (catches numpy int types); the same pattern is needed for reals. Add a test: `_canonicalize_source(np.float32(1.0))` must raise `GenerationError`.

---

### F2 -- HIGH | Performance

**Files:** `src/decoy_engine/generation/composite/_person.py:186-212`, `src/decoy_engine/generation/composite/_address.py:133-154`

**Issue:** `_generate_deterministic` in both `CompositePerson` and `CompositeAddress` iterates all rows in Python:

```python
is_null = source.isna()
for i in range(len(source)):
    if is_null.iloc[i]:          # pandas scalar access per row
        ...
    canonical = _canonicalize_source(source.iloc[i])  # pandas scalar access per row
```

The `is_null.iloc[i]` and `source.iloc[i]` pattern is the same O(n) Python-loop hotspot identified in 06-04-hash-dateshift F1/F2 (`_hash.py`, `_date_shift.py`). Each `.iloc[i]` call has significant per-call overhead vs. vectorized access. For a 1M-row masking job on a person composite, this loop runs 4 per-row Python function calls + 4 list appends per row = 8M Python operations before any output is assembled.

**Why it matters:** This is the hot path for every composite deterministic masking job. At 100K rows it is the wall-clock bottleneck by a wide margin.

**Bottleneck type:** CPU-bound Python loop. Profile with `py-spy top --pid <pid>` or `python -m cProfile` to confirm.

**Fix (pattern from FPE strategy):**

```python
def _generate_deterministic(self, spec: ProviderSpec, source: pd.Series) -> dict[str, pd.Series]:
    assert spec.seed is not None  # noqa: S101
    first_arr, last_arr, dob_arr = self._pools(spec)
    domains = self._resolve_domains(spec)
    fmt = str(spec.extra.get("email_format", _DEFAULT_EMAIL_FORMAT))
    n = len(source)
    first_out = np.empty(n, dtype=object)
    last_out  = np.empty(n, dtype=object)
    email_out = np.empty(n, dtype=object)
    dob_out   = np.empty(n, dtype=object)
    null_mask = source.isna().to_numpy()
    non_null_idx = np.where(~null_mask)[0]
    source_values = source.to_numpy()           # one bulk copy, no per-row overhead
    for i in non_null_idx:
        canonical = _canonicalize_source(source_values[i])
        latent = derive(spec.seed, self.coherent_namespace, canonical)
        first = first_arr[int.from_bytes(latent[0:8], "big") % len(first_arr)]
        last  = last_arr[int.from_bytes(latent[8:16], "big") % len(last_arr)]
        domain = domains[int.from_bytes(latent[16:24], "big") % len(domains)]
        dob    = dob_arr[int.from_bytes(latent[24:32], "big") % len(dob_arr)]
        first_out[i] = first
        last_out[i]  = last
        email_out[i] = self._email(first, last, domain, fmt)
        dob_out[i]   = dob
    first_out[null_mask] = None
    last_out[null_mask]  = None
    email_out[null_mask] = None
    dob_out[null_mask]   = None
    idx = source.index
    return {
        "first_name": pd.Series(first_out, index=idx),
        "last_name":  pd.Series(last_out,  index=idx),
        "email":      pd.Series(email_out, index=idx),
        "dob":        pd.Series(dob_out,   index=idx),
    }
```

Apply the same pattern to `CompositeAddress._generate_deterministic`. The `_canonicalize_source` loop cannot be vectorized (it dispatches per dtype) but replacing `source.iloc[i]` with `source_values[i]` avoids the per-call pandas overhead. The `to_numpy()` call replaces two O(n) `.iloc` accesses per row with one bulk copy.

---

### F3 -- HIGH | Data

**File:** `src/decoy_engine/storm/postmask/fk_preservation.py:163`

**Issue:** `_check_one_fk` builds a Python set from the full parent PK series with no size cap:

```python
parent_set = set(parent_pks.tolist())
```

For a 10M-row UUID-keyed parent table, this materializes ~900MB in Python set memory. The composite FK path in `_check_composite_fk` already has a `_PARENT_TUPLE_CAP = 10_000_000` guard with a warning-and-skip (added per QA-4 F8). The single-column path never received the same fix. Production tables (audit logs, event streams) commonly exceed 10M rows; the single-column FK is the common case.

**Why it matters:** OOM on the postmask runner for a single large FK edge. The composite path is protected; the single-column path is not. Asymmetry makes the protection appear complete when it is not.

**Fix:** Add the cap guard to `_check_one_fk` before `parent_set = set(...)`:

```python
_PARENT_PK_CAP = 10_000_000
if len(parent_pks) > _PARENT_PK_CAP:
    return FKPreservationFinding(
        parent_table=parent_table,
        parent_column=parent_col,
        child_table=child_table,
        child_column=child_col,
        severity="warning",
        orphan_count=0,
        total_child_rows=len(child_fks),
        orphan_rate=0.0,
        namespace=namespace,
        message=(
            f"parent table {parent_table!r} has {len(parent_pks)} non-null PKs, "
            f"above the {_PARENT_PK_CAP}-row cap for single-column FK orphan "
            "detection. Skipping orphan count; use a sample-based audit for "
            "tables this large."
        ),
    )
```

---

### F4 -- MEDIUM | Correctness

**Files:** `src/decoy_engine/generation/composite/_person.py:207-212`, `src/decoy_engine/generation/composite/_address.py:150-154`

**Issue:** `_generate_deterministic` constructs output series with `pd.Series(values_list)` which assigns `RangeIndex(0, n)` regardless of `source`'s actual index:

```python
return {
    "first_name": pd.Series(first_vals),   # index = RangeIndex(0, n)
    ...
}
```

If `source` has a non-default index (e.g., from a `.loc[boolean_mask]` slice, a concat with non-contiguous rows, or a `.reset_index()` mismatch), the caller that assigns these series back to a DataFrame with the original index gets NaN-fill for every output column. The behavior is silent: no exception, no warning, just null output.

**Why it matters:** Any masking path that calls `_generate_deterministic` on a source series sliced from a filtered frame will produce all-null composite columns for rows outside the default integer range.

**Fix:** Pass `index=source.index` to every `pd.Series()` constructor in both composite generators (see the fix snippet in F2 above, which includes `index=idx`).

---

### F5 -- MEDIUM | Correctness

**File:** `src/decoy_engine/storm/postmask/policy_validation.py:209-221`

**Issue:** The module docstring explicitly names FPE self-mappings as a legitimate exception: "Deterministic strategies where the source happens to be the same as the output by coincidence (e.g. an FPE mask that maps to itself for a given key). Rare; treated as **warning** so the operator can confirm." The implementation produces `severity="fail"` for all non-passthrough bytes-identical cases:

```python
# All bytes-identical, non-passthrough, multi-distinct cases:
return PolicyValidationFinding(..., severity="fail", ...)
```

An FPE mask with a charset where one value self-maps (statistically, 1/n probability per value per charset) produces a false-positive `fail` finding. The operator sees a `fail` compliance finding for a job that ran correctly.

**Why it matters:** False-positive `fail` findings erode trust in the postmask checker and require operator triage on every FPE job that contains a self-mapping value.

**Fix options:**
1. **(preferred)** Add a `_DETERMINISTIC_BUT_POSSIBLY_SELF_MAPPING: frozenset[str]` containing `{"fpe", "hash"}` strategies, and demote their severity from `fail` to `warning` in the bytes-identical branch, with message "output is byte-identical to source; for a deterministic strategy this may be a self-mapping coincidence rather than a mask failure."
2. **(minimal)** Update the docstring to reflect the actual behavior (bytes-identical is always `fail`) if the team wants to be conservative.

The team should decide which approach matches the intended UX, but the current code disagrees with its own docstring.

---

### F6 -- MEDIUM | Design

**File:** `src/decoy_engine/generation/pool/_cache.py:153-162`

**Issue:** `get_default_pool_cache()` uses the same non-atomic singleton pattern flagged in five prior QA reviews (pandas-fpe-compile F3, disguises-profile F14, strategies-instrumentation F3, synthesize-determinism F1, post-validation F2):

```python
_DEFAULT_CACHE: PoolCache | None = None

def get_default_pool_cache() -> PoolCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = PoolCache()
    return _DEFAULT_CACHE
```

Under CPython GIL the assignment is effectively atomic and the race produces at worst two `PoolCache()` instances where the second overwrites the first -- both empty, both correct. Under free-threaded Python (PEP 703, active in CPython 3.13+) the check-then-act is a genuine race.

**Why it matters:** Systemic pattern. The fix has been recommended five times in prior QA sessions. Consolidating on `functools.lru_cache(maxsize=None)` or a module-level lock would resolve all instances at once.

**Fix (consistent with prior recommendations):**

```python
import functools

@functools.lru_cache(maxsize=None)
def get_default_pool_cache() -> PoolCache:
    return PoolCache()
```

Note: `_reset_default_pool_cache_for_tests()` must also clear the `lru_cache` via `get_default_pool_cache.cache_clear()`.

---

### F7 -- LOW | Reliability

**File:** `src/decoy_engine/context.py:164-224`

**Issue:** `emit_lineage`, `emit_fidelity`, `emit_quarantine`, and `emit_throughput_sample` all silently swallow every exception from the structured method:

```python
try:
    fn(kind, label, type_)
except Exception:
    pass  # no log, no trace
```

`emit_step` was fixed in QA-8 F4 to log a `DEBUG` message on `TypeError`. The other four helpers received no corresponding fix. A DB failure in a `JobLogger.lineage()` call (e.g., deadlock, serialization error, connection timeout) would be completely invisible: no log line, no metric, no alert. The engine continues normally while the reporting UI silently loses step lineage data.

**Why it matters:** Debugging production job failures that drop lineage records requires reading DB logs; no engine-side trace is present.

**Fix:** Add a `DEBUG` log (not `WARNING` -- these are expected to swallow production transients) to the bare `except Exception` block in each helper, following the `emit_step` pattern:

```python
except Exception:
    import logging
    logging.getLogger(__name__).debug(
        "emit_lineage: structured call raised %s; swallowed.",
        type(exc).__name__,
        exc_info=True,
    )
```

---

### F8 -- LOW | Reliability

**File:** `src/decoy_engine/connectors/gcs.py:114`

**Issue:** `_build_gcs_client` raises bare `json.JSONDecodeError` when `service_account_json` contains malformed JSON:

```python
sa_info = json.loads(config.service_account_json.get_secret_value())
```

SDK callers that catch `PermanentError` / `TransientError` from `check()` and `write()` will not intercept this. The check() method wraps its GCS exception via `_wrap_gcs_error(exc)`, but the JSON parse error occurs before any GCS call and escapes that wrapper.

**Why it matters:** The platform's connector health-check loop (which calls `check()`) would surface a traceback instead of a clean `CheckResult(ok=False, detail=...)`. The S3 connector has the same gap.

**Fix:** Wrap the `_build_gcs_client` call in `check()` with a broad `except Exception`, or validate JSON format in `GCSConfig.model_validator` before the client is ever constructed:

```python
# In GCSConfig:
@model_validator(mode="after")
def _validate_sa_json(self) -> "GCSConfig":
    if self.service_account_json is not None:
        try:
            json.loads(self.service_account_json.get_secret_value())
        except json.JSONDecodeError as exc:
            raise ValueError(f"service_account_json is not valid JSON: {exc}") from exc
    return self
```

---

### F9 -- LOW | Performance

**File:** `src/decoy_engine/generation/pool/_builder.py:169`

**Issue:** `distinct_count` for object dtype arrays calls `.tolist()` before constructing the set:

```python
distinct_count = len(set(values.tolist()))
```

For a 10K-pool of email strings, `.tolist()` materializes ~10K Python string objects into a list that is immediately discarded after `set()` construction. `len(set(values))` iterates the numpy array directly without an intermediate list, halving the transient allocation.

**Why it matters:** Minor; the intermediate list is O(pool_size) and short-lived. Negligible at default pool_size=10K but measurable at pool_size=100K+.

**Fix:** `distinct_count = len(set(values))`.

---

### F10 -- NIT | Design

**File:** `src/decoy_engine/providers_v2/mimesis/_adapter.py:151`

**Issue:** The `_unseeded` cache keys on `mlocale.value` (a string). Mimesis `Generic` instances are not thread-safe (they hold internal RNG state). If the MimesisAdapter instance is shared across concurrent non-deterministic generates (two threads both calling `generate()` with the same locale and `spec.seed=None`), they share one `Generic` instance whose internal RNG state races. The non-deterministic path is intentionally non-deterministic, so the race has no correctness consequence -- but it could produce duplicates or sequences that don't look random under high concurrency.

**Why it matters:** Low impact today (non-deterministic outputs are not expected to be reproducible). Document the constraint ("not safe for concurrent non-deterministic generates from a shared adapter") or create per-thread instances via `threading.local()` if the platform uses thread-per-job.

---

## 3. Performance Notes

| Module | Bottleneck | How to measure |
|--------|-----------|----------------|
| `composite/_person.py:_generate_deterministic` | CPU -- Python loop O(n) with `.iloc` per row | `python -m cProfile -s cumtime` on a 100K-row composite mask call; `_generate_deterministic` should dominate |
| `composite/_address.py:_generate_deterministic` | Same as above | Same measurement |
| `fk_preservation.py:_check_one_fk` | Memory -- `set(parent_pks.tolist())` for large PKs | `memory_profiler` on a job with a 5M-row FK edge |
| `pool/_builder.py` | Minor -- `set(values.tolist())` allocates transient list | `timeit` with pool_size=100K |

The primary bottleneck across composite generators is the Python row-loop. The fix in F2 eliminates `.iloc` overhead (estimated 3-5x throughput gain at 100K rows, consistent with the FPE-vs-hash strategy benchmark in 06-04). No profiling claim here without a real number -- measure with `timeit` or `py-spy` against the fixed version before quoting throughput.

---

## 4. Suggested Tests

| Test | Module | What to verify |
|------|--------|----------------|
| `test_canonicalize_float32_raises` | `pool/_canonicalize.py` | `_canonicalize_source(np.float32(1.0))` raises `GenerationError(code="float_canonicalization_unsupported")` |
| `test_canonicalize_float16_raises` | same | Same for `np.float16(1.0)` |
| `test_composite_person_non_default_index` | `composite/_person.py` | Source series with index starting at 1000; assert no NaN in output series |
| `test_composite_address_non_default_index` | `composite/_address.py` | Same |
| `test_fk_preservation_single_col_large_parent` | `fk_preservation.py` | Synthesize a parent frame with 11M rows; assert `check_fk_preservation` returns `severity="warning"` without OOM |
| `test_policy_validation_fpe_self_map` | `policy_validation.py` | Set `src_col == out_col` for a column with `strategy="fpe"` and multiple distinct values; assert severity is `warning` (not `fail`) after the fix |
| `test_gcs_config_malformed_sa_json` | `connectors/gcs.py` | Construct `GCSConfig(service_account_json=SecretStr("not-json"), ...)` and assert `ValidationError` at config construction, not at runtime |
| `test_pool_cache_lru_after_cache_clear` | `pool/_cache.py` | After `get_default_pool_cache.cache_clear()`, a new call returns a fresh empty cache |
| `test_emit_lineage_swallowed_exception_logged` | `context.py` | Patch `logger.lineage` to raise; assert a DEBUG message is emitted (after fix) |

---

## 5. What's Good

- **`_canonicalize.py` integer encoding** is well-designed: ASN.1 DER-inspired minimal two's-complement with length prefix handles both Python unbounded ints and numpy scalars via `numbers.Integral`, and bools are dispatched before the integral check. The v2 protocol bump that fixed the 8-byte overflow is well-documented.
- **`_pool_adapter.py` cache-before-build** (identity_for -> get -> build only on miss) is correct and efficiently closes the structural cache-miss gap that existed in prior sprints.
- **`fk_preservation.py` composite FK via `MultiIndex.isin`** is the right approach (avoids itertuples O(n) per row, vectorized in pandas/numpy). The `_PARENT_TUPLE_CAP` guard for the composite path shows awareness of the OOM risk; it just needs to be extended to the single-column path (F3).
- **`expressions.py` `_SafeRe` proxy** is a clean solution to simpleeval's module-reference restriction. Exposing only named methods prevents `__module__` attribute access while preserving `re.sub(...)` / `re.search(...)` spelling in formulas.
- **`policy_validation.py` row-count mismatch guard** (Dennis M13) and the index-alignment `reset_index` fix (QA-4 F6) are both handled.
- **`profile/_pii.py`** correctly uses a high-confidence threshold before mapping to `PIIClass`, and the custom-detector silent-drop + built-in-drift `WARNING` log is the right pattern for keeping the closed enum in sync without hard-failing jobs.
- **`context.py make_key_resolver`** validates master key length at construction time (not at first use), which surfaces misconfiguration before any data is processed.
- **`providers_v2/mimesis/_adapter.py`** correctly rejects `deterministic=True` at the direct-generate layer (pool-routed only), preserving adapter-capability symmetry with `FakerAdapter`.
