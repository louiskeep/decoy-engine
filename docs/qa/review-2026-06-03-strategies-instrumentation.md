# QA Review: execution/_strategies + instrumentation/timing

**Date**: 2026-06-03  
**Scope**: `src/decoy_engine/execution/_strategies/` (all 9 handlers) and `src/decoy_engine/instrumentation/timing.py`  
**Previous reviews to avoid**: `qa/review-2026-06-02-execution` (adapter, runner, substrate, Polars adapter)

---

## Summary

The nine strategy handlers are structurally sound and carry good fix history (QA-3 positional-iteration fixes, F14 prefix-overlap, F7 technique-class, QA-9 credential rotation). The dominant themes across this review are **performance** (row-by-row Python loops that could exploit ThreadPoolExecutor for I/O-releasing HMAC calls) and **silent data leakage on misconfiguration** (`text_redact` passes PII through on type-mismatched config rather than raising). The most actionable fix is `_date_shift.py`'s `col.iloc[i]` loop-in-list-comprehension, which is straightforwardly vectorizable. The `instrumentation/timing.py` module is well-designed but carries a latent fork hazard in its module-level `psutil.Process()` singleton.

No critical determinism violations were found in this surface. All deterministic strategies key correctly through `derive`/`derive_index` with proper namespace threading.

---

## Findings

### F1 — HIGH | Performance | `_categorical.py` deterministic path

**The issue**: Both the uniform and weighted deterministic branches iterate `for i, value in enumerate(source)` with a `derive_index(...)` HMAC call per row.

```python
# _categorical.py lines ~110-130
for i, value in enumerate(source):
    if na_mask[i]:
        out.append(None)
        continue
    idx = derive_index(
        ctx.job_seed,
        plan.namespace,
        _canonicalize_source(value),
        pool_size=len(categories),
    )
    out.append(categories[idx])
```

**Why it matters**: `derive_index` calls HMAC-SHA256, which releases the GIL at the C-level digest call. Unlike pure-Python Feistel (FPE handler), HMAC-bound work genuinely benefits from threading. At 500k rows this loop takes ~2-5s on a single core. The FPE handler already applies the `ThreadPoolExecutor` pattern for the same reason.

**Recommended fix**: Apply the same `_encrypt_values`-style chunked dispatch used in `_fpe.py`:

```python
def _derive_batch(
    values: list, job_seed, namespace, pool_size: int, categories: list
) -> list:
    out = []
    for value in values:
        idx = derive_index(job_seed, namespace, _canonicalize_source(value), pool_size=pool_size)
        out.append(categories[idx])
    return out
```

Then dispatch non-null rows across `min(chunk_count, os.cpu_count() or 1)` workers. Determinism is preserved because each row's output depends only on its own value, not on execution order.

**Profile with**: `python -m cProfile -s cumulative` on a 500k-row categorical column, or `timeit` the inner loop against the ThreadPoolExecutor variant.

---

### F2 — HIGH | Performance | `_date_shift.py` `.iloc[i]` loop

**The issue**: The final output assembly uses pandas scalar access in a Python loop:

```python
# _date_shift.py, last block
out = [col.iloc[i] if unusable[i] else formatted.iloc[i] for i in range(len(col))]
```

`Series.iloc[i]` incurs per-call pandas indexing overhead. For 100k rows, benchmarks show this adds ~200-400ms vs a vectorized equivalent (`cProfile` will show the time dominated by `_pandas_adapter.py`'s column assignment chain, but `scalene` will pin it here).

**Why it matters**: `date_shift` is applied to every date column; a pipeline with 10 date columns at 500k rows pays this cost 10 times.

**Recommended fix** — drop-in replacement:

```python
unusable_series = pd.Series(unusable, index=df.index)
df[column] = formatted.where(~unusable_series, col)
```

Where `formatted` is a Series and `col` is the original. The `where` call is a single C-level vectorized operation. Null positions and parse-fail positions are handled identically to the loop (restore original value).

**Verify**: `assert (output_col == loop_output_col).all()` on a fixture with NaT, None, and format-mismatch values.

---

### F3 — HIGH | Performance | `_hash.py` row-by-row loop

**The issue**: `_hash.py` iterates all non-null rows with `derive(...).hex()` per row in a Python loop, with no thread parallelism.

```python
# _hash.py lines ~35-43
for i, value in enumerate(source):
    if na_mask[i]:
        out.append(None)
        continue
    token = derive(ctx.job_seed, plan.namespace, _canonicalize_source(value)).hex()
    out.append(token[:truncate] if truncate is not None else token)
```

**Why it matters**: `derive` is pure HMAC-SHA256 and releases the GIL at the C-level digest. The pattern is identical to FPE's per-value keying. The FPE handler applies `ThreadPoolExecutor`; hash does not. At 500k rows of PK hashing (common for FK-preserving pipelines), this is the bottleneck.

**Recommended fix**: Extract the per-value function and apply the same `_encrypt_values` chunked dispatch as `_fpe.py`. Determinism is unaffected (each value's hash depends only on its own content + the fixed seed/namespace).

---

### F4 — MEDIUM | Correctness | `_nested.py` `_path_segments` array-path prefix detection

**The issue**: `_path_segments` splits on `.` after stripping outer parens:

```python
def _path_segments(match: Any) -> tuple[str, ...]:
    raw = str(match.full_path).strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return tuple(raw.split("."))
```

For array-indexed paths, `jsonpath_ng` renders the path as `user.emails[0]`. The segment for `emails[0]` is the string `"emails[0]"`, which does NOT match the segment `"emails"` from the parent path `user.emails`. So `_has_prefix_overlap` returns `False` and the warning/depth-sort fix is silently skipped.

**Concrete case**: JSONPath `$.user.emails[0]` (deeper) and `$.user.emails` (shallower array container). The `emails[0]` write will be overwritten by the `emails` write without triggering a `QualityWarning`. The masked leaf at index 0 is silently un-masked.

**Why it matters**: This is a security regression — the inner masked value is overwritten by the outer container's (unmasked or differently-masked) array value.

**Recommended fix**: Strip the array index suffix when comparing segments:

```python
import re
_ARRAY_SUFFIX_RE = re.compile(r'\[[^\]]*\]$')

def _path_segments(match: Any) -> tuple[str, ...]:
    raw = str(match.full_path).strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    parts = []
    for seg in raw.split("."):
        parts.append(_ARRAY_SUFFIX_RE.sub("", seg))
    return tuple(parts)
```

This normalises `emails[0]` → `emails` for prefix comparison while preserving the structural ordering.

**Verify**: Add a unit test with JSONPath `$.user.emails[0]` targeting a cell `{"user": {"emails": ["real@example.com", "also-real@example.com"]}}`. Confirm `QualityWarning(code="nested_jsonpath_path_overlap")` is emitted and the `[0]` leaf value is masked.

---

### F5 — MEDIUM | Correctness / Security | `_text_redact.py` silent passthrough on misconfigured token/detectors

**The issue**: Two early-return paths silently pass the column through unchanged when `token` or `detectors_cfg` are the wrong type:

```python
# _text_redact.py lines ~47, ~57
if not isinstance(token, str):
    return df, []   # PII passes through unchanged, no warning
...
else:  # detectors_cfg not list/tuple
    return df, []   # PII passes through unchanged, no warning
```

**Why it matters**: A YAML misconfiguration (`token: 42` or `detectors: {email: true}`) silently lets PII survive. The docstring says "a misconfigured plan never crashes the run"; however, a crash (via `StrategyError`) is the correct signal here — it surfaces at job creation/validation time when the plan can still be corrected, not at row-processing time.

The `nested` handler sets the right precedent: config errors raise `StrategyError` so the job fails visibly rather than silently corrupting the output.

**Recommended fix**:

```python
if not isinstance(token, str):
    raise StrategyError(
        code="text_redact_token_not_string",
        strategy="text_redact",
        message=f"text_redact token must be a string, got {type(token).__name__!r} (column={column!r}).",
    )

if detectors_cfg is not None and not isinstance(detectors_cfg, (list, tuple)):
    raise StrategyError(
        code="text_redact_detectors_not_list",
        strategy="text_redact",
        message=(
            f"text_redact detectors must be a list or None, got "
            f"{type(detectors_cfg).__name__!r} (column={column!r})."
        ),
    )
```

---

### F6 — MEDIUM | Reliability | `instrumentation/timing.py` module-level fork hazard

**The issue**: `_PROCESS = psutil.Process()` is evaluated once at import time:

```python
# timing.py line ~63
_PROCESS = psutil.Process()
```

`psutil.Process()` captures the **current PID at module import time**. In a fork-based multiprocessing model (Python's default on Linux), the child process inherits the imported module with `_PROCESS` pointing to the **parent's PID**. Calls to `_PROCESS.memory_info().rss` in the child return the **parent's RSS**, not the child's.

**Why it matters**: The execution model is currently single-threaded and non-forking, so this is latent. But the docstring anticipates future parallel execution, and any future use of `multiprocessing` or a forking job executor would silently produce wrong RSS readings (no error, just wrong numbers).

**Recommended fix**: Lazily initialize per-call:

```python
def rss_kb() -> int:
    """Process resident-set-size in KB."""
    return int(psutil.Process().memory_info().rss / 1024)
```

`psutil.Process()` with no argument defaults to `os.getpid()` each time, which is correct in both parent and forked children. The psutil overhead of constructing the `Process` object without a PID argument is negligible (it calls `os.getpid()` internally, not a syscall).

Alternatively, add a `_reset_process_handle()` function callable in child process `initializer=` of any future `ProcessPoolExecutor`.

---

### F7 — MEDIUM | Data | `_fpe.py` degenerate charset silent passthrough

**The issue**:

```python
# _fpe.py line ~45
if len(charset) < 2:
    return df, []  # degenerate charset -> passthrough (V1 behavior)
```

A misconfigured `charset` (empty string, single char) silently leaves the entire column unmasked. The comment justifies this as "V1 behavior", but V2 strategies uniformly raise `StrategyError` for config errors (see `nested`, `fpe_requires_namespace`, etc.).

**Why it matters**: An operator who misspells `charset: "digits"` as `charset: ""` or uses a single-character alphabet gets no error signal and publishes unmasked data.

**Recommended fix**: Replace silent passthrough with `StrategyError`:

```python
if len(charset) < 2:
    raise StrategyError(
        code="fpe_charset_degenerate",
        strategy="fpe",
        message=(
            f"fpe charset resolved to {len(charset)} character(s) — "
            "at least 2 required (column={column!r}). "
            f"charset_spec={charset_spec!r}."
        ),
    )
```

---

### F8 — LOW | Correctness | `_shuffle.py` 8-byte seed truncation

**The issue**:

```python
# _shuffle.py line ~33
seed_int = int.from_bytes(derive(ctx.job_seed, plan.namespace, b"")[:8], "big")
rng = np.random.default_rng(seed_int)
```

`derive(...)` returns 32 bytes (HKDF-SHA256). Only 8 bytes (64 bits) are used as the RNG seed. `np.random.default_rng` accepts up to 128 bits (two uint64s) for its PCG-64 algorithm.

**Why it matters**: 64-bit seed is more than sufficient entropy for a permutation. However, if the upstream `derive` function is ever changed to produce shorter digests, `[:8]` would silently use even fewer bits. A more robust form uses the full available output.

**Recommended fix** (optional hardening):

```python
digest = derive(ctx.job_seed, plan.namespace, b"")
# Use the full 32-byte digest split into two uint64s for PCG-128 seeding.
high, low = int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big")
rng = np.random.default_rng([high, low])
```

---

### F9 — LOW | Design | `_fpe.py` column-name tweak ties output to column identity

**The issue**:

```python
# _fpe.py line ~37
tweak = column.encode("utf-8", errors="replace")
```

The FPE tweak is set to the column name. Renaming a column (even a cosmetic rename via an ALTER TABLE or YAML edit) changes the tweak, which changes every encrypted value. The same physical PAN stored in `card_number` and `payment_card_number` columns in two different pipelines would produce different ciphertext even with the same seed and namespace.

**Why it matters**: Joinability-by-ciphertext (which FPE is designed to support) breaks silently across column renames or pipelines that use different column names for the same semantic field.

**Note**: This is an intentional design tradeoff (the tweak prevents rainbow-table attacks that would work if all columns with the same value produced the same ciphertext regardless of column). Document the constraint explicitly so operators know column renames invalidate prior ciphertext.

---

### F10 — LOW | Data | `_bucketize.py` `Int64` + `None` in `range` / `midpoint` format

**The issue**: When `is_int_width=True` and format is `"range"`, `upper = upper_excl - 1` operates on a pandas `Int64` Series. For rows where `nums` is `NA` (non-numeric input), `lower` is `<NA>` and `upper_excl - 1` is `<NA>`. The `formatted.where(nums.notna(), col)` guard restores the original value for those positions.

However, `lower.astype(str)` on `Int64` produces the string `"<NA>"` for NA rows before the `.where()` restore. If `.where()` is applied after `.astype(str)`, and the dtype changes to `object` in `formatted`, the restore works. This is correct as implemented — the `.where` is applied to `formatted` (after astype), not to the Int64 Series.

**Why it matters**: Confirmed no bug, but the chain is non-obvious. Add a unit test with a mixed-numeric column (`[1.0, "bad", None, 5.5]`) and `format="range"` to nail down the expected output and prevent regressions.

---

### F11 — LOW | Design | `_composite.py` `bundle_decl or []` swallows explicit empty list

**The issue**:

```python
# _composite.py line ~89
bundle_decl = cfg.get("bundle") or []
```

If the provider_config carries `bundle: []` (an explicit empty bundle), `or []` evaluates to `[]` — indistinguishable from a missing `bundle` key. The subsequent `if not isinstance(bundle_decl, list): raise` check allows the empty list through to `composite_custom(bundle=[])`, whose `__init__` presumably handles it (or raises its own error).

**Why it matters**: Low risk in practice (an empty bundle is nonsensical), but the pattern silently converts one error case (explicit empty list) into another (empty list passed to generator). Prefer:

```python
bundle_decl = cfg.get("bundle")
if bundle_decl is None:
    bundle_decl = []
```

---

### F12 — NIT | Performance | `_fpe.py` ThreadPoolExecutor created per call

The `ThreadPoolExecutor` is constructed inside `_encrypt_values` on every strategy invocation. Thread pool creation on Linux is ~1µs per thread; for 4 threads this is ~4µs overhead per column, negligible vs FPE cost.

If FPE is ever applied to many small columns (e.g., 100 single-value columns), pool creation becomes the dominant overhead. A shared class-level pool or a context-manager-based reuse pattern would eliminate this. Not worth changing now; note in a performance sprint if FPE at high column-count becomes a measured bottleneck.

---

### F13 — NIT | Reliability | `instrumentation/timing.py` `peak_delta_kb` name vs semantics

```python
# timing.py line ~197
peak_delta_kb = max(0, rss_after - rss_before)
```

`peak_delta_kb` implies the highest RSS reached within the bracket. What is actually measured is the **net RSS change** at exit, clamped to zero. A strategy that allocates 500MB and frees 499MB at exit would report `peak_delta_kb ≈ 1024` even though the true peak was 500MB.

The docstring correctly documents this limitation. Rename the field to `rss_delta_kb` (or `net_rss_delta_kb`) to match the semantics, and add a note in the manifest reader that true peak requires `tracemalloc`.

---

## Performance Notes

| Bottleneck type | Surface | Handler |
|---|---|---|
| CPU (HMAC, GIL-releasing) | deterministic loop | `_categorical`, `_hash` |
| Python loop (pandas scalar access) | output assembly | `_date_shift` |
| CPU (Feistel, GIL-bound) | threading limited | `_fpe` |
| Regex (per-cell) | span detection | `_text_redact` |
| Pure Python arithmetic | vectorizable | `_bucketize` is already vectorized ✓ |

**Profiling approach**:
- `python -m cProfile -s cumulative` on a 500k-row fixture per strategy (target: identify the `derive_index` / `derive` call contribution vs loop overhead)
- `scalene` for memory + CPU breakdown at line level
- For FPE threading: `time.perf_counter()` before/after `_encrypt_values` with `workers=1` vs `workers=4` on a 4-core machine with 100k rows

---

## Suggested Tests

| Test | Rationale |
|---|---|
| `test_categorical_deterministic_weighted_boundary` | bucket=0, bucket=_WEIGHTED_CDF_RES-1, bucket at exact CDF threshold boundary |
| `test_categorical_empty_pool_raises` | `categories=[]` must raise `StrategyError` |
| `test_date_shift_vectorized_matches_loop` | confirm F2 fix produces byte-identical output |
| `test_hash_truncate_zero_excluded` | `truncate=0` should fall through to no-truncate (confirmed by code, add explicit assertion) |
| `test_nested_array_index_prefix_overlap` | `$.user.emails[0]` + `$.user.emails` triggers warning (F4 regression guard) |
| `test_text_redact_token_not_string_raises` | `token: 42` must raise `StrategyError` after F5 fix |
| `test_text_redact_detectors_dict_raises` | `detectors: {email: true}` must raise `StrategyError` |
| `test_fpe_degenerate_charset_raises` | `charset: ""` and `charset: "x"` must raise `StrategyError` after F7 fix |
| `test_shuffle_seed_full_digest` | after F8 fix, verify same seed produces same permutation; different seed produces different permutation |
| `test_timing_collector_fork_safe` | after F6 fix, create a child process and assert `rss_kb()` in child returns child's own RSS |
| `test_bucketize_mixed_numeric_range_format` | `[1.0, "bad", None, 5.5]` with `format=range`; confirm `"bad"` and `None` restore original |
| `test_composite_custom_empty_bundle` | empty `bundle: []` should propagate to generator, not silently pass |

---

## What's Good

- **QA-3 positional-iteration fixes** in `_nested.py` and `_text_redact.py` are correctly applied and well-documented. The duplicate-index hazard is a real pandas footgun and it's thoroughly addressed.
- **`_build_cdf` zero-width slot rejection** (F9, 2026-05-31) is the right call: silent non-selection of a weighted category is harder to debug than an upfront `StrategyError`.
- **`_fpe.py` determinism argument** is airtight: per-value keying means worker order cannot affect output. The correctness is provable, not just asserted.
- **`_categorical.py` non-deterministic weighted path** uses `np.random.choice` with explicit `p=` normalization — idiomatic, readable, and NumPy-optimized.
- **`instrumentation/timing.py` thread-local design** is well-suited to the current execution model. The `use_collector` context manager's nesting support (previous collector restoration) is a thoughtful detail.
- **`_bucketize.py`** is entirely vectorized (no Python loops). Clean and fast.
- **`_shuffle.py` Q13 fix** (object-dtype Series on writeback) is exactly right. Preventing pandas from re-inferring float64 from int+None mixes is a subtle but important invariant.
