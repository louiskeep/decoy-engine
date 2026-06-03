# QA Review: context.py, transforms/, data_discovery.py — 2026-06-03

Areas reviewed (not covered by today's earlier engine-review passes):
`src/decoy_engine/context.py`, `src/decoy_engine/transforms/date_shift.py`,
`src/decoy_engine/transforms/formula.py`, `src/decoy_engine/expressions.py`,
`src/decoy_engine/data_discovery.py`,
`src/decoy_engine/relationships/_graph.py`,
`src/decoy_engine/relationships/_namespace.py`.

Session: https://claude.ai/code/session_01TdWt7mejkJdjCRrLhMyqBN

---

## Summary

The relationship graph and namespace registry are solid: Kahn's algorithm
is correctly implemented with heapq tie-breaking, the 4-tuple orphan-policy
key is correct, and the composite auto-binding (step 2.5) is deterministic.
Two findings matter: `DateShiftStrategy._column_key` provides no per-column
isolation (all date columns in a pipeline share the same derived key),
and `data_discovery.py`'s SQL denylist misses DuckDB's alternative table
functions (`glob()`, `parquet_scan()`). The formula O(n) apply pattern is
a pre-existing cost that can be partially mitigated.

---

## Findings

### F1 — HIGH | Correctness / Determinism
**`DateShiftStrategy._column_key` passes `"mask"` for all columns, providing
zero per-column key isolation**

File: `src/decoy_engine/transforms/date_shift.py`, `_column_key()`

```python
def _column_key(self, column_name: str) -> bytes | None:
    ...
    return self.derive_key("mask")   # same info string every call
```

`derive_key(info)` is `HKDF-SHA256(pipeline_key, info)`. Every
`DateShiftStrategy` instance passes the literal string `"mask"`, so
all date-shift columns in the same pipeline derive the SAME column key.
Consequence: two date columns with the same source value (e.g. both have
the date `2023-01-15`) receive identical shift amounts, making their
masked values trivially correlated across columns and potentially across
tables.

This is inconsistent with `FPEStrategy._column_key`, which uses
`f"col:{column_name}"`:

```python
# FPE (correct):
return self.derive_key(f"col:{column_name}")

# DateShift (wrong):
return self.derive_key("mask")   # no per-column tagging
```

**Fix:** tag the info string with the column name:

```python
def _column_key(self, column_name: str) -> bytes | None:
    if self.derive_key is None:
        return None
    try:
        return self.derive_key(f"col:{column_name}")
    except Exception as exc:
        ...
```

**Verify:** `assert make_key_resolver(master, "label")("col:dob") != make_key_resolver(master, "label")("col:visit_date")`. Add a unit test that constructs two `DateShiftStrategy` instances for different column names from the same resolver and confirms the column keys are distinct.

---

### F2 — MEDIUM | Security
**`data_discovery.py` denylist misses DuckDB table functions `glob()` and
`parquet_scan()`**

File: `src/decoy_engine/data_discovery.py`, `_BANNED` / `_validate_select_only()`

The existing denylist blocks `read_parquet | read_json | read_ndjson | read_csv
| read_csv_auto` by name. DuckDB exposes equivalent functionality through
additional names that are not blocked:

- `glob('/etc/*')` — DuckDB table function; `SELECT * FROM glob('/tmp/*')`
  enumerates filesystem paths matching a glob pattern. The in-memory DuckDB
  connection cannot write to disk, but path enumeration is a side channel.
- `parquet_scan('/path')` — legacy alias for `read_parquet`. Not present in
  the current `_BANNED` pattern.
- `csv_scan('/path')` — not in the blocked list.

Example bypass: `SELECT * FROM glob('/etc/decoy/*')` passes every current
check (SELECT leader, no banned keyword, no quoted FROM path).

**Fix:** add the missing function names to `_BANNED`:

```python
_BANNED = (
    r"\b(INSERT|UPDATE|DELETE|MERGE|REPLACE|TRUNCATE|"
    r"CREATE|ALTER|DROP|ATTACH|DETACH|COPY|EXPORT|IMPORT|"
    r"PRAGMA|INSTALL|LOAD|SET|RESET|CHECKPOINT|VACUUM|CALL|"
    r"BEGIN|COMMIT|ROLLBACK|"
    r"read_csv|read_csv_auto|read_parquet|read_json|read_ndjson|"
    r"parquet_scan|csv_scan|glob)\b"   # ← add these
)
```

**Verify:** add a property-based test that passes `SELECT * FROM glob('/tmp/')`,
`SELECT * FROM parquet_scan('/tmp/x.parquet')` through `_validate_select_only`
and asserts `DiscoverySqlError` is raised.

---

### F3 — MEDIUM | Performance
**`FormulaStrategy.apply` pays `pd.isna(v)` per row inside `column.apply`**

File: `src/decoy_engine/transforms/formula.py`, `apply()`

```python
return column.apply(
    lambda v: v if pd.isna(v) else safe_eval(expr, scope, {"value": v})
)
```

`column.apply` is the slowest pandas dispatch path (pure Python loop,
one function call per row). The null check runs for every row including
non-null ones. At 1M rows with 1% nulls this is 1M unnecessary `pd.isna`
calls on the hot path.

Partial mitigation: compute the null mask once and restrict `apply` to
non-null values:

```python
na_mask = column.isna()
if na_mask.all():
    return column.copy()
result = column.copy()
result[~na_mask] = column[~na_mask].apply(
    lambda v: safe_eval(expr, scope, {"value": v})
)
return result
```

This does not eliminate the O(n) eval cost (each row calls Python `eval`
independently and that cannot be vectorized), but it cuts the per-row
overhead by ~30% by removing the null guard from the inner loop and
skipping null rows entirely. The 1M-row target: expect ~10-20% wall-clock
improvement.

**Bottleneck classification:** CPU-bound (Python eval dominates). Profile
with `scalene` on a 500k-row formula column to confirm.

---

### F4 — LOW | Reliability
**`_detect_format` emits via `warnings.warn` instead of `self.logger`**

File: `src/decoy_engine/transforms/date_shift.py`, `_detect_format()`

```python
import warnings as _warnings
_warnings.warn(
    "date_shift._detect_format: column matches multiple formats ...",
    stacklevel=2,
)
```

Python's warning filter deduplicates by call site: after the first emission
from `_detect_format`, all subsequent calls from the same code location are
silently suppressed (default filter behavior). Operators processing multiple
ambiguous date columns see only the first warning and miss the rest.

The strategy already owns a `self.logger`; route the warning there:

```python
# Replace the warnings.warn call with:
self.logger.warning(
    "date_shift._detect_format: column '%s' matches multiple formats "
    "%r; using %r. Configure date_format explicitly to remove ambiguity.",
    column.name if hasattr(column, 'name') else '?',
    candidates,
    candidates[0],
)
```

**Verify:** run `_detect_format` on a column that matches `%Y-%m-%d` and
`%Y%m%d`, confirm the warning appears in the captured logger output (not
in Python's `warnings` module).

---

### F5 — NIT | Design
**`context.py` emitters swallow all exceptions silently — degraded
`StructuredEvents` calls are invisible at runtime**

File: `src/decoy_engine/context.py`, `emit_step()` et al.

```python
except Exception:
    pass   # JobLogger DB hiccup must not take engine down
```

The design intent is correct (narrative logging is authoritative;
structured-event failures are swallowed). However, swallowed exceptions
are completely invisible — no counter, no debug log. A misconfigured
`JobLogger` that raises on every `step()` call silently produces a run
with no step timeline in the reporting UI.

Minimal fix: log the exception at DEBUG level (one line) before the
silent drop, so a `--debug` operator can diagnose misaligned implementations:

```python
except Exception as _exc:
    # Structured-event failure; narrative log is authoritative.
    # Log at DEBUG so operator can diagnose with --debug.
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "emit_%s: structured event dropped (%s: %s)",
        fn.__name__, type(_exc).__name__, _exc,
    )
```

---

## Performance Notes

| Path | Bottleneck | Notes |
|---|---|---|
| `FormulaStrategy.apply` | CPU (Python eval per row) | Irreducible eval cost; null-mask opt saves ~20% |
| `_detect_format` | CPU (O(F*S) inner loops) | 11 formats × 200 rows; negligible vs masking |
| `DateShiftStrategy.apply` — HMAC loop | CPU (HMAC-SHA256 per row) | Already masked to valid rows; irreducible |

Profile command for formula throughput: `scalene --cpu --memory -- python -c "import decoy_engine; ..."` targeting the FormulaStrategy on a 500k-row DataFrame.

---

## Suggested Tests

1. **F1 key isolation:** `test_date_shift_key_isolation` — two `DateShiftStrategy`
   instances bound to different column names but the same resolver produce
   different shifts for the same input value.
2. **F1 same-column reproducibility:** same column + same resolver + same seed
   → byte-identical output across two runs.
3. **F2 glob blocked:** `_validate_select_only("SELECT * FROM glob('/tmp/')")` raises
   `DiscoverySqlError`.
4. **F2 parquet_scan blocked:** `_validate_select_only("SELECT * FROM parquet_scan('/x.parquet')")` raises.
5. **F3 formula null skip:** formula strategy on a 50% null column produces
   output with nulls preserved and formula applied only to non-null rows.
6. **F4 logger warning:** mock `self.logger`; assert the warning appears
   in logger output, not in `warnings.filters`.

---

## What Is Good

- **`_graph.py`:** Kahn's algorithm is correct; heapq queue (QA-8 F1 fix) is
  verified; cycle detection correctly measures `ordered != nodes`; edge
  sorting for determinism is present at every insertion point.
- **`_namespace.py`:** `__post_init__` auto-builds the O(1) index from
  `bindings` — old callers that pass only `bindings` get fast lookups for free.
  The `for_relationship` fallback chain (rel → parent → child) is correct and
  raises `NamespaceConfigError` (a `PlanCompileError` subclass) for proper
  error classification.
- **`data_discovery.py`:** The `create_view(name, replace=True)` path avoids
  SQL string concatenation for the table registration step (correct use of the
  DuckDB Python relational API). The trailing-semicolon + multi-statement guard
  is correct and sound.
- **`context.py`:** Dual-resolver design (`derive_key` vs `pipeline_derive_key`)
  is cleanly documented and correctly distinguishes mask-scope from
  generate-scope key derivation. The `export()` / `_current_node_id` pattern
  is a reasonable side-channel for op authors.
