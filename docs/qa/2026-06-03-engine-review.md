# Engine QA Review — 2026-06-03

Reviewer: QA/performance pass (automated session)
Scope: `src/decoy_engine/` full pass, focus on determinism, correctness, and performance

---

## Summary

The determinism envelope (`_derive.py`, `_hkdf.py`) and the primary masking strategies (hash, shuffle, FPE, pool/faker) are architecturally sound. The biggest issues are two missed optimisation fixes in the hot sampler paths that the `_deterministic` Q6 fix already solved, a TypeError lurking in `_match_source_cardinality` for mixed-type columns, and a silent bijection break in FPE's `validate_luhn` mode. The formula `safe_eval` sandbox is not a real sandbox — this matters if operator formula configs could ever be supplied by untrusted parties.

---

## Findings

---

### F1 — HIGH | Performance | `_match_source_cardinality` uses `.iloc[i]` per row

**File:** `generation/pool/_sampler.py`, lines 241–246

```python
for i in range(n):
    if is_null.iloc[i]:          # ← O(n) pandas scalar unboxing
        output.append(pd.NA)
    else:
        output.append(value_map[source.iloc[i]])   # ← O(n) pandas scalar unboxing
```

The `_deterministic` path received the S21 Q6 fix — materialise `source.tolist()` and `source.isna().to_numpy()` once before the loop. `_match_source_cardinality` was not updated with the same fix. At 1 M rows with MATCH_SOURCE_CARDINALITY mode this is 2 M pandas C-level scalar unbox calls (each creates a temporary Python object) compared to two vectorised ops + one Python list traversal. The bottleneck is CPU/object-allocation, not I/O.

**Verify:** `python -m timeit` a 1 M-row `.iloc[i]` loop vs `.tolist()` + indexing; expect ≥ 2× wall-time difference.

**Fix:**

```python
def _match_source_cardinality(self, pool, n, source, rng, scale):
    ...
    src_list = source.tolist()          # materialise once
    is_null_arr = source.isna().to_numpy()
    output: list[Any] = []
    for i in range(n):
        if is_null_arr[i]:
            output.append(pd.NA)
        else:
            output.append(value_map[src_list[i]])
    return pd.Series(output)
```

---

### F2 — HIGH | Performance | `sample_bundle` deterministic path uses `.iloc[i]` per row

**File:** `generation/pool/_sampler.py`, lines 307–308

```python
for i in range(n):
    if is_null.iloc[i]:                    # ← O(n) pandas scalar unboxing
        ...
    canonical = _canonicalize_source(source.iloc[i])   # ← O(n)
```

Same Q6 miss as F1, but on the composite/bundle hot path. For a 50-column composite table of 500 K rows this costs ~25 s of avoidable overhead vs. ~2 s with `.tolist()`.

**Fix:** same materialisation pattern as `_deterministic`:

```python
src_list   = source.tolist()
is_null_arr = source.isna().to_numpy()
for i in range(n):
    if is_null_arr[i]:
        ...
    canonical = _canonicalize_source(src_list[i])
```

---

### F3 — HIGH | Correctness | `_match_source_cardinality`: `sorted(unique)` raises `TypeError` on mixed-type columns

**File:** `generation/pool/_sampler.py`, line 235

```python
source_uniques = sorted(source.dropna().unique())
```

`pd.Series.unique()` returns a numpy object array. When the column contains heterogeneous Python types (e.g., `numpy.int64` mixed with `str`, which can arise from object-dtype columns after CSV ingestion or nullable integer columns), `sorted()` raises:

```
TypeError: '<' not supported between instances of 'str' and 'int'
```

`_generate_reference_column` already has the fix pattern:

```python
try:
    ref_values = sorted(raw_pool)
except TypeError:
    ref_values = sorted(raw_pool, key=str)
```

Apply the same here.

**Fix:**

```python
try:
    source_uniques = sorted(source.dropna().unique().tolist())
except TypeError:
    source_uniques = sorted(source.dropna().unique().tolist(), key=str)
```

**Test:** `source = pd.Series([1, "a", 2, "b", None])` with `MATCH_SOURCE_CARDINALITY` — currently raises, must not after fix.

---

### F4 — HIGH | Correctness | FPE `validate_luhn=True` silently breaks the bijection

**File:** `transforms/fpe.py`, lines 288–289

```python
if validate_luhn and n >= 2:
    result = result[:-1] + _luhn_check_digit(result[:-1])
```

The Feistel permutation produces a bijection over the charset. Replacing the last character with the Luhn check digit collapses that bijection: two distinct source strings whose Feistel outputs differ only in the last character now map to the same masked output (since the check digit is a deterministic function of the first `n-1` characters). If the masked column is used as a joinability key (the primary use case of FPE), duplicate masked PANs cause FK integrity failures and incorrect join results.

There is no warning at the call site or in the return value to signal this.

**Minimum fix — emit a QualityWarning:**

```python
if validate_luhn and n >= 2:
    result = result[:-1] + _luhn_check_digit(result[:-1])
    # Emit a warning because Luhn correction breaks the bijection;
    # two source values can now share the same masked output.
```

Return this as a `QualityWarning` to the caller (alongside `StrategyError` if joinability is required) and document it in the strategy schema. Alternatively, gate `validate_luhn` on the plan explicitly declaring the column non-joinable.

**Test:** generate two 16-digit source strings whose Feistel output differs only in the final digit; assert `validate_luhn=True` produces a collision.

---

### F5 — HIGH | Correctness/Security | `safe_eval` `__builtins__: {}` is not a sandbox

**File:** `expressions.py`, line 88

```python
return eval(expr, globals_, locals_)  # noqa: S307
```

Setting `__builtins__: {}` in the globals dict is a commonly misunderstood technique. It does not prevent attribute traversal through existing objects in scope:

```python
# This executes arbitrary code despite __builtins__: {}
[c for c in ().__class__.__mro__[-1].__subclasses__()
 if c.__name__ == 'Popen'][0](['id']).communicate()
```

The formula scope exposes `str`, `int`, `float`, `round`, `min`, `max`, `len`, and Faker helpers — any of these are starting points for attribute climbing.

**Threat model caveat:** If formula expressions are exclusively configured by operators (internal staff who are already trusted with full system access), the practical risk is low. The concern becomes critical the moment formula text is editable by end-users or ingested from external sources (pipeline configs uploaded via API, customer-supplied templates).

**Fix options (in priority order):**
1. If formula authors are always trusted operators: document the threat model explicitly and add a comment to `safe_eval` so no future sprint widens the surface without review.
2. If any untrusted input path exists now or is planned: replace `eval` with a proper restricted evaluator (`asteval`, `simpleeval`, or a whitelist-only AST transformer).

**Immediate action:** audit every call site of `safe_eval` / `eval` in the engine and confirm whether any ingests user-controlled strings. Add a test that attempts the class-traversal escape and asserts it raises rather than executes.

---

### F6 — MEDIUM | Correctness | `Decimal` canonicalization ignores trailing-zero equivalences

**File:** `generation/pool/_canonicalize.py`, line 97

```python
if isinstance(value, Decimal):
    return str(value).encode("utf-8")
```

`str(Decimal("1.00"))` → `b"1.00"` and `str(Decimal("1"))` → `b"1"`. These are mathematically equal but produce different canonical bytes, so the same logical value entered with different precision yields different masked output. This breaks the joinability guarantee: a PK `Decimal("1.00")` in the parent table and a FK `Decimal("1")` in the child table will map to different hash tokens even though they refer to the same entity.

**Fix:**

```python
if isinstance(value, Decimal):
    return str(value.normalize()).encode("utf-8")
```

`Decimal.normalize()` collapses `1.00` → `1` and `1.0E+3` → `1E+3`. Note that `normalize()` converts `Decimal("0")` to `Decimal("0E+0")` in some implementations — add a special case or use `str(value.normalize().to_eng_string())` if that matters for your data.

**Test:** `derive(seed, ns, _canonicalize_source(Decimal("1.00"))) == derive(seed, ns, _canonicalize_source(Decimal("1")))` — must be True after fix, currently False.

---

### F7 — MEDIUM | Performance | `_generate_faker_column` per-row `faker_inst.seed_instance` mutates module-global `random`

**File:** `generators/columns.py`, lines 372–374

```python
for i in range(num_rows):
    row_rng.seed(row_seed)
    faker_inst.seed_instance(row_seed)   # ← mutates random.seed() module-globally
    values.append(provider_func(**faker_kwargs))
```

The comment at line 367 acknowledges this: "Faker.seed_instance still mutates module-level random.seed internally (Faker library limitation)". For a multi-column job where two faker columns share a process, column A's per-row Faker seeding clobbers the module-level RNG state that column B's `self._rng` and any other module-global `random.*` caller depends on between column A's rows. In the S9 pool path this is mitigated (Faker runs in a batch + the seeded pool seed is stable), but the legacy `_generate_faker_column` path has no such protection.

**Impact:** non-deterministic output from module-global `random` callers (e.g., the MASK_GLOBALS `randint`/`choice` bindings in `expressions.py` lines 53–55) interleaved with `_generate_faker_column` calls.

**Fix (legacy path):** if this generator is still actively used, wrap the per-row Faker generation in the `_FAKER_CALL_LOCK` that `synthesize.py` uses, and document that the legacy generator is not safe for concurrent multi-column generation in a single process. Prefer routing callers to the pool path.

---

### F8 — MEDIUM | Reliability | Formula eval errors silently null-fill with no volume threshold

**File:** `generators/columns.py`, lines 1232–1237 and 1279–1290

```python
except Exception as exc:
    self.logger.warning(f"Formula column {col_name!r} row {i} eval error: {exc}")
    values.append(None)
```

Every row where the formula raises produces a `None` in the output. For a formula with a broken expression (e.g., wrong variable name), this silently produces an all-`None` column that passes schema validation and downstream masking. The operator sees warnings in logs but no job failure.

**Fix:** track error count per column; if it exceeds a configurable threshold (default: 10 % of rows, or 1 for deterministic/non-nullable columns), raise `ColumnGenerationError` after the loop rather than returning a partially-nulled series.

---

### F9 — LOW | Performance | `FPEStrategy._encrypt` rebuilds `charset_set` per value

**File:** `transforms/fpe.py`, line 216

```python
charset_set = set(charset)
```

Called once per value inside the list comprehension on line 194. For a 1 M-row column with a 62-char charset, this allocates 1 M sets of 62 elements each. `charset` is constant for the lifetime of the `apply()` call.

**Fix:** compute `charset_set = set(charset)` once in `apply()` and pass to `_encrypt` as a parameter.

---

### F10 — LOW | Performance | `estimate_pool_bytes` loops all elements for object-dtype pools

**File:** `generation/pool/_value_pool.py`, lines 72–79

```python
for v in pool.values:
    if isinstance(v, str):
        total += min(len(v) * 4, 4096)
    else:
        total += 64
```

For a pool of 100 K strings this is 100 K Python loop iterations in the cache budget check path, which runs after every pool build. The dominant cost is the Python loop overhead, not the arithmetic.

**Fix (if pool sizes exceed 10 K in practice):** vectorise via `np.vectorize` or a numpy frompyfunc call, or cap the sampling to the first N elements for large pools:

```python
sample = pool.values[:min(len(pool.values), 1000)]
avg = sum(min(len(str(v)) * 4, 4096) for v in sample) / len(sample)
return int(avg * pool.size)
```

---

### F11 — NIT | Design | `MASK_GLOBALS` module-level RNG bindings not isolated

**File:** `expressions.py`, lines 53–55

```python
"randint": _random.randint,
"choice":  _random.choice,
"random":  _random.random,
```

`make_mask_globals(rng)` exists (line 59) but `MASK_GLOBALS` itself is the default and exposes module-global state. Any call site that passes `MASK_GLOBALS` directly (rather than `make_mask_globals(per_formula_rng)`) shares RNG state across formula evaluations. Audit all call sites; deprecate direct use of `MASK_GLOBALS` in favour of `make_mask_globals`.

---

## Performance Notes

| Path | Bottleneck | Complexity | Measurement |
|---|---|---|---|
| `_deterministic` (scalar, Q6-fixed) | CPU/HMAC | O(n) | Profile with `py-spy top` on a 1 M-row column |
| `_match_source_cardinality` (F1) | CPU/object-alloc | O(n) pandas iloc | `timeit` `.iloc[i]` loop vs `.tolist()` traverse |
| `sample_bundle` deterministic (F2) | CPU/object-alloc | O(n) pandas iloc | same |
| `_generate_faker_column` | CPU/Faker reseed | O(n) Faker seeding | `cProfile` on a 100 K-row faker column; Faker `seed_instance` should dominate |
| FPE per-value | CPU/HMAC × 8 rounds | O(n × len × rounds) | Baseline: `_fpe_pure` on 1 M 9-digit values; should be ~30 s without charset_set fix, ~27 s with F9 |

Profile command for F1/F2: `python -m cProfile -s cumulative your_job_script.py | head -40`

---

## Suggested Tests

1. **F1/F2 regression test:** run `PoolSampler.sample` in `MATCH_SOURCE_CARDINALITY` and `sample_bundle` deterministic mode on 100 K rows; assert wall time < 5 s (will fail before fix, pass after).

2. **F3 regression test:** `source = pd.Series([1, "a", 2, "b", None])`, call `PoolSampler.sample(..., mode=MATCH_SOURCE_CARDINALITY)` — currently raises `TypeError`, must succeed after fix.

3. **F4 bijection test:** enumerate all 2-char digit strings; apply FPE with `validate_luhn=True`; assert output has fewer than 100 distinct values (i.e., collisions exist). Document the expected collision rate.

4. **F5 escape test:** formula = `"[c for c in ().__class__.__mro__[-1].__subclasses__() if 'Popen' in c.__name__]"`, assert `safe_eval` raises rather than returning a list containing Popen.

5. **F6 Decimal normalisation test:** `_canonicalize_source(Decimal("1.00")) == _canonicalize_source(Decimal("1"))` — must be True after fix.

6. **F7 cross-column RNG contamination test:** generate column A (faker) and column B (categorical with module-global `random.choice`) in the same ColumnGenerator; generate B before A and after A; assert B's output is identical in both orderings.

7. **F8 formula error threshold test:** formula = `"undefined_name"`, `num_rows=1000` — assert the generator raises after fix rather than returning 1000 `None` values.

---

## What's Good

- **HKDF envelope (`_hkdf.py`, `_derive.py`):** RFC 5869 implementation is clean, pinned to reference vectors, and the length-prefixed injection-safe encoding for namespace + source is correct. The `DeriveContext` amortisation of HKDF is a good fix.
- **`_kahn_sorted` (`_runner.py`):** heapq-based Kahn with sorted tie-break is correct and replaces what the comment says was O(n² log n). The sorted children push keeps output byte-stable.
- **Pool immutability (`_value_pool.py`):** `numpy.setflags(write=False)` after build is exactly right — it makes in-place sort attempts raise loudly rather than silently re-keying the sampler.
- **FPE single-char fix (F2 2026-06-01 in code):** the QA-10 F2 fix for the single-character degenerate case (uniform rotation rather than value-dependent rotation) is correct.
- **`hkdf_extract` salt guard:** rejecting empty salt explicitly (rather than silently using the RFC all-zero default) is the right defensive posture.
- **`_derive_pool_seed`:** using a structured namespace `pool/{provider}/{locale}/{namespace}` for the pool-seed derivation avoids cross-context collisions cleanly.
- **Polars shuffle parity:** byte-identical permutation seed to the pandas path (`derive(job_seed, namespace, b"")` → `default_rng`) is correct.
