# QA Review — Session 4: providers_v2, config, storm, internal, expressions, relationships, validation

**Date:** 2026-06-02  
**Reviewer:** Claude (QA agent, session 4)  
**Branch:** `qa/review-2026-06-02-providers-config-storm`  
**Scope:** Areas not covered by sessions 1-3 (date_shift, fpe, synthesize, discovery, when_gate, _pandas_adapter, _runner, _transforms, _graph, context, sdk, connectors/s3, determinism, generation, plan, quality)

Files examined:
- `src/decoy_engine/providers_v2/_faker_adapter.py`
- `src/decoy_engine/providers_v2/_registry.py`
- `src/decoy_engine/providers_v2/_real_registry.py`
- `src/decoy_engine/internal/faker_setup.py`
- `src/decoy_engine/internal/crypto.py`
- `src/decoy_engine/internal/logging.py`
- `src/decoy_engine/internal/base.py`
- `src/decoy_engine/config/_pipeline.py`
- `src/decoy_engine/config/_tables.py`
- `src/decoy_engine/config/_transforms.py`
- `src/decoy_engine/relationships/_graph.py`
- `src/decoy_engine/relationships/_namespace.py`
- `src/decoy_engine/storm/profiler.py`
- `src/decoy_engine/expressions.py`
- `src/decoy_engine/errors.py`
- `src/decoy_engine/validation/_config.py`

---

## 1. Summary

The core masking/generation logic is well-structured and the prior QA sessions have hardened the hot paths. This session surfaces one **Critical** determinism violation in the V2 Faker adapter that affects pool-built non-deterministic columns, two **High** thread-safety gaps in the provider registry and logger, and a cluster of **Medium** issues around RNG state sharing in formula expressions, namespace parsing for schema-qualified names, and bare exception catching in the validation layer. Nothing here is a show-stopper, but the Critical item must be fixed before S5 PoolAdapter lands in production.

---

## 2. Findings (ranked)

---

### F-1 — CRITICAL · Determinism  
**`providers_v2/_faker_adapter.py`, `generate_batch()` permanently seeds the shared Faker instance**

```python
# _faker_adapter.py ~line 175
if spec.seed is not None:
    self._faker(spec.locale).seed_instance(int.from_bytes(spec.seed, "big"))
return [self._faker_call(provider, spec) for _ in range(count)]
```

`FakerAdapter` caches one `Faker` instance per locale in `self._faker_instances`. `seed_instance()` permanently advances that instance's internal RNG. The default registry is a singleton (`get_default_registry()`), which means every pipeline shares the same `FakerAdapter` and the same `_faker_instances` dict. 

**Impact:** Once any pool-build call seeds `en_US` Faker, every subsequent `_faker_call()` in ANY job sharing that process — including calls on columns declared as non-deterministic — produces values that are a deterministic continuation of that seeded sequence, not fresh random values. Two back-to-back jobs with different seeds but the same column schema will produce different non-deterministic column values for job B depending purely on the seed used by job A. This violates the S4 contract ("seed=None stays non-deterministic") and is exactly the kind of hidden nondeterminism the application context classifies as Critical.

**Verify:** Run two jobs sequentially — job A with any pool-eligible column, job B with a purely non-deterministic column on the same locale. Compare job B's output with a fresh-process baseline. They will differ.

**Fix:** For seeded batch builds, use a temporary Faker instance rather than mutating the cached one:

```python
def generate_batch(self, provider, *, spec, count):
    if spec.deterministic:
        raise ProviderError(code="capability_violation", message="...")
    if spec.seed is not None:
        # Use a throwaway instance so the shared cache is not contaminated.
        tmp = faker_module.Faker(spec.locale or self._default_locale)
        tmp.seed_instance(int.from_bytes(spec.seed, "big"))
        return [self._faker_call_on(tmp, provider, spec) for _ in range(count)]
    return [self._faker_call(provider, spec) for _ in range(count)]
```

Factor out `_faker_call_on(fake, provider, spec)` that accepts an explicit Faker instance rather than calling `self._faker(spec.locale)`. The existing `_faker_call` stays as the default (cached-instance) path.

---

### F-2 — HIGH · Determinism  
**`expressions.py`, `MASK_GLOBALS` binds module-level RNG; formula columns share state**

```python
# expressions.py
MASK_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    ...
    "randint": _random.randint,   # module-level random
    "choice": _random.choice,
    "random": _random.random,
}
```

`make_mask_globals(rng)` exists as the correct per-formula factory (QA-1 M21), but it is only effective if every `FormulaStrategy` caller switches to it. Any caller still using the module-level `MASK_GLOBALS` dict directly shares module-global RNG state with every other formula column in the same process. Column B's output in a given run depends on how many times column A called `randint` before B ran.

**Impact:** Non-determinism within a single run: running the same pipeline twice on the same process will produce different formula values if RNG state leaks between calls. This is a Critical property violation masked by a Medium code-layer risk: the fix exists, adoption is the gap.

**Verify with Hypothesis:**
```python
@given(st.integers(0, 1000), st.integers(0, 1000))
def test_formula_determinism(seed1, seed2):
    # Run formula with MASK_GLOBALS twice with same inputs → must match
    # Then run in different order and confirm output independence
    ...
```

**Fix:** Audit every call site of `safe_eval(..., MASK_GLOBALS, ...)`. Replace with:
```python
rng = random.Random(formula_seed)  # formula_seed derived from pipeline seed + column name
globals_ = make_mask_globals(rng)
result = safe_eval(expr, globals_, locals_)
```
Remove or deprecate the plain `MASK_GLOBALS` export once all callers are migrated.

---

### F-3 — HIGH · Reliability  
**`providers_v2/_faker_adapter.py`, `_V2_CUSTOM_PROVIDERS` dict is unprotected; TOCTOU race on hot path**

```python
# _faker_adapter.py
_V2_CUSTOM_PROVIDERS: dict[str, Callable[[Any], Any]] = {}

def register_faker_provider_v2(name, fn):
    _V2_CUSTOM_PROVIDERS[name] = fn          # no lock

def _faker_call(self, provider, spec):
    if provider in _V2_CUSTOM_PROVIDERS:     # check
        return _V2_CUSTOM_PROVIDERS[provider](...)  # access — two bytecodes
```

V1's registry in `faker_setup.py` uses `_PROVIDER_LOCK` consistently (F1 2026-06-01 fix). V2 does not. A concurrent `_unregister_faker_provider_v2(name)` between the `in` check and the `[provider]` access raises `KeyError` on the hot masking path. Under the platform's `atomic_swap_db_providers` pattern, this window is realistic.

**Fix:** Import and reuse `_PROVIDER_LOCK` from `faker_setup`, or define a dedicated lock in `_faker_adapter.py`:
```python
_V2_LOCK = threading.Lock()

def register_faker_provider_v2(name, fn):
    with _V2_LOCK:
        _V2_CUSTOM_PROVIDERS[name] = fn

def _faker_call(self, provider, spec):
    with _V2_LOCK:
        fn = _V2_CUSTOM_PROVIDERS.get(provider)
    if fn is not None:
        return fn(self._faker(spec.locale))
    ...
```

---

### F-4 — HIGH · Reliability  
**`internal/logging.py`, `get_logger()` singleton and `_configure_logger()` not thread-safe**

```python
_LOGGER_INSTANCE = None  # module global

def get_logger(config=None):
    global _LOGGER_INSTANCE
    if _LOGGER_INSTANCE is None:          # TOCTOU
        _LOGGER_INSTANCE = _create_logger(config)
    elif config:
        _configure_logger(_LOGGER_INSTANCE, config)  # calls handlers.clear() — not atomic
    return _LOGGER_INSTANCE

def _configure_logger(logger, config):
    if logger.handlers:
        logger.handlers.clear()   # not thread-safe
```

Two threads calling `get_logger()` simultaneously when `_LOGGER_INSTANCE is None` will both call `_create_logger()`, opening duplicate file handles. `logger.handlers.clear()` is not atomic; a concurrent log write reading `logger.handlers` can see an empty list mid-clear, causing a message to be dropped.

**Fix:**
```python
_LOGGER_LOCK = threading.Lock()

def get_logger(config=None):
    global _LOGGER_INSTANCE
    with _LOGGER_LOCK:
        if _LOGGER_INSTANCE is None:
            _LOGGER_INSTANCE = _create_logger(config)
        elif config:
            _configure_logger(_LOGGER_INSTANCE, config)
    return _LOGGER_INSTANCE
```
`_configure_logger` should hold the lock for the duration of handler clear + re-add. Consider Python's `logging.config.dictConfig()` pattern instead of manual handler manipulation.

---

### F-5 — HIGH · Correctness  
**`validation/_config.py`, bare `except Exception` hides engine bugs as PipelineValidationError**

```python
def _select_validator(data: dict) -> Any:
    if data.get("version") == 1:
        from decoy_engine.config import PipelineConfig
        try:
            PipelineConfig.model_validate(data)
        except Exception as exc:  # catches ImportError, AttributeError, MemoryError, ...
            raise PipelineValidationError(str(exc)) from exc
        return None
```

If `PipelineConfig.model_validate()` triggers an engine bug (e.g., `AttributeError` from a malformed model validator, `ImportError` from a missing dependency), it surfaces to the caller as `PipelineValidationError` with a confusing message rather than propagating as the real exception. Operators and CI will diagnose it as a config error and waste time fixing YAML.

**Fix:** Catch `pydantic.ValidationError` specifically:
```python
import pydantic
try:
    PipelineConfig.model_validate(data)
except pydantic.ValidationError as exc:
    raise PipelineValidationError(str(exc)) from exc
# Other exceptions propagate as-is
```

---

### F-6 — MEDIUM · Data  
**`relationships/_namespace.py`, namespace entry parsing fails for schema-qualified table names**

```python
# _namespace.py ~line 190
table, col_part = entry.split(".", 1)
cols = tuple(col_part.split("__")) if "__" in col_part else (col_part,)
```

A namespace `declared_by` entry like `"public.users.email"` (schema-qualified) splits as `table="public"`, `col_part="users.email"`. The column would be mapped to the wrong (table, columns) key, silently producing a missing or wrong namespace at compile time.

**Impact:** Any pipeline using schema-qualified table names in the `namespaces` block will silently produce `namespace_missing` errors at plan-compile time, or worse, resolve to a wrong namespace and mask FK columns under different namespaces than their parents — breaking the same-FK → same-mask invariant.

**Verify:** Does the engine accept PostgreSQL schema-qualified names (`schema.table`) anywhere? If yes, the delimiter scheme for namespace entries must be revisited. If table names are guaranteed to contain no dots, add a validation step in `NamespaceConfig` and document the constraint.

**Fix (if schema-qualified names are out of scope):** Add a Pydantic validator in `NamespaceConfig`:
```python
@field_validator("declared_by", mode="before")
@classmethod
def _no_dots_in_table(cls, v):
    for entry in v:
        if entry.count(".") != 1:
            raise ValueError(f"namespace entry {entry!r} must be 'table.column' with no schema prefix")
    return v
```

---

### F-7 — MEDIUM · Reliability  
**`providers_v2/_registry.py`, `get_default_registry()` singleton not thread-safe under concurrent first call**

```python
_DEFAULT_REGISTRY: ProviderRegistry | None = None

def get_default_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:      # TOCTOU
        # ... expensive build: FakerAdapter, 9 DecoyNative adapters,
        #     Mimesis check, 6 CompositeAdapters ...
        _DEFAULT_REGISTRY = ProviderRegistry(bindings)
    return _DEFAULT_REGISTRY
```

Under concurrent first-call (e.g., two jobs starting simultaneously in a threaded platform runner), both threads build the full registry. The builds are idempotent but expensive (9 adapter constructions, optional Mimesis import). One registry is discarded. No correctness bug, but the thundering-herd latency spike on cold start is avoidable.

**Fix:**
```python
_REGISTRY_LOCK = threading.Lock()

def get_default_registry() -> ProviderRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is not None:   # fast path, no lock
        return _DEFAULT_REGISTRY
    with _REGISTRY_LOCK:
        if _DEFAULT_REGISTRY is None:   # double-checked locking
            _DEFAULT_REGISTRY = _build_registry()
    return _DEFAULT_REGISTRY
```

---

### F-8 — MEDIUM · Reliability  
**`internal/logging.py`, `ProgressLogger.current += increment` not thread-safe; `time.time()` not monotonic**

Two issues in `ProgressLogger`:

1. `self.current += increment` is a read-modify-write with no lock. If a future multi-threaded driver updates progress from multiple threads, counts will be silently under-reported. The class docstring acknowledges this; the risk grows as the engine adds parallel column processing.

2. `time.time()` is used for elapsed time. On NTP-adjusted systems or containers with clock drift, `time.time()` can go backwards, producing negative elapsed time and infinite ETA estimates. Use `time.monotonic()` instead for all elapsed-time measurements.

**Fix:**
```python
import threading

class ProgressLogger:
    def __init__(self, logger, total, message="Progress"):
        ...
        self._lock = threading.Lock()
        self.start_time: float | None = None

    def start(self):
        self.start_time = time.monotonic()   # not time.time()
        ...

    def update(self, increment=1):
        with self._lock:
            self.current += increment
        elapsed = time.monotonic() - self.start_time if self.start_time else 0
        ...
```

---

### F-9 — LOW · Security  
**`internal/crypto.py`, `deterministic_hash()` should be removed, not merely deprecated**

```python
def deterministic_hash(value: Any, seed: int = 0) -> str | None:
    warnings.warn("...", DeprecationWarning, stacklevel=2)
    ...
    return hashlib.sha256(f"{value}{seed}".encode()).hexdigest()
```

This function concatenates value and seed as strings before hashing — a reversible pattern for any known value (brute-force the seed). The `DeprecationWarning` is machine-readable if CI treats warnings as errors, but it remains callable. The CODEMAP says S9 removes the V1 strategy stack; this function should be deleted at that milestone rather than lingering as an invitation for copy-paste into new code.

**Fix:** Delete at S9. In the meantime, add a CI check that no import of `deterministic_hash` exists outside of its own test.

---

### F-10 — LOW · Design  
**`internal/faker_setup.py`, `make_faker()` fallback uses `Faker()` without explicit locale**

```python
except (AttributeError, ValueError, TypeError) as exc:
    _log.warning("make_faker: locale %r is invalid...", locale, ...)
    return Faker()   # implicit en_US
```

`Faker()` with no arguments defaults to `en_US` in current Faker versions, but this is implicit. Use `Faker("en_US")` for explicit documented intent and resilience against future Faker API changes.

---

### F-11 — NIT · Design  
**`storm/profiler.py`, `assert profile is not None` should be a `RuntimeError` guard**

```python
assert profile is not None  # try block sets it before exiting normally
return profile
```

`assert` is compiled away with `-O`. Invariant guards in production library code should use `if profile is None: raise RuntimeError(...)`. The compiler comment is good but not a substitute.

---

### F-12 — NIT · Design  
**`validation/_config.py`, error message for non-V2 configs is self-contradictory**

```
"v1 mask + v1 generate config shapes are no longer validated by
validate_config (S9 removal). Use a `version: 1` PipelineConfig..."
```

The message tells users to use a `version: 1` config after saying V1 is no longer supported. Both old and new schemas use `version: 1` as the schema version field, so this is inherently ambiguous. Rewrite:

```python
raise PipelineValidationError(
    "Config must declare `version: 1` at the top level to use the V2 PipelineConfig "
    "validator. Legacy formats (top-level masking_rules, or tables: as a dict) are no "
    "longer supported as of S9."
)
```

---

## 3. Performance Notes

### `storm/profiler.py` — `_compute_k_anonymity` groupby sweep

**Bottleneck:** CPU + memory. With `_K_ANON_MAX_CANDIDATES = 10`, the function runs C(10,2) + C(10,3) = 165 `groupby().size()` operations. At 100k rows and 10 candidate columns each: ~165 × 10ms = **~1.65 seconds** (not "well under a second" as the inline comment claims). On full-scan mode (no sampling cap) against a 5M-row source, this becomes ~82 seconds.

**Profile with:** `py-spy record --output profile.svg -- python -c "from decoy_engine.storm.profiler import run_storm; ..."` with a synthetic 100k-row DataFrame and 10 quasi-ID candidates.

**Mitigation options (not all are necessary; benchmark first):**
- Enforce the `sample_row_cap` before `_compute_k_anonymity` (currently, k-anon runs on the full sampled `df`, which may be up to `sample_row_cap` rows — confirm this is actually bounded).
- Vectorize the combo sweep: build a single `df[candidate_names]` slice and iterate combos on that, avoiding per-combo column selection.
- Reduce `_K_ANON_MAX_CANDIDATES` to 8 (C(8,2)+C(8,3) = 84 groupbys, ~49% fewer) with negligible practical impact on detection quality.

### `storm/profiler.py` — `_classify_numeric_range` Python iteration

The money-detection loop in `_classify_numeric_range` iterates over up to 200 values in Python:
```python
for v in sample:
    fv = float(v)
    if abs(fv - round(fv, 2)) < 1e-9: ...
```

Vectorize with:
```python
rounded = sample.round(2)
two_dp_hits = ((sample - rounded).abs() < 1e-9).sum()
has_fractional = ((sample - sample.round()).abs() >= 1e-9).any()
```

At 200 values this is a Nit; becomes relevant if the sample size is raised.

### `relationships/_namespace.py` — `namespace_to_columns` membership check is O(n)

```python
if key not in namespace_to_columns[ns_name]:
    namespace_to_columns[ns_name].append(key)
```

`namespace_to_columns` values are `list`s; `key not in list` is O(len(list)). With hundreds of columns per namespace this is O(n²) for the namespace-build step. Switch to `dict[str, list]` with a companion `set` for membership:

```python
namespace_to_columns_set: dict[str, set] = defaultdict(set)
# ... membership check:
if key not in namespace_to_columns_set[ns_name]:
    namespace_to_columns[ns_name].append(key)
    namespace_to_columns_set[ns_name].add(key)
```

At typical pipeline sizes (dozens of columns per namespace) this is Medium priority; at 500+ columns per namespace it becomes a latency tail.

---

## 4. Suggested Tests

### Determinism / Reproducibility

```python
def test_generate_batch_does_not_contaminate_unseeded_calls():
    """F-1: seeded generate_batch must not change subsequent non-seeded output."""
    adapter = FakerAdapter()
    spec_seeded = ProviderSpec(seed=b"\x00" * 8, deterministic=False)
    spec_random = ProviderSpec(seed=None, deterministic=False)
    
    # Build a pool (seeds the shared Faker instance)
    _ = adapter.generate_batch("person_name", spec=spec_seeded, count=10)
    
    # Non-seeded results should vary between runs
    results_a = adapter.generate_batch("person_name", spec=spec_random, count=5)
    results_b = adapter.generate_batch("person_name", spec=spec_random, count=5)
    assert results_a != results_b, "Non-seeded batch must not be deterministic"
```

```python
@given(st.text(min_size=1), st.text(min_size=1))
def test_formula_isolation(expr_a, expr_b):
    """F-2: two formula scopes built from different seeds produce independent RNG."""
    rng_a = random.Random(42)
    rng_b = random.Random(99)
    scope_a = make_mask_globals(rng_a)
    scope_b = make_mask_globals(rng_b)
    # Scopes share no state; consuming scope_a's RNG must not change scope_b's next value
    _ = safe_eval("randint(0, 1000)", scope_a, {})
    result_b_before = safe_eval("randint(0, 1000)", scope_b, {})
    _ = safe_eval("randint(0, 1000)", scope_a, {})
    result_b_after = safe_eval("randint(0, 1000)", scope_b, {})
    assert result_b_before == result_b_after
```

### Thread Safety

```python
def test_v2_provider_register_concurrent():
    """F-3: concurrent register + call must not KeyError."""
    import threading
    errors = []
    def _register():
        for i in range(100):
            register_faker_provider_v2(f"test_{i}", lambda f: f"v{i}")
            _unregister_faker_provider_v2(f"test_{i}")
    def _call():
        adapter = FakerAdapter()
        spec = ProviderSpec(seed=None, deterministic=False)
        for _ in range(100):
            try:
                adapter.generate("person_name", spec=spec)
            except Exception as e:
                if "KeyError" in type(e).__name__:
                    errors.append(e)
    t1 = threading.Thread(target=_register)
    t2 = threading.Thread(target=_call)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, f"KeyError on concurrent access: {errors}"
```

### Namespace

```python
def test_namespace_schema_qualified_name_rejected():
    """F-6: schema.table.column format raises at validation, not silently maps wrong."""
    config = {
        "namespaces": {"ns1": {"declared_by": ["public.users.email"]}},
        "tables": [],
    }
    with pytest.raises((NamespaceConfigError, pydantic.ValidationError)):
        build_namespace_registry(config, profile=_empty_profile())
```

### Validation

```python
def test_validate_config_does_not_catch_non_validation_exceptions():
    """F-5: ImportError from a broken PipelineConfig dep must not become PipelineValidationError."""
    from unittest.mock import patch
    with patch("decoy_engine.config.PipelineConfig.model_validate",
               side_effect=ImportError("broken dep")):
        with pytest.raises(ImportError):    # NOT PipelineValidationError
            validate_config({"version": 1, ...})
```

### STORM performance regression

```python
@pytest.mark.benchmark
def test_k_anon_under_1s_on_100k_rows(benchmark):
    """_compute_k_anonymity must complete in under 2 seconds on 100k rows, 10 candidates."""
    import numpy as np
    rng = np.random.default_rng(0)
    df = pd.DataFrame({
        f"col_{i}": rng.integers(0, 20, size=100_000)  # low cardinality candidates
        for i in range(10)
    })
    fields = [FieldStats(name=f"col_{i}", distinct_count=20, unique_rate=0.1,
                         inferred_type="integer", ...) for i in range(10)]
    result = benchmark(_compute_k_anonymity, df, fields)
    assert result[0] is not None
```

---

## 5. What's Good

- **`config/_pipeline.py` cycle detection**: The iterative DFS (replacing recursive) is correct and handles the `Iterator` ref cleanly. The `orphan_policy` 4-tuple key fix (S13-rebaseline P1) is the right call — the change from a 2-tuple to a 4-tuple key is a model correctness fix, not a performance hack.

- **`internal/faker_setup.py` `atomic_swap_db_providers`**: Excellent structural fix for the CRITICAL F1 finding from QA-internal-synth-providers. The single-lock-acquisition swap eliminates the partial-unregister window entirely. The pattern is textbook.

- **`relationships/_graph.py`**: The Kahn heapq implementation is correct and clean. Topological ordering via `heapq.heappush`/`heappop` gives O(E log V) rather than O(V² log V) from the prior sort-on-every-insert pattern. The multi-parent FK detection using `by_child` grouping is also sound.

- **`storm/profiler.py`**: The F-1 (sampling never actually applied) fix is straightforward and the `random_state=42` is correct for deterministic profiling reproducibility. The F-9 (double dateutil parse) and F-4 (vectorized casing) fixes show good profiling instincts — replacing per-row Python loops with pandas str operations gives the ~50x speedup cited.

- **`relationships/_namespace.py`**: The `__post_init__` index rebuild for `NamespaceRegistry` (QA-8 F2) correctly handles legacy callers constructing without `_index`, and the `object.__setattr__` bypass for a frozen dataclass is the standard pattern. The composite auto-binding (Step 2.5) correctly uses the whole-group tuple as the registry key.

- **`providers_v2/_registry.py`**: The double-import guard `_mimesis_available()` using `importlib.util.find_spec` (not a direct import) is the right approach — it avoids the install-message `ImportError` on the non-Mimesis path. The registry's immutable-override pattern (`override()` returns a new instance) is clean and test-friendly.

- **`errors.py`**: Well-organized exception hierarchy with stable `code` attributes. `FlagPauseSignal` inheriting from `DecoyError` (not `Exception`) but documented as a control-flow signal is correct. `PKDuplicatesError.code` as a class attribute (not instance) is a nice touch for the manifest assembler.
