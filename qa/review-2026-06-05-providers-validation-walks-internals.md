# QA Review: providers\_v2 / internal / walks / validation

**Date:** 2026-06-05  
**Reviewer:** Claude (QA session DtlqF)  
**Scope:** `src/decoy_engine/providers_v2/` (all files), `src/decoy_engine/internal/` (all files), `src/decoy_engine/walks/` (all files), `src/decoy_engine/validation/` (all files including `post/`)

---

## 1. Summary

The most serious issue is a **Critical determinism defect** in `providers_v2/_faker_adapter.py`: the singleton `FakerAdapter` holds a shared `_faker_instances` dict, and `generate_batch` seeds a locale's Faker instance in-place without any lock. Under concurrent pool builds (multi-threaded platform jobs), two jobs that hit the same locale race to call `seed_instance`, so each job may generate values seeded by the other job's seed — output is non-deterministic in exactly the conditions where determinism is most important. A closely related High finding is that the V2 custom-provider table (`_V2_CUSTOM_PROVIDERS`) lacks the lock that the V1 table (`_PROVIDER_LOCK` in `faker_setup.py`) correctly uses. Everything else in these packages is generally solid and well-commented.

---

## 2. Findings

### F1 — CRITICAL | Determinism
**File:** `src/decoy_engine/providers_v2/_faker_adapter.py:104–115`

**Issue:** `FakerAdapter._faker_instances` is a per-instance dict initialised in `__init__`. The default registry constructs exactly one `FakerAdapter()` and binds all 19 Faker-catalog providers to it. `generate_batch` seeds the cached locale instance before generating:

```python
if spec.seed is not None:
    self._faker(spec.locale).seed_instance(int.from_bytes(spec.seed, "big"))
return [self._faker_call(provider, spec) for _ in range(count)]
```

There is no lock around the seed + generate pair. Two concurrent pool-build calls for the same locale (e.g. `en_US`) will race: thread A calls `seed_instance(42)`, thread B calls `seed_instance(99)`, and then both generate from whichever seed landed last. The output is silently wrong — no error, just different values than the config declared.

**Impact:** Determinism guarantee is broken under any concurrent job execution. The S5 PoolBuilder contract (`same seed + same config = same pool`) fails silently.

**Fix:** Do not seed the shared cached instance. Instead, create a fresh Faker instance per `generate_batch` call when a seed is supplied:

```python
def generate_batch(
    self, provider: str, *, spec: ProviderSpec, count: int
) -> Sequence[Any]:
    if spec.deterministic:
        raise ProviderError(code="capability_violation", message="...")
    if spec.seed is not None:
        # Fresh instance: seed isolation so concurrent calls don't race.
        seeded_fake = faker_module.Faker(spec.locale or self._default_locale)
        seeded_fake.seed_instance(int.from_bytes(spec.seed, "big"))
        method_name = _FAKER_METHOD_MAP.get(provider)
        if method_name is None:
            raise AdapterError(code="unknown_provider", message=f"...")
        kwargs = dict(_FAKER_DEFAULT_KWARGS.get(provider, {}))
        kwargs.update(spec.extra)
        return [_coerce_locale_result(getattr(seeded_fake, method_name)(**kwargs))
                for _ in range(count)]
    return [self._faker_call(provider, spec) for _ in range(count)]
```

The fresh-instance allocation per batch is cheap relative to the Faker generation cost. Alternatively, use a `threading.Lock` keyed on `(locale, seed)` but that is harder to reason about.

**Verify:** `pytest -x tests/unit/providers_v2/ -k seed` plus a property-based test:
```python
# Hypothesis test: same seed x2 in concurrent threads gives identical output
from concurrent.futures import ThreadPoolExecutor
def _build(seed_bytes):
    return adapter.generate_batch("person_name", spec=ProviderSpec(seed=seed_bytes), count=50)
with ThreadPoolExecutor(2) as ex:
    f1, f2 = ex.submit(_build, b"\x00"*8), ex.submit(_build, b"\x00"*8)
assert f1.result() == f2.result()
```

---

### F2 — HIGH | Determinism
**File:** `src/decoy_engine/providers_v2/_faker_adapter.py:58, 199–207`

**Issue:** `_V2_CUSTOM_PROVIDERS: dict[str, Callable] = {}` is a module-level dict. `register_faker_provider_v2` writes to it without any lock; `_faker_call` reads it with a bare `if provider in _V2_CUSTOM_PROVIDERS` check. The V1 equivalent in `faker_setup.py` correctly uses `_PROVIDER_LOCK` for both reads and writes (fixed as QA-internal-synth-providers F1 on 2026-06-01). The V2 table was introduced after that fix and didn't inherit it.

**Impact:** Under concurrent platform API calls (register-while-generating), a masking job can see a partially-registered provider, or a register call can race a concurrent read and produce a `KeyError` after the `in` check but before the lookup. In CPython today the GIL protects individual dict operations, but the TOCTOU between `in` and `[]` is a logical race that will break under free-threading (Python 3.13+).

**Fix:** Add a module-level lock mirroring `_PROVIDER_LOCK`:

```python
_V2_PROVIDER_LOCK = threading.Lock()

def register_faker_provider_v2(name: str, fn: Callable) -> None:
    with _V2_PROVIDER_LOCK:
        _V2_CUSTOM_PROVIDERS[name] = fn

def _unregister_faker_provider_v2(name: str) -> None:
    with _V2_PROVIDER_LOCK:
        _V2_CUSTOM_PROVIDERS.pop(name, None)
```

And in `_faker_call`:
```python
with _V2_PROVIDER_LOCK:
    fn = _V2_CUSTOM_PROVIDERS.get(provider)
if fn is not None:
    return fn(self._faker(spec.locale))
```

---

### F3 — MEDIUM | Reliability
**File:** `src/decoy_engine/internal/logging.py:24–31`

**Issue:** `_LOGGER_INSTANCE` is a module-level global set without a lock:

```python
_LOGGER_INSTANCE = None

def get_logger(config=None):
    global _LOGGER_INSTANCE
    if _LOGGER_INSTANCE is None:
        _LOGGER_INSTANCE = _create_logger(config)
    elif config:
        _configure_logger(_LOGGER_INSTANCE, config)
    return _LOGGER_INSTANCE
```

`_configure_logger` calls `logger.handlers.clear()` on the existing logger. If another thread is mid-log-emit when this runs, the handler list is mutated under the active emit — Python's `logging` module has its own internal lock (`Handler.acquire()`) but `handlers.clear()` bypasses it. In practice the race window is tiny, but on the reconfigure path (e.g. the platform reloading config after a settings change) it can drop log records.

**Impact:** Rare dropped log lines and potential `IndexError` in multi-threaded reconfigure scenarios.

**Fix:** Python's `logging.getLogger()` returns the same named logger on every call; configure it once at startup. If dynamic reconfiguration is needed, use `logging.config.dictConfig` which acquires the root lock. The singleton pattern adds no value for a named logger.

```python
def get_logger(config=None):
    logger = logging.getLogger("decoy_engine")
    if not logger.handlers and config:
        _configure_logger(logger, config)
    elif config:
        # Let logging.config handle reconfiguration thread-safely
        pass
    return logger
```

---

### F4 — MEDIUM | Performance
**File:** `src/decoy_engine/walks/hazards.py:200`

**Issue:** The DFS cycle detector uses `path.index(neighbor)` to find where a back-edge cycle starts:

```python
idx = path.index(neighbor)
cycle = tuple(path[idx:])
```

`list.index()` is O(path-length). In a worst-case schema — a long chain with a single back-edge at the end — this is called once per cycle but each call scans the full path. With a path of 1000 nodes (already handled by the iterative-DFS fix that removed the recursion limit), the index scan is 1000 ops. For multiple cycles, total cost is O(cycles * path-depth). The existing fix (iterative DFS instead of recursive) correctly targeted the stack-overflow problem; this is a separate cost.

**Impact:** For very large schemas (1000+ tables, O(n) cycles), cycle detection slows perceptibly. Normal mid-market schemas (50–200 tables) are unaffected.

**Fix:** Track node positions with an auxiliary dict updated alongside `path`:

```python
path: list[str] = []
path_index: dict[str, int] = {}  # node -> position in path

# On push:
path_index[neighbor] = len(path)
path.append(neighbor)

# On pop:
node_to_pop = path.pop()
del path_index[node_to_pop]

# On back-edge:
idx = path_index[neighbor]  # O(1)
cycle = tuple(path[idx:])
```

This reduces back-edge cost from O(path-depth) to O(1).

**Measure:** `timeit` with a 500-node linear chain + one back-edge, then a fully-connected tournament graph.

---

### F5 — MEDIUM | Design
**File:** `src/decoy_engine/validation/_config.py:57–65`

**Issue:** The error message in the fallthrough branch of `_select_validator` is self-contradictory:

```python
raise PipelineValidationError(
    "v1 mask + v1 generate config shapes are no longer validated by "
    "validate_config (S9 removal). Use a `version: 1` PipelineConfig "
    "(see decoy_engine.PipelineConfig.model_validate) for v2 mask + "
    "v2 generate configs."
)
```

A user whose YAML has no `version` key (or has `version: 2`) reads: "v1 shapes are removed — use a version: 1 config". They don't know that `version: 1` means the *new* V2 PipelineConfig format. The terminology collision (internal sprint numbering "V2" == YAML key `version: 1`) is confusing.

Separately, `except Exception as exc` in the `PipelineConfig.model_validate` call wraps infrastructure failures (`ImportError`, `MemoryError`) into `PipelineValidationError`, making them indistinguishable from validation failures at the caller.

**Fix for the message:**
```python
raise PipelineValidationError(
    "Config must declare `version: 1` at the top level to use "
    "PipelineConfig validation. Configs without a `version` key "
    "(the legacy masking_rules / tables formats) are no longer "
    "supported (removed in S9). See decoy_engine.PipelineConfig."
)
```

**Fix for bare except:** Catch `PipelineConfigError` (the typed shape) explicitly and let infrastructure errors propagate:
```python
from decoy_engine.errors import PipelineConfigError
try:
    PipelineConfig.model_validate(data)
except PipelineConfigError as exc:
    raise PipelineValidationError(str(exc)) from exc
# Let ImportError, MemoryError, etc. propagate as themselves
```

---

### F6 — LOW | Correctness
**File:** `src/decoy_engine/walks/hazards.py:145–160`

**Issue:** `_detect_alternative_parents` emits a hazard description that states the XOR semantic as a fact:

```
"{source_table} has nullable FKs to {N} different parents: ..."
```

The docstring acknowledges the limitation ("the user confirms whether the 'exactly one' semantic actually holds"), but the `Hazard.description` field that surfaces in the UI doesn't communicate the uncertainty. A table with two legitimately independent optional FKs (e.g. `events.user_id` and `events.api_key_id`, both nullable, neither XOR) will show this hazard with no indication it's a heuristic.

**Fix:** Prefix the description with a qualifier:
```python
description=(
    f"{source_table} has nullable FKs to "
    f"{len(targets)} different parents "
    f"(possible XOR pattern — review): "
    f"{', '.join(sorted(targets))}"
),
```

---

### F7 — LOW | Data
**File:** `src/decoy_engine/walks/cross_file.py:84`

**Issue:** `nullable = fs.null_rate > 0` marks a column as nullable only if the STORM sample contained at least one null. For large columns where nulls are rare (e.g. 1 in 10 000 rows), a modest sample may see zero nulls and produce `nullable=False`. This false-non-nullable signal causes `_detect_alternative_parents` to miss the ALT pattern for that column.

**Impact:** Low — ALT detection already errs conservative; a missed nullable flag makes it slightly more conservative. No masking logic depends on this nullable flag.

**Fix:** Use a threshold rather than a strict zero: `nullable = fs.null_rate > 0 or getattr(fs, 'nullable_in_schema', False)`. If `FieldStats` carries a schema-level nullable flag, prefer that over the sample-derived rate.

---

### F8 — LOW | Correctness
**File:** `src/decoy_engine/internal/faker_setup.py:186–197`

**Issue:** `make_faker` catches `(AttributeError, ValueError, TypeError)` when the Faker constructor raises on an invalid locale. Faker's internal locale loading can also raise `ModuleNotFoundError` (if a locale-specific provider is a separate package not installed) or `LookupError`. The fallback-to-en-US logging would not trigger, and the exception would propagate unexpectedly.

**Impact:** Low in practice — most Faker locales are bundled. Third-party locale packages could expose this.

**Fix:**
```python
try:
    return Faker(locale)
except Exception as exc:  # intentionally broad: locale loading can fail many ways
    _log.warning(
        "make_faker: locale %r is invalid (%s: %s); falling back to en_US",
        locale, type(exc).__name__, exc,
    )
    return Faker()
```

---

### F9 — NIT | Performance
**File:** `src/decoy_engine/internal/logging.py:153–179`

**Issue:** `ProgressLogger.update` uses f-strings in every `logger.info(...)` call:

```python
self.logger.info(f"{self.message}: {percentage:.1f}% complete ...")
```

F-strings are evaluated eagerly before the logger checks whether the INFO level is enabled. For a ProgressLogger driven by a row loop at 100k rows/sec with `increment=1`, this constructs and discards ~100k strings per second when INFO is disabled.

**Fix:** Use `%`-style lazy formatting:
```python
self.logger.info(
    "%s: %.1f%% complete (%d/%d) - %s - ETA: %s",
    self.message, percentage, self.current, self.total, speed_str, eta_str,
)
```

Alternatively, `ProgressLogger.update` should only log at configurable intervals (every N records or every K seconds), not on every call. Emitting a log line per row at 100k rows/sec is I/O-bound overhead that dominates the masking pipeline.

---

## 3. Performance Notes

- **Bottleneck classification for `generate_batch`:** CPU-bound (Faker method calls). The shared-instance lock contention fix (F1) adds one heap allocation per batch call. For pool sizes of 1000–10 000 rows, this is negligible.
- **Cycle detection (F4):** I/O-negligible, CPU-bound. Profile with `cProfile` against the MediaWiki/OpenStreetMap schema fixture in `tests/perf_fixtures/`. Measure `detect_hazards()` with a 500-node synthetic chain: `python -m timeit -s "from tests.perf_fixtures import big_chain; from decoy_engine.walks.hazards import detect_hazards" "detect_hazards(big_chain)"`. Expect >10x speedup from the O(1) path_index fix at 500 nodes.
- **Logger f-strings (F9):** Profile with `scalene` or `py-spy` against a large mask run with `level: WARNING`. If ProgressLogger.update accounts for >1% of wall time, apply the fix.
- **`_FAKER_METHOD_MAP` lookup:** O(1) dict, not a concern.

---

## 4. Suggested Tests

### F1 — Concurrent seed isolation
```python
# tests/unit/providers_v2/test_faker_adapter_concurrent.py
import threading
from decoy_engine.providers_v2._faker_adapter import FakerAdapter
from decoy_engine.providers_v2._adapter import ProviderSpec

def test_generate_batch_seed_isolation():
    adapter = FakerAdapter()
    seed_a = (42).to_bytes(8, "big")
    seed_b = (99).to_bytes(8, "big")
    results_a, results_b = [], []
    threads = [
        threading.Thread(target=lambda: results_a.extend(
            adapter.generate_batch("person_name", spec=ProviderSpec(seed=seed_a), count=50))),
        threading.Thread(target=lambda: results_b.extend(
            adapter.generate_batch("person_name", spec=ProviderSpec(seed=seed_b), count=50))),
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    # Run again single-threaded and compare
    ref_a = adapter.generate_batch("person_name", spec=ProviderSpec(seed=seed_a), count=50)
    assert results_a == ref_a  # would fail before the fix
```

### F2 — V2 provider lock
```python
# Verify register + generate don't race
def test_v2_provider_register_concurrent():
    import threading, time
    from decoy_engine.providers_v2._faker_adapter import (
        register_faker_provider_v2, _unregister_faker_provider_v2
    )
    errors = []
    def toggler():
        for _ in range(1000):
            register_faker_provider_v2("_test", lambda f: "x")
            _unregister_faker_provider_v2("_test")
    def reader():
        for _ in range(1000):
            try:
                from decoy_engine.providers_v2._faker_adapter import _V2_CUSTOM_PROVIDERS
                _ = dict(_V2_CUSTOM_PROVIDERS)  # snapshot
            except Exception as e:
                errors.append(e)
    ts = [threading.Thread(target=toggler), threading.Thread(target=reader)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errors
```

### F4 — Cycle detection O(1) path
```python
# tests/unit/walks/test_hazards_perf.py
def test_cycle_detection_large_chain():
    """500-node linear chain + single back-edge must complete in <100ms."""
    import time
    from decoy_engine.walks.types import SchemaSnapshot, Table, Column, Edge
    tables = [Table(name=f"t{i}", schema="", columns=(Column("id", "int", False, True),)) for i in range(500)]
    edges = tuple(Edge(f"t{i}", "id", f"t{i+1}", "id", True) for i in range(499))
    edges += (Edge("t499", "id", "t0", "id", True),)  # back edge
    snap = SchemaSnapshot(db_kind="postgres", schema_name="", tables=tuple(tables), declared_edges=edges, connector_id=None)
    t0 = time.perf_counter()
    from decoy_engine.walks.hazards import detect_hazards
    detect_hazards(snap)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1, f"cycle detection took {elapsed:.3f}s on 500-node chain"
```

### F5 — validate_config error message
```python
def test_validate_config_no_version_key_error_message():
    from decoy_engine.errors import PipelineValidationError
    from decoy_engine.validation import validate_config
    import pytest
    with pytest.raises(PipelineValidationError, match="version: 1"):
        validate_config({"masking_rules": []})

def test_validate_config_version2_rejected():
    from decoy_engine.errors import PipelineValidationError
    from decoy_engine.validation import validate_config
    import pytest
    with pytest.raises(PipelineValidationError):
        validate_config({"version": 2, "tables": {}})
```

### F8 — make_faker locale fallback completeness
```python
def test_make_faker_module_not_found_locale():
    """A locale string that triggers an unexpected error type still falls back."""
    from unittest.mock import patch
    from decoy_engine.internal.faker_setup import make_faker
    with patch("faker.Faker", side_effect=ModuleNotFoundError("no module")):
        f = make_faker("bad_locale")
    # Should not raise; result is the en_US fallback
    assert f is not None
```

---

## 5. What's Good

- **`atomic_swap_db_providers`** in `faker_setup.py` is an excellent design: it eliminates the window where concurrent jobs see zero DB-backed providers, and the audit comment is precise about the pre-fix failure mode. This is the exact pattern F2 should copy for the V2 table.
- **Iterative DFS** in `hazards._detect_cycles` correctly replaces the recursive version; the comment cites the exact failure mode (MediaWiki/OpenStreetMap schemas) and the fix is correctly isolated to just the stack management.
- **`infer_cross_file_edges` tie-break via `sorted(table_names)`** (F2 from the 2026-06-01 QA session) is clean and the comment is precise about the PYTHONHASHSEED nondeterminism it fixed.
- **`_make_reflected_provider`** gracefully handles kwargs mismatch by inspecting the method signature at wrap time rather than at call time — the right place to pay the reflection cost.
- **`PostValidationRunner`** correctly wraps each scan in a try/except so a crashing scan produces a failed outcome rather than aborting the whole quality phase; the quality_summary is always produced.
- **`internal/crypto.py`** `deterministic_hash` DeprecationWarning is well-placed: it's machine-readable so CI tools that treat warnings as errors will catch accidental new usage.
