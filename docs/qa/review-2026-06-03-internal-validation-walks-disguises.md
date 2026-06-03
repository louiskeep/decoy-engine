# QA Review: internal/, validation/, walks/, disguises/

**Date:** 2026-06-03
**Branch:** qa/review-2026-06-03-internal-validation-walks-disguises
**Scope:** Modules not covered by prior QA sessions (2026-05-30 through 2026-06-03):
- `src/decoy_engine/internal/` — crypto, faker_setup, logging, memory, base, fs
- `src/decoy_engine/validation/` — _config.py, post/_runner.py, post/_scan.py, post/_checks/*
- `src/decoy_engine/walks/` — hazards, cross_file, inference, graph, types
- `src/decoy_engine/disguises/` — loader, schema, YAML bundles
- `src/decoy_engine/instrumentation/` — timing

---

## 1. Summary

The cryptographic and Faker primitives in `internal/` are largely sound —
the HMAC migration and the `atomic_swap_db_providers` fix are solid work.
The most important finding is in the post-execution leakage scan: **FPE
(Format-Preserving Encryption) columns are incorrectly routed through the
set-membership leakage check**, which will produce false hard-fails on
any run that uses FPE on a domain with meaningful collision probability
(SSNs, credit cards, postcodes). A second critical finding: `BaseMasker`
imports from a `.logger` module that no longer exists, crashing any
subclass that omits a logger argument. Both must be fixed before
post-validation is enabled for FPE-heavy pipelines.

---

## 2. Findings

---

### F1 — CRITICAL | Correctness
**`internal/base.py:27` — `BaseMasker` imports a non-existent module**

```python
from .logger import MaskerLogger          # no logger.py in internal/
self.logger = MaskerLogger.get_logger()
```

`internal/` contains `logging.py`, not `logger.py`. Any `BaseMasker`
subclass that is instantiated without an explicit `logger=` argument
raises `ModuleNotFoundError` at instantiation time.

`ConfigValidator` and `MaskingStrategy` (same file) correctly import
from `decoy_engine.internal.logging`, so this is a stale V1 reference
that was not updated when the module was renamed.

**Impact:** Any surviving code path that constructs a `BaseMasker`
subclass without a logger keyword argument is silently broken today.
If `BaseMasker` is now dead code (no V2 subclasses), delete the class
rather than leaving a broken import in the repo.

**Fix:**
```python
# Option A — fix the import if BaseMasker is still in use:
from decoy_engine.internal.logging import get_logger
self.logger = logger if logger else get_logger()

# Option B — delete BaseMasker if it has no V2 subclasses:
# (confirm with: grep -r "BaseMasker" src/ -- if empty, delete)
```

---

### F2 — CRITICAL | Correctness / Determinism
**`internal/crypto.py:32` — `deterministic_hash` produces collisions via separator-less concatenation**

```python
value_str = f"{value}{seed}"
hash_obj = hashlib.sha256(value_str.encode())
```

`deterministic_hash("a", seed=11)` == `deterministic_hash("a1", seed=1)`
because both produce `sha256("a11")`. Any two inputs where one value's
string representation is a prefix of another's (with the seed completing
the match) collide silently.

The function already emits a `DeprecationWarning`. But code still calling
it gets wrong behavior, not just a noisy warning.

**Impact:** Any active caller using this as an FK-preservation key will
produce silent mapping collisions: two distinct source values hash to the
same masked value, breaking referential integrity. Verify with:
```bash
grep -r "deterministic_hash" src/ tests/
```
If callers remain, migrate them to `hmac_hex` (the correct primitive is
already present) and then delete this function.

**Fix:** Add a separator before removing the function:
```python
# Interim: prevent collisions while the migration is in progress
value_str = f"{value}\x00{seed}"   # null byte separator
```

---

### F3 — HIGH | Correctness / Data
**`validation/post/_checks/_leakage.py` — FPE columns routed through set-membership check; false hard-fails guaranteed**

The leakage scan classifies strategies into two buckets:

- `_VALUE_REUSE_STRATEGIES = {"shuffle", "categorical"}` → positional check
- Everything else → set-membership check ("any source value in output = leak")

FPE (`fpe`) is in "everything else." But FPE's output domain is identical
to its input domain by design: a masked SSN is always a valid SSN, a
masked credit card is always a valid card number. The output WILL contain
values that appear in the source set — not because the original value
leaked, but because the domain is finite and shared.

For SSNs (~1 billion unique values) with N source rows, the probability
that at least one FPE output coincidentally matches a source value is
roughly `1 - (1 - N/B)^N` where B is the domain size. For N = 100K SSN
rows: ~1% per column. For N = 1M SSNs: ~63%. This triggers a hard-fail
(`failed = True`) and terminates the job.

**Impact:** All post-validated pipelines using `fpe` on large SSN,
payment-card, or postcode columns will suffer spurious hard-fails in
production at rates that scale with dataset size. The job reports
"leakage detected" when no leakage occurred.

**Fix:** Add `fpe` to the positional check group:
```python
_VALUE_REUSE_STRATEGIES = frozenset({"shuffle", "categorical", "fpe"})
```
FPE's positional fixed-point rate should be negligible (a tweakable cipher
avoids key-dependent fixed points), so the positional warning path is the
correct model for FPE. Add a comment citing the domain-overlap rationale.

**Verify:** Write a property-based test (Hypothesis) that generates a random
FPE key and a realistic-size SSN column, runs the leakage scan, and asserts
`failed=False` even when FPE outputs overlap with source values.

---

### F4 — HIGH | Design / Reliability
**`internal/logging.py:24` — process-global singleton logger in library code**

```python
_LOGGER_INSTANCE = None

def get_logger(config=None):
    global _LOGGER_INSTANCE
    if _LOGGER_INSTANCE is None:
        _LOGGER_INSTANCE = _create_logger(config)
    elif config:
        _configure_logger(_LOGGER_INSTANCE, config)  # clears all handlers
    return _LOGGER_INSTANCE
```

Three compounding problems:

1. **Test isolation breaks:** the first test that calls `get_logger()`
   wins. Every subsequent test inherits that configuration, including its
   file handler. Test B that wants a console-only logger still gets the
   file handler configured by test A.

2. **Reconfiguration clears handlers mid-flight:** `_configure_logger`
   calls `logger.handlers.clear()`. Any concurrent log write during that
   window finds no handlers and silently discards the record. Python's
   logging lock protects individual `emit()` calls, but not the
   list-level `clear()` + `append()` sequence.

3. **Library code should not own logger configuration:** the platform
   already configures its own logging hierarchy. `get_logger()` adding
   a `RotatingFileHandler` to `"decoy_engine"` fights the platform's
   centralized log routing and produces duplicate file writes.

**Impact:** Flaky tests, log records silently dropped on concurrent
calls, and platform log files written to unexpected locations in
Docker/K8s deployments where the platform controls the log directory.

**Fix:** Replace the singleton with the standard library pattern — call
`logging.getLogger("decoy_engine")` directly and let the application/
platform configure handlers. Remove `_configure_logger` and
`_LOGGER_INSTANCE`. Document that callers set up handlers at the
application layer, not inside the engine. `ProgressLogger` still works;
it just receives whatever logger the caller passes in.

---

### F5 — HIGH | Performance / Memory
**`validation/post/_checks/_leakage.py` (and `_fk_validity.py`) — full-column materialization to Python lists**

`column_values()` in `_scan.py` calls `.to_pylist()` on every PyArrow
column it touches:

```python
values: list[Any] = table.column(column).to_pylist()
return values
```

The leakage scan calls this for every masked column in every table, for
both the output and source tables. For a 10M-row table with 20 masked
string columns, this allocates roughly 20 × 2 × (10M × ~50 bytes) ≈
**20 GB** of Python objects. The set-membership check then holds two
copies simultaneously (source set + output list).

`_fk_validity.py`'s `_key_set()` materializes parent key tuples into a
Python `set` — the same class of problem for large parent tables.

**Impact:** OOM crash or severe GC pressure on tables that comfortably
fit in Arrow's zero-copy columnar format but not as Python lists.

**Bottleneck classification:** Memory-bound. The CPU cost of set
construction is O(n); the memory pressure is the dominant issue.

**Fix:** Use PyArrow's native set operations for set membership:
```python
import pyarrow.compute as pc

# leakage: build source set as PyArrow array, use pc.is_in()
src_arr = src_table.column(col_name).drop_null()
out_arr = out_table.column(col_name).drop_null()
leaked_mask = pc.is_in(out_arr, value_set=src_arr)
leaked_count = pc.sum(leaked_mask).as_py()

# fk_validity: build key set using PyArrow join
# (use pc.is_in on the child column against the parent column)
```

PyArrow `is_in` operates on Arrow buffers without Python-object
overhead; for the same 10M-row case the peak memory stays within
Arrow's columnar representation (~50x less than `to_pylist()`).

**Profile with:** `memory_profiler` or `scalene` on a 1M-row fixture
(`tests/perf_fixtures/`) with `post_validation: true` to confirm the
allocation profile before and after.

---

### F6 — MEDIUM | Correctness / Reliability
**`validation/_config.py:60` — error message contradicts the version field semantics**

```python
raise PipelineValidationError(
    "v1 mask + v1 generate config shapes are no longer validated by "
    "validate_config (S9 removal). Use a `version: 1` PipelineConfig ..."
)
```

This message fires when a config is missing `version:` entirely (or
has `version: 2`). The text says "v1 mask + v1 generate" but the
recommended fix is "use `version: 1`" — which refers to the V2 product
generation's config schema. An operator seeing `version: 2` in their
YAML would be told "use `version: 1`" and conclude the tool is broken.

**Fix:** Separate the "missing version" and "wrong version" branches:
```python
v = data.get("version")
if v == 1:
    try:
        PipelineConfig.model_validate(data)
    except Exception as exc:
        raise PipelineValidationError(str(exc)) from exc
    return None
elif v is None:
    raise PipelineValidationError(
        "Config is missing a top-level `version:` field. "
        "Add `version: 1` for the current PipelineConfig schema."
    )
else:
    raise PipelineValidationError(
        f"Unsupported config version {v!r}. "
        "The current engine expects `version: 1`."
    )
```

---

### F7 — MEDIUM | Performance
**`internal/faker_setup.py:239` — `get_faker_providers()` reflects all Faker methods on every call**

```python
for name in dir(fake):
    ...
    providers[name] = _make_reflected_provider(attr)
```

`dir(fake)` returns ~350+ attributes. For each callable, `_inspect.signature`
is called (which parses the method's type annotations). If `get_faker_providers`
is called once per table-run, the cost is absorbed at setup time. If it is
called per-row or per-batch, it dominates hot-path time.

**Impact:** At 10K rows/sec and per-row `get_faker_providers` calls,
reflection overhead (~1-5ms per call) limits throughput to ~200-1000
rows/sec — 10x-50x throughput regression.

**Action required (2 steps):**
1. Confirm call frequency: `grep -r "get_faker_providers" src/` and trace
   the call chain from the Faker masking strategy's `apply()`.
2. If called more than once per pipeline run, cache the result keyed on
   the seeded Faker instance's locale (or the locale string alone, since
   the provider set depends only on the locale, not the seed):
```python
@functools.lru_cache(maxsize=16)
def _build_provider_template(locale_key: str) -> dict[str, Callable]:
    """Cache the reflected provider dict by locale. Callers rebind
    the fake instance (which carries the per-value seed) at call time."""
    ...
```

---

### F8 — MEDIUM | Reliability
**`validation/post/_checks/_determinism_sample.py` — fails silently without identifying the offending column**

```python
for col_name, seed in table_seed.per_column:
    ...
    if failed:
        break
if failed:
    break
return ScanOutcome(name=_NAME, failed=failed)
```

The returned `ScanOutcome` has `failed=True` but carries no indication of
which table or column violated the same-source-same-output invariant. The
quality report shows `"determinism_sample"` in `failed_checks` with no
further context.

**Impact:** Debugging a determinism regression requires re-running the job
with debug logging and manually bisecting which column broke.

**Fix:** Emit a `QualityWarning` on the failing column before breaking:
```python
from decoy_engine.generation.pool._events import QualityWarning

violations: list[QualityWarning] = []
...
if mapping[source] != masked:
    violations.append(QualityWarning(
        code="determinism_violation",
        provider="determinism_sample",
        column=col_name,
        detail={"table": table_name, "source_repr": repr(source)[:80]},
    ))
    failed = True
    break
...
return ScanOutcome(name=_NAME, failed=failed, warnings=tuple(violations))
```

Note: `source_repr` must be truncated and must NOT include the actual
source value — use `repr(source)[:80]` as a type hint only, or omit the
value entirely and include only the column path.

---

### F9 — MEDIUM | Reliability
**`validation/post/_runner.py:_merge` — scan output collision silently overwrites keys**

```python
distinct_counts.update(outcome.distinct_counts)
null_counts.update(outcome.null_counts)
...
```

If two scans both emit a result for the same `"users.email"` key (e.g.
during a future refactor where a scan is split), the second silently wins.
The `failed_checks` tuple is separate and correct, but the counts/validity
reports are silently corrupted.

**Fix:** Add a development-time assertion:
```python
for k in outcome.distinct_counts:
    assert k not in distinct_counts, (
        f"Scan collision on distinct_counts[{k!r}] — "
        f"check that SCANS don't overlap their output keys"
    )
distinct_counts.update(outcome.distinct_counts)
```
Or log a warning in production mode where assertions may be disabled.

---

### F10 — LOW | Correctness
**`internal/crypto.py:50` — `hmac_seed` truncates to 32 bits**

```python
return int.from_bytes(digest[:4], "big")
```

This produces a 32-bit unsigned integer (max ~4.3B). For Faker's
`seed_instance(n)`, the seed modulus is the Mersenne Twister's internal
state space which accepts any integer, but in practice Python's `random`
module only keeps 32 bits of initial entropy from integer seeds anyway.
Acceptable for Faker generation, but callers that use `hmac_seed` for
other purposes should be aware of the 32-bit ceiling.

No code change needed unless a future use case requires > 32-bit seeds;
document the 32-bit contract in the docstring.

---

### F11 — LOW | Reliability
**`internal/memory.py:56` — bare `except Exception` swallows all errors silently**

```python
except Exception:
    return None
```

`get_memory_usage` returns `None` for both "psutil unavailable" and
"unexpected runtime error". Callers can't distinguish the two.

**Fix:** At minimum, log the exception:
```python
except Exception as exc:
    _log.debug("get_memory_usage failed: %s", exc)
    return None
```

---

### F12 — LOW | Correctness
**`walks/cross_file.py:_table_name_from_source_label` — extension strip accepts numeric extensions**

```python
if 1 <= len(ext) <= 5 and ext.isalnum():
    name = stem
```

`"data.123"` has extension `"123"` which is `isalnum()` → True, so
`"data.123"` → `"data"`. Numeric-only extensions are unusual in practice
but the condition is misleading. More precisely, `ext.isalpha()` would be
safer, or an explicit allowlist of known file extensions.

---

### F13 — LOW | Data
**`disguises/schema.py` — Pydantic models allow extra YAML fields (silent drop)**

None of the Disguise/TriggerSpec/FieldRule models configure
`model_config = ConfigDict(extra="forbid")`. A YAML bundle with a
typo'd key (e.g. `triggerrs:`) is silently accepted but the trigger
configuration is empty — the disguise then matches nothing and produces
no warnings.

**Fix:**
```python
from pydantic import ConfigDict

class Disguise(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ...
```
Do the same for `TriggerSpec` and `FieldRule`. The CI smoke test that
loads all bundles will then catch authoring errors.

---

### F14 — NIT | Design
**`internal/logging.py:ProgressLogger` — f-strings in log calls prevent lazy evaluation**

```python
self.logger.info(
    f"{self.message}: {percentage:.1f}% complete ({self.current:,}/{self.total:,}) - ..."
)
```

When the log level is above INFO, Python's logging will call the logger
but discard the record — but the f-string interpolation (including
division and the format operations) has already been evaluated. Use `%`
formatting to defer string construction until the record is actually
emitted:

```python
self.logger.info(
    "%s: %.1f%% complete (%s/%s) - %s - ETA: %s",
    self.message, percentage,
    f"{self.current:,}", f"{self.total:,}",   # commas still need f-string
    speed_str, eta_str,
)
```

---

### F15 — NIT | Design
**`instrumentation/timing.py:13` — `psutil.Process()` at module import time**

```python
_PROCESS = psutil.Process()
```

`memory.py` imports psutil defensively inside functions (graceful
degradation if psutil is unavailable). `timing.py` imports it at module
level and calls `psutil.Process()` at import time. If `psutil` is not
installed, importing `decoy_engine.instrumentation.timing` raises
`ImportError`, whereas `memory.py` would silently degrade.

Either: make psutil a hard required dependency (and update `pyproject.toml`
with `psutil >= 5.9` in `dependencies`), or guard both modules the same way.

---

## 3. Performance Notes

**Bottleneck classification for post-validation suite:**

| Phase | Bottleneck | Dominant cost |
|---|---|---|
| Leakage scan | Memory-bound | `to_pylist()` on source + output columns |
| FK validity scan | Memory-bound | `to_pylist()` on parent key columns |
| Determinism sample | CPU-bound | Dict hash operations over `sample_size` rows |
| Cardinality, null audit | I/O-bound | PyArrow column access (cheap) |

The leakage + FK validity scans are the hot path. For a 10-table schema
with 1M rows/table and 20 masked columns per table, the estimated peak
allocation from `to_pylist()` is:

```
columns_touched × 2 (source + output) × rows × avg_value_size
= 200 × 2 × 1M × 50 bytes ≈ 20 GB Python objects
```

This will OOM a container before any processing happens on the sets.

**What to profile:**
```bash
# Run with memory_profiler on the post-validation fixture:
python -m memory_profiler -o post_val_profile.txt \
    tests/perf_fixtures/run_post_validation_fixture.py

# Or use scalene for combined CPU + memory:
scalene --cpu --memory tests/perf_fixtures/run_post_validation_fixture.py
```

**Recommended fix path:** migrate `column_values()` to return
`pa.Array | pa.ChunkedArray` and let each scan use `pyarrow.compute` ops
(`pc.is_in`, `pc.value_counts`, `pc.unique`) rather than Python list
comprehensions. This is a single change to `_scan.py:column_values` that
benefits all scans.

---

## 4. Suggested Tests

| Module | Test case |
|---|---|
| `internal/crypto.py` | Property (Hypothesis): `deterministic_hash(v, s)` must not equal `deterministic_hash(v2, s2)` when `str(v)+str(s) == str(v2)+str(s2)` — verifies the collision, and confirms the fix resolves it |
| `internal/crypto.py` | `hmac_hex(key, None)` → `None`; `hmac_seed(key, None)` → `0` |
| `internal/faker_setup.py` | `atomic_swap_db_providers` under concurrent readers: launch 10 threads calling `get_faker_providers`, one thread calling `atomic_swap_db_providers`; assert zero partial-view observations |
| `internal/logging.py` | Two-thread race on `get_logger()`: both threads create a logger simultaneously; assert exactly one `RotatingFileHandler` on the resulting instance |
| `validation/post/_leakage` | FPE column with domain-overlap: source has `["123-45-6789"]`, FPE output also happens to contain `"123-45-6789"` (different row); assert `failed=False` after fix |
| `validation/post/_leakage` | Shuffle column with 100% fixed points (identity permutation); assert `failed=False`, `warnings` has `value_reuse_fixed_point` |
| `validation/post/_determinism_sample` | Non-deterministic column: same source value maps to two different outputs; assert `failed=True` and `warnings` identifies the column |
| `disguises/loader.py` | Directory with one valid YAML + one malformed YAML; assert loader returns 1 disguise and logs an error, does not raise |
| `disguises/schema.py` | YAML with an extra unknown key; assert `ValidationError` (after adding `extra="forbid"`) |
| `walks/hazards.py` | Linear chain of 2000 tables (each FKs to the next): assert no RecursionError, returns CIR=0 (no cycle) |
| `walks/cross_file.py` | `_table_name_from_source_label("data.123")` → verify behavior; `"customers.csv.gz"` → `"customers.csv"` (only final extension stripped) |
| `walks/inference.py` | `parent_team_id` column with no `parent_team` table but a `teams` table: assert edge to `teams` |
| `instrumentation/timing.py` | Zero-overhead gate: call `timed_strategy` 1M times with no collector; assert <2% overhead vs bare loop (per existing test contract) |

---

## 5. What's Good

- **`internal/crypto.py`** — the HMAC migration is exactly right. `hmac_hex` and `hmac_seed` use
  HMAC-SHA256 correctly, handle `None` inputs, use `utf-8` with `errors="replace"`,
  and the deprecation warning on `deterministic_hash` is the correct mechanism
  for making the transition machine-verifiable in CI.

- **`internal/faker_setup.py`** — `atomic_swap_db_providers` is a clean
  solution to the partial-view race: one lock acquisition, full swap. The snapshot
  of the custom registry in `get_faker_providers` is also correct. The list-provider
  closure (`fn=fn, fake=fake` default-argument capture) properly avoids late-binding.

- **`walks/hazards.py`** — the iterative DFS cycle detector (F4 fix) is
  correctly equivalent to the recursive version, handles the degenerate
  single-node case, and the `_canonical_cycle` rotation for deduplication
  is provably correct. The hazard taxonomy (HUB, SR, PE, PM, ALT, CIR)
  matches established ER modeling literature.

- **`walks/cross_file.py`** — the PYTHONHASHSEED determinism fix (F2 fix:
  sorted iteration over `table_names`) is exactly the right one-line
  patch; it also correctly centralises the sort so all call sites inherit
  the fix without separate audits.

- **`validation/post/_checks/_leakage.py`** — the substitution vs. value-reuse
  distinction is architecturally correct and the "never echo PII in warnings,
  only counts" contract is maintained properly. The FPE misclassification
  (F3 above) is a narrow fix, not a design rethink.

- **`instrumentation/timing.py`** — the thread-local collector pattern is
  the right design: zero overhead when inactive, safe under concurrent
  execution, no behavior change to existing code paths. The `rss_kb`
  sampling approach is correctly documented as "net delta, not high-water
  mark" with an appropriate pointer to `tracemalloc` for finer measurement.

- **`disguises/loader.py`** — per-file error isolation (F8 fix) is
  correct: a single malformed bundle no longer aborts the full load.
  Sorted `glob` ensures deterministic load order.
