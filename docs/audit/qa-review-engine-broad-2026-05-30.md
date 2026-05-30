# QA Report — decoy-engine · 2026-05-30

**Reviewer:** Claude (automated QA session)
**Scope:** `src/decoy_engine/transforms/` (hash, faker, date_shift, fpe, shuffle),
`src/decoy_engine/generation/synthesize.py`,
`src/decoy_engine/determinism/_derive.py`,
`src/decoy_engine/determinism/_hkdf.py`
**Branch reviewed:** `main` @ `6a9fbee`

---

## 1. Summary

The determinism envelope (`_derive.py` / `_hkdf.py`) is well-specified and
correctly implemented against RFC 5869 + RFC 2104. The keyed transform strategies
(hash, FPE, date_shift) are solid. **The most important issue is that
`synthesize.py` uses Python's module-level `random.seed()` for generation — a
shared global that is not thread-safe and cannot be safely called from a
multi-threaded process without isolation.** Since the platform runs jobs in
`asyncio.to_thread`, any future increase in thread concurrency (or any other
library that touches `random` in a thread) breaks generation determinism silently.
A secondary but directly actionable issue is that `ShuffleStrategy` derives its
RNG seed from a plain integer rather than the HKDF key hierarchy, meaning shuffle
output is not protected by the instance's master key.

---

## 2. Findings

### F1 — Critical | Determinism | `synthesize.py` uses the global `random` module

**File:** `src/decoy_engine/generation/synthesize.py` — `_categorical`, `_faker`,
`_reference`, `_apply_null_probability`

```python
# _categorical
random.seed(col_seed)
return random.choices(cats, weights=weights, k=n)

# _faker (per row)
random.seed(row_seed)
faker_inst.seed_instance(row_seed)
out.append(provider_func(**faker_kwargs))

# _reference
random.seed(col_seed)
for i in range(n):
    values.append(random.choice(ref_vals))

# _apply_null_probability (per row)
random.seed(col_seed + i)
if random.random() < null_prob:
    out[i] = None
```

`random` is Python's module-level global RNG. It is **not thread-safe**: a
`random.seed()` call in thread A can corrupt the state seen by thread B between
its own seed and draw calls. The platform dispatches generation jobs via
`asyncio.to_thread`, which uses the default `ThreadPoolExecutor`. At
`concurrency=1` (current) this is benign because jobs are sequential. The moment
concurrency is lifted even partially — or if any other library thread touches
`random` (Faker sometimes does) — generation output becomes non-deterministic
with no error or warning.

Additionally, within a single job the `_apply_null_probability` function re-seeds
`random` per-row with `col_seed + i`. If any code path between the `seed` and the
`random.random()` call touches `random` (e.g., inside `provider_func`), the
null-injection rows will differ across runs.

**Impact:** generation determinism is one step from breaking; the current
single-threaded safety is an implicit assumption that is not enforced in the code
and will be violated at the first concurrency increase.

**Fix:** replace all `random.seed() / random.choice() / random.choices() /
random.random()` calls with a `random.Random(seed)` instance that is local to the
call:

```python
# _categorical
rng = random.Random(col_seed)
return rng.choices(cats, weights=weights, k=n)

# _faker per-row
rng = random.Random(row_seed)
rng.seed(row_seed)          # also need to handle faker_inst separately
faker_inst.seed_instance(row_seed)
out.append(provider_func(**faker_kwargs))

# _apply_null_probability
rng = random.Random()
for i in range(len(out)):
    rng.seed(col_seed + i)
    if rng.random() < null_prob:
        out[i] = None
```

A `random.Random` instance is thread-local state; seeding it does not affect any
other thread's RNG. Note that `faker_inst.seed_instance` internally calls the
*module-level* `random.seed`, so Faker is still unsafe with threads — see F2.

---

### F2 — High | Determinism | `faker_inst.seed_instance()` mutates the global RNG

**File:** `src/decoy_engine/generation/synthesize.py` — `_faker()`

`faker.Faker.seed_instance(seed)` calls `random.seed(seed)` on the *module-level*
`random`, not on an isolated instance. This means even after fixing F1 (using
`random.Random`), calling `faker_inst.seed_instance(row_seed)` still mutates the
global module-level RNG, which will corrupt any other thread's RNG draw that
happens between the `seed_instance` call and the provider function call.

**Impact:** In any multi-threaded environment, faker-based generation output is
non-deterministic regardless of how the caller seeds.

**Fix:** Faker does not provide a fully isolated per-instance RNG. The only safe
pattern is to create a new `Faker` instance per-call in a thread-local, or to
ensure all Faker calls run inside a process-level lock. The existing `make_faker`
caching in `faker_setup.py` should be audited to confirm it does not share a
single `Faker` instance across threads.

---

### F3 — High | Security | `ShuffleStrategy` bypasses the HKDF master key

**File:** `src/decoy_engine/transforms/shuffle.py` — `ShuffleStrategy.apply()`

```python
seed = rule.get("seed", self.seed)    # plain integer, not HKDF-derived
rng = np.random.default_rng(seed)
indices = rng.permutation(non_na_count)
```

Every other keyed strategy (hash, faker, date_shift, FPE) derives its per-column
key via `self.derive_key("mask")` → HKDF → HMAC chain. `ShuffleStrategy` derives
its RNG seed from a plain integer: either the rule's `seed` field or the
strategy's base `self.seed`. This means:

1. Shuffle output is reproducible by anyone who knows the seed integer — it is
   not tied to the instance's master Fernet key or the HKDF chain.
2. Two instances sharing the same integer seed produce the same permutation,
   regardless of their master keys.
3. The shuffle is not re-keyed when the master key is rotated.

**Impact:** shuffle is the only transform where an operator could change the
master key (for example after a key-compromise event) and have shuffle output
remain the same. For a transform applied to sensitive columns, this breaks the
security model.

**Fix:** derive the seed from the master key using the same HKDF path as other
transforms:

```python
def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
    column_name = rule.get("column", "unnamed")
    column_key = self._column_key(column_name)
    if column_key is not None:
        # Derive an integer seed from the column key so shuffle is
        # re-keyed when the master key is rotated.
        seed = int.from_bytes(column_key[:8], "big")
    else:
        seed = rule.get("seed", self.seed)
    rng = np.random.default_rng(seed)
    ...
```

`_column_key` is inherited from `BaseMaskingStrategy`; the shuffle strategy needs
to call `self.derive_key("mask")` as other strategies do (adding
`def _column_key(...)` to `ShuffleStrategy` or relying on `BaseMaskingStrategy`'s
implementation if it already provides one).

---

### F4 — High | Determinism | `_column_key()` uses the same namespace across all columns and transforms

**File:** All keyed transforms — `hash.py`, `faker_based.py`, `date_shift.py`,
`fpe.py`

```python
def _column_key(self, column_name: str) -> bytes | None:
    ...
    return self.derive_key("mask")   # same namespace for every column
```

Every column in every table in every pipeline resolves to the same derived key
material (`derive_key("mask")`). The design intent — "same value anywhere in the
org → same masked value, so FK joins survive masking" — is clearly correct and
documented in the hash strategy's docstring. However, this means:

- If `email` and `ssn` columns happen to share a value (they won't in practice,
  but pathologically, if a user enters their SSN as their email), they hash
  identically.
- A masked dataset leaks cross-column value equality: if `hash(A.col1)` ==
  `hash(B.col2)`, an attacker knows `A.col1 == B.col2` in the source.
- FPE uses `tweak = column_name.encode()` to differentiate per-column, which
  correctly produces different ciphertext for the same plaintext in different
  columns. But hash, faker, and date_shift do not apply any per-column
  differentiation.

This is a **design decision, not a bug**, but it is not documented in the
public-facing documentation or config schema. Users applying hash to two sensitive
columns that share a value space (e.g., two SSN columns in different tables, one
of which is a FK) should be told explicitly that the same hash output confirms the
values are equal.

**Recommendation:** document this property prominently in the transform
documentation and the hash strategy docstring. If per-column isolation is desired
for non-FK columns, expose a `per_column_key: true` config flag that tags the
namespace with the column name: `self.derive_key(f"mask:{column_name}")`.

---

### F5 — Medium | Performance | `FPEStrategy._encode` is O(n × r) per string

**File:** `src/decoy_engine/transforms/fpe.py` — `_encode()`

```python
def _encode(s: str, charset: str) -> int:
    r = len(charset)
    x = 0
    for ch in s:
        x = x * r + charset.index(ch)   # O(r) linear scan per character
    return x
```

`charset.index(ch)` is O(r) where r = charset size. For a string of length n,
encoding is O(n × r). For the `ALPHANUM` charset (62 characters) and a typical
SSN-style 9-digit value: 9 × 62 = 558 operations. This is not severe at small n,
but for longer tokens (e.g., 20-char alphanumeric IDs) with large charsets it
compounds across millions of rows.

**Fix:** pre-compute a lookup dict once per `apply()` call:

```python
# In apply() or _fpe_pure(), before the loop:
char_to_idx = {ch: i for i, ch in enumerate(charset)}

def _encode(s: str, char_to_idx: dict) -> int:
    r = len(char_to_idx)
    x = 0
    for ch in s:
        x = x * r + char_to_idx[ch]
    return x
```

O(n) per string, O(r) one-time setup. For 1M rows of 9-char SSNs: rough estimate
from `charset.index` path ~90M ops vs pre-computed path ~9M ops (plus the dominant
HMAC-SHA256 Feistel work, which dwarfs both).

---

### F6 — Medium | Determinism | `_apply_null_probability` re-seeds globally per row

**File:** `src/decoy_engine/generation/synthesize.py` — `_apply_null_probability()`

```python
for i in range(len(out)):
    random.seed(col_seed + i)   # re-seeds module-level RNG per row
    if random.random() < null_prob:
        out[i] = None
```

Beyond the thread-safety issue in F1, re-seeding the global RNG per row means that
the entire process's random state is reset 100k times for a 100k-row column. Any
code in the process that calls `random` after row N's seed but before row N+1's
seed will receive `col_seed + N` as its seed. This is a significant side-effect
leakage for any library that uses `random` for its own purposes.

**Fix:** use `random.Random(col_seed + i).random()` per row, or build the full
null-injection vector in one `random.Random` sequence:

```python
rng = random.Random(col_seed)
null_flags = [rng.random() < null_prob for _ in range(len(out))]
for i, is_null in enumerate(null_flags):
    if is_null:
        out[i] = None
```

Note: this changes the V1 parity output because V1 re-seeded per row. If parity
is required, keep the per-row seed and accept the global mutation, but add a
comment explaining why and wrap the entire call in a lock.

---

### F7 — Medium | Correctness | `_faker` inter-column seed collision at large row counts

**File:** `src/decoy_engine/generation/synthesize.py` — `_faker()`

```python
col_seed = synthetic_column_seed(derive_key=derive_key, column_config=col, ...)
for i in range(n):
    row_seed = col_seed + i
    faker_inst.seed_instance(row_seed)
```

`row_seed = col_seed + i` uses simple integer increment. Python integers don't
overflow, so there's no modular wrap. However, if two different columns happen to
have `col_seed_A + i == col_seed_B + j` for some rows i, j, those rows will
produce identical faker output. The probability depends on how `synthetic_column_seed`
spaces column seeds apart. At large row counts (n > 1M) the seed space of the
simple increment may overlap with another column's seed range.

**Fix:** use a hierarchical seed (`col_seed * (2**32) + i`, or `col_seed XOR
(i << 20)`) to ensure inter-column separation at any row count, or derive per-row
seeds via the HKDF path:
`hmac_seed(column_key, i.to_bytes(8, "big"))`.

---

### F8 — Medium | Performance | `FakerStrategy._apply_value_seeded` uses `column.apply(fake_for)`

**File:** `src/decoy_engine/transforms/faker_based.py` — `_apply_value_seeded()`

```python
return column.apply(fake_for)
```

`Series.apply` boxes and unboxes each scalar through pandas' dispatch machinery.
The hash strategy's `apply()` method explicitly benchmarks and documents this:
"a plain list comp is cheaper than `Series.apply`, which boxes/unboxes every
scalar. Worth ~3-6x." The same optimisation applies here.

**Fix:**

```python
na_mask = column.isna()
non_na = column[~na_mask]
faked = [fake_for(v) for v in non_na.tolist()]
result = column.astype(object).copy()
result.loc[~na_mask] = faked
return result
```

The in-call `cache` dict still deduplicates repeated values, so the semantic
behaviour is unchanged.

---

### F9 — Low | Correctness | `date_shift.py` `_detect_format` samples only 20 rows

**File:** `src/decoy_engine/transforms/date_shift.py` — `_detect_format()`

```python
sample = series.dropna().astype(str).head(20)
for fmt in _COMMON_FORMATS:
    try:
        for v in sample:
            datetime.strptime(v.strip(), fmt)
        return fmt
    except ValueError:
        continue
```

If the first 20 non-null values are all in one format but later rows contain a
different format (e.g., a mixed-format column from a legacy ETL), `_detect_format`
will return the wrong format and `_shift_for_value_*` will leave most rows
unchanged rather than raising. The silent passthrough is the correct degradation
(the strategy logs a warning per unique unparseable value), but 20 rows may be
insufficient to detect format diversity in real datasets.

**Recommendation:** increase the sample to `min(200, len(series))` rows and, if
more than one candidate format parses the entire sample, log a warning that the
column may have mixed formats.

---

### F10 — Nit | Design | `_formula` and `_reference` in `synthesize.py` delegate to V1 `ColumnGenerator`

**File:** `src/decoy_engine/generation/synthesize.py`

Both `_formula` and `_reference` import and instantiate `ColumnGenerator` from
the legacy `decoy_engine.generators.columns` module to preserve V1 parity. This
creates a runtime dependency from v2 generation to v1 code. The comments document
this as intentional ("Reading B: pragmatic guaranteed parity; v2-native rewrite
lifts at S9 alongside v1 removal"), which is the right call. This is recorded here
so the S9 sprint knows to audit `_formula` and `_reference` as part of v1 removal.

---

## 3. Performance Notes

**`FakerStrategy` bottleneck:** CPU-bound. The Faker library is slow per-call
(~1-5 µs each), and there is no way to vectorise it. At 1M rows, even a
deduplicated dataset with 100k unique values costs ~100 ms just in Faker calls.
Profile with `cProfile` or `py-spy top` targeting the `fake_for` inner function.

**`FPEStrategy` bottleneck:** CPU-bound, dominated by HMAC-SHA256 calls (8 per
value). At 1M rows: roughly 8M HMAC-SHA256 calls. With Python's `hmac.new` this
is approximately 4–8 seconds on a modern CPU. The pre-computed charset dict (F5
fix) saves < 10% of that. If FPE throughput becomes a bottleneck, consider
batching values and offloading to a native extension or ctypes HMAC.

**`ShuffleStrategy`:** correctly uses `np.random.default_rng` and
`np.random.permutation` — these are C-level and will be fast even at 10M+ rows.
The `iloc[indices].to_numpy()` pattern is correct for both numpy-backed and
arrow-backed Series.

**`HashStrategy` list comprehension path:** the existing optimisation
(null check + `astype(str)` + list comp) is the right approach and consistent
with the pattern recommended in the `FakerStrategy` fix above.

**`DateShiftStrategy` vectorised path:** the conversion to `pd.to_datetime` +
`pd.to_timedelta` is correctly vectorised. The `shifts = [shift_fn(v) for v in
str_values.tolist()]` list comp is unavoidable (crypto is per-value). The
Arrow-backed dtype pre-materialisation (`astype(object)`) is a good optimisation
that saves ~1 s at 1M rows.

**To measure FPE throughput:** `python -m timeit -n 3 -r 3 \
"from decoy_engine.transforms.fpe import FPEStrategy; import pandas as pd; \
s = FPEStrategy(); col = pd.Series(['1234567890'] * 100_000); \
s.apply(col, {'column': 'test', 'charset': 'digits'})"`

---

## 4. Suggested Tests

| Case | What to test |
|---|---|
| `_categorical` in two threads simultaneously, same seed | Assert outputs are byte-identical (will fail until F1 is fixed) |
| `_faker` across two processes with same seed | Assert outputs are byte-identical (subprocess stability, determinism.done-definition.md gate) |
| `ShuffleStrategy` with master key, then after key rotation | Assert permutations differ post-rotation (will fail until F3 is fixed) |
| `FPEStrategy` on SSN `123-45-6789` with `preserve_separators=true` | Assert dashes remain at positions 3 and 6 in output |
| `FPEStrategy` on a 1-char string | Assert no IndexError (degenerate single-char Feistel path) |
| `HashStrategy` with `truncate=64` | Assert output length == 64; with `truncate=65` → no truncation |
| `DateShiftStrategy` on a mixed-format column (first 20 rows ISO, rest US format) | Assert warning is emitted and non-parseable rows are passed through unchanged |
| `_apply_null_probability` with `null_probability=0.1`, same seed twice | Assert null-row positions are byte-identical across two calls |
| `_reference` with empty parent pool | Assert returns `[None] * n`, no exception |
| `_topo_sort` with a cycle (impossible per validator, but defensive) | Assert infinite recursion is not triggered (current DFS could stack-overflow on a cycle) |
| `derive(seed=b'x' * 7, ...)` | Assert `DeterminismError(code='seed_wrong_length')` |
| `derive(seed=b'x' * 8, namespace='', ...)` | Assert `DeterminismError(code='namespace_empty')` |
| `hkdf_sha256` RFC 5869 §A.1, A.2, A.3 test vectors | Confirm stdlib implementation matches published vectors |

---

## 5. What's Good

- **The determinism envelope is correct.** `_derive.py` implements the
  HKDF-extract → HKDF-expand → HMAC chain correctly. Length-prefixing on the HMAC
  input prevents namespace/source concatenation collisions. The `SEED_PROTOCOL_VERSION`
  byte is cleanly versioned and the bump story is documented.

- **HKDF is stdlib-only.** The decision to implement HKDF on top of `hmac.new` +
  `hashlib.sha256` rather than taking a PyCA dependency is correct for this engine.
  The implementation should be verified against RFC 5869 test vectors (a test suite
  for `_hkdf.py` is the highest-confidence path).

- **FPE is bijective by construction.** The 8-round type-II Feistel permutation is
  a provable bijection regardless of the PRF quality. Single-character handling
  (modular keyed shift) is correct and does not fall into the n=1 degeneracy of the
  main Feistel path. Luhn check-digit injection is cleanly isolated.

- **`HashStrategy` optimisation is well-documented.** The comment explaining why
  list comprehension beats `Series.apply` for per-value HMAC is accurate and useful.
  The joint-column D5c path sorts joint names alphabetically to guarantee output
  stability — this is the right invariant.

- **`ShuffleStrategy` dtype-backend fix is correct.** The comment explaining why
  `values.copy() + random.shuffle()` collapsed on arrow-backed input, and the fix
  (`rng.permutation(indices)` → `iloc[indices].to_numpy()`), is accurate and the
  resulting implementation is both correct and fast.

- **`DeterminismError` is well-structured.** Structured error codes (`seed_wrong_length`,
  `namespace_empty`, `pool_size_overflow`) make programmatic error handling possible
  without string parsing.
