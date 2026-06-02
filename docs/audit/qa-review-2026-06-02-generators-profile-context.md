# QA Review — 2026-06-02
## Modules: generators, profile, context, data_discovery, errors, sdk, walks, instrumentation, disguises

**Reviewer:** QA automation  
**Base SHA:** `484d52d0750cde3f80fc833ae759731a6ec4a9ae` (main)  
**Scope:** Modules not covered by prior 2026-06-02 QA sessions. Excluded (already reviewed today): `determinism/`, `generation/`, `plan/`, `quality/`, `execution/`, `providers_v2/`, `config/`, `storm/`, `internal/`, `expressions.py`, `relationships/`, `validation/`, `transforms/`, `connectors/`.

---

## 1. Summary

The generation pipeline (`generators/columns.py`) and profiling layer (`profile/`) are well-engineered with thorough inline QA traceability — the seed-protocol version bumps, vectorised null injection, and RNG isolation fixes are all correct and needed. The single most important issue is a **silent contract gap in `context.py:make_key_resolver`**: the mask resolver is documented as pipeline-label-free (cross-pipeline FK stability), but the only factory function available always binds a `pipeline_label`, making it trivially easy for callers to build a pipeline-scoped mask resolver while believing they have an instance-scoped one. Everything else in this session is performance (O(n²) cardinality bounds, O(n) year-string ops, full-file loads before sampling) or reliability (TypeError over-catch, DuckDB row-limit bypass, duplicate table name collision).

---

## 2. Findings

### F1 · Critical · Correctness/Determinism · `context.py:make_key_resolver` — mask API contract is undocumented and unenforced

**Code:** `context.py`, `make_key_resolver` (last function in file)

`ExecutionContext.derive_key` is documented as the mask resolver that "pre-binds the tenant master instance key (and only the master)" for cross-pipeline FK stability. But the sole factory `make_key_resolver(master, pipeline_label)` always derives subkeys from `pipeline_key = hkdf(master, f"pipeline:{pipeline_label}")`. If a caller passes different `pipeline_label` values when building `derive_key` for different pipelines, the same source value produces different masked bytes in each pipeline — silently breaking cross-pipeline FK referential integrity. The engine cannot detect this because `derive_key` is typed as `Callable[[str], bytes]` with no embedded contract.

**Impact:** Silent loss of the FK-stability masking guarantee. Two pipelines masking the same `email` column produce different masked bytes; any downstream join on that column returns wrong results. No warning, no error, no manifest entry.

**Fix:** Introduce two separate factories with unambiguous names:

```python
def make_mask_resolver(master: bytes) -> Callable[[str], bytes]:
    """Mask resolver: pipeline-label-free. Same field always maps to the same bytes."""
    if not isinstance(master, (bytes, bytearray)) or len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    def resolver(info: str) -> bytes:
        return _hkdf_sha256(master, f"mask:{info}")
    return resolver

def make_generate_resolver(master: bytes, pipeline_label: str) -> Callable[[str], bytes]:
    """Generate resolver: pipeline-scoped. Different label -> different output."""
    if not isinstance(master, (bytes, bytearray)) or len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    pipeline_key = _hkdf_sha256(master, f"pipeline:{pipeline_label}")
    def resolver(info: str) -> bytes:
        return _hkdf_sha256(pipeline_key, info)
    return resolver
```

Deprecate `make_key_resolver` with a docstring redirect. Update all callers to use the correct factory for their intent.

---

### F2 · High · Performance · `data_discovery.py:run_discovery_sql` — `fetchmany` doesn't prevent DuckDB full-table materialization

**Code:** `data_discovery.py`, lines `con.execute(sql)` → `rel.fetchmany(row_limit)`

`fetchmany(row_limit)` limits Python-side rows returned but not DuckDB's internal query execution. A query like `SELECT * FROM large_table ORDER BY rand()` causes DuckDB to scan the entire table and sort it before the Python cursor returns a single row. The module comment "the query itself is unbounded, but we truncate the materialized result" accurately describes the bug.

**Impact:** Against a 50M-row Parquet, the full table is scanned and potentially sorted in DuckDB's in-process allocator before Python fetches anything. Can OOM a platform worker despite `row_limit=10_000`.

**Fix:** Rewrite the user SQL to push the limit into the planner:

```python
wrapped = f"SELECT * FROM ({sql}) AS _q LIMIT {row_limit}"
try:
    rel = con.execute(wrapped)
except duckdb.Error as exc:
    raise DiscoverySqlError(f"SQL execution failed: {exc}") from exc
# fetchall() is safe now: DuckDB stops at row_limit
raw = rel.fetchall()
```

Alternatively, `LIMIT` injection on the original SQL (requires parsing to detect existing LIMIT; wrapping is simpler and correct).

---

### F3 · High · Performance · `generators/columns.py:_apply_cardinality_bounds` — O(pool × num_rows) donor phase

**Code:** `_apply_cardinality_bounds`, donor-candidate comprehension inside `for pv in ref_values` loop

```python
# Current — O(num_rows) per pool value:
candidates = [i for i, v in enumerate(values) if v == pv and i not in already_free]
```

For a reference pool of 1,000 distinct values and 100,000 generated rows this is 10^8 Python-level comparisons.

**Impact:** A cardinality-bounded reference column with a modest pool causes multi-second pauses on 10K+ row tables. `cProfile` target: `_apply_cardinality_bounds` when `min_per_parent > 0` and `len(ref_values) > 50`.

**Fix:** Build an inverted index before the loop:

```python
# Build once — O(num_rows):
index: dict[Any, list[int]] = {}
for i, v in enumerate(values):
    index.setdefault(v, []).append(i)

# Inside loop — O(count_per_value) per pv instead of O(num_rows):
candidates = [i for i in index.get(pv, []) if i not in already_free]
```

Total donor scan drops from O(pool × num_rows) to O(num_rows).

---

### F4 · High · Performance · `walks/hazards.py:_detect_cycles` — O(n) `path.index` inside DFS back-edge detection

**Code:** `_detect_cycles`, inside the `while stack` loop:

```python
idx = path.index(neighbor)   # O(n) list scan
cycle = tuple(path[idx:])
```

The iterative DFS fix (QA-F4) correctly avoided Python recursion depth, but `path.index(neighbor)` is still O(n) on a list. In a schema with 500 nodes and a dense cycle structure, each back-edge discovery costs O(500).

**Impact:** On large real-world schema imports (MediaWiki ~340 tables, Magento ~400+ tables) the cycle detector degrades to O(n²). Profile with `timeit` against a synthetic 500-table ring graph.

**Fix:** Maintain a parallel O(1) lookup dict:

```python
path: list[str] = []
path_pos: dict[str, int] = {}   # node -> index in path

# On push:
color[neighbor] = GRAY
path_pos[neighbor] = len(path)
path.append(neighbor)

# On pop:
path.pop()
del path_pos[node]
color[node] = BLACK

# Back-edge:
idx = path_pos[neighbor]   # O(1)
cycle = tuple(path[idx:])
```

---

### F5 · High · Correctness · `context.py:emit_step` — TypeError catch too broad; implementation-side TypeErrors silently swallowed

**Code:** `emit_step`, `except TypeError:` block

```python
except TypeError:
    # misdiagnosed as a signature-compat issue even if the TypeError
    # originated inside fn() from a JobLogger bug
    import logging
    logging.getLogger(__name__).debug("emit_step: step() rejected new kwargs...")
    try:
        fn(name, status=status, rows_in=rows_in, rows_out=rows_out)
    except Exception:
        pass
```

Any `TypeError` raised *inside* `fn()` (e.g., a JobLogger method attempting `None + int`) is caught here, misidentified as a signature mismatch, and silently swallowed after the fallback also fails.

**Impact:** JobLogger implementation bugs during structured event emission are completely invisible — no warning log, no crash, no manifest entry. Operators see empty step timelines in the UI with no diagnostic.

**Fix:** Narrow the catch to actual signature mismatches:

```python
except TypeError as exc:
    if "unexpected keyword argument" not in str(exc):
        raise  # real error inside step(), not a compat issue
    # signature compat fallback follows...
```

---

### F6 · High · Performance · `profile/_source.py:_load_file_source` — full file loaded before sampling

**Code:** `_load_file_source`

```python
if fmt == "csv":
    return pd.read_csv(path)   # entire file
if fmt == "parquet":
    return pd.read_parquet(path)   # entire file
```

`sample_rows` is never threaded into `_load_source`. The full file is loaded, then `walk_dataframe` trims it — meaning a 10GB CSV is fully parsed and held in memory before a single row is discarded.

**Impact:** Workers OOM against large source files despite `sample_rows=10_000`. The parameter creates a false safety guarantee.

**Fix:** Thread `sample_rows` into `_load_source` and use it at read time:

```python
def _load_file_source(source_descriptor, sample_rows=None):
    ...
    if fmt == "csv":
        return pd.read_csv(path, nrows=sample_rows)   # stop reading after sample_rows
    if fmt == "parquet":
        import pyarrow.parquet as pq
        pf = pq.read_table(path)
        if sample_rows and len(pf) > sample_rows:
            pf = pf.slice(0, sample_rows)
        return pf.to_pandas()
```

Note: `pd.read_csv(nrows=N)` reads only N rows; Parquet row-group streaming requires PyArrow directly. For S3/GCS sources the same pattern applies: pass `nrows` / `BytesIO` slice before converting to DataFrame.

---

### F7 · Medium · Performance · `generators/columns.py:_generate_reference_column` — per-row dict lookup and `choices(k=1)` in weighted loop

**Code:** `_generate_reference_column`, `elif distribution == "weighted":` branch

```python
for i in range(num_rows):
    elif distribution == "weighted":
        weights = column_config.get("weights")   # per-row dict lookup
        if not weights or len(weights) != len(ref_values):
            weights = None
        values.append(ref_rng.choices(ref_values, weights=weights, k=1)[0])  # k=1 per row
```

`column_config.get("weights")` is a redundant dict lookup on every row. `choices(k=1)` called num_rows times in a Python loop is far slower than one `choices(k=num_rows)` call (the overhead is O(1) setup per call; doing it per-row means O(num_rows) setup costs).

**Impact:** At 100K rows, the weighted path does 100K dict lookups + 100K choices setup vs 1 lookup + 1 choices call. Benchmark: `timeit` `choices(pool, k=1) × 100_000` vs `choices(pool, k=100_000)` — expect 10–100× difference.

**Fix:**

```python
# Resolve once before loop:
if distribution == "weighted":
    weights = column_config.get("weights")
    if not weights or len(weights) != len(ref_values):
        weights = None
    values = ref_rng.choices(ref_values, weights=weights, k=num_rows)
elif distribution == "sequential":
    values = [ref_values[i % len(ref_values)] for i in range(num_rows)]
else:  # random or fallback
    values = ref_rng.choices(ref_values, k=num_rows)
```

Byte-identical RNG output for the same `ref_rng` state.

---

### F8 · Medium · Performance · `generators/columns.py:_generate_distribution_datetime` — O(n) Python string ops for year boundaries

**Code:** `_generate_distribution_datetime`, year_starts/year_ends construction

```python
year_starts = pd.to_datetime([f"{y}-01-01" for y in years_arr])  # O(n) strings
year_ends = pd.to_datetime(
    ["9999-12-31" if y >= 9999 else f"{y + 1}-01-01" for y in years_arr]
)  # O(n) strings
```

Two O(n) Python list comprehensions each produce `num_rows` format strings, then `pd.to_datetime` parses each individually.

**Impact:** For 1M rows: ~2M string format ops + ~2M parse ops in Python before any vectorized work. Profile with `scalene` — the Python fraction will dominate. The existing `rng.choice` + `rng.random` steps are vectorized; this undoes their efficiency.

**Fix:** Vectorized dict-of-arrays construction:

```python
# Vectorized year-to-datetime, no string formatting:
year_starts = pd.to_datetime(dict(year=years_arr, month=np.ones(num_rows, dtype=int), day=np.ones(num_rows, dtype=int)))
next_years = np.where(years_arr >= 9999, 9999, years_arr + 1)
year_ends = pd.to_datetime(dict(year=next_years, month=np.ones(num_rows, dtype=int), day=np.ones(num_rows, dtype=int)))
# For y==9999, set year_end to Dec 31 23:59:59 in ns
mask_9999 = years_arr >= 9999
year_ends_ns = year_ends.view("int64").copy()
year_ends_ns[mask_9999] = pd.Timestamp("9999-12-31 23:59:59.999999999").value
```

---

### F9 · Medium · Correctness · `walks/cross_file.py:storm_profiles_to_snapshot` — duplicate table names silently overwrite each other

**Code:** `storm_profiles_to_snapshot`

If two profiles resolve to the same table name after `_table_name_from_source_label` (e.g., `archive/customers.csv` and `data/customers.csv` both become `customers`), the snapshot contains two `Table` objects with `name="customers"`. Consumers like `infer_cross_file_edges` build `{t.name: t for t in snapshot.tables}` — silently keeping only the last one. Hazard detection and FK inference run on incomplete data with no warning.

**Impact:** Silent loss of one table's column analysis. Inferred edges that should reference the dropped table are missed; HUB/CIR hazards may be wrong.

**Fix:** Detect collisions and either deduplicate (append `_1`, `_2`) or raise:

```python
table_names: list[str] = []
seen: dict[str, int] = {}
for profile in profiles:
    base = _table_name_from_source_label(profile.source_label)
    count = seen.get(base, 0)
    seen[base] = count + 1
    table_names.append(base if count == 0 else f"{base}_{count}")
```

Or raise `ValueError(f"duplicate table name {base!r} from source labels ...")` to force the caller to deduplicate profiles upstream.

---

### F10 · Medium · Design · `errors.py:ValidationError` — not a subclass of `DecoyError`

**Code:** `class ValidationError(Exception)` at line ~55

A caller using `except DecoyError` to catch all engine errors will silently miss `ValidationError`. The docstring explains the wrapping pattern but doesn't warn callers about this exception hierarchy gap. The stated V2.0-C goal was to consolidate exceptions so callers depend on one module — an exception that isn't in the `DecoyError` tree undermines that.

**Impact:** Platform/CLI code relying on `except DecoyError` as a catch-all for engine errors will let `ValidationError` propagate unexpectedly.

**Fix:**

```python
class ValidationError(DecoyError):
    ...
```

If `ValidationError` must remain separately catchable from pure engine errors, use a mixin:

```python
class _ValidationMixin:
    pass

class ValidationError(_ValidationMixin, DecoyError):
    ...
```

Callers can then `except DecoyError` or `except _ValidationMixin` as their use case requires.

---

### F11 · Medium · Correctness · `generators/columns.py:_generate_distribution_categorical` — `other_label` read from wrong object

**Code:** `_generate_distribution_categorical`

```python
other_label = snapshot.get("other_label", "<other>")
```

The docstring says "Operators who want literal-only output... can set `other_label` **on the config**". But the code reads from `snapshot` (a profiler-generated artifact), not from `column_config` (the operator-authored YAML). There is no path for an operator to override this from YAML.

**Impact:** The documented configuration surface doesn't work; `other_label` is effectively read-only for the operator.

**Fix:**

```python
other_label = column_config.get("other_label") or snapshot.get("other_label", "<other>")
```

---

### F12 · Low · Reliability · `instrumentation/timing.py` — module-level `psutil.Process()` breaks after fork

**Code:** `_PROCESS = psutil.Process()` at module top

`psutil.Process()` captures the calling PID at import time. After `os.fork()`, the child process inherits `_PROCESS` pointing at the parent's PID. Any `_rss_kb()` call in the child reports the parent's RSS — a completely wrong value.

**Impact:** Latent; current single-threaded execution model is safe. Becomes a silent correctness bug if the engine adopts `multiprocessing` for parallel generation.

**Fix:** Lazy construction — the `psutil.Process()` call itself costs ~1μs, negligible vs the `memory_info()` syscall:

```python
def _rss_kb() -> int:
    return int(psutil.Process().memory_info().rss / 1024)

# Remove module-level _PROCESS entirely.
```

---

### F13 · Low · Data · `data_discovery.py:_coerce` — DuckDB complex types produce Python repr, not JSON

**Code:** `_coerce`, fallthrough `return str(value)`

DuckDB returns Python `dict` (STRUCT), `list` (LIST/ARRAY), and `decimal.Decimal` for those column types. None match the early-return guards; all fall through to `str(value)` which produces Python repr (`{'a': 1}` instead of `{"a": 1}`) — invalid JSON. The `DiscoveryResult` docstring promises "JSON-friendly Python types."

**Impact:** A SELECT against a Parquet file with STRUCT or ARRAY columns returns unparseable strings. FastAPI serializes them as strings, not nested objects.

**Fix:** Add explicit cases:

```python
import decimal

def _coerce(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    if isinstance(value, decimal.Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value)
```

---

### F14 · Low · Reliability · `profile/_serialize.py:profile_from_json` — no schema_version validation

**Code:** `_profile_from_dict`, line `schema_version=data["schema_version"]`

There is no check that `data["schema_version"] == 1`. Deserializing a future schema_version 2 blob (which may have new required fields) produces a cryptic `KeyError` rather than a clear version-mismatch error.

**Fix:**

```python
version = data.get("schema_version")
if version != 1:
    raise ValueError(
        f"profile_from_json: unsupported schema_version {version!r}; expected 1."
    )
```

---

### F15 · Nit · Design · `walks/diff.py:DriftResult.new_pii` — unnecessary `default_factory` for immutable sentinel

**Code:** `DriftResult`, field `new_pii`

```python
new_pii: tuple[dict, ...] = field(default_factory=tuple)
```

`()` is immutable and shareable. `field(default=())` is correct and idiomatic; `default_factory=tuple` constructs a new empty tuple on every instantiation for no benefit.

**Fix:**

```python
new_pii: tuple[dict, ...] = field(default=())
```

---

### F16 · Nit · Design · `disguises/loader.py` — overly broad `except Exception` on Disguise construction

**Code:** `load_disguises`, `except Exception as exc:` on `Disguise(**data)`

The comment says "pydantic.ValidationError + any other constructor failure." The broad catch also swallows `MemoryError`, `RecursionError`, and `SystemExit`.

**Fix:**

```python
from pydantic import ValidationError as PydanticValidationError

except (PydanticValidationError, TypeError, ValueError) as exc:
    _log.error("load_disguises: schema validation failed for %s: %s", path.name, exc)
```

---

## 3. Performance Notes

| Bottleneck | Module | Complexity | Type | How to measure |
|---|---|---|---|---|
| `_apply_cardinality_bounds` donor scan | generators/columns.py | O(pool × rows) → O(rows) | CPU | `cProfile` on 10K rows, 100-value pool |
| `_generate_reference_column` weighted loop | generators/columns.py | O(rows) setup cost × rows | CPU | `timeit`: `choices(k=1) × N` vs `choices(k=N)` |
| `_generate_distribution_datetime` year strings | generators/columns.py | O(rows) Python string ops | CPU | `scalene` — Python fraction will dominate |
| `_detect_cycles` back-edge `path.index` | walks/hazards.py | O(nodes) per back-edge | CPU | `timeit` against a 500-table synthetic ring |
| `_load_file_source` full-file load | profile/_source.py | O(file_size) before sampling | I/O + memory | `memory_profiler` on a 1GB CSV |
| `run_discovery_sql` unbounded DuckDB scan | data_discovery.py | O(full_table_scan) | DuckDB CPU/mem | `EXPLAIN` + RSS before/after `con.execute` |
| `FileSource.head()` default scan | sdk.py | O(bucket_files) | I/O | Count `list()` calls per `head()` call in integration test |

---

## 4. Suggested Tests

1. **Mask resolver pipeline independence (documents F1 contract gap):**
   ```python
   def test_mask_resolver_pipeline_independence():
       master = os.urandom(32)
       r_a = make_key_resolver(master, "pipeline-a")
       r_b = make_key_resolver(master, "pipeline-b")
       # This currently FAILS (they differ) — test pins the broken contract
       # so a fix is visible:
       assert r_a("col:email") != r_b("col:email")  # documents the gap
   ```

2. **run_discovery_sql row_limit memory guard (F2):**
   ```python
   def test_discovery_row_limit_stops_duckdb_scan(tmp_path):
       # Write a Parquet with 200_000 rows; confirm DuckDB doesn't materialize all.
       # Measure: query with LIMIT in subquery vs not; compare via EXPLAIN scan count.
       result = run_discovery_sql("SELECT * FROM t ORDER BY val", {"t": str(pq_path)}, row_limit=10)
       assert len(result.rows) == 10
   ```

3. **_apply_cardinality_bounds determinism + O(n) regression (F3):**
   ```python
   @pytest.mark.parametrize("pool_size", [10, 100, 500])
   def test_cardinality_bounds_deterministic(pool_size):
       pool = list(range(pool_size))
       rng = random.Random(42)
       values = rng.choices(pool, k=1000)
       r1 = _apply_cardinality_bounds(values[:], pool, 2, 5, rng=random.Random(42))
       r2 = _apply_cardinality_bounds(values[:], pool, 2, 5, rng=random.Random(42))
       assert r1 == r2
   ```

4. **emit_step does not swallow internal TypeErrors (F5):**
   ```python
   def test_emit_step_reraises_internal_typeerror():
       class BuggyLogger:
           def step(self, name, **kw):
               raise TypeError("None is not str")  # impl bug, not compat issue
       with pytest.raises(TypeError, match="None is not str"):
           emit_step(BuggyLogger(), "test", status="running")
   ```

5. **duplicate table names in cross_file_walk (F9):**
   ```python
   def test_cross_file_walk_duplicate_table_name_collision():
       profiles = [
           StormProfile(source_label="data/customers.csv", ...),
           StormProfile(source_label="archive/customers.csv", ...),
       ]
       result = run_cross_file_walk(profiles)
       # Either unique names or explicit error; silent overwrite is the bug:
       table_names = [t for t in result.snapshot_summary]
       # assert len(set(table_names)) == len(table_names)  # deduplicated
   ```

6. **ValidationError caught by DecoyError (F10):**
   ```python
   def test_validation_error_is_decoy_error():
       e = ValidationError("bad config")
       assert isinstance(e, DecoyError)
   ```

7. **profile_from_json schema_version guard (F14):**
   ```python
   def test_profile_from_json_rejects_unknown_version(valid_profile_json):
       data = json.loads(valid_profile_json)
       data["schema_version"] = 99
       with pytest.raises(ValueError, match="unsupported schema_version"):
           profile_from_json(json.dumps(data))
   ```

8. **profile_source seed non-determinism warning (regression for Q15):**
   ```python
   def test_profile_source_warns_when_no_seed(tmp_csv):
       config = {"sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_csv)}}}
       with pytest.warns(UserWarning, match="non-deterministic"):
           profile_source(config)  # no seed= kwarg, no global_settings.seed
   ```

9. **profile_source same seed → same hash (F6 regression test):**
   ```python
   @hypothesis.given(seed=st.integers(0, 2**31 - 1))
   def test_profile_source_reproducible(seed, tmp_csv):
       config = {"sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_csv)}}}
       h1 = profile_hash(profile_source(config, seed=seed, sample_rows=100))
       h2 = profile_hash(profile_source(config, seed=seed, sample_rows=100))
       assert h1 == h2
   ```

---

## 5. What's Good

- **`generators/columns.py`**: Exceptionally thorough inline QA traceability — every seed-protocol version bump, vectorised null injection fix, and RNG isolation change is cross-referenced to a named QA finding. Rare in production codebases and genuinely useful for auditing.
- **`generators/derivation.py`**: The R3.10 fingerprint-based seeding design is correct — severing output from the display column name using `json.dumps(sort_keys=True)` as a canonical form is reproducible and rename-stable.
- **`data_discovery.py`**: Security hardening is thorough. The banned-keyword regex covers all known DuckDB file-reading functions, the quoted-path-FROM guard closes the `FROM '/etc/...'` bypass, and the single-statement check blocks multi-statement injection. All three layers are documented with their QA finding provenance.
- **`walks/hazards.py`**: Iterative DFS cycle detector (QA-F4) is correct and produces byte-identical output to the recursive version. Canonical cycle normalization prevents duplicate reporting. Good.
- **`profile/_types.py`**: `__post_init__` validators on `ColumnProfile`, `TableProfile`, and `Profile` fail loud at construction time — catching cardinality invariant violations (`null_count > row_count`, `distinct_count > row_count`, composite column length mismatch) before they corrupt downstream planner artifacts.
- **`instrumentation/timing.py`**: The zero-overhead short-circuit `if collector is None: yield; return` is correct; the documented <2% overhead budget is achievable with this design and the test that pins it is the right approach.
- **`profile/_pii.py`**: Clean separation of built-in vs custom detector handling — custom detectors drop silently, unrecognized built-ins log a WARNING. The CI-level symmetry test is the right enforcement mechanism.
- **`profile/_source.py`**: S3 connector wraps network and auth errors without leaking botocore internals into logs (Q17/Q18 pattern). The `response["Body"]` context manager for connection pool release is correct.
