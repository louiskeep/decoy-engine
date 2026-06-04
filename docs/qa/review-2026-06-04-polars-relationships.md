# QA Review: Polars Execution Adapter + Relationship Graph
**Date:** 2026-06-04
**Reviewer:** QA/Performance (automated session)
**Scope:**
- `src/decoy_engine/execution/polars/_polars_adapter.py`
- `src/decoy_engine/execution/polars/_conversion_boundary.py`
- `src/decoy_engine/execution/polars/_source_reader.py`
- `src/decoy_engine/execution/polars/_target_writer.py`
- `src/decoy_engine/relationships/_graph.py`
- `src/decoy_engine/relationships/_namespace.py`

---

## 1. Summary

The Polars adapter and relationship graph are generally well-structured with
solid determinism discipline and clear Oracle fallback semantics. The most
important issue is a **silent silent-skip** in `_run_polars_native` when a
work-node references a table absent from `sources`: the column masking is
dropped with no error, no log entry, and no diagnostic in `quality_metrics`.
A secondary concern is the **unnecessary pa->pl->pa double conversion** in the
Oracle fallback path, which costs latency and is not guaranteed lossless for
all Arrow types.

---

## 2. Findings

### F1 - High | Correctness
**Silent skip when `node.table not in frames` in `_run_polars_native`**

`_polars_adapter.py`, inside `_run_polars_native`:
```python
for node in work:
    if node.table not in frames:
        continue   # <-- column masking silently dropped
```
The `work` list is built from the plan and can reference tables that are not
in `sources` (e.g. a column in a table the caller did not pass). When that
happens the column is left unmasked and no error, warning, or log is emitted.
The downstream `executed_substrate` dict is built from `work` unfiltered, so
it claims the skipped strategy ran as "polars" even though it did not.

**Impact:** A misconfigured plan silently produces output where some columns
are never masked. In a masking product this is a data-integrity failure.

**Fix:** Replace the silent skip with a `QualityWarning` (at minimum) or an
`ExecutionError` (preferred for the pure-polars path where every table should
be present):
```python
if node.table not in frames:
    raise ExecutionError(
        code="missing_source_table",
        message=(
            f"Plan references table {node.table!r} which is not in sources. "
            f"Tables present: {sorted(frames)!r}."
        ),
    )
```
If there is a legitimate reason some tables are absent (e.g. generate-only
tables), document the contract and emit a `QualityWarning` instead of raising.
Also fix the `executed_substrate` dict to exclude skipped nodes:
```python
"executed_substrate": {
    node.strategy: "polars"
    for node in work
    if node.table in frames
},
```

---

### F2 - Medium | Performance + Correctness
**Wasteful and potentially lossy pa->pl->pa round-trip in `_run_via_pandas_oracle`**

`_polars_adapter.py`, `_run_via_pandas_oracle`:
```python
substrate_sources: dict[str, pa.Table] = {
    table: boundary.to_arrow(boundary.to_polars(tbl))
    for table, tbl in sources.items()
}
```
Every Oracle-fallback job converts every source table from `pa.Table` ->
`pl.DataFrame` -> `pa.Table` before handing it to the pandas adapter. The
only purpose is to charge the boundary timing accumulators.

Two problems:
1. **Performance:** For large tables this is a 2x read of every source column
   for no masking benefit. An FK-heavy job that falls back to Oracle pays this
   on every run.
2. **Correctness (latent):** The module docstring asserts the round-trip is
   "lossless," but Polars does not preserve all Arrow metadata: `pa.large_string`
   is downcast to `pa.utf8`, chunked arrays are re-chunked, and
   `pa.dictionary`-encoded columns with non-standard index widths can differ.
   Any downstream pandas strategy that inspects Arrow schema metadata will
   silently receive altered tables.

**Impact:** Phantom latency on every oracle fallback; potential silent schema
mutation for metadata-bearing tables.

**Fix:** Track timing via the boundary only, without performing the round-trip
on oracle-fallback sources. If the timing split between substrate legs is
required, pass the boundary's accumulators as a side-channel without converting
source data:
```python
# Only time the conversion, do not mutate sources
for tbl in sources.values():
    t0 = time.perf_counter()
    frame = pl.from_arrow(tbl)
    boundary.pa_to_pl_ms += (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    frame.to_arrow()
    boundary.pl_to_pa_ms += (time.perf_counter() - t1) * 1000.0
result = self._pandas.run(plan, sources, ...)  # pass original sources
```
If the round-trip is truly required (e.g. to normalize chunked arrays), document
that explicitly and add a golden test that exercises `pa.large_string`,
`pa.dictionary`, and `pa.list_` columns through the Oracle path to verify
bit-for-bit parity with a direct pandas run.

---

### F3 - Medium | Correctness
**`_graph.py`: multi-parent check misses same-parent-table / different-parent-column FKs**

`_graph.py`, `build_relationship_graph`:
```python
parent_tables = sorted({r.parent_table for r in rels})
if len(parent_tables) > 1:
    raise PlanCompileError(code="multi_parent_fk_unsupported", ...)
```
The guard rejects a child key `(child_table, child_columns)` pointing to two
different *tables*. It does NOT reject two `Relationship` entries with the same
`(child_table, child_columns)` but different `(parent_table, parent_columns)` on
the *same* parent table (e.g. `orders.customer_id -> customers.id` AND
`orders.customer_id -> customers.legacy_id`). Both entries pass the guard and
both become `RelationshipEdge` rows in `edges_sorted`.

**Impact:** Downstream consumers that call `parents_of(child_table, child_columns)`
receive two edges and must handle the ambiguity. The execution adapter's FK
resolution loop (expected: one parent per child column) would silently pick one
or error unpredictably depending on iteration order.

**Fix:** Extend the check to also reject duplicate `parent_columns` within the
same `parent_table`:
```python
parent_keys = sorted({(r.parent_table, r.parent_columns) for r in rels})
if len(parent_keys) > 1:
    raise PlanCompileError(code="multi_parent_fk_unsupported", ...)
```

---

### F4 - Low | Correctness
**`_graph.py`: malformed `children` entry in `check_orphan_fk_policy_completeness` silently skips without diagnostic**

If a config entry's `children` list contains a dict with `columns: "id"` (a
string scalar rather than `["id"]`), the type check
`isinstance(child_cols, list) and all(isinstance(c, str) for c in child_cols)`
silently skips the entry. The key is never added to `config_lookup`. Later, the
profile-relationship loop raises `orphan_fk_policy_missing` with an error message
pointing at the relationship tuple, not at the malformed config entry. The
operator sees a confusing "config has no matching relationship entry" error for
a relationship that appears to be present.

**Fix:** When the type check fails, raise `PlanCompileError(code='orphan_fk_policy_malformed')`
with the precise `path=f"relationships[{idx}].children[{child_idx}]"` and a message
explaining the expected `columns: [list_of_strings]` shape.

---

### F5 - Low | Reliability
**`_namespace.py`: `NamespaceRegistry.__post_init__` rebuilds `_index` on every direct construction call, even for large registries**

`_namespace.py`, `NamespaceRegistry.__post_init__`:
```python
def __post_init__(self) -> None:
    if not self._index:
        built = {
            bound: binding.namespace
            for binding in self.bindings
            for bound in binding.declared_by
        }
        object.__setattr__(self, "_index", built)
```
The guard `if not self._index` is always True for callers that construct
`NamespaceRegistry(bindings=some_bindings)` without passing `_index` (the
`default_factory=dict` yields `{}`). This is by design (the docstring says so),
but it means test code that constructs a registry with large bindings pays the
rebuild cost once at construction. Not a production hotpath since the registry is
built once per compile, but the misleading `Lazy-default` comment in the
dataclass field suggests the rebuild is deferred; in fact it always fires at
construction when `_index` is omitted.

**Fix:** The behavior is correct. Remove the "Lazy-default" comment and replace
with: "Populated at construction from `bindings` when not explicitly passed."
The current comment misleads readers into thinking the index is built on first
use.

---

### F6 - Nit | Design
**`_source_reader.py`: `read_source_polars` accumulates pl->pa conversion into `boundary.pl_to_pa_ms` but the docstring labels both legs as "source"**

The docstring says:
```
When `boundary` is given, the file-read leg accrues to `source_read_ms` and
the pl->pa conversion to `pl_to_pa_ms`.
```
This is correct behavior but `source_read_ms` and `pl_to_pa_ms` are documented
in `ConversionBoundary` as separate concerns. The docstring implies
`source_read_ms` is the "file -> pl.DataFrame" leg and `pl_to_pa_ms` is
"pl.DataFrame -> pa.Table". The reader correctly charges each accumulator.
No behavioral issue; it's just worth verifying the breakdown shows up
correctly in `quality_metrics["conversion_breakdown"]` in integration tests.

---

## 3. Performance Notes

**Bottleneck classification:**
- `_run_polars_native`: CPU-bound per-column strategy application. The
  `ConversionBoundary` accumulators correctly track the I/O boundary cost.
  With `max_workers=4` reserved for future polars-native per-column parallelism,
  the current serial `for node in work` loop is O(N * column_cost).
- `_run_via_pandas_oracle`: adds a **2x pa<->pl conversion cost** on every
  source table (F2). For a 100-column, 1M-row table this is measurable.
  Profile with `timeit` or `py-spy` on the Oracle path to quantify.
- `build_relationship_graph`: Kahn's algorithm with `heapq` (already fixed
  per QA-8 F1). O((V + E) log V) — correct and fast for typical pipeline
  sizes.
- `build_namespace_registry`: O(N_columns * N_relationships). Single-pass;
  fast for the sizes this engine targets.

**What to measure:**
```bash
python -m cProfile -s cumulative -m pytest tests/integration/golden/ -k "polars"
```
Focus on `boundary.to_polars` + `boundary.to_arrow` call counts in oracle-fallback
tests. For F2, run:
```python
import timeit
timeit.timeit(lambda: boundary.to_arrow(boundary.to_polars(big_arrow_table)), number=100)
```

---

## 4. Suggested Tests

| Test | File | Why |
|---|---|---|
| `test_polars_native_skips_missing_table_raises` | `tests/unit/execution/polars/` | F1: assert `ExecutionError(code="missing_source_table")` when work node references absent table |
| `test_polars_oracle_does_not_mutate_sources` | `tests/unit/execution/polars/` | F2: verify oracle path passes original `pa.Table` objects to pandas, not round-tripped copies |
| `test_polars_oracle_lossless_large_string` | `tests/parity/` | F2: `pa.large_string` column survives oracle round-trip with bit-for-bit parity |
| `test_polars_oracle_lossless_dictionary` | `tests/parity/` | F2: `pa.dictionary` encoded column survives oracle round-trip |
| `test_multi_parent_same_table_different_cols_rejected` | `tests/unit/relationships/` | F3: same child key -> same parent table different parent columns raises `multi_parent_fk_unsupported` |
| `test_malformed_children_entry_raises_policy_malformed` | `tests/unit/relationships/` | F4: `children: [{columns: "id"}]` (string scalar) raises descriptive error |
| `test_executed_substrate_excludes_skipped_nodes` | `tests/unit/execution/polars/` | F1: quality_metrics only lists strategies that ran |

---

## 5. What's Good

- **Determinism:** The Kahn sort uses `heapq` with tuple comparison for
  byte-stable ordering. The `NamespaceRegistry._index` rebuild correctly uses
  sorted namespace keys for deterministic iteration. The `_stable_hash` in
  `helpers.py` uses `sort_keys=True` JSON serialization. All clean.
- **Oracle semantics:** The `fallback_to_pandas` / `_POLARS_NATIVE_STRATEGIES`
  design is principled: a strategy is native only when it appears in both the
  sentinel set and the handler registry (no drift possible).
- **Type safety:** `ConversionBoundary` uses `__slots__`, float accumulators,
  and explicit `isinstance(frame, pl.DataFrame)` guard post-`from_arrow`.
- **Relationship validation:** Multi-parent FK detection, orphan-policy
  completeness, cycle detection, and the 4-tuple key fix for same-parent
  different-child policies are all present and well-tested.
- **Namespace auto-binding:** The `__post_init__` O(1) lookup index is a
  correct and well-documented optimization over the previous O(B*K) scan.
