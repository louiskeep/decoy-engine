# QA Review: PandasExecutionAdapter + Post-Execution Validation Suite

**Date:** 2026-06-06
**Reviewer:** QA/Performance Engineering
**Files reviewed:**
- `src/decoy_engine/execution/_pandas_adapter.py`
- `src/decoy_engine/execution/_runner.py`
- `src/decoy_engine/execution/_when_gate.py`
- `src/decoy_engine/validation/post/_runner.py`
- `src/decoy_engine/validation/post/_scan.py`
- `src/decoy_engine/validation/post/_checks/__init__.py`
- `src/decoy_engine/validation/post/_checks/_leakage.py`
- `src/decoy_engine/validation/post/_checks/_fk_validity.py`
- `src/decoy_engine/validation/post/_checks/_determinism_sample.py`
- `src/decoy_engine/validation/post/_checks/_sampled_values.py`
- `src/decoy_engine/validation/post/_checks/_format_rules.py`

---

## 1. Summary

The pandas adapter and Kahn-sorted work-ordering are well-structured; the Q7 batch-materialization fix and the heapq Kahn replacement are sound. The critical problem is in the post-execution validation suite: `run_leakage` treats `formula` strategy as a substitution check and will **hard-fail correct pipelines** whose formula produces output values that happen to appear in the source (identity formulas, rounding, COALESCE with a source-present default). A secondary critical path: FK child columns with `PRESERVE` orphan policy retain source values in the output, and the same substitution check will incorrectly flag them as leaks. The `when_gate` pandas writeback has a latent index-misalignment risk that the polars gate already patched but the pandas gate has not.

---

## 2. Findings

---

### F1 -- Critical | Correctness
**`run_leakage`: substitution check produces hard-fail false positives for `formula` strategy and PRESERVE-policy FK groups**

File: `src/decoy_engine/validation/post/_checks/_leakage.py`, lines 72-86

```python
source_values = {v for v in src_vals if v is not None}
if not source_values:
    continue
leaked = {v for v in out_vals if v is not None and v in source_values}
if leaked:
    failed = True
```

The set-membership check fires whenever any output value appears in the source value set. Two cases produce incorrect hard failures:

**Case A -- `formula` strategy.** A formula that is partially or fully structure-preserving (e.g., `UPPER(${value})` applied to an all-uppercase column, `COALESCE(${value}, 'FALLBACK')` when no nulls exist, `ROUND(${value}, 0)` on round-valued data, or any arithmetic identity) will produce output values that are present in the source. Every such value triggers `failed = True`. The formula strategy is a legitimate substitution in the general case but is not injective in the value-domain sense -- no assumption that output domains are disjoint from source domains is warranted.

**Case B -- FK_GROUP_STRATEGY with PRESERVE orphan policy.** `masked_columns()` in `_scan.py` labels FK group columns with `FK_GROUP_STRATEGY = "<fk_group>"`. This label is not in `_VALUE_REUSE_STRATEGIES`, so it falls through to the substitution check. Under `OrphanPolicy.PRESERVE`, child orphan values are intentionally retained from the source. Those source values appear in the output and will match `source_values`, triggering `failed = True` on a correctly-behaving pipeline.

**Impact:** Any pipeline using `formula` strategy or `PRESERVE`-policy FK relationships with the `post_validation: true` flag will hard-fail the leakage scan even when masking is correct. This violates the post-validation contract ("the scanner audits correctness; it does not reject correct runs").

**Fix:**

1. Add `formula` to `_VALUE_REUSE_STRATEGIES` **or** introduce a third category (e.g., `_STRUCTURE_PRESERVING_STRATEGIES`) with a positional-check-only or warning-only policy:

```python
# Strategies whose output may legitimately contain source values because the
# formula domain is not guaranteed disjoint from the source domain.
_WARNING_ONLY_STRATEGIES = frozenset({"formula"})
```

Then in the scan loop:

```python
if strategy in _WARNING_ONLY_STRATEGIES:
    # Warn on collisions but do not hard-fail; a formula output matching
    # a source value is normal and does not constitute a leakage breach.
    leaked_count = sum(1 for v in out_vals if v is not None and v in source_values)
    if leaked_count:
        warnings.append(QualityWarning(
            code="formula_source_collision",
            provider=strategy,
            column=col_name,
            detail={"table": table_name, "collision_count": leaked_count},
        ))
    continue
```

2. For FK_GROUP_STRATEGY with PRESERVE policy: the scan has no access to the orphan policy at the strategy-label level. Options:
   - Skip `FK_GROUP_STRATEGY` from hard-fail leakage (demote to warning or skip entirely; FK integrity is audited by `run_fk_validity` which is the correct check for FK correctness).
   - Pass `relationship_graph` through the scan and check `edge.orphan_policy` per column.

The simplest safe fix is to exclude `FK_GROUP_STRATEGY` from the substitution check entirely (FK resolution preserves referential integrity, not value secrecy -- the correct privacy guarantee for FK columns is that the child references a masked parent, which `run_fk_validity` already audits):

```python
_SKIP_LEAKAGE_STRATEGIES = frozenset({"passthrough", FK_GROUP_STRATEGY})

if strategy in _SKIP_LEAKAGE_STRATEGIES:
    continue
```

---

### F2 -- High | Correctness
**`run_with_when_gate` (pandas): writeback index-alignment not guarded against handler-internal index reset**

File: `src/decoy_engine/execution/_when_gate.py`, lines 145-148

```python
sub_df = df.loc[mask].copy()
sub_df, warnings = handler.run(sub_df, column, plan, ctx)
df.loc[mask, column] = sub_df[column]  # label-aligned assignment
```

`df.loc[mask, column] = sub_df[column]` aligns by pandas index label. If any handler internally resets the index (e.g., calls `reset_index(drop=True)` before building a return frame, or constructs `pd.DataFrame({col: generated_values})` with a default RangeIndex), the returned `sub_df` carries indices `[0, 1, 2, ...]` instead of the original row labels. The label-aligned assignment then writes to the wrong rows (or introduces NaN for non-matching labels), producing **silent data corruption** in the masked output without any error.

The polars gate already patched this identical issue (QA-3 F13, 2026-05-31) using a positional anchor column. The pandas gate was not updated at the same time.

**Impact:** Any strategy that internally resets the DataFrame index when called on a subset (e.g., a faker-backed handler that builds a fresh DataFrame of generated values) will silently corrupt the non-when-gate rows if those rows happen to share index labels 0..k with the unreseted sub_df.

**Fix:** Mirror the polars gate's anchor approach, or use positional `.iloc` for the writeback:

```python
sub_df = df.loc[mask].copy()
sub_df, warnings = handler.run(sub_df, column, plan, ctx)
# Use positional assignment to guard against handler-internal index reset.
# pandas iloc-based writeback is immune to label misalignment.
positions = mask.to_numpy().nonzero()[0]
df.iloc[positions, df.columns.get_loc(column)] = sub_df[column].to_numpy()
return df, warnings
```

This is the same semantic as the polars gate's anchor + `sub_pdf[anchor_col].to_numpy()` writeback, translated to pandas.

**Verification:** Add a test with a mock handler that calls `sub_df.reset_index(drop=True)` before returning, over a frame where `when:` matches non-contiguous rows (e.g., rows 2, 5, 8). Assert the output matches the expected masked values at rows 2, 5, 8 and is unchanged at all others.

---

### F3 -- High | Correctness
**`run_determinism_sample`: double early exit suppresses all failures after the first non-deterministic column**

File: `src/decoy_engine/validation/post/_checks/_determinism_sample.py`, lines 42-50

```python
for source, masked in list(zip(src_vals, out_vals, strict=True))[: ctx.sample_size]:
    if source is None:
        continue
    if source in mapping and mapping[source] != masked:
        failed = True
        break          # exits the row loop
    mapping[source] = masked
if failed:
    break              # exits the column loop
```

The `if failed: break` at the column level is followed by the same guard at the table level (lines 48-49). The scan returns after the first non-deterministic column in the first table. A pipeline with three non-deterministic columns would report one failure; the other two are invisible.

**Impact:** Operators debugging a determinism regression see only the first failing column and iterate fix-by-fix, missing that the problem is systemic. More importantly, the `QualitySummary.failed_checks` includes `determinism_sample` only once -- there's no per-column breakdown showing which columns are affected.

**Fix:** Collect all non-deterministic columns, set `failed = True` for any, and include a per-column detail list in a `QualityWarning`:

```python
def run_determinism_sample(ctx: ScanContext) -> ScanOutcome:
    failed = False
    warnings: list[QualityWarning] = []
    for table_name, table_seed in ctx.plan.seed_envelope.per_table:
        out_table = ctx.outputs.get(table_name)
        src_table = ctx.sources.get(table_name)
        if out_table is None or src_table is None:
            continue
        for col_name, seed in table_seed.per_column:
            if not seed.deterministic:
                continue
            # ... range checks ...
            mapping: dict[object, object] = {}
            col_failed = False
            for source, masked in itertools.islice(
                zip(src_vals, out_vals, strict=True), ctx.sample_size
            ):
                if source is None:
                    continue
                if source in mapping and mapping[source] != masked:
                    col_failed = True
                    break
                mapping[source] = masked
            if col_failed:
                failed = True
                warnings.append(QualityWarning(
                    code="determinism_violation",
                    provider="determinism_sample",
                    column=col_name,
                    detail={"table": table_name},
                ))
    return ScanOutcome(name=_NAME, failed=failed, warnings=tuple(warnings))
```

Also fixes F4 (see below) by using `itertools.islice` instead of `list(zip(...))[:n]`.

---

### F4 -- Medium | Performance
**`run_determinism_sample`: materializes O(n) zip list before slicing to `sample_size`**

File: `src/decoy_engine/validation/post/_checks/_determinism_sample.py`, line 41

```python
for source, masked in list(zip(src_vals, out_vals, strict=True))[: ctx.sample_size]:
```

`list(zip(...))` materializes all n row-pairs before the `[:ctx.sample_size]` slice discards everything beyond position 100 (default). For a 1M-row table this allocates a 1M-element list of 2-tuples only to iterate the first 100. `src_vals` and `out_vals` are already full Python lists (from `column_values`), so this is an avoidable O(n) allocation.

**Bottleneck classification:** CPU + memory (allocation cost; no I/O).

**Fix:** `itertools.islice(zip(src_vals, out_vals, strict=True), ctx.sample_size)` -- zero extra allocation, same semantics. Already shown in F3 fix above.

---

### F5 -- Medium | Performance
**`run_fk_validity._row_keys`: O(n*k) Python loop mirrors the Q7 issue already fixed in `_pandas_adapter.py`**

File: `src/decoy_engine/validation/post/_checks/_fk_validity.py`, lines 88-96

```python
def _row_keys(table: pa.Table, columns: tuple[str, ...]) -> list[tuple[object, ...] | None]:
    col_values = [column_values(table, c) for c in columns]
    if not col_values:
        return []
    n = len(col_values[0])
    keys: list[tuple[object, ...] | None] = []
    for i in range(n):
        row = tuple(col_values[j][i] for j in range(len(columns)))
        keys.append(None if any(x is None for x in row) else row)
    return keys
```

This is an O(n*k) Python loop. For the same 1M-row child table with a 3-column FK, that's 3M inner-loop iterations plus 3M `any()` calls over 3-element tuples. The Q7 fix in `_pandas_adapter.py` replaced an analogous pattern with `zip(*col_lists)` iteration. The same fix applies here.

**Bottleneck classification:** CPU (Python loop overhead; no I/O, no allocation beyond the output list).

**Fix:**

```python
def _row_keys(table: pa.Table, columns: tuple[str, ...]) -> list[tuple[object, ...] | None]:
    col_values = [column_values(table, c) for c in columns]
    if not col_values:
        return []
    keys: list[tuple[object, ...] | None] = []
    for row in zip(*col_values):
        keys.append(None if any(x is None for x in row) else row)
    return keys
```

`zip(*col_values)` drives the iteration at C-speed for the outer loop; `any(x is None for x in row)` is still a Python short-circuit but is now iterating a k-element tuple (k typically 1-3) rather than calling `col_values[j][i]` per cell.

**Profile command:** `python -m timeit` with a 1M-row 2-column PyArrow table; or `scalene` on a realistic FK validity run. Expect 2-4x throughput improvement for composite-key FK tables.

---

### F6 -- Medium | Correctness
**`run_with_when_gate_polars`: full-frame `to_pandas()` for predicate eval on every `when`-gated column**

File: `src/decoy_engine/execution/_when_gate.py`, line 174

```python
pdf = frame.to_pandas()
mask = _eval_predicate(pdf, plan.when, plan.strategy)
```

For every `when`-gated column in a multi-column polars run, the full frame is converted to pandas just to evaluate the boolean predicate. For a 1M-row frame, each `to_pandas()` call copies the entire frame. If 5 columns have `when:` conditions, the frame is converted 5 times. The pandas gate doesn't pay this cost (it operates on pandas frames natively).

**Bottleneck classification:** Memory + CPU (conversion cost; I/O-free).

**Fix (deferred-acceptable):** Cache the pandas conversion per frame within the `run_with_when_gate_polars` call, or pass a pre-converted pandas frame as an optional argument. This is a known substrate cost and may be acceptable; flag for the S13 parallelism sprint which will revisit the polars path.

---

### F7 -- Low | Design
**`_make_remap_fn`: REMAP masking ignores the parent column's `when` condition**

File: `src/decoy_engine/execution/_pandas_adapter.py`, lines 369-392

The REMAP closure calls `handler.run(tmp, pcol, pnode.plan_slice, ctx)` directly, bypassing `run_with_when_gate`. If the parent column carries a `when:` predicate (e.g., "mask this SSN only when `status == 'active'`"), orphan values would still be remapped through the handler unconditionally, potentially applying the masking strategy to values that the `when` gate would have excluded.

Whether this is intentional depends on the REMAP contract definition. If REMAP semantics are "always mask the orphan value so it looks like a legitimate masked value regardless of the when gate," the current behavior is correct. If REMAP semantics should respect the parent's conditional masking scope, it should route through `run_with_when_gate` with a synthetic single-row frame.

**Action required:** Confirm the REMAP-when contract in the spec and add a docstring note to `_make_remap_fn` that explicitly states which interpretation is intended. If REMAP should bypass `when:`, add `# when: deliberately bypassed for REMAP: orphan values always need a plausible masked form`.

---

### F8 -- Low | Reliability
**`_format_rules.run_format_rules`: regex recompiled per column, not per provider**

File: `src/decoy_engine/validation/post/_checks/_format_rules.py`, line 44

```python
pattern = re.compile(caps.format_regex)
```

This compiles the provider's format regex inside the inner column loop. For a table with 20 SSN columns that all use `synthetic_ssn`, the regex is compiled 20 times. Python's `re` module caches compiled patterns (up to 512 by default), so this is typically a cache hit after the first compile -- but the lookup overhead is still present.

**Fix:** Compile outside the column loop using a local dict, or hoist the compile to module level in the provider's capabilities object. Nit in most schemas; notable for bulk-column anonymization pipelines.

---

### F9 -- Low | Reliability
**`get_default_executor`: TOCTOU race on `_DEFAULT_EXECUTORS`**

File: `src/decoy_engine/execution/_pandas_adapter.py`, lines 400-416

```python
cached = _DEFAULT_EXECUTORS.get(substrate)
if cached is None:
    cached = select_execution_adapter()
    _DEFAULT_EXECUTORS[substrate] = cached
```

Under concurrent calls (FastAPI thread pool), two threads can both observe `cached is None` and both call `select_execution_adapter()`. The second write overwrites the first adapter. The race is benign (the adapter is stateless and the result is idempotent) but wastes one initialization. Python dict operations are GIL-protected, so no corruption. Note if adapter initialization acquires external resources (future S13 parallelism work), this race becomes non-benign.

---

## 3. Performance Notes

**Bottleneck hierarchy (post-validation suite):**
1. `column_values()` materializes entire columns to Python lists for every scan. For a 10M-row table scanned by 7 checks, this is 7 full-column materializations per column. The scan suite would benefit from a single-pass column cache within `ScanContext`. Profile with `py-spy` or `scalene` on a realistic 1M-row multi-column pipeline; expect `to_pylist()` to dominate.
2. `_row_keys` (F5) is the next hotspot for FK-heavy pipelines.
3. `_determinism_sample`'s `list(zip(...))` (F4) is minor relative to F1/F2 but trivially fixable.

**Pandas adapter `_parent_map`:** The `any(pd.isna(x) for x in raw)` null check (line 350) calls `pd.isna` per element. For primitive Python types (int, float, str, None) from `.tolist()`, `x is None` is faster. For columns with numpy scalar values, `pd.isna` is needed to catch `np.nan`. A combined check `x is None or (isinstance(x, float) and math.isnan(x))` covers both without pandas overhead.

**Complexity summary:**
- `_kahn_sorted`: O((n + e) log n) -- correct and optimal for DAG topo-sort.
- `build_work_list`: O(c) where c = total columns -- no concern.
- `run_leakage`: O(c * n) for set construction -- acceptable; bottleneck is `to_pylist()`.
- `run_fk_validity._row_keys`: O(n * k) Python loop -- fix in F5.

---

## 4. Suggested Tests

| # | What to test | Why |
|---|---|---|
| T1 | `run_leakage` with a `formula` strategy whose output equals source for every row (identity formula) | Verifies F1 fix: should warn, not hard-fail |
| T2 | `run_leakage` with a PRESERVE-policy FK group where 50% of child rows are orphans (retained source values) | Verifies F1 fix Case B: no false hard-fail |
| T3 | `run_with_when_gate` (pandas) with a mock handler that calls `reset_index(drop=True)` before returning | Verifies F2 fix: correct values at correct rows, no NaN bleed |
| T4 | `run_determinism_sample` with 3 non-deterministic columns in the same table | Verifies F3 fix: all 3 columns reported in warnings, `failed=True` |
| T5 | `run_determinism_sample` with sample_size=10 on a 10M-row column | Verifies F4 fix: no O(n) list materialization (measure with `tracemalloc`) |
| T6 | `run_fk_validity` with a 1M-row child table + 3-column composite FK | Performance regression guard for F5; run time should not scale super-linearly |
| T7 | `_make_remap_fn` with a parent column that has a `when:` predicate; verify REMAP behavior is documented | Clarifies F7 intent |
| T8 | `run_leakage` with `<fk_group>` strategy where the parent is passthrough (FK child = source parent = output parent) | Edge case: passthrough parent with FK child should not trigger leakage |

---

## 5. What's Good

- **Kahn-sorted topo sort** (F9 reference in `_runner.py`): the heapq-based Kahn sort with sorted tie-break is correct, byte-stable, and has the right O((n+e) log n) complexity. The `placed < keys` cycle detection is clean.
- **Q7 batch-materialize fix** in `_pandas_adapter._resolve_fk_node` and `_parent_map`: replacing `iloc[i]` per-row with `tolist()` + indexed Python lists was the right fix and the comment clearly attributes the change.
- **`_fk_key_value` normalization**: the int/float dtype coercion to normalize FK key components across pandas' int64/float64 dtype split is subtle and correct.
- **`run_sampled_values` privacy gate**: reading from `outputs` (never `sources`) with the explicit passthrough exclusion is the right design. PII cannot reach the manifest through this path.
- **`_eval_predicate` scope clamping**: pinning `engine="numexpr"` with `local_dict={}` and `global_dict={}` is the correct defense against scope-walk attacks on the formula gate.
- **Post-validation runner crash isolation** (`_runner.py` lines 94-109): wrapping each scan in a try/except and converting exceptions to `failed=True` `ScanOutcome` is the right approach for an audit surface -- a crashing scan should never silently skip; the job must fail with an observable error.
