# QA Review: nested strategy, shuffle, bucketize, orphan, DeriveContext, crypto

**Date:** 2026-06-06  
**Branch:** `qa/review-2026-06-06-nested-shuffle-bucketize-derive`  
**Reviewer:** QA/Performance agent  
**Scope:** First review of the following modules (not touched by any prior QA branch):

- `src/decoy_engine/execution/_strategies/_nested.py`
- `src/decoy_engine/execution/_strategies/_shuffle.py`
- `src/decoy_engine/execution/_strategies/_bucketize.py`
- `src/decoy_engine/execution/_strategies/_orphan.py`
- `src/decoy_engine/determinism/_derive.py` + `_hkdf.py`
- `src/decoy_engine/internal/crypto.py`
- `src/decoy_engine/internal/logging.py`

---

## 1. Summary

The determinism core (`_hkdf.py`, `_derive.py`, `derive()`/`derive_index()`) is sound -- RFC 5869 correctly implemented, well-tested against reference vectors, and the injection-proof length-prefixed envelope is a good design. The critical gap is in `_nested.py`: `NestedStrategyHandler` builds the child `ColumnSeed` with `provider=plan.provider` (the outer column's provider), so any `nested -> faker` configuration silently inherits `None` as the faker provider and crashes immediately with a `ValueError`. There is no path for the caller to supply the child faker provider name. The second most important finding is that `_bucketize.py` silently passes through original (unmasked) data when the config is invalid -- a V1 default that is dangerous in a masking engine because the job returns success while PII flows through. The `DeriveContext` has a design footgun where `derive_source` requires the caller to re-pass the namespace despite the context being built for a specific namespace; a wrong namespace diverges silently.

---

## 2. Findings

### F1 -- CRITICAL | Correctness

**File:** `src/decoy_engine/execution/_strategies/_nested.py:175-189`

**Issue:** `NestedStrategyHandler.run` builds the child `ColumnSeed` with `provider=plan.provider`, where `plan` is the outer (nested) column's seed:

```python
child_seed = ColumnSeed(
    namespace=plan.namespace,
    strategy=child_strategy_name,
    provider=plan.provider,   # <-- outer column's provider, not the child's
    ...
    provider_config=child_provider_config,
    ...
)
```

For a `nested` column, `plan.provider` is either `None` or whatever provider the outer column was configured with -- it is not the child strategy's provider name. When `child_strategy_name` is `"faker"`, `FakerStrategyHandler.run` guards:

```python
if plan.provider is None:
    raise ValueError(f"faker strategy on column {column!r} has no provider")
```

So `nested -> faker` always raises. There is also no path for the caller to supply the child faker provider name through `strategy_config`: the `strategy_config` dict is materialized as `child_provider_config` (which becomes `plan.provider_config` on the child), but `FakerStrategyHandler` reads `plan.provider` -- a separate field -- for the faker provider name. Any `provider` key in `strategy_config` ends up in `build_config` and is ignored by `PoolBuilder`.

**Why it matters:** `nested -> faker` is listed as a valid combination (the anti-recursion guard explicitly rejects only `nested -> nested`; the handler resolves `child_handler = SCALAR_HANDLERS.get(child_strategy_name)` and `faker` IS in `SCALAR_HANDLERS`). A HIPAA-compliant use case is masking the `$.name` leaf inside a JSON blob with a Faker `first_name` provider. Every such configuration currently crashes at runtime.

**Fix:** Extract the child provider from `child_strategy_config` and supply it as `provider=` in the child seed, stripping it from `child_provider_config` so it does not also land in `build_config`:

```python
child_provider = child_strategy_config.pop("provider", None)  # extract before packing
if isinstance(child_strategy_config, dict):
    child_provider_config = tuple(sorted(child_strategy_config.items()))
else:
    child_provider_config = ()

child_seed = ColumnSeed(
    namespace=plan.namespace,
    strategy=child_strategy_name,
    provider=child_provider,          # <-- child's own provider, not the parent's
    backend_type=plan.backend_type,
    backend_version=plan.backend_version,
    cardinality_mode=plan.cardinality_mode,
    deterministic=plan.deterministic,
    provider_config=child_provider_config,
    coherent_with=plan.coherent_with,
    technique_class=technique_class_for(child_strategy_name),
    when=None,
)
```

Note: `child_strategy_config` must be a local mutable copy before `.pop()`. The caller already does `child_strategy_config = cfg.get("strategy_config") or {}` which can return the original dict; add `child_strategy_config = dict(child_strategy_config)` before the pop.

**Verify:** Write a round-trip test: configure a nested column targeting `$.ssn` with `strategy: faker, strategy_config: {provider: ssn}`, run it, assert no `ValueError` and that the leaf value changed.

---

### F2 -- HIGH | Correctness

**File:** `src/decoy_engine/execution/_strategies/_bucketize.py:46-48`

**Issue:** When `_resolve_width` returns `None` (neither a valid `preset` nor a positive numeric `width` is supplied), the strategy silently passes through the original column unchanged:

```python
width = self._resolve_width(cfg)
if width is None:
    return df, []  # invalid config -> passthrough (V1 behavior)
```

No `QualityWarning` is emitted. No log message. The job completes normally. A column configured with `strategy: bucketize` but a misspelled `preset` name (e.g. `prset: by_decade`) or a zero/negative `width` will pass sensitive values through as if bucketize ran successfully.

**Why it matters:** In a masking context, silent passthrough of original data is a PII leak. The V1 comment justifies the behavior as backwards-compatible, but V2's contract -- per the done-definition and engineering best-practices -- is that misconfiguration should be loud, not silent. Validation at plan-compile time should ideally catch this, but if it reaches the strategy (e.g. due to a late-binding config), passthrough cannot be the failure mode.

**Fix:** Emit a `QualityWarning` so the operator sees the misconfiguration without a hard job failure (preserving V1 compatibility for existing pipelines):

```python
width = self._resolve_width(cfg)
if width is None:
    return df, [
        QualityWarning(
            code="bucketize_invalid_config",
            provider="bucketize",
            column=column,
            detail={"cfg": str(cfg)},
        )
    ]
```

Longer term: `PipelineConfig.model_validate` should reject a `bucketize` column with no `preset` and no valid `width` at plan-compile time (a `VALIDATION_CODES` entry + `validate_config` check), making this branch unreachable from a validated plan.

**Verify:** `BucketizeStrategyHandler().run(df, col, plan_with_no_width, ctx)` should return a `QualityWarning`; the column values should remain (passthrough), but the warning should be present.

---

### F3 -- HIGH | Correctness / Reliability

**File:** `src/decoy_engine/determinism/_derive.py:109-133` (`DeriveContext.derive_source`)

**Issue:** `DeriveContext` pre-computes the HMAC key from `(seed, namespace)` in `for_column`, but stores only `_hmac_key` -- not `namespace`. `derive_source` then requires the caller to re-supply `namespace`:

```python
def derive_source(self, namespace: str, source: bytes) -> bytes:
    namespace_bytes = namespace.encode("utf-8")
    hmac_input = (
        bytes([SEED_PROTOCOL_VERSION])
        + len(namespace_bytes).to_bytes(4, "big")
        + namespace_bytes
        + len(source).to_bytes(4, "big")
        + source
    )
    return hmac.new(self._hmac_key, hmac_input, hashlib.sha256).digest()
```

The docstring warns: "callers MUST pass the same namespace used in `for_column` or the output diverges from `derive(...)`". But there is no runtime assertion enforcing this. A caller that passes `namespace=""` (empty) or a different namespace string produces output that silently diverges from `derive(seed, correct_namespace, source)`. The divergence is undetectable without a separate oracle.

**Why it matters:** The purpose of `DeriveContext` is to be a drop-in performance optimization for `derive()`: same inputs, same output, fewer HKDF calls. A silent divergence is a determinism contract violation -- re-running the pipeline with the raw `derive()` call produces different masked values than the cached context path, breaking reproducibility.

**Fix:** Store the namespace in the frozen dataclass and assert equality on entry:

```python
@dataclass(frozen=True)
class DeriveContext:
    _hmac_key: bytes
    _namespace: str   # added

    @classmethod
    def for_column(cls, seed: bytes, namespace: str) -> DeriveContext:
        # ... existing validation ...
        key = hkdf_sha256(ikm=seed, salt=_SALT, info=namespace.encode("utf-8"), length=32)
        return cls(_hmac_key=key, _namespace=namespace)

    def derive_source(self, namespace: str, source: bytes) -> bytes:
        if namespace != self._namespace:
            raise DeterminismError(
                code="namespace_mismatch",
                message=(
                    f"derive_source called with namespace={namespace!r} but context "
                    f"was built for {self._namespace!r}. Output would silently "
                    "diverge from derive()."
                ),
            )
        # ... rest unchanged ...
```

Alternatively, remove the `namespace` parameter from `derive_source` entirely and use `self._namespace` internally. That is the cleaner design: the caller supplies the namespace once at construction, and it is bound for the lifetime of the context.

**Verify:** `DeriveContext.for_column(seed, "ns_a").derive_source("ns_b", b"value")` must raise `DeterminismError(code="namespace_mismatch")` after the fix.

---

### F4 -- MEDIUM | Correctness

**File:** `src/decoy_engine/execution/_strategies/_nested.py:227-246` (`_path_segments`, `_has_prefix_overlap`)

**Issue:** `_path_segments` splits `str(match.full_path)` on `.` to produce a tuple of path components. jsonpath_ng represents array index nodes as `[N]` (e.g. `Index(0).__str__() == "[0]"`), and `Child(Fields("items"), Index(0))` serializes as `items.[0]` -- with the dot before the bracket. Splitting `items.[0].ssn` by `.` yields `("items", "[0]", "ssn")`, which is correct and gives depth 3.

However, the outer-paren stripping logic handles only a single wrapper:

```python
if raw.startswith("(") and raw.endswith(")"):
    raw = raw[1:-1]
```

For compound recursive paths that jsonpath_ng wraps with double parens (e.g. `((items.name))`), only one layer is stripped, leaving `(items.name)` as the raw string. Splitting that on `.` gives `("(items", "name)")` -- the leading `(` fuses with `items` and the trailing `)` fuses with `name`. This corrupts depth comparison for recursive wildcards (`$..**`) on documents where the outer object also matches.

**Why it matters:** A false-negative in `_has_prefix_overlap` means the writeback loop applies parent and child writes in deepest-first order, but the parent-write erroneously clobbers the child-write because the overlap was not detected. A JSON field (`$.ssn`) whose ancestor (`$.*`) is also targeted would have the ancestor's masked value silently overwrite the SSN's masked value -- a PII leakage.

**Verify:** Construct a document `{"ssn": "123-45-6789"}`, use target `$.*` to trigger a match on the root value, and confirm that `_path_segments` produces the expected tuple without stray parens.

**Fix:** Replace the single-strip with an iterative paren-strip:

```python
def _path_segments(match: Any) -> tuple[str, ...]:
    raw = str(match.full_path).strip()
    while raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1].strip()
    return tuple(raw.split("."))
```

This is safe: the loop terminates because each iteration reduces `len(raw)` by at least 2, and the empty-string case (should never happen for a valid match) exits immediately.

---

### F5 -- MEDIUM | Correctness

**File:** `src/decoy_engine/execution/_strategies/_nested.py:199-218` (child seed `when` field)

**Issue:** The child seed hard-codes `when=None`:

```python
child_seed = ColumnSeed(
    ...
    when=None,
)
```

The parent's `plan.when` is the conditional expression that gates whether the masking strategy runs for a given row. If the parent has a `when` clause (e.g. `when: "col > 0"`), it is evaluated by the runner before dispatching to the strategy. That is correct: the nested handler is invoked only for rows that pass the `when` gate.

However, setting `when=None` on the child seed means that if the child strategy itself inspects `plan.when` internally (which current handlers do not, but the field exists and a future strategy might), it would silently ignore the parent's gate. More practically: `technique_class_for` returns the correct class for the child, but a future instrumentation hook that reads `child_seed.when` to correlate conditional masking telemetry would get `None` rather than the inherited condition.

**Why it matters:** Low impact today; all current child strategies ignore `plan.when`. But the field is public in `ColumnSeed` and this is a latent correctness gap.

**Fix:** Inherit the parent's `when`: `when=plan.when`. If the semantics of `when` are genuinely different for the child (e.g. the child operates on extracted leaf values which have no row concept), document why `None` is correct rather than leaving it as an unexplained hardcoded value.

---

### F6 -- MEDIUM | Design

**File:** `src/decoy_engine/execution/_strategies/_shuffle.py:26-35`

**Issue:** The non-deterministic shuffle derives the seed from `ctx.job_seed` + `plan.namespace`, using the empty bytes source `b""`. The seed derivation path is:

```python
seed_int = int.from_bytes(derive(ctx.job_seed, plan.namespace, b"")[:8], "big")
rng = np.random.default_rng(seed_int)
```

The empty source `b""` is intentional: shuffle is a whole-column operation, not per-row. But the seed is the same for every run of the same job with the same namespace. This means:

1. Re-running the same job twice on a table with the same rows produces the same permutation -- correct.
2. Re-running the same job on a table with DIFFERENT rows (e.g. new rows added) also produces the same permutation SHAPE (same relative order of values), because the permutation is derived purely from the seed, not from the source values. A newly added value that happens to be inserted at position k will receive a permuted value from a different position, but its relative position in the output is stable across runs. This is the documented behavior for shuffle.

However: the `rng.permutation(len(non_na_values))` draws from the RNG a number of times proportional to the number of non-null values. If the number of non-null values changes between runs (rows added or deleted), the permutation is completely different, not just extended. A pipeline that assumed row-stable shuffle ("the same source value always maps to the same output") would see unstable output as the table grows.

**Why it matters:** Not a bug in the current spec (shuffle is documented as a within-run permutation, not a stable value-to-value mapping). But operators may not realize this. A comment clarifying the instability when row counts change would prevent misuse.

**Fix (documentation):** Add to the module docstring:

```
Stability note: the permutation is seeded from (job_seed, namespace) and
is stable within a single run (same seed -> same permutation). Across runs
where the number of non-null rows changes, the permutation is entirely
re-derived and there is no row-stable value mapping. Use a keyed strategy
(hash, faker in deterministic mode) when you need source-to-output stability
across incremental runs.
```

---

### F7 -- LOW | Correctness

**File:** `src/decoy_engine/internal/crypto.py:62-70` (`hmac_seed`)

**Issue:** `hmac_seed` returns `0` when `value is None`:

```python
def hmac_seed(key: bytes, value: Any) -> int:
    if value is None:
        return 0
```

The caller uses this to seed `Faker.seed_instance(0)`. Two problems:

1. Any two distinct callers passing `None` get the same seed (0), potentially generating duplicate synthetic values for null-originated rows -- breaking the "each row should map to a unique output" expectation in high-cardinality columns.
2. `Faker.seed_instance(0)` is a well-known fixed point; combined with the deterministic faker, `None` rows always produce the first output of the provider's sequence, which is predictable and thus slightly weaker than a random-looking value.

FakerStrategyHandler preserves source nulls via `na_mask`, so `None` should never reach `hmac_seed` in the normal path. The `-> 0` is a silent sentinel for a call that should not occur. A hard error would expose the bug earlier.

**Fix:**

```python
def hmac_seed(key: bytes, value: Any) -> int:
    if value is None:
        raise ValueError(
            "hmac_seed: value must not be None; null rows should be "
            "filtered at the strategy level before computing HMAC seeds."
        )
    msg = str(value).encode("utf-8", errors="replace")
    return int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest()[:4], "big")
```

If backwards compatibility requires a sentinel, change the return to `-1` (invalid as a `seed_instance` argument) so an accidental use will raise rather than silently produce a weak seed.

---

### F8 -- LOW | Reliability

**File:** `src/decoy_engine/internal/logging.py:21-27` (`get_logger` singleton)

**Issue:** The module-level singleton pattern is a non-atomic check-then-act:

```python
_LOGGER_INSTANCE = None

def get_logger(config=None):
    global _LOGGER_INSTANCE
    if _LOGGER_INSTANCE is None:          # read
        _LOGGER_INSTANCE = _create_logger(config)  # write
    elif config:
        _configure_logger(_LOGGER_INSTANCE, config)
    return _LOGGER_INSTANCE
```

Under CPython's GIL, the assignment is effectively atomic and the worst case is two `_create_logger` calls where the second overwrites the first -- both are empty and equivalent. Under free-threaded CPython (PEP 703, active in 3.13+) this is a genuine race. The same non-atomic singleton pattern has been flagged in six prior QA reviews for other modules; the recommended fix is `functools.lru_cache(maxsize=None)`.

**Why it matters:** Low impact today (single-threaded startup). Systemic pattern that should be resolved in one pass.

**Fix:**

```python
import functools

@functools.lru_cache(maxsize=None)
def _get_default_logger():
    return _create_logger(None)

def get_logger(config=None):
    if config:
        logger = _get_default_logger()
        _configure_logger(logger, config)
        return logger
    return _get_default_logger()
```

Note: `_configure_logger` clears all handlers before adding new ones (line 57: `logger.handlers.clear()`). This means a `get_logger(config=new_config)` call on the singleton silently removes any user-added handlers. Document this behavior or guard against it.

---

### F9 -- LOW | Design

**File:** `src/decoy_engine/execution/_strategies/_orphan.py:56-65`

**Issue:** `OrphanPolicy.PRESERVE` and `OrphanPolicy.WARN` share the same writeback loop (both keep the source key unmasked). The condition is:

```python
# PRESERVE and WARN both keep the source key unmasked.
for pos, key in zip(orphan_positions, orphan_keys, strict=True):
    masked[pos] = key
warnings: list[QualityWarning] = []
if policy is OrphanPolicy.WARN:
    warnings.append(...)
```

The `masked[pos] = key` line assigns the source key tuple (e.g. `("123-45-6789",)`) to the output. For a WARN orphan, the source PII key flows into the output. The docstring states this is by spec (PRESERVE = keep original source key). However, there is no comment explaining the security implication: an orphan row in WARN mode has unmasked PII in the output. An operator reading "WARN" might assume the value was masked with a warning, not that it was left completely unmasked.

**Why it matters:** Not a bug, but a documentation gap that could lead to compliance failures if an operator misreads WARN as "masked with a warning" rather than "passed through unmasked with a warning".

**Fix (documentation):** Add a comment at the writeback point:

```python
# PRESERVE and WARN both keep the source key unmasked (original PII preserved).
# WARN emits one aggregated QualityWarning per edge so the operator is notified,
# but the output value is NOT masked. Operators should treat WARN as "allow one
# source of truth for orphan PII" and audit Storm output before signing off.
for pos, key in zip(orphan_positions, orphan_keys, strict=True):
    masked[pos] = key
```

---

### F10 -- NIT | Performance

**File:** `src/decoy_engine/execution/_strategies/_nested.py:117-128`

**Issue:** The leaf collection loop builds `leaf_values: list[Any]` by iterating `per_position_state` and appending each match's `.value`. The `temp_df = pd.DataFrame({temp_col: leaf_values})` then materializes a DataFrame just to pass to the child handler. For child strategies that accept any Python list (not requiring a DataFrame column specifically), this DataFrame construction adds overhead.

This is low impact in practice: the leaf count per column is bounded by `num_rows * max_matches_per_cell`, and DataFrame construction at that scale is O(n). The design is correct and the pattern is consistent with how other adapters use the execution interface. Mentioning it for completeness.

**No fix required.** If profiling reveals this as a bottleneck, the child strategy interface would need to accept a list directly -- a larger interface change outside this module's scope.

---

## 3. Performance Notes

| Module | Bottleneck | How to measure |
|--------|-----------|----------------|
| `_nested.py` | CPU -- Python row-loop with `json.loads`/`json.dumps` per cell + jsonpath eval | `python -m cProfile -s cumtime` on a 100K-row nested-JSON column; JSON parse should dominate |
| `_nested.py:_has_prefix_overlap` | CPU -- O(n^2) pair scan over matches per cell | Irrelevant in practice (n < 50 per cell); no action needed unless jsonpath expressions with thousands of matches are a use case |
| `_shuffle.py` | CPU -- `rng.permutation(n)` then Python scatter loop | numpy permutation is fast; the Python scatter loop (`for offset, position in enumerate(...)`) is the bottleneck for large n. Vectorizable via `out_arr = np.empty(n, dtype=object); out_arr[non_na_positions] = permuted; out_arr[na_mask] = None` |
| `_bucketize.py` | Negligible -- already vectorized via numpy/pandas | Correct; no action |
| `_orphan.py` | CPU -- Python loop over orphan positions | Orphan count is typically small; no action |
| `determinism/_derive.py:DeriveContext` | HKDF cost amortized correctly; per-row HMAC is the expected O(n) cost | Profile per-column HMAC cost with `timeit` if 1M-row columns are in scope |

For `_shuffle.py`, the scatter loop can be vectorized without changing the algorithm:

```python
out_arr = np.empty(len(source), dtype=object)
out_arr[non_na_positions] = permuted
na_mask_positions = np.where(na_mask)[0]
if len(na_mask_positions):
    out_arr[na_mask_positions] = None
df[column] = pd.Series(out_arr, dtype=object, index=df.index)
```

This eliminates the Python per-row loop entirely. Estimated throughput gain at 1M rows: 5-10x (consistent with the `.iloc`-loop -> numpy vectorization gains observed in 06-04 reviews). Verify with `timeit` before quoting.

---

## 4. Suggested Tests

| Test | Module | What to verify |
|------|--------|----------------|
| `test_nested_faker_child_provider_propagated` | `_nested.py` | Configure `strategy: nested, strategy_config: {provider: ssn}` on a column containing `{"id": 1, "ssn": "123-45-6789"}`; target `$.ssn`; assert no exception and the leaf value is replaced |
| `test_nested_faker_missing_provider_raises` | `_nested.py` | Omit `provider` from `strategy_config` for a faker child; assert `ValueError` or `StrategyError` with a clear message (not a raw `None` provider crash) |
| `test_bucketize_invalid_preset_emits_warning` | `_bucketize.py` | `BucketizeStrategyHandler().run(df, col, plan_with_preset="typo", ctx)` returns non-empty warnings; column is unchanged |
| `test_bucketize_zero_width_emits_warning` | `_bucketize.py` | Same but `width=0`; same assertion |
| `test_derive_context_namespace_mismatch_raises` | `_derive.py` | `DeriveContext.for_column(seed, "ns_a").derive_source("ns_b", b"v")` raises `DeterminismError(code="namespace_mismatch")` |
| `test_derive_context_output_matches_scalar_derive` | `_derive.py` | `DeriveContext.for_column(seed, ns).derive_source(ns, src) == derive(seed, ns, src)` for 100 random inputs |
| `test_nested_path_segments_no_stray_parens` | `_nested.py` | `_path_segments(match)` for a recursive-descent match never produces a segment starting or ending with `(` or `)` |
| `test_nested_double_paren_path_stripped` | `_nested.py` | Construct a mock match whose `full_path.__str__` returns `((user.name))`; assert `_path_segments` returns `("user", "name")` |
| `test_orphan_warn_output_is_unmasked` | `_orphan.py` | WARN policy on an orphan key; assert the output value equals the input source key (not a masked value) and a warning is present |
| `test_shuffle_vectorized_null_preservation` | `_shuffle.py` | Series with 30% nulls; after shuffle, null positions are still null, non-null multiset is preserved, and non-null positions are shuffled |
| `test_hmac_seed_none_raises` | `internal/crypto.py` | `hmac_seed(key, None)` raises `ValueError` (after fix) |
| `test_hmac_hex_none_returns_none` | `internal/crypto.py` | `hmac_hex(key, None)` returns `None` (existing behavior; regression guard) |

---

## 5. What's Good

- **`_nested.py` QA-3 F2 positional iteration fix** is well-executed: replacing index-label-keyed iteration with positional `col.to_list()` + `iloc`-free writeback correctly handles duplicate-index DataFrames (a real pandas gotcha).
- **`_nested.py` QA-3 F14 deepest-first writeback** is the right approach for overlapping-path masking. The sort key (`-len(_path_segments(m))`) is simple and correct for the tree depth comparison.
- **`_nested.py` config error promotion to `StrategyError`** (QA-3 F12): raising immediately for missing `target` or `strategy` instead of returning passthrough is the correct security posture. The error messages are specific and actionable.
- **`_shuffle.py` Q13 object-dtype fix**: explicit `dtype=object` in the output `pd.Series` is the right guard against int+None->float64 coercion.
- **`_orphan.py` FAIL/REMAP/WARN/PRESERVE dispatch** is clean and the `strict=True` zip guards against length mismatches between orphan positions and keys.
- **`_bucketize.py` preset/width validation** in `_resolve_width` correctly rejects booleans before checking `isinstance(raw, int)` (since `bool` is a subclass of `int` in Python) -- a subtle but important guard.
- **`determinism/_hkdf.py` RFC 5869 implementation** is minimal and correct. The `hkdf_extract` empty-salt rejection (QA-10 F12) is a good defensive guard that prevents silent degradation to the all-zero PRK. Reference-vector tests against §A.1/A.2/A.3 are the right verification approach.
- **`determinism/_derive.py` SEED_PROTOCOL_VERSION** versioning with detailed commit-message-style comments per bump is excellent practice -- makes coordination between manifests and the engine obvious.
- **`internal/crypto.py` `hmac_hex` and `hmac_seed`**: using `hmac.new` (stdlib, constant-time) rather than SHA256 directly is the correct security choice. The `DeprecationWarning` on `deterministic_hash` (QA-internal F12) is a good machine-readable signal for CI tools that treat warnings as errors.
- **`internal/logging.py` F3 PermissionError fallback** for read-only filesystems (Docker/K8s) is the right resilience pattern: degrade to console logging gracefully rather than crashing the process.
