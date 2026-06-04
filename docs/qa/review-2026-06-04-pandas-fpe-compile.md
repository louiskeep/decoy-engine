# QA Review: PandasExecutionAdapter, FPE Strategy, compile_plan config validator

**Date:** 2026-06-04
**Scope:**
- `src/decoy_engine/execution/_pandas_adapter.py`
- `src/decoy_engine/execution/_strategies/_fpe.py`
- `src/decoy_engine/config/_pipeline.py`

**Previously reviewed (avoid overlap):** `_strategies/{_hash,_date_shift,_categorical,_formula}`,
`_sampler`, `_runner`, `_when_gate`, `synthesize.py`, `relationships/*`, `connectors/{s3,sftp}`,
`determinism/*`, `expressions.py`, `polars/_polars_adapter.py`.

---

## Summary

The `PandasExecutionAdapter` is well-structured: FK key normalization, vectorized null-mask
extraction, and the S21 Q7 `.tolist()` fixes are all correct. The single most important
issue is a **silent skip when a source table is absent from `sources`** — the same pattern
flagged as F1 (High) in `qa/review-2026-06-04-polars-relationships` for the Polars adapter.
A plan node whose source was not provided is silently skipped; the caller receives a partial
`ExecutionResult` with no error, meaning a table can be left entirely unmasked without
detection. The FPE chunking uses an unnecessary numpy object-array round-trip, and the
`compile_plan` cycle detector iterates `set[str]` deps with PYTHONHASHSEED-sensitive order,
making the reported cycle path non-deterministic.

---

## Findings

### F1 — HIGH | Correctness | `_pandas_adapter.py:198-200` — silent skip for missing source table

**The issue:**

```python
for node in ordered:
    if node.table not in frames:
        continue      # <- silent no-op
```

When a table declared in the plan is absent from the `sources` dict passed by the caller,
every node for that table is silently skipped. The `ExecutionResult.outputs` dict contains
no entry for that table. No exception is raised, no warning is emitted.

**Impact:** A caller that passes an incomplete `sources` map — due to a typo, a missed
table, or a logic bug — receives a successful-looking result in which the missing table's
columns were never masked. The production runner catches this only if it notices the
missing key in `result.outputs`, which is not explicitly checked. This is identical to
the finding in `qa/review-2026-06-04-polars-relationships` (F1, High) for the Polars
adapter; both adapters share the defect.

**Recommended fix:** Add an up-front check immediately after building `frames`:

```python
frames: dict[str, pd.DataFrame] = {t: tbl.to_pandas() for t, tbl in sources.items()}

# Check that every planned table has a source before any masking begins.
planned_tables = {name for name, _ in plan.seed_envelope.per_table}
missing = planned_tables - frames.keys()
if missing:
    raise ExecutionError(
        code="source_table_missing",
        message=(
            f"source table(s) {sorted(missing)!r} are in the plan but were not "
            "provided in `sources`. Pass all planned tables or use run_single "
            "for single-table jobs."
        ),
    )
```

The inner `if node.table not in frames: continue` guard can remain as a defensive
fall-through after the upfront check.

---

### F2 — MEDIUM | Performance | `_pandas_adapter.py:351` — `pd.isna()` called per element inside Python loop

**The issue:**

```python
src_lists = [s.tolist() for s in src_series]  # already plain Python objects
masked_lists = [s.tolist() for s in masked_series]

out: dict[_KeyTuple, _KeyTuple] = {}
for i in range(n):
    raw = [col[i] for col in src_lists]
    if any(pd.isna(x) for x in raw):   # <- pd.isna() in a loop over Python objects
        continue
    src_t = tuple(_fk_key_value(x) for x in raw)
    ...
```

`src_lists` is already materialized Python objects via `.tolist()`. Calling `pd.isna(x)`
on a plain Python value (`int`, `float`, `str`, `None`) boxes the value back through the
pandas API. For a 1M-row parent table with a 3-column composite FK, this is 3M
unnecessary `pd.isna()` dispatch calls.

**Complexity:** O(n × k) `pd.isna()` calls where k = number of parent columns. For
k=1 (simple FK) the overhead is real but modest; for k=3 composite FKs it triples.
Per `cProfile` this is measurable (estimate: ~0.1µs per `pd.isna()` scalar call →
300ms wasted for 3M calls).

**Recommended fix:**

```python
import math as _math

_py_isna = lambda x: x is None or (isinstance(x, float) and _math.isnan(x))

for i in range(n):
    raw = [col[i] for col in src_lists]
    if any(_py_isna(x) for x in raw):
        continue
```

This avoids all pandas API overhead since `src_lists` already contains Python objects.
The `_fk_key_value` downstream also handles the null path correctly.

---

### F3 — MEDIUM | Design / Reliability | `_pandas_adapter.py:413-416` — module-level `_DEFAULT_EXECUTORS` not thread-safe

**The issue:**

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

The read-check-write is not atomic. Under CPython's GIL this is safe in practice (dict
operations are each GIL-protected and the init cost of a duplicate adapter is low). Under
free-threaded Python 3.13+, two threads can both read `None`, both call
`select_execution_adapter()`, and both store — one result is silently discarded. This is
the same pattern flagged as F2 in `api/connectors/crypto.py` and F2 in
`api/jobs/v2_runner.py`.

**Recommended fix:**

```python
import functools

@functools.lru_cache(maxsize=None)
def get_default_executor() -> ExecutionAdapter:
    return select_execution_adapter()
```

Or a `threading.Lock` double-checked lock. `lru_cache` is the idiomatic, GIL-and-
free-threaded-safe choice for singleton init.

---

### F4 — LOW | Performance | `_fpe.py:101` — unnecessary numpy object-array round-trip

**The issue:**

```python
chunks = [list(chunk) for chunk in np.array_split(np.array(values, dtype=object), workers)]
```

`values` is a Python `list[str]`. This line:
1. Boxes every string into a numpy `object` dtype array — O(n) allocation.
2. Calls `np.array_split` to produce sub-arrays — O(workers) slicing.
3. Immediately re-lists each chunk — O(n) unboxing.

Net: two O(n) allocations just to split a Python list. For 100K non-null FPE values
this is ~0.5–1ms of wasted numpy overhead, paid before any Feistel work begins.

**Recommended fix:**

```python
import math as _math

size = _math.ceil(len(values) / workers)
chunks = [values[i * size : (i + 1) * size] for i in range(workers) if values[i * size : (i + 1) * size]]
```

Or more cleanly:
```python
chunks = [values[i::workers] for i in range(workers)]
```

(Interleaved slicing produces equal-length chunks for any `workers`; contiguous slicing
also works and may have better cache behavior.)

**Thread-safety note:** `_V1_FPE._encrypt` was inspected and confirmed stateless —
`FPEStrategy._encrypt` reads only module-level `_CHARSET_INDEX` (a read-only dict built
at import time), calls pure functions (`_feistel`, `_prf`), and invokes thread-safe
`logging.Logger` methods. No instance-state mutation. Concurrent `ThreadPoolExecutor`
calls to `_V1_FPE._encrypt` are race-free under the current implementation.

---

### F5 — NIT | Determinism | `config/_pipeline.py:205` — cycle detector iterates `set[str]`; error path is PYTHONHASHSEED-sensitive

**The issue:**

```python
deps: dict[str, set[str]] = {}
# ...
d: set[str] = set()
d.add(ref_table)
deps[table.name] = d
# ...
stack.append((start, iter(deps[start])))  # iterating a set
```

`deps[start]` is a `set[str]`. Python sets have PYTHONHASHSEED-sensitive iteration
order. The DFS finds cycles correctly regardless of traversal order (any DFS with
three-color marking is complete). However, when a cycle exists, the `path` list at the
point of detection — which becomes the error message — depends on which node was
visited first. Two runs on the same config with different `PYTHONHASHSEED` values can
produce different error messages. For an operator triaging a cycle in a large config,
a stable, reproducible error path matters.

**Recommended fix:** Change `d` to `list[str]` with a manual dedup check, or sort when
pushing to the stack:

```python
stack.append((start, iter(sorted(deps[start]))))
```

Sorting is O(k log k) per push where k = number of parent tables per table, typically
small. Cycle detection semantics are unchanged.

---

## Performance Notes

- **Primary bottleneck (FPE workloads):** `_fpe.py` — O(n) HMAC-SHA256 calls. The
  Feistel round function is the dominant cost. Profile with `py-spy top --pid <pid>`
  to measure; `HMAC.digest()` will show as the hot frame.
- **Secondary bottleneck (composite FK workloads):** `_parent_map` — O(n) per edge,
  called once and cached. F2's `pd.isna()` overhead is additive; significant for wide
  composite FKs on large parent tables.
- **Benchmark command:** `python -m cProfile -s cumulative -m pytest
  tests/integration/golden/test_execution_e2e.py -k fpe --no-header 2>&1 | head -30`
- **Memory:** `_parent_map` builds a `dict[tuple, tuple]` of n entries. For 1M rows
  with 3-column composite keys (3 Python objects per key, ~56 bytes per object on
  CPython), that's ~168MB per parent map. Consider whether large FK tables should
  use a hash-join strategy instead.

---

## Suggested Tests

1. **`test_pandas_adapter_missing_source_table`** (regression for F1): construct a
   2-table plan (A and B); call `adapter.run(plan, {"A": table_a})` with B absent.
   Assert `ExecutionError(code="source_table_missing")` is raised, not a silent
   partial result.

2. **`test_fpe_chunked_vs_serial_parity`** (regression for F4 refactor): run
   `FpeStrategyHandler` with `chunk_count=4` and `chunk_count=1` on the same 10K-row
   column with a fixed seed. Assert `outputs_4chunks == outputs_1chunk` byte-for-byte.

3. **`test_parent_map_composite_null_handling`**: parent table with a 3-column
   composite FK and ~50% null rows. Assert: (a) null child FKs pass through as null,
   (b) non-null FKs remap correctly, (c) rows whose parent key has a null component
   are treated as orphans per the orphan policy.

4. **`test_reference_cycle_error_deterministic`** (regression for F5 fix): config
   with a 3-table reference cycle. Call `PipelineConfig.model_validate()` 10 times.
   Assert all 10 `ValueError` messages are byte-identical. (Run under `PYTHONHASHSEED`
   rotation to expose the regression: set `PYTHONHASHSEED=0`, `PYTHONHASHSEED=1`, etc.)

5. **`test_get_default_executor_concurrent`** (regression for F3 fix): call
   `get_default_executor()` from 10 threads simultaneously; assert no crash and all
   returned objects have the same `id()`.

---

## What's Good

- **FK key normalization (`_fk_key_value`):** the int/float dtype collapse (int64
  parent vs float64-because-null child) is correct and covers the `bool` edge case
  explicitly. This is a subtle invariant that's easy to miss.
- **Pre-mask snapshot:** capturing parent columns before any masking begins
  (lines 176–184) is the correct approach; it means a child FK always sees the
  pre-mask parent key regardless of execution order within the loop.
- **REMAP closure (`_make_remap_fn`):** orphan keys are re-masked through the parent
  column's own strategy handler, making remapped orphans format-consistent with
  legitimately masked values. Correct and non-obvious.
- **S21 Q7 `.tolist()` vectorization** is in place for both `_resolve_fk_node` and
  `_parent_map`; the per-row `iloc[i]` pattern is eliminated.
- **`_fpe.py` thread-safety:** `_V1_FPE._encrypt` is stateless; concurrent Feistel
  calls from `ThreadPoolExecutor` are safe by construction.
