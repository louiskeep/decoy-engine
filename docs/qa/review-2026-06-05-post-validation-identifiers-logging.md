# QA Review: Post-Validation Scans, Identifier Providers, Internal Logging, Walks Hazards

**Date:** 2026-06-05
**Scope:**
- `src/decoy_engine/validation/post/` (all 9 scans + runner + scan context)
- `src/decoy_engine/providers_v2/identifiers/` (`_ssn`, `_pan`, `_npi`, `_mrn`, `_iban`, `_ein`)
- `src/decoy_engine/internal/` (`crypto.py`, `logging.py`, `memory.py`, `faker_setup.py`, `base.py`, `fs.py`)
- `src/decoy_engine/walks/` (`hazards.py`, `cross_file.py`, `diff.py`, `graph.py`, `inference.py`)

**Previously reviewed (avoid overlap):**
`execution/_strategies/{_hash,_date_shift,_categorical,_formula,_text_redact,_fpe}`,
`execution/_pandas_adapter.py`, `execution/polars/_polars_adapter.py`,
`execution/_when_gate.py`, `generation/synthesize.py`, `relationships/*`,
`connectors/{s3,sftp,gcs}`, `determinism/*`, `expressions.py`, `storm/*`,
`disguises/*`, `quality/*`, `config/*`, `plan/_compile.py`,
`providers_v2/{_registry,_faker_adapter,_real_registry}`,
`providers_v2/mimesis/*`, `generators/*`, `transforms/*`.

---

## Summary

The post-validation scan suite (`validation/post/`) and identifier providers
(`providers_v2/identifiers/`) are architecturally sound. The most important finding is
a **systemic `to_pylist()` pattern across all 9 post-validation scans**: every scan
materializes full source and output columns as Python lists via `column_values()`, which
calls `pa.ChunkedArray.to_pylist()`. On a 10M-row table with 20 masked columns, the 9
scans collectively allocate on the order of tens of billions of Python objects, making
post-validation prohibitively slow and memory-hungry at production scale. PyArrow's
native compute API eliminates this overhead entirely for the operations in question.
The second-most-important finding is a non-atomic logger singleton (`internal/logging.py`)
whose handler-clearing reconfiguration is a data-race hazard under concurrent calls.

---

## Findings

### F1 — HIGH | Performance | `validation/post/` — `column_values()` materializes full columns as Python lists across all 9 scans

**The issue:**

Every scan in the suite calls `column_values(table, col_name)`, defined in `_scan.py:72`:

```python
def column_values(table: pa.Table, column: str) -> list[Any]:
    values: list[Any] = table.column(column).to_pylist()
    return values
```

`to_pylist()` converts every Arrow value to a Python object: integers become Python `int`,
strings become Python `str`, nulls become `None`. Each Python object carries ~28-56 bytes
of overhead on CPython (vs ~8 bytes in Arrow). Then each scan performs pure-Python set
operations, list comprehensions, or element-wise comparisons over the resulting lists.

The call count across the 9 scans on a single table pass (N rows, M masked columns, K PK columns):

| Scan | `column_values` calls | Rows materialized |
|---|---|---|
| `leakage` | 2 per masked column (src + out) | 2NM |
| `null_audit` | 2 per masked column | 2NM |
| `pk_uniqueness` | 1 per PK column | NK |
| `cardinality` | 1–2 per masked column | ≤2NM |
| `determinism_sample` | 2 per deterministic column | ≤2NM |
| `format_rules` | 1 per format-bearing column | NF |
| `fk_validity` | 1 per FK column per relationship | variable |
| `composite_coherence` | 3–4 per coherence group | variable |
| `sampled_values` | 1 per masked column | NM |

For N=10M, M=20, K=2: the three source-comparison scans alone (`leakage`, `null_audit`,
`determinism_sample`) each allocate ~400M Python objects. At ~28 bytes per Python `str`
object, `leakage` alone requires ~11GB of transient heap before GC. CPython's allocator
does not immediately reclaim this; the GC pause between scans is several seconds. Total
peak RSS across a full 9-scan pass on a 10M-row / 20-column table is estimated at
**30–60GB** of Python-object overhead — enough to OOM on a standard 32GB worker node.

**The bottleneck is CPU-bound object allocation, not I/O.** Profile the scan suite with:

```bash
python -m scalene --cpu --memory -m pytest tests/integration/golden/ -k post_validation
```

The `to_pylist` frames will dominate allocation.

**Recommended fix:** Replace `column_values()` calls with PyArrow compute operations that
stay in C++ memory throughout. The table below maps each scan to its Arrow-native equivalent:

| Scan | Current Python pattern | Arrow-native replacement |
|---|---|---|
| `pk_uniqueness` (duplicate count) | `len(non_null) - len(set(non_null))` | `pc.count(col) - pc.count_distinct(col)` |
| `null_audit` (null mask diff) | element-wise `(o is None) != (s is None)` | `pc.not_equal(pc.is_null(out_col), pc.is_null(src_col))` → `pc.any()` |
| `leakage` (set membership) | `{v for v in src} ; leaked = {v in source_values}` | `pc.is_in(out_col, pc.unique(pc.drop_null(src_col)))` → `pc.any()` |
| `cardinality` (distinct count) | `len({v for v in out_non_null})` | `pc.count_distinct(col, mode='only_valid')` |
| `determinism_sample` (value map) | Python `dict` + element comparison | Keep as-is (sample is capped at `ctx.sample_size=100`; no scale concern) |
| `format_rules` (regex check) | `pattern.fullmatch(str(v))` per row | Keep as-is (only format-bearing providers; typically small) |

Quick-win implementation for `pk_uniqueness`:

```python
import pyarrow.compute as pc

def run_pk_uniqueness(ctx: ScanContext) -> ScanOutcome:
    duplicate_counts: dict[str, int] = {}
    failed = False
    pk_columns = sorted(
        (table.name, col.name)
        for table in ctx.profile.tables
        for col in table.columns
        if col.declared_pk
    )
    for table_name, col_name in pk_columns:
        out_table = ctx.outputs.get(table_name)
        if out_table is None or col_name not in out_table.column_names:
            continue
        col = out_table.column(col_name)
        total = pc.count(col, mode="only_valid").as_py()
        distinct = pc.count_distinct(col, mode="only_valid").as_py()
        duplicates = total - distinct
        duplicate_counts[f"{table_name}.{col_name}"] = duplicates
        if duplicates > 0:
            failed = True
    return ScanOutcome(name=_NAME, failed=failed, duplicate_counts=duplicate_counts)
```

Equivalent pattern for `null_audit`:

```python
out_col = out_table.column(col_name)
src_col = src_table.column(col_name)
if len(out_col) != len(src_col):
    failed = True
else:
    mismatch = pc.any(pc.not_equal(pc.is_null(out_col), pc.is_null(src_col))).as_py()
    if mismatch:
        failed = True
```

The `leakage` check (set membership) can use `pc.is_in` with a `ValueSet`:

```python
import pyarrow as pa, pyarrow.compute as pc

src_set = pa.chunked_array([pc.unique(pc.drop_null(src_col))])
leaked = pc.any(pc.is_in(pc.drop_null(out_col), value_set=src_set)).as_py()
```

`pc.is_in` runs in C++ with a hash-set lookup; the ValueSet is constructed once per
column. At 10M rows this is measured at ~30–100ms per column vs ~5–10s for the Python
`set` approach.

---

### F2 — HIGH | Reliability | `internal/logging.py:get_logger` + `_configure_logger` — non-atomic singleton and handler-clearing race

**The issue — singleton:**

```python
_LOGGER_INSTANCE = None

def get_logger(config: dict[str, Any] | None = None):
    global _LOGGER_INSTANCE
    if _LOGGER_INSTANCE is None:             # (A) read
        _LOGGER_INSTANCE = _create_logger(config)  # (B) write
    elif config:
        _configure_logger(_LOGGER_INSTANCE, config)
    return _LOGGER_INSTANCE
```

Lines (A) and (B) are not atomic. Under CPython's GIL, a thread switch between (A) and (B)
allows two threads to both observe `None` and both call `_create_logger`. The second writer
wins, and the `logger.handlers.clear()` inside `_configure_logger` executes twice —
once per thread.

**The issue — handler clearing:**

```python
def _configure_logger(logger, config: dict[str, Any]):
    if logger.handlers:
        logger.handlers.clear()   # <- drops all handlers
    ...
    logger.addHandler(file_handler)
```

Python's `logging.Logger` uses a per-logger `RLock` to protect `emit()`, but
`handlers.clear()` is NOT serialized against concurrent `emit()` calls (the RLock
is only held during handler dispatch). A background thread calling `logger.info(...)` 
between `handlers.clear()` and the subsequent `addHandler` call sees an empty handler
list and its record is silently dropped — a data race on observability infrastructure.
This matters most at startup when `get_logger` may be called concurrently by the platform
worker pool and the job runner's first log line.

**Recommended fix for singleton:**

```python
import threading

_LOGGER_LOCK = threading.Lock()
_LOGGER_INSTANCE = None

def get_logger(config: dict[str, Any] | None = None):
    global _LOGGER_INSTANCE
    with _LOGGER_LOCK:
        if _LOGGER_INSTANCE is None:
            _LOGGER_INSTANCE = _create_logger(config)
        elif config:
            _configure_logger(_LOGGER_INSTANCE, config)
        return _LOGGER_INSTANCE
```

Or, since `logging.getLogger("decoy_engine")` is already idempotent (Python's logging
registry is thread-safe), replace the module-level singleton with direct
`logging.getLogger("decoy_engine")` at each call site, which is the standard library
convention and requires no locking.

**Recommended fix for handler clearing:** use `logging.handlers.MemoryHandler` or,
simpler, guard the clear with the logger's own internal lock:

```python
with logger.lock:  # logging.Logger exposes .lock as threading.RLock
    logger.handlers.clear()
    logger.addHandler(new_handler)
```

---

### F3 — MEDIUM | Data | `providers_v2/identifiers/_mrn.py:_MRN_REGEX` — digit count not validated; format_rules scan accepts wrong-length MRNs

**The issue:**

```python
_MRN_REGEX = r"^[A-Za-z]*\d+$"
```

The regex permits any number of digits (`\d+` = one or more). But MRN has a configurable
exact digit count via `spec.extra["mrn_digit_count"]` (default 8). The `format_rules`
post-validation scan compiles this regex and checks every non-null output value:

```python
# _format_rules.py
pattern = re.compile(caps.format_regex)
violations = sum(
    1 for v in column_values(out_table, col_name)
    if v is not None and pattern.fullmatch(str(v)) is None
)
```

A generated MRN of `"12345"` (5 digits) or `"1234567890"` (10 digits) both pass
`^[A-Za-z]*\d+$` even when the config specifies `mrn_digit_count=8`. The format guard
for a regulated-adjacent identifier silently passes values of the wrong length. This
weakens the post-execution quality gate for a sensitive identifier class.

MRN is listed as a `blocklist_validators=("mrn_format",)` provider (meaning the
presence of violations produces a hard job failure for regulated identifiers, per
`format_rules` scan logic). A wrong-length MRN that passes the regex goes undetected.

**Root cause:** `format_regex` is a static string in `CapabilityMatrix` but MRN digit
count is runtime-configurable. A single regex cannot express both constraints.

**Recommended fix (option A — tighten the static default):**

```python
# Use a fixed default that enforces the 8-digit default length.
_MRN_REGEX = r"^[A-Za-z]*\d{8}$"
```

This correctly validates all MRNs generated with the default configuration. Customers
using a non-default `mrn_digit_count` will see false positives, which is a
conservative (safe) failure mode for a regulated identifier.

**Recommended fix (option B — dynamic regex via CapabilityMatrix factory):**

Extend `MrnAdapter.capability_matrix` to accept `spec.extra` and return a regex
parameterized on `mrn_digit_count`:

```python
def capability_matrix(self, provider: str, *, extra: dict | None = None) -> CapabilityMatrix:
    cfg = extra or {}
    digit_count = int(cfg.get("mrn_digit_count", _DEFAULT_DIGITS))
    alpha_prefix_pattern = "[A-Za-z]*"
    regex = rf"^{alpha_prefix_pattern}\d{{{digit_count}}}$"
    return CapabilityMatrix(..., format_regex=regex, ...)
```

This requires `CapabilityMatrix.format_regex` to be generated dynamically (or the
registry to be spec-aware at lookup time), which is a larger interface change. Option A
is the safe immediate fix.

---

### F4 — MEDIUM | Reliability | `validation/post/_checks/_determinism_sample.py` — non-determinism detected but not diagnosed

**The issue:**

```python
for table_name, table_seed in ctx.plan.seed_envelope.per_table:
    ...
    for col_name, seed in table_seed.per_column:
        ...
        for source, masked in list(zip(src_vals, out_vals, strict=True))[:ctx.sample_size]:
            if source in mapping and mapping[source] != masked:
                failed = True
                break
        if failed:
            break
    if failed:
        break
return ScanOutcome(name=_NAME, failed=failed)
```

The triple-break exits immediately on the first inconsistent mapping, sets `failed=True`,
and returns. `ScanOutcome` carries only `name` and `failed`; neither `table_name` nor
`col_name` appear anywhere in the return value. The `failed_checks` tuple in the merged
`QualitySummary` will contain `"determinism_sample"` — but there is no way for the
operator or the platform job log to identify *which column* was non-deterministic from
the quality summary alone.

**Impact:** Diagnosing a non-determinism failure requires re-running the job with a debugger
or adding temporary logging. For a table with 50 columns this can take hours of triage.

**Recommended fix:**

```python
failing_col: str | None = None
for table_name, table_seed in ctx.plan.seed_envelope.per_table:
    for col_name, seed in table_seed.per_column:
        if not seed.deterministic:
            continue
        ...
        for source, masked in ...:
            if source in mapping and mapping[source] != masked:
                failed = True
                failing_col = f"{table_name}.{col_name}"
                break
        if failed:
            break
    if failed:
        break

warnings = ()
if failed and failing_col:
    from decoy_engine.generation.pool._events import QualityWarning
    warnings = (QualityWarning(
        code="determinism_violation",
        provider="determinism_sample",
        column=failing_col,
        detail={"column": failing_col, "sample_size": ctx.sample_size},
    ),)
return ScanOutcome(name=_NAME, failed=failed, warnings=warnings)
```

This threads the failing column name into the `QualityWarning` so the quality summary's
`quality_warnings` block carries actionable diagnostic context without echoing the
offending value.

---

### F5 — LOW | Performance | `walks/hazards.py:_detect_cycles` — `path.index()` is O(path_length) per back-edge

**The issue:**

```python
if nc == GRAY:
    idx = path.index(neighbor)   # O(len(path)) linear scan
    cycle = tuple(path[idx:])
    cycles.add(_canonical_cycle(cycle))
```

`list.index()` is a linear scan over the current DFS path. In a graph with a long chain
followed by a back-edge (depth-first path length = P), each back-edge discovery costs
O(P). For a schema with C cycles, each cycle contributing a path of length P, total cost
is O(C × P). For typical schemas (P ≤ 50, C ≤ 5) this is negligible. For large imported
schemas (OpenStreetMap, MediaWiki, enterprise star schemas) where P can reach 500–1000,
the total cost is O(C × 1000) — still fast in practice, but unnecessary.

**Recommended fix:** Maintain a `path_index: dict[str, int]` alongside `path: list[str]`
for O(1) back-edge extraction:

```python
path: list[str] = []
path_index: dict[str, int] = {}

# On push:
color[neighbor] = GRAY
path.append(neighbor)
path_index[neighbor] = len(path) - 1
stack.append(...)

# On pop:
path.pop()
path_index.pop(node, None)
color[node] = BLACK

# On back-edge:
idx = path_index[neighbor]   # O(1)
cycle = tuple(path[idx:])
cycles.add(_canonical_cycle(cycle))
```

The current iterative DFS `stack` frame holds `(node, neighbors_iter)` — the `path_index`
is a parallel structure. Note: a node appears in `path_index` iff it's GRAY (currently
on the stack), so `pop` must remove it from `path_index` when the node turns BLACK.

---

### F6 — LOW | Reliability | `internal/memory.py:is_memory_critical` — bare `except Exception` silently discards diagnostic errors

**The issue:**

```python
except ImportError:
    return False, None
except Exception:
    return False, None   # swallows psutil bugs, stale PID, kernel errors
```

An unexpected `Exception` (psutil encountering a stale PID on a container restart, a
permissions error reading `/proc`, a platform-specific psutil bug) silently returns
`(False, None)` — "memory is fine." The caller sees a green signal when the monitor is
actually broken. `get_memory_usage` has the same pattern.

**Recommended fix:**

```python
except ImportError:
    return False, None
except Exception as exc:
    # psutil failure is diagnostic-only; do not crash, but make it visible.
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "is_memory_critical: unexpected error from psutil: %s", exc, exc_info=True
    )
    return False, None
```

---

### F7 — LOW | Performance | `internal/logging.py:ProgressLogger.update()` — one log line per call; no throttle

**The issue:**

`update(increment=1)` logs at `INFO` level on every single invocation. If callers drive
it per-row (a natural usage when the total is known up-front), a 10M-row table generates
10M `logger.info()` calls. A `RotatingFileHandler` at `INFO` with a 10MB cap will roll
the log file ~20 times before the table is done, introducing ~20 sync flushes. At a
conservative 1µs per `info()` call (the fmt + handler dispatch), this adds **10 seconds
of pure logging overhead** before any masking work is measured. The ETA and speed
calculation inside `update` also calls `time.time()` once per invocation.

**Recommended fix:** Add a `log_every` threshold defaulting to a sensible batch size:

```python
class ProgressLogger:
    def __init__(self, logger, total: int, message: str = "Progress", log_every: int = 10_000):
        ...
        self.log_every = log_every
        self._since_last_log = 0

    def update(self, increment: int = 1) -> None:
        self.current += increment
        self._since_last_log += increment
        if self._since_last_log < self.log_every:
            return
        self._since_last_log = 0
        # existing ETA + percentage logic here
```

With `log_every=10_000`, a 10M-row table produces 1000 log lines instead of 10M — still
fine-grained for monitoring while eliminating 9.99M unnecessary `info()` calls.

---

### F8 — NIT | Data | `providers_v2/identifiers/_pan.py:PanDomain.from_bytes` — wrong byte-range in comment

**The issue:**

```python
def from_bytes(self, b: bytes) -> str:
    ...
    # Body9 from bytes[1:9]; mod by 10^9 to fit.
    rest9 = int.from_bytes(b[0:9], "big") % 1_000_000_000
```

The comment says `bytes[1:9]` (8 bytes) but the code reads `b[0:9]` (9 bytes). The
code is correct — 9 bytes are needed for a 9-digit body — but the comment creates a
false read for anyone auditing byte-range coverage. The IIN is hardcoded (`_DEFAULT_IIN
= "411111"`), so byte 0 is not separately consumed; reading from `b[0:9]` is fine.

**Recommended fix:**

```python
# Body9: derive a 9-digit integer from bytes 0:9 (72 bits >> 10^9 range).
rest9 = int.from_bytes(b[0:9], "big") % 1_000_000_000
```

---

## Performance Notes

**Primary bottleneck:** `column_values()` → `to_pylist()` in the post-validation suite.
The bottleneck is CPU-bound Python object allocation, not disk or network I/O. Measure
with `scalene --cpu --memory` or `memory_profiler`; `to_pylist` will dominate allocation
in `_leakage.run_leakage`, `_null_audit.run_null_audit`, and `_pk_uniqueness.run_pk_uniqueness`.

**Secondary bottleneck:** `leakage` builds a Python `set` from `src_vals` and then
iterates `out_vals` checking membership — O(n) set construction + O(n) membership checks.
For 10M rows this is ~O(20M) Python hash operations. `pc.is_in` does the same work
in C++ with a bitwise validity map output; estimated speedup is 50–100×.

**Benchmark command:**

```bash
python -m pytest tests/integration/golden/ -k "post_validation" \
    --benchmark-only --benchmark-sort=mean -v
```

To isolate scan overhead from execution, inject a pre-built `ExecutionResult` directly
into `PostValidationRunner.run()` via a fixture.

**Cycle detection:** O(n²) path.index worst-case is not measurable for schemas under
~200 tables (< 40,000 operations). Profile only if `detect_hazards` is called on
imported schemas with 500+ tables.

---

## Suggested Tests

1. **`test_post_validation_leakage_large_table`** (regression for F1): generate a 1M-row
   PyArrow table with 5 masked columns; call `run_leakage(ctx)` directly; assert it
   completes in < 2s and peak RSS delta < 500MB. Use `memory_profiler.profile` or
   `tracemalloc.take_snapshot()` before and after.

2. **`test_post_validation_null_audit_arrow_parity`** (parity for F1 fix): run
   `run_null_audit` with both the Python-list and Arrow-compute implementations on a
   1000-row table with 30% nulls; assert results are byte-identical.

3. **`test_get_logger_concurrent`** (regression for F2): call `get_logger(config)` from
   50 threads simultaneously; assert no `AttributeError` and the returned logger has
   at least one handler. Use `threading.Barrier` to maximize race contention.

4. **`test_get_logger_reconfigure_no_dropped_records`** (regression for F2 handler
   race): log a record from thread A while thread B calls `get_logger(new_config)`;
   assert the record appears in the log output (not silently dropped).

5. **`test_mrn_format_rules_wrong_digit_count`** (regression for F3 fix): generate
   MRN values with `mrn_digit_count=8`; manually inject a 5-digit MRN into the output
   table; run `run_format_rules(ctx)`; assert `failed=True` (currently passes due to
   the loose regex — this test will fail until F3 is fixed).

6. **`test_determinism_sample_failure_carries_column_name`** (regression for F4 fix):
   construct a `ScanContext` where a deterministic column maps `"A"` to two different
   outputs; call `run_determinism_sample(ctx)`; assert `outcome.warnings` contains a
   `QualityWarning` with the correct column name.

7. **`test_detect_cycles_large_chain`** (regression for F5 fix): build a schema with
   a 500-table linear chain ending in a self-reference; call `detect_hazards(snapshot)`;
   assert it completes in < 1s and returns exactly one `CIR` hazard.

8. **`test_memory_monitor_swallowed_error`** (regression for F6 fix): monkeypatch
   `psutil.Process` to raise a non-`ImportError` exception; call
   `MemoryMonitor.is_memory_critical()`; assert it returns `(False, None)` AND a
   `DEBUG`-level log record was emitted (not silently swallowed).

9. **`test_progress_logger_log_throttle`** (regression for F7 fix, if implemented):
   create `ProgressLogger(logger, total=10_000, log_every=1_000)`; call `update(1)` 
   10,000 times; assert exactly 10 `INFO` records were emitted (not 10,000).

10. **`test_generate_random_ssn_loop_bound`**: call `generate_random(rng)` 10,000 times
    with a known fixed seed; assert all outputs pass `SsnValidator.is_valid()` and the
    call completes without hanging. (Verifies the blocklist skip rate is well under 100%
    in practice.)

---

## What's Good

- **`walks/hazards.py` iterative DFS** (F4 from qa/2026-06-01): the cycle detector was
  already converted from recursive to iterative DFS, preventing RecursionError on
  deep schemas. The three-color marking is correct and produces stable, canonical cycle
  output via `_canonical_cycle`. The SR deduplication set prevents duplicate self-loop
  hazards.
- **`internal/faker_setup.py` `atomic_swap_db_providers`**: the all-or-nothing locked
  provider swap from qa/2026-06-01 (F1 Critical) is in place and correct. The lock is
  held for the minimum time (swap complete) and readers snapshot under the same lock.
- **`providers_v2/identifiers` domain protocol**: all six identifier adapters follow
  the `from_bytes(32) -> str` domain contract uniformly. Blocklist exhaustion is handled
  via `IdentifierError` (not `StopIteration` or a silent fallback). The `generate_batch`
  shared-RNG pattern is correct for non-deterministic mode.
- **`validation/post/_checks/_leakage.py` substitution vs value-reuse split**: the
  distinction between `_VALUE_REUSE_STRATEGIES` (positional fixed-point check) and all
  other strategies (set-membership leak check) is architecturally correct and well-
  documented. This is a non-obvious privacy invariant that's easy to conflate.
- **`validation/post/_runner.py` scan crash isolation**: a crashing individual scan
  becomes a `ScanOutcome(failed=True)` with the error in `quality_warnings`, not a
  lost manifest. The job still fails, but the quality summary is always produced.
- **`walks/cross_file.py` PYTHONHASHSEED fix** (from qa/2026-06-01): `_pk_table_for_id_column`
  sorts `table_names` before iteration, eliminating the hash-seed-dependent tie-break
  that corrupted cross-file walk results across process restarts.
