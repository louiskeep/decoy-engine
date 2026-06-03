# QA Review: decoy-engine -- 2026-06-03

Reviewer: QA agent (claude-sonnet-4-6)
Scope: determinism layer, execution strategies, generation/synthesize, plan compiler, transforms

---

## Summary

The determinism envelope is sound and the core HKDF/HMAC chain is correctly implemented.
The most important finding is **three execution strategies (`hash`, `date_shift`, `categorical`) call
`derive()` per row and therefore pay the full 2-HMAC HKDF cost on every single row**, even though
`DeriveContext` was introduced in QA-10 F4 specifically to amortize that cost.
At scale (1M rows, 50-column table) this is ~25 seconds of avoidable HKDF work per job.
Secondary to that, `_date_shift.py` builds its final output column using per-row `Series.iloc[i]`
accesses inside a Python loop, a known-expensive pandas pattern.

---

## Findings

### F-1 -- High | Performance
**`hash`, `date_shift`, `categorical` call `derive()` per row; `DeriveContext` is not wired in**

Files:
- `src/decoy_engine/execution/_strategies/_hash.py:55`
- `src/decoy_engine/execution/_strategies/_date_shift.py:63`
- `src/decoy_engine/execution/_strategies/_categorical.py:158,172`

`DeriveContext` was introduced in QA-10 F4 (2026-06-01) with the explicit comment:

> "At 1M rows per column the wasted HKDF work is two HMAC-SHA256 invocations per row
> (~0.5s/column on a modern core; ~25s for a 50-column masked table)."
> -- `_derive.py:114-120`

All three strategies import `derive` directly and call it in a Python for-loop, recomputing
`hkdf_sha256()` on every row. The fix -- use `DeriveContext.for_column(seed, namespace)` once
and then `ctx.derive_source(namespace, source)` per row -- is implemented in `_derive.py` and
documented, but not applied to these handlers.

**Impact**: For 1M rows, ~2 extra HMAC-SHA256 calls per row per strategy. The DeriveContext
docstring puts this at ~0.5s/column on a modern core. A 50-column table with hash + date_shift
+ categorical columns loses 25+ seconds to recomputable HKDF.

**Bottleneck classification**: CPU-bound (HMAC-SHA256 in stdlib `_hashlib`).

**Recommended fix** for `_hash.py`:

```python
from decoy_engine.determinism import DeriveContext

# In run():
ctx_d = DeriveContext.for_column(ctx.job_seed, plan.namespace)
for i, value in enumerate(source):
    if na_mask[i]:
        out.append(None)
        continue
    token = ctx_d.derive_source(plan.namespace, _canonicalize_source(value)).hex()
    out.append(token[:truncate] if truncate is not None else token)
```

Apply the same pattern to `_date_shift.py:63` and `_categorical.py:158,172`.

**Verify**: `timeit` a 1M-row hash column with derive() vs DeriveContext. Expect ~2x speedup
on a modern core (cutting 3 HMAC calls per row to 1).

---

### F-2 -- High | Performance
**`_date_shift.py` builds output with per-row `Series.iloc[i]` access in a Python loop**

File: `src/decoy_engine/execution/_strategies/_date_shift.py:69`

```python
out = [col.iloc[i] if unusable[i] else formatted.iloc[i] for i in range(len(col))]
```

`Series.iloc[i]` is the slowest pandas access pattern: it boxes a Python scalar per call, paying
~O(1) overhead per element but with a large constant (~1-10 µs). For 1M rows this loop calls
`col.iloc[i]` up to 2M times. The fix is one numpy extraction before the loop:

```python
col_arr = col.to_numpy(dtype=object)
fmt_arr = formatted.to_numpy(dtype=object)
out = [col_arr[i] if unusable[i] else fmt_arr[i] for i in range(len(col))]
```

Or fully vectorized with numpy where (preferred):

```python
df[column] = pd.Series(
    np.where(unusable, col.to_numpy(dtype=object), formatted.to_numpy(dtype=object)),
    dtype=object,
    index=df.index,
)
```

**Impact**: At 1M rows with 20% unusable (nulls + unparseable), ~1.6M `.iloc[i]` calls avoided.
Expect 5-15x speedup on that line.

**Bottleneck classification**: CPU-bound (Python interpreter overhead per pandas scalar unboxing).

**Verify**: `python -m cProfile -s cumulative` a date_shift run on 1M rows; look for time in
`_pandas_core_indexing.py`.

---

### F-3 -- High | Performance
**FPE `ThreadPoolExecutor` over GIL-bound work; `np.array_split` numpy-object roundtrip**

File: `src/decoy_engine/execution/_strategies/_fpe.py:91-106`

Two sub-issues:

**3a. Thread parallelism on GIL-bound code.**
The Feistel inner loops (`_encode`, `_feistel`, `_decode`) are pure Python integer arithmetic;
they hold the GIL. The only GIL release is inside `hmac.new(...).digest()` (the C-level hash
computation), which takes ~0.5-1 µs. The Python overhead per Feistel call dominates.
`ThreadPoolExecutor` with N workers adds thread context-switching cost without gaining real
parallelism on the CPU-bound path. The cap at `os.cpu_count()` limits damage (the comment
acknowledges this), but 4 threads competing for a GIL-bound workload will be slower than 1
on a loaded system.

The comment says "net-negative on a 2-vCPU CI runner" -- this applies equally to any system
where the Python Feistel dominates the HMAC. Real speedup requires either:
1. `ProcessPoolExecutor` with picklable task state (significant pickling overhead for closures), or
2. Moving to a C-extension FPE (deferred per the design note).

**3b. Unnecessary numpy object-array roundtrip.**
`np.array_split(np.array(values, dtype=object), workers)` at line 101 converts a Python list of
strings to a numpy object array solely to call `array_split`, then `list(chunk)` converts each
chunk back. This adds ~2x memory allocation + GC pressure with no benefit.

Fix:

```python
# Simple interleaved slice, no numpy needed
chunks = [values[i::workers] for i in range(workers) if values[i::workers]]
```

Or with a slicing helper:

```python
chunk_size = (len(values) + workers - 1) // workers
chunks = [values[i * chunk_size:(i + 1) * chunk_size] for i in range(workers)]
chunks = [c for c in chunks if c]
```

**Impact**: 3b alone saves one object-array allocation per column invocation.
For 3a, consider removing the ThreadPoolExecutor entirely until a ProcessPoolExecutor or
C-level FPE lands: serial execution is faster on GIL-bound single-core workloads and produces
identical output.

**Verify**: `scalene --cpu --memory` on an FPE-heavy pipeline; compare wall time for
`_encrypt_values` at workers=1 vs workers=4 on a 4-core host.

---

### F-4 -- Medium | Correctness (latent)
**`DeriveContext.derive_source` silently diverges if caller passes wrong namespace**

File: `src/decoy_engine/determinism/_derive.py:157-174`

The docstring warns: "callers MUST pass the same namespace used in `for_column` or the output
diverges from `derive(...)`". There is no runtime assertion. A strategy that builds a
`DeriveContext` with one namespace and inadvertently calls `derive_source` with a different
one will produce valid-looking but wrong output -- the masked values will be deterministic
but will not match any other invocation using the scalar `derive()` function.

Since `DeriveContext` has no stored namespace (only `_hmac_key`), there is nothing to compare
against. The risk is low today (no strategy uses `DeriveContext` yet -- see F-1), but as
adoption grows, a copy-paste error here will silently break joinability.

**Recommended fix**: Store the namespace in the dataclass and assert in `derive_source`:

```python
@dataclass(frozen=True)
class DeriveContext:
    _hmac_key: bytes
    _namespace: str  # added

    @classmethod
    def for_column(cls, seed: bytes, namespace: str) -> "DeriveContext":
        # ... (existing validation + key derivation)
        return cls(_hmac_key=key, _namespace=namespace)

    def derive_source(self, namespace: str, source: bytes) -> bytes:
        if namespace != self._namespace:
            raise DeterminismError(
                code="namespace_mismatch",
                message=(
                    f"derive_source called with namespace {namespace!r} but "
                    f"context was built for {self._namespace!r}; output would "
                    "diverge from scalar derive(). Check caller."
                ),
            )
        # ... rest unchanged
```

The frozen dataclass ensures no mutation after construction.

---

### F-5 -- Medium | Performance
**`_categorical.py` deterministic path: per-row Python for-loop, no batch canonicalization**

File: `src/decoy_engine/execution/_strategies/_categorical.py:151-184`

Even after applying F-1 (DeriveContext), the deterministic categorical path iterates rows with
a Python for-loop and calls `_canonicalize_source(value)` per row. For `str` inputs,
`_canonicalize_source` encodes the string to UTF-8 bytes. Batch UTF-8 encoding across the
whole column is faster:

```python
# After building ctx_d = DeriveContext.for_column(...)
non_na_vals = source[~na_mask]
canonicalized = [_canonicalize_source(v) for v in non_na_vals.to_numpy(dtype=object)]
# ... then derive per-row from pre-built list
```

More importantly, the non-deterministic path is already vectorized (`rng.integers(0, n)`),
so there is a ~10x throughput gap between the two modes on large inputs. The deterministic path
cannot easily be batch-vectorized (each row needs its own HMAC), but batching canonicalization
and using `DeriveContext` closes most of the gap.

**Impact**: Medium at current scale; higher impact as column cardinality grows.

---

### F-6 -- Medium | Correctness
**`synthesize._topo_sort`: dead condition on line 178**

File: `src/decoy_engine/generation/synthesize.py:178`

```python
for start in deps:
    if start in visited or start not in deps:  # `start not in deps` is always False
```

`start` is yielded by `for start in deps:`, so it is always a key of `deps`. The second
condition `start not in deps` can never be True. This is dead code -- harmless but may
mislead a reader into thinking there is an early-exit for missing nodes.

**Recommended fix**: Remove the unreachable clause:

```python
for start in deps:
    if start in visited:
        continue
```

---

### F-7 -- Low | Reliability
**`_faker.py:68-69`: double null-restoration in deterministic mode**

File: `src/decoy_engine/execution/_strategies/_faker.py:67-69`

```python
na_mask = source.isna().to_numpy()
values = list(sampled)
df[column] = [None if na_mask[i] else values[i] for i in range(n)]
```

In deterministic mode, `PoolSampler.sample()` already preserves null positions (confirmed by
the module comment: "the sampler preserves them in deterministic mode"). The outer handler
re-applies the null mask, which is redundant for deterministic mode. The result is correct
because the double-null is a no-op: `None if na_mask[i] else None` still yields `None`.

However, if a future `PoolSampler` change returns a non-null value for a null-source row (e.g.
a "fill nulls" cardinality mode), this handler would silently override it back to `None`. The
handler should document which path is authoritative:

- Option A: Rely on the sampler's null preservation; remove the outer mask.
- Option B: Keep the outer mask as a belt-and-suspenders guarantee; document that the sampler
  must also preserve nulls.

Current code is Option B without the documentation.

---

### F-8 -- Nit | Correctness
**`_fpe.py:60-61`: `operand.to_bytes` minimum length uses `max(..., 1)` but `operand=0`**

File: `src/decoy_engine/transforms/fpe.py:60`

```python
operand_b = operand.to_bytes(max((operand.bit_length() + 7) // 8, 1), "big")
```

For `operand = 0`, `bit_length()` returns 0, `(0 + 7) // 8 = 0`, so `max(0, 1) = 1`, encoding
as `b'\x00'`. This is correct. The comment in `max(..., 1)` is implicitly "handle zero".
Worth a one-line inline comment for the next reader:

```python
# bit_length()=0 for operand=0; max(...,1) ensures at least one byte.
operand_b = operand.to_bytes(max((operand.bit_length() + 7) // 8, 1), "big")
```

---

## Performance Notes

| Hot path | Big-O | Bottleneck | Measure with |
|---|---|---|---|
| `hash` per column | O(n * 3 HMAC) today; O(n * 1 HMAC) with F-1 fix | CPU (HMAC-SHA256) | `timeit` 1M-row hash column |
| `date_shift` per column | O(n) Python loop + `Series.iloc` per row | CPU + pandas overhead | `cProfile -s cumulative` |
| `categorical` deterministic | O(n * 3 HMAC) + Python loop | CPU (HMAC) | `scalene --cpu` |
| FPE per column | O(n * 8 * 2 HMAC) = O(16n HMAC) | CPU (Feistel HMAC rounds) | `py-spy record` |
| `_parent_map` | O(n) + O(n * k * `pd.isna`) | CPU + pandas per-scalar | `cProfile` on FK-heavy run |

The F-1 fix (DeriveContext) is expected to cut hash/date_shift/categorical by ~1.5-2x wall clock.
Profile before and after with `scalene --cpu --memory` on a realistic 500K-row fixture with 10+
masked columns.

---

## Suggested Tests

1. **DeriveContext parity** (covers F-1): for each of `hash`, `date_shift`, `categorical`,
   assert that running with the current `derive()` call and with `DeriveContext.derive_source()`
   produce byte-identical output for the same seed + namespace + source.

2. **Date-shift output rebuild** (covers F-2): assert that the `_date_shift` output is
   byte-identical before and after converting the `col.iloc[i]` loop to numpy-backed slicing.

3. **FPE chunk parity** (existing gate, verify): assert that `_encrypt_values(values, fn)`
   with `workers=1` and `workers=4` produce byte-identical lists. Run with a 100-element
   fixture of mixed-length strings.

4. **`DeriveContext` namespace mismatch** (covers F-4): assert that calling
   `ctx.derive_source("other_namespace", b"x")` after building `ctx` for `"namespace_a"`
   raises `DeterminismError(code="namespace_mismatch")`.

5. **Categorical non-deterministic mode reproducibility check**: running non-deterministic
   mode twice on the same column should produce different output ~99.9% of the time. Run
   `assert set(run1) != set(run2) or len(categories) == 1` as a probabilistic smoke test.

6. **`_topo_sort` on a chain of 1100 nodes** (covers RecursionError guard): assert no
   `RecursionError` and correct post-order for a 1100-node linear chain.

7. **`hmac_seed(key, None) == 0`**: document or test the intent that `None` always returns
   seed 0. A seed of 0 is not trivially distinguishable from a legitimate `HMAC % 2^32 == 0`
   result, so callers that skip nulls upstream should be the guard.

---

## What's Good

- The HKDF + HMAC envelope (`_derive.py`, `_hkdf.py`) is clean: length-prefixed inputs prevent
  namespace/source collision, the RFC 5869 reference test vectors are cited, and the
  `DeterminismError` is typed with codes that callers can inspect.
- `DeriveContext` is a well-designed amortization primitive -- the gap is only that it is not
  yet wired into the three strategies that need it most (F-1).
- The FPE single-character special case (QA-10 F2) is correctly bijective: `F` is independent
  of the source character, making the output a keyed uniform rotation.
- `_topo_sort` being iterative (not recursive) is the right call for deep reference chains.
- `_nested.py` positional iteration (`col.to_list()` + offset cursor) correctly avoids the
  duplicate-index `col.at[row_idx]` bug (QA-3 F2).
- `_build_cdf` in `_categorical.py` rejects zero-width CDF slots early with a typed error
  rather than silently ignoring below-resolution weights.
- `synthesize._apply_null_probability` using `rng.seed(col_seed + i)` on a reused `random.Random`
  instance is the right pattern: same first-draw as `Random(col_seed + i)` but amortizes
  Mersenne Twister initialization.
