# QA Review: config/, context.py, data_discovery.py

**Date:** 2026-06-05  
**Reviewer:** Claude (QA agent, session GnqbS)  
**Repos:** decoy-engine  
**Branch:** claude/sleepy-brahmagupta-GnqbS  
**Files reviewed:**
- `src/decoy_engine/config/_pipeline.py`
- `src/decoy_engine/config/_tables.py`
- `src/decoy_engine/config/_sources.py`
- `src/decoy_engine/context.py`
- `src/decoy_engine/data_discovery.py`

**Prior QA overlap:** None -- all five files are fresh territory. The engine's `validation/post/`, `providers_v2/identifiers/`, `internal/`, and `walks/` modules were covered in the 2026-06-05 session that preceded this one.

---

## 1. Summary

`data_discovery.py` has a security gap that needs immediate patching: DuckDB table-function aliases (`parquet_scan`, `csv_scan`, `glob`) are not in the denylist, leaving the discovery surface as an arbitrary-file-read vector. Everything else -- the PipelineConfig validation choke-point, the config models, and the ExecutionContext -- is well-designed. The config layer correctly handles the FC-1 mixed-mode shape, uses iterative DFS for cycle detection (fixing a prior recursion hazard), and validates reference-column params eagerly at config load time rather than at runtime. Two secondary concerns: `emit_step()` swallows TypeError too broadly, and `GenerateColumnConfig` accepts unknown extras without warning the author of likely typos.

---

## 2. Findings

### F1 -- CRITICAL | Security | `src/decoy_engine/data_discovery.py`

**What the code does:** `run_discovery_sql` opens an in-memory DuckDB connection, registers on-disk Parquet files as views, and executes a user-supplied SELECT statement. `_validate_select_only` enforces a keyword allowlist/denylist.

**Issue:** The denylist bans `read_parquet` and `read_csv_auto` but misses their DuckDB aliases:

| Banned | Not banned (same semantics) |
|---|---|
| `read_parquet` | `parquet_scan` |
| `read_csv_auto` | `csv_scan` |
| `read_json` | `json_scan` |
| (none) | `glob` (filesystem enumeration) |
| (none) | `FROM glob('...')` via relational API |

Any of the right-column forms bypasses `_BANNED_RE` and executes without error:
```sql
SELECT * FROM parquet_scan('/data/keys/.decoy_master_key')
SELECT filename FROM glob('/data/**')
```

**Impact:** Arbitrary file read as the platform OS user. On the V1 single-node Docker deployment, `/data/keys/.decoy_master_key` and `/data/keys/.tm_secret_key` are in the default mount path. A user with access to the data-viewer SQL tab can exfiltrate the master encryption key.

**Fix:**
```python
# In _BANNED (the raw pattern string):
_BANNED = (
    r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|TRUNCATE|"
    r"CREATE|ALTER|DROP|ATTACH|DETACH|COPY|EXPORT|IMPORT|"
    r"PRAGMA|INSTALL|LOAD|SET|RESET|CHECKPOINT|VACUUM|CALL|"
    r"BEGIN|COMMIT|ROLLBACK|"
    r"read_csv|read_csv_auto|read_parquet|read_json|read_ndjson|"
    # Add aliases:
    r"parquet_scan|csv_scan|json_scan|glob|read_blob|"
    r"scan_parquet|scan_csv)\b"  # historical aliases in older DuckDB builds
)

# Also extend _QUOTED_PATH_FROM_RE to catch FROM glob(...):
_GLOB_FUNC_RE = re.compile(r"\bglob\s*\(", re.IGNORECASE)

# In _validate_select_only, add:
if _GLOB_FUNC_RE.search(body):
    raise DiscoverySqlError(
        "glob() is not allowed in discovery queries."
    )
```

**Verify:**
```python
# Each of these must raise DiscoverySqlError:
pytest.raises(DiscoverySqlError, _validate_select_only, "SELECT * FROM parquet_scan('/etc/passwd')")
pytest.raises(DiscoverySqlError, _validate_select_only, "SELECT * FROM csv_scan('/etc/hosts')")
pytest.raises(DiscoverySqlError, _validate_select_only, "SELECT filename FROM glob('/data/**')")
```

**Regression check:** Verify that valid queries (`SELECT * FROM my_table`, `WITH cte AS (SELECT 1) SELECT * FROM cte`) still pass.

---

### F2 -- MEDIUM | Reliability | `src/decoy_engine/context.py:emit_step` (lines ~107-135)

**Issue:** `emit_step` catches `TypeError` from an older `step()` signature and falls back to the 3-kwarg form:
```python
except TypeError:
    try:
        fn(name, status=status, rows_in=rows_in, rows_out=rows_out)
    except Exception:
        pass
```
If `step()` raises `TypeError` for a **different** reason (a real coding bug, e.g., `step()` internally calls `some_dict[None]` which is not a TypeError -- bad example, but: `step()` internally calls `int("not_a_number")` would be ValueError, but imagine a method that incorrectly passes a non-string to a string formatter), the outer `except TypeError` catches it, the fallback fires, and both errors are silently suppressed. The only signal is a single DEBUG log line which most deployments won't surface.

**Impact:** A genuine bug in the platform `JobLogger.step()` implementation could be invisible for many runs. The structured event timeline in the reporting UI would silently stop updating without any error visible to the operator.

**Preferred fix:** Inspect the function's signature once (cached) and branch on arity, rather than catching TypeError:
```python
import inspect, functools

@functools.lru_cache(maxsize=None)
def _step_accepts_new_kwargs(fn) -> bool:
    try:
        sig = inspect.signature(fn)
        return "node_id" in sig.parameters
    except (ValueError, TypeError):
        return False

def emit_step(logger, name, *, status="running", ...):
    if logger is None:
        return
    fn = getattr(logger, "step", None)
    if fn is None:
        return
    try:
        if _step_accepts_new_kwargs(fn):
            fn(name, status=status, rows_in=rows_in, rows_out=rows_out,
               error_class=error_class, error_msg=error_msg, node_id=node_id)
        else:
            fn(name, status=status, rows_in=rows_in, rows_out=rows_out)
    except Exception:
        pass
```
The `lru_cache` pins the signature check per function object so it runs once per logger type across the lifetime of the process.

**If the signature-inspection approach is too invasive**, at minimum add an `except TypeError` guard that re-raises if the error message does NOT mention the new kwargs:
```python
except TypeError as exc:
    if "node_id" not in str(exc) and "error_class" not in str(exc):
        raise  # real bug, not a version-compat issue
    # version compat fallback ...
```

---

### F3 -- MEDIUM | Correctness | `src/decoy_engine/config/_pipeline.py:_reference_graph_valid` (lines ~112-160)

**Issue:** Inside the cycle-detection DFS, `path.index(parent_name)` is called when a back-edge is found to build the human-readable cycle description:
```python
if ps == GRAY:
    idx = path.index(parent_name)
    cycle = [*path[idx:], parent_name]
    raise ValueError(f"reference cycle in generate config: {' -> '.join(cycle)}")
```
`list.index()` is O(n) where n is the length of `path` (the current DFS stack depth). This runs at most once per detected cycle (after which the validator raises and exits), so the absolute cost is bounded and the issue will not manifest in practice. However, it violates the standard expectation that DFS cycle detection is O(V + E).

**Fix:** Maintain a parallel dict `path_idx: dict[str, int]` mapping each node currently on the path to its index, updated alongside `path.append` / `path.pop`:
```python
path: list[str] = []
path_idx: dict[str, int] = {}

# on push:
path_idx[start] = len(path)
path.append(start)

# on pop (replace path.pop() call site):
node_to_pop = path.pop()
del path_idx[node_to_pop]

# on back-edge:
idx = path_idx[parent_name]  # O(1)
cycle = [*path[idx:], parent_name]
raise ValueError(...)
```

---

### F4 -- LOW | Design | `src/decoy_engine/config/_tables.py:GenerateColumnConfig`

**Issue:** `GenerateColumnConfig` uses `extra="allow"` (required by the V1 flat-params design). The `_type_params_present` validator catches *missing* required params (e.g., no `faker_type` for a `faker` column). But it does not catch *unrecognized* extras that look like typos. A YAML like:
```yaml
type: faker
faekr_type: email   # typo: "faekr_type" instead of "faker_type"
```
passes `_type_params_present` (because `extras.get("faker_type")` returns None and the validator raises... wait, it DOES raise). Let me re-examine.

Actually: `if self.type == "faker" and not extras.get("faker_type"): raise ValueError(...)` -- this DOES catch the typo case because `extras.get("faker_type")` is falsy when the key is absent. The typo `faekr_type` is not `faker_type`, so the validator correctly rejects it.

**Revised finding:** The validator is correct for the four named types. The residual issue is that `extra="allow"` passes THROUGH all unknown extras (e.g., `weight: 0.5`, `seed: 42`, `arbitrary_future_key: value`) silently, even for types where they are meaningless. There is no warning path for unexpected extras. This is the documented trade-off (per the Dennis S6-ENG-1 gate Q-S6-1 comment), but it means a user debugging why their `weight: 0.5` on a `faker` column has no effect will get no signal from the validator.

**Low-priority fix** (for V2.1 when the extra-params surface is frozen): switch to `extra="forbid"` per type by adding per-type subclasses, or add a `_warn_unknown_extras` model validator that emits a structured warning (not an error) for unknown keys.

---

### F5 -- LOW | Security | `src/decoy_engine/context.py:make_key_resolver` (line ~268)

**Issue:** `make_key_resolver` validates that `master` is exactly 32 bytes:
```python
if not isinstance(master, (bytes, bytearray)) or len(master) != 32:
    raise ValueError("master key must be 32 bytes")
```
But `pipeline_label` is an unconstrained string. It is used as HKDF `info` after `.encode("utf-8")`. Two edge cases:
1. **NUL bytes:** Python strings can contain `\x00`. If a label is `"pipeline:foo\x00bar"`, the HKDF `info` parameter includes the NUL byte. This is technically valid HKDF behavior (info is opaque bytes), but NUL bytes in labels are almost certainly unintentional and could cause divergence if a C implementation trims at NUL.
2. **Excessively long labels:** A 10 MB `pipeline_label` encodes to 10 MB of HKDF `info`, causing a very slow (or memory-intensive) HKDF computation. This is a minor DoS vector if the platform passes user-supplied pipeline names without length validation upstream.

**Fix:**
```python
if not pipeline_label or len(pipeline_label) > 512:
    raise ValueError(
        "pipeline_label must be a non-empty string of at most 512 characters"
    )
if "\x00" in pipeline_label:
    raise ValueError("pipeline_label must not contain NUL bytes")
```

---

### F6 -- LOW | Correctness | `src/decoy_engine/config/_pipeline.py:_per_table_kind_consistency` (line ~73)

**Issue:** The `row_count` check uses `isinstance(table.row_count, int)`, which admits `bool` (`True` / `False`) since `bool` is a subclass of `int` in Python:
```python
if not isinstance(table.row_count, int) or table.row_count < 0:
    raise ValueError(...)
```
In practice, Pydantic's `int` field coerces `True` to `1` and `False` to `0` before this validator runs, so `isinstance` sees the coerced integer. `row_count: True` in YAML becomes `row_count=1` -- which passes validation and generates one row. This is benign but surprising.

**Fix:** Use `type(table.row_count) is int` if you want to strictly exclude booleans, or add a comment noting that Pydantic coercion makes the `isinstance` check sufficient:
```python
# Pydantic coerces bool to int before validators run (True -> 1, False -> 0);
# isinstance(True, int) is True in Python, but this is safe here.
```

---

### F7 -- NIT | Design | `src/decoy_engine/config/_sources.py:S3Source`

**Issue:** `S3Source.endpoint_url` and `GCSSource.credentials_ref` have multi-sentence `description=` Field arguments. These are useful for the OpenAPI schema, but the descriptions are on the Pydantic Field, not in a docstring. If the field is serialized (e.g., `model_dump()`) and then re-validated, the descriptions are silently dropped. This is not a bug -- descriptions are metadata only -- but it's worth noting that the human-readable intent here is accurate (credentials are opaque; endpoint_url is for S3-compatible services), and nothing in the engine should be reading the description at runtime.

**No fix required.** Confirmed intent matches implementation.

---

## 3. Performance Notes

- **`_reference_graph_valid` complexity:** O(V + E) DFS with O(n) `path.index()` at cycle detection (runs at most once per invocation, on error path). Functional cost is negligible; the fix in F3 is for correctness hygiene.
- **`run_discovery_sql` bottleneck:** DuckDB `connect(":memory:")` + `read_parquet(...).create_view(...)` opens a new in-process DuckDB instance per query. DuckDB initialization has a non-trivial startup cost (~5-50 ms depending on extension loading). For a latency-sensitive endpoint, caching the connection per-request thread (or using a DuckDB connection pool) would reduce overhead. **Profile:** `timeit` `duckdb.connect(":memory:")` vs reusing a persistent in-memory DB across queries.
- **`fetchmany(row_limit)` vs `fetchall`:** Correct choice. For wide result sets (many columns, 10,000 rows), `fetchmany` still materializes a potentially large structure. Consider capping not just row count but also total estimated bytes (`len(cols) * row_limit * avg_cell_size`).
- **`_pipeline.py` validator chain:** Three `@model_validator(mode="after")` decorators on `PipelineConfig` run in definition order. Each iterates over `self.tables`. For a 1,000-table pipeline, this is three O(n) passes. For realistic configs (< 100 tables) this is irrelevant.

---

## 4. Suggested Tests

| Test | Priority | File |
|---|---|---|
| `test_discovery_parquet_scan_rejected` -- `SELECT * FROM parquet_scan('/etc/passwd')` raises `DiscoverySqlError` | CRITICAL | `tests/unit/test_data_discovery.py` |
| `test_discovery_csv_scan_rejected` -- `SELECT * FROM csv_scan('/etc/hosts')` raises | CRITICAL | `tests/unit/test_data_discovery.py` |
| `test_discovery_glob_rejected` -- `SELECT filename FROM glob('/data/**')` raises | CRITICAL | `tests/unit/test_data_discovery.py` |
| `test_discovery_valid_select_passes` -- regression: normal SELECT still works after denylist extension | HIGH | `tests/unit/test_data_discovery.py` |
| `test_emit_step_real_typeerror_propagates` -- JobLogger.step raises TypeError for a non-compat reason; verify it surfaces | MEDIUM | `tests/unit/test_context.py` |
| `test_make_key_resolver_nul_label_rejected` -- NUL byte in pipeline_label raises ValueError | LOW | `tests/unit/test_context.py` |
| `test_make_key_resolver_overlong_label_rejected` -- 1000-char label raises ValueError | LOW | `tests/unit/test_context.py` |
| `test_reference_graph_cycle_1000_tables` -- 1000-node cycle raises ValueError without RecursionError | LOW | `tests/unit/test_config.py` |
| `test_generate_column_type_params_typo` -- `faker` column with `fkaer_type: email` raises ValidationError | MEDIUM | `tests/unit/test_config.py` |

---

## 5. What's Good

- **`_validate_select_only` architecture** is sound: strip comments first, then check leader keyword, then scan for banned keywords, then check quoted-path FROM. The layered approach means each guard is independent and easier to audit than a single monolithic regex.
- **`run_discovery_sql` uses the DuckDB relational API** (`con.read_parquet(path).create_view(name, replace=True)`) instead of string-formatting the path into SQL. This eliminates path-injection through the view registration step -- the fix for F1 is additive (extend the denylist) rather than architectural.
- **Iterative DFS in `_reference_graph_valid`** (the post-QA-walks-F4 fix) correctly eliminates the recursion-depth hazard for large pipeline configs. The three-color marking with explicit stack and path list is a clean implementation.
- **`PipelineConfig` extra="forbid"** at every model level makes unknown-key detection a hard validation error at the choke-point, not a silent pass-through. This catches YAML typos (e.g., `taables:` instead of `tables:`) before any engine code runs.
- **`context.py` emit_* helpers** are well-structured: null-check on logger, getattr-lookup (no hard import of StructuredEvents), and top-level exception swallow (DB hiccup in JobLogger mustn't take the engine down). The separation of `Logger` (runtime_checkable) from `StructuredEvents` (not runtime_checkable) is the correct design for optional structured surfaces.
- **`make_key_resolver` 32-byte master check** is explicit and raises early. The docstring cross-reference to `api/keys/make_resolver` (the platform counterpart) is helpful for auditability.
