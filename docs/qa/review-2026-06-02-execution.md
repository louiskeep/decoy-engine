# QA Review — Execution Layer, Connectors, SDK, Relationships, Context
**Date:** 2026-06-02  
**Reviewer:** QA/Performance  
**Scope:** `src/decoy_engine/execution/_pandas_adapter.py`, `execution/_runner.py`, `execution/_transforms.py`, `relationships/_graph.py`, `relationships/_namespace.py`, `context.py`, `sdk.py`, `connectors/s3.py`  
**Prior reviews avoided:** `date_shift`, `fpe`, `synthesize`, `data_discovery`, `_when_gate` (covered 2026-06-02 by `qa/review-2026-06-02-engine`)

---

## 1. Summary

The execution layer is solid: the FK-resolution pipeline is correct, the Kahn topos sort is byte-stable, the `when_gate` security scope-clamp (Dennis C1) is correctly applied in `_transforms.py`, and the S3 connector has proper error classification and multipart streaming. The two issues that matter most are (1) a missing `OR` keyword in `db_io.py`'s SQL injection denylist that allows filter-bypass tautologies — but that file lives in `decoy-platform` (see companion review); and (2) in this repo, the `_parent_map` function pays a per-row `pd.isna()` Python call after already batch-materializing to lists, a correctness-neutral but measurable throughput cost at 1M-row parent tables. No determinism violations found in this scope.

---

## 2. Findings

### F1 — HIGH | Performance | `execution/_pandas_adapter.py` `_parent_map`

**Issue:** After batch-materializing parent columns with `s.tolist()` (the Q7 fix), the null check still calls `pd.isna(x)` per element in a Python generator:

```python
src_lists = [s.tolist() for s in src_series]
...
for i in range(n):
    raw = [col[i] for col in src_lists]
    if any(pd.isna(x) for x in raw):   # ← scalar pd.isna per row per col
        continue
```

`pd.isna()` on a Python scalar goes through a C dispatch but still invokes the pandas type-check machinery. For a 1 M-row parent table with a 3-column composite FK this is ~3 M scalar `pd.isna` calls on top of the already-paid `tolist()`. The Q7 optimization removed the `iloc[i]` bottleneck but left this second hot-path inside the same loop.

**Impact:** Throughput regression at large table × multi-column FK cross-products. Benchmark with `timeit` or `py-spy` flamegraph on a 1 M-row parent table: the null-check loop should show as a prominent frame.

**Fix:** Pre-compute the null mask on the full column lists before the loop using vectorized numpy:

```python
import math
_isna_scalar = lambda x: x is None or (isinstance(x, float) and math.isnan(x))

src_lists = [s.tolist() for s in src_series]
# Vectorize null-check: shape (k, n) bool array -> (n,) bool OR-reduced
if len(src_series) == 1:
    row_has_null = [_isna_scalar(x) for x in src_lists[0]]
else:
    # zip + any() over columns — still Python but avoids pd.isna dispatch
    row_has_null = [
        any(_isna_scalar(src_lists[j][i]) for j in range(len(src_lists)))
        for i in range(n)
    ]

for i in range(n):
    if row_has_null[i]:
        continue
    ...
```

Alternatively, compute null positions on the original pandas Series (before `tolist()`) and materialize a single numpy boolean array, then index it in the loop:

```python
# Pre-tolist null mask — fully vectorized
if len(src_series) == 1:
    row_null = src_series[0].isna().to_numpy()
else:
    row_null = pd.concat(src_series, axis=1).isna().any(axis=1).to_numpy()
src_lists = [s.tolist() for s in src_series]
masked_lists = [s.tolist() for s in masked_series]
for i in range(n):
    if row_null[i]:
        continue
    ...
```

Verify with `python -m timeit` against a 1 M-row fixture: expect ≥ 20 % wall-time reduction in `_parent_map` for 3-column FKs.

---

### F2 — MEDIUM | Design | `execution/_pandas_adapter.py` `get_default_executor()` singleton

**Issue:** `_DEFAULT_EXECUTORS` is a module-level dict mutated without a lock:

```python
_DEFAULT_EXECUTORS: dict[str, ExecutionAdapter] = {}

def get_default_executor() -> ExecutionAdapter:
    substrate = resolve_substrate()
    cached = _DEFAULT_EXECUTORS.get(substrate)
    if cached is None:
        cached = select_execution_adapter()
        _DEFAULT_EXECUTORS[substrate] = cached
    return cached
```

Two concurrent first calls for the same substrate (possible in an async worker or a threaded server) both see `None`, both build an adapter, and the second write silently discards the first. The result is two live adapters where one is immediately orphaned. Not a correctness bug (both adapters are stateless), but it leaks resources and indicates an implicit threading assumption.

**Impact:** Low today (workers run one job at a time in the current executor model). Becomes a real issue if the runner moves to a thread-pool or async model per Dennis S13 parallelism work.

**Fix:** Use `functools.lru_cache` keyed on the substrate string, which Python guarantees is thread-safe via the GIL:

```python
import functools

@functools.lru_cache(maxsize=4)
def _build_executor_for_substrate(substrate: str) -> ExecutionAdapter:
    from decoy_engine.execution._substrate import select_execution_adapter
    return select_execution_adapter()

def get_default_executor() -> ExecutionAdapter:
    from decoy_engine.execution._substrate import resolve_substrate
    return _build_executor_for_substrate(resolve_substrate())
```

The `_reset_default_executor_for_tests()` function needs a corresponding `_build_executor_for_substrate.cache_clear()` call.

---

### F3 — MEDIUM | Correctness | `relationships/_graph.py` `check_orphan_fk_policy_completeness` — empty children silently skips registration

**Issue:** An operator writing a `relationships` config entry that declares `orphan_policy` at the top level but has an empty or missing `children` list:

```yaml
relationships:
  - parent:
      table: employees
      columns: [id]
    orphan_policy: preserve  # set, but...
    children: []             # ...empty: no config_lookup entry is created
```

The `for child in children:` loop runs zero times, so `config_lookup[key]` is never set. When `build_relationship_graph` later looks up the policy for `employees.id -> ...`, it hits the `if key not in config_lookup` guard and raises `PlanCompileError(code='orphan_fk_policy_missing')`. The error message correctly identifies the problem but the cause — empty `children` silently invalidating an otherwise-correct `orphan_policy` — is non-obvious.

**Impact:** Developer confusion: a config that looks correct fails with a cryptic "policy missing" error. No silent data corruption, but a DX footgun.

**Fix:** Add a guard before iterating children:

```python
children = entry.get("children", [])
if not isinstance(children, list) or not children:
    raise PlanCompileError(
        code="orphan_fk_policy_no_children",
        path=f"relationships[{idx}]",
        message=(
            f"Relationship entry for parent {parent_table}.{parent_cols} "
            "declares orphan_policy but has no 'children' entries. "
            "Each relationship entry requires at least one child."
        ),
    )
```

Alternatively, document the empty-children behaviour explicitly in the error for `orphan_fk_policy_missing` ("check that the config entry has a non-empty 'children' list").

---

### F4 — LOW | Performance | `connectors/s3.py` `S3FileSource._client_or_build()` TOCTOU under concurrency

**Issue:** Lazy client construction is not thread-safe:

```python
def _client_or_build(self):
    if self._client is None:
        self._client = _build_s3_client(self.config)
    return self._client
```

Two threads calling the same `S3FileSource` instance concurrently both see `None`, both build a client. The second overwrites the first. Both are valid clients; the first is silently GC'd. Not a correctness bug today (the connector is not shared across threads in current usage) but violates the principle of least surprise for downstream contributors.

**Impact:** Low — resource waste only. No correctness issue in the current single-threaded connector usage.

**Fix:** Use `threading.Lock` or rely on the fact that `boto3.client()` construction is cheap enough to build eagerly in `__init__`. Given the lazy construction reason is explicitly "cheap and side-effect-free," a lock is the safer fix:

```python
import threading

def __init__(self, config: S3Config) -> None:
    super().__init__(config)
    self._client = None
    self._lock = threading.Lock()

def _client_or_build(self):
    if self._client is None:
        with self._lock:
            if self._client is None:  # double-checked
                self._client = _build_s3_client(self.config)
    return self._client
```

Same fix applies to `S3FileSink` and `GCSFileSource`/`GCSFileSink` (same pattern exists in `gcs.py`).

---

### F5 — LOW | Reliability | `sdk.py` `FileSource.head()` default is O(N) with no bail-out

**Issue:** The default `head()` implementation calls `self.list()` and walks the entire object namespace to find one path:

```python
def head(self, path: str) -> FileMeta:
    for item in self.list():
        if item.path == path:
            return item
    raise PermanentError(f"{type(self).name}: head failed, path not found: {path!r}")
```

If a third-party connector omits a native `head()` override and the bucket has millions of objects, calling `head()` triggers an O(N) listing scan. The engine may call `head()` speculatively (e.g., to check existence before `open()`), making this a silent throughput sinker.

**Impact:** Low for first-party connectors (all override `head()`). High for third-party connectors that don't read the docstring carefully.

**Fix:** Add a `maxscan` guard so the default bails after a configurable number of items and raises a helpful error:

```python
_HEAD_DEFAULT_MAX_SCAN = 10_000

def head(self, path: str, *, _max_scan: int = _HEAD_DEFAULT_MAX_SCAN) -> FileMeta:
    """...existing docstring...
    The default implementation scans up to _max_scan items before raising.
    Override with a native HEAD operation for large buckets.
    """
    for i, item in enumerate(self.list()):
        if item.path == path:
            return item
        if i >= _max_scan:
            raise PermanentError(
                f"{type(self).name}: head scan exceeded {_max_scan} items "
                f"without finding {path!r}. Override head() with a native HEAD call."
            )
    raise PermanentError(f"{type(self).name}: head failed, path not found: {path!r}")
```

---

### F6 — NIT | Design | `context.py` `_hkdf_sha256` wrapper adds indirection without value

**Issue:** `_hkdf_sha256` in `context.py` delegates immediately to `decoy_engine.determinism._hkdf.hkdf_sha256` with a zero salt and hardcoded `info` encoding. The wrapper adds a call frame and a docstring explaining it replaced a divergent implementation, but the replacement comment now belongs in a changelog, not in a long-lived module:

```python
def _hkdf_sha256(master: bytes, info: str, length: int = 32) -> bytes:
    """S21 Q11 fix (2026-05-30): routes through the canonical RFC 5869..."""
    from decoy_engine.determinism._hkdf import hkdf_sha256
    return hkdf_sha256(ikm=master, salt=b"\x00" * 32, info=info.encode("utf-8"), length=length)
```

`make_key_resolver` calls `_hkdf_sha256` twice. The wrapper obfuscates the actual HKDF call site and makes it harder to audit the salt choice (zero salt) as an intentional design decision.

**Fix:** Call `hkdf_sha256` directly in `make_key_resolver` (import at module top), drop `_hkdf_sha256`. Add a one-line comment on the zero-salt choice if it needs explaining.

```python
from decoy_engine.determinism._hkdf import hkdf_sha256 as _hkdf_sha256_impl

_HKDF_SALT = b"\x00" * 32  # zero salt: key material via IKM; no external entropy needed

def make_key_resolver(master: bytes, pipeline_label: str) -> Callable[[str], bytes]:
    ...
    pipeline_key = _hkdf_sha256_impl(ikm=master, salt=_HKDF_SALT,
                                     info=f"pipeline:{pipeline_label}".encode(), length=32)
    def resolver(info: str) -> bytes:
        return _hkdf_sha256_impl(ikm=pipeline_key, salt=_HKDF_SALT,
                                 info=info.encode(), length=32)
    return resolver
```

---

### F7 — NIT | Design | `execution/_transforms.py` `_apply_dedupe` implicit `keep='first'`

**Issue:** `df.drop_duplicates(subset=op.columns)` uses `keep='first'` by default but this is not explicit. In a determinism-focused codebase, implicit defaults are a readability hazard — a future author changing the default in a pandas update would not see a clear call site to check.

**Fix:** Pass `keep='first'` explicitly:

```python
return df.drop_duplicates(subset=op.columns, keep='first').reset_index(drop=True)
```

---

## 3. Performance Notes

**Bottleneck classification for this scope:**

| Subsystem | Bottleneck type | Key metric |
|---|---|---|
| `_parent_map` null check | CPU (Python loop) | `pd.isna()` dispatch per row per column |
| FK key building (`_fk_key_value` loop) | CPU (Python loop) | `numbers.Integral` isinstance check per value |
| Arrow ↔ pandas boundary conversion | Memory + CPU | Materialization per table; ~10–15 ms/GB |
| S3 multipart upload (connector) | I/O + network | 5 MB minimum part size appropriate; first-part latency dominates small files |

**What to profile:**

- `py-spy record -o flamegraph.svg -- python -m pytest tests/integration/golden/test_execution_e2e.py -k "1M_rows"` — the `_parent_map` + `_resolve_fk_node` frames should visibly shrink after F1 fix.
- Memory: `memory_profiler` on `_pandas_adapter.run()` with a 1 GB source table — boundary conversion (`tbl.to_pandas()`) is expected to dominate; anything in masking logic that grows beyond ~1.2× source size is a leak candidate.
- Complexity: `_kahn_sorted` is `O((n + e) log n)` — already correct post QA-10 F9 fix. No further algorithmic improvement needed at expected scale.

---

## 4. Suggested Tests

1. **`test_parent_map_null_perf`** — Property test: generate a 1M-row parent DataFrame, call `_parent_map` with 1-, 2-, and 3-column FK keys. Assert wall time ≤ 500 ms (`timeit`). Guards against regression on the Q7 + F1 fix.

2. **`test_get_default_executor_concurrent`** — Spawn 4 threads all calling `get_default_executor()` simultaneously. Assert exactly one adapter instance is returned (verify by identity, `is`). Currently this would fail (or pass non-deterministically) — should be fixed first per F2.

3. **`test_orphan_policy_empty_children`** — Config with a `relationships` entry that has `orphan_policy` set but `children: []`. Assert `PlanCompileError(code='orphan_fk_policy_no_children')` is raised rather than the confusing `orphan_fk_policy_missing` from downstream.

4. **`test_transforms_dedupe_keep_first_is_deterministic`** — Two DataFrames with identical rows in different orders; assert `_apply_dedupe` with `subset=[...]` returns the same rows on both inputs when sorted by the subset columns. This would fail if `keep` were ever changed to 'last' silently.

5. **`test_s3_head_default_scan_limit`** — `S3FileSource` subclass returning 10_001 items from `list()` with the target at position 10_001. Assert `head()` raises `PermanentError` containing "scan exceeded" rather than returning the item or hanging.

6. **`test_fk_key_value_int_float_collision`** — Edge case: parent has `id=1` (int64), child FK reads back as `1.0` (float64 due to nullable int). Assert `_fk_key_value(1.0) == _fk_key_value(1)` and that the FK resolve path maps them to the same masked value. Regression guard for the Dennis slice-2h F2 fix.

---

## 5. What's Good

- **`_kahn_sorted` correctness:** The heapq Kahn topo sort (QA-10 F9) is byte-stable and O((n+e) log n). Pushes children in `sorted()` order before each heappush to preserve the lexicographic guarantee — correct.
- **`_fk_key_value` normalization:** The int/float collapse (`numbers.Integral` → `int`, whole-number float → `int`) correctly handles the pandas int64/float64 dtype split at the FK boundary. `isinstance(value, bool)` guard before `Integral` is the right order (bool is a subtype of int).
- **`_transforms.py` eval scope clamping:** `local_dict={}, global_dict={}` + `engine='numexpr'` is the right two-layer fix for Dennis C1. The `pd.api.types.is_bool_dtype()` fix for nullable `BooleanDtype` (QA-10 F8) is correct.
- **`connectors/s3.py` error mapping:** `ConnectTimeoutError` + `ReadTimeoutError` correctly classified as `TransientError` (not `PermanentError`), fixing the job-abort-on-timeout regression. Abort-on-multipart-failure is best-effort with the original error re-raised — correct priority.
- **`check_orphan_fk_policy_completeness` 4-tuple key:** The S13-rebaseline P1 fix correctly broadens the dedup key from `(parent_table, parent_cols)` to `(parent_table, parent_cols, child_table, child_cols)` so different children of the same parent can have different orphan policies.
- **Serial FK dispatch:** The comment explaining why Faker parallelism is deferred to S13 (shared RNG, determinism break) is correct and clearly communicates the design constraint to future authors.
