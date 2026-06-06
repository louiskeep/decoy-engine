# QA Review: generation/synthesize.py (`generate_tables`)

**Date:** 2026-06-06
**Branch:** `qa/review-2026-06-06-synthesize-generation`
**Reviewer:** QA/Performance agent
**Scope:** First review of the following module (not touched by any prior QA branch):

- `src/decoy_engine/generation/synthesize.py`

Related modules examined for context but not line-reviewed (covered in prior sessions):
- `src/decoy_engine/generators/derivation.py` (synthetic_column_seed)
- `src/decoy_engine/internal/faker_setup.py` (make_faker, get_faker_providers)

---

## 1. Summary

`synthesize.py` implements `generate_tables`, the V2 generation entry point that produces one Arrow table per generate table in a config. The parity seeding, per-column instance-local `random.Random`, and the QA-7 Faker lock fixes are well-executed. The most important finding is a **determinism violation in `_topo_sort`**: the dependency sets `deps[name]` are `set[str]`, whose iteration order is PYTHONHASHSEED-dependent. For tables with multiple parents, the output dict insertion order of `generate_tables` can vary across processes and machines, breaking the same-seed-same-output contract. The second most important finding is that `_FAKER_CALL_LOCK` is held for the entire per-column row loop — for a 1M-row Faker column, the lock is held continuously for the full batch, serializing all concurrent generation threads for that duration with no yield point.

---

## 2. Findings

### F1 -- HIGH | Determinism

**File:** `src/decoy_engine/generation/synthesize.py:93-107` (deps set construction in `generate_tables`)

**Issue:** `deps[name]` is typed as `set[str]` and built by `d.add(ref)`. In `_topo_sort`, when the DFS processes a node's parents it iterates `iter(deps.get(start, ()))` — iterating a Python set. Python set iteration order is PYTHONHASHSEED-randomized. For a table whose `generate_columns` reference two or more distinct parent tables, the relative DFS traversal order of those parents varies across Python processes.

```python
d: set[str] = set()          # <-- hash-randomized iteration
for col in t["generate_columns"]:
    if col.get("type") == "reference":
        ref = col["reference_table"]
        d.add(ref)
deps[name] = d
```

In `_topo_sort`:
```python
stack: list[...] = [(start, iter(deps.get(start, ())))]  # iterates a set
```

The topo-sort is correct (parents always precede children), but when two independent subtrees have no ordering constraint between them, the DFS yields them in hash-order. This determines the insertion order of `generate_tables`' returned `dict[str, pa.Table]`. A downstream caller that iterates `tables.items()` to write database rows (e.g. building auto-increment FK sequences) sees a different table order across runs with different PYTHONHASHSEED values, breaking the same-seed-same-output guarantee.

**Why it matters:** The application contract ("same seed, same output, across runs and machines") is violated for any multi-parent table configuration. This is a hidden form of nondeterminism that does not manifest in single-parent-chain configs (the common case) and is invisible in unit tests run within a single process.

**Verify:**
```python
import subprocess, sys
outputs = set()
for seed in range(50):
    result = subprocess.run(
        [sys.executable, "-c",
         "from decoy_engine.generation.synthesize import generate_tables; "
         "import json; cfg = ...; print(list(generate_tables(cfg).keys()))"],
        env={"PYTHONHASHSEED": str(seed)},
        capture_output=True, text=True,
    )
    outputs.add(result.stdout.strip())
assert len(outputs) == 1, f"Non-deterministic key order: {outputs}"
```

**Fix:** Change the deps container from `set[str]` to `list[str]` with manual deduplication (preserving declaration order) and update `_topo_sort`'s type hint accordingly:

```python
# In generate_tables, replace:
d: set[str] = set()
...
d.add(ref)
deps[name] = d

# With:
d: list[str] = []
...
if ref not in d:       # O(n_parents) dedup; n_parents is small in practice
    d.append(ref)
deps[name] = d
```

Update the `_topo_sort` signature: `def _topo_sort(deps: dict[str, list[str]]) -> list[str]`. The DFS logic is unchanged; `iter(list)` is already order-stable.

---

### F2 -- HIGH | Performance

**File:** `src/decoy_engine/generation/synthesize.py:204-218` (`_faker`, `_FAKER_CALL_LOCK` scope)

**Issue:** The `_FAKER_CALL_LOCK` is acquired once and held for the entire `for i in range(n)` row loop. Any thread that calls `_faker` (regardless of locale or table) must wait for the full batch to complete before it can acquire the lock.

```python
with _FAKER_CALL_LOCK:                    # acquired once ...
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)
    for i in range(n):                    # ... held for ALL n rows
        row_seed = col_seed + i
        faker_inst.seed_instance(row_seed)
        out.append(provider_func(**faker_kwargs))
```

For a 1M-row Faker column, the lock is held continuously for the full batch (estimate: 1M rows * ~5 µs/row = ~5 seconds). Thread B is blocked for that entire period. With two concurrent generate jobs each having a 1M-row Faker column, total wall time approaches 2x serial time rather than 1x parallel time.

**Why it matters:** The docstring correctly notes this is an accepted limitation until V2.1 replaces the shared singleton with per-call fresh Faker instances. This finding is logged to drive the V2.1 priority: before V2.1, concurrent generation workloads cannot benefit from parallelism for Faker columns.

**Bottleneck classification:** CPU-bound (Faker provider calls) + lock contention. Not I/O-bound.

**What to measure:**
```bash
python -m timeit -n 5 -s "
from decoy_engine.generation.synthesize import generate_tables
import threading, time
cfg = {  # two-table config each with 100K-row faker column
    'tables': [
        {'name': 'a', 'generate_columns': [{'name': 'n', 'type': 'faker', 'faker_type': 'name'}], 'row_count': 100000},
        {'name': 'b', 'generate_columns': [{'name': 'n', 'type': 'faker', 'faker_type': 'name'}], 'row_count': 100000},
    ],
    'global_settings': {'seed': 42},
}
" "generate_tables(cfg)"
```
Compare to two sequential single-table calls. The ratio should approach 2.0x if fully serialized.

**Fix (V2.1):** Replace the shared `_DEFAULT_FAKER` singleton with a per-call fresh `Faker()` instance inside `_faker`. Fresh instances do not share state, removing the need for any lock:

```python
def _faker(...) -> list[Any]:
    ...
    # V2.1: no lock needed; each call owns a fresh Faker instance
    faker_inst = make_faker(locale) if locale else Faker()
    faker_inst.seed_instance(col_seed)
    ...
    out = []
    for i in range(n):
        faker_inst.seed_instance(col_seed + i)
        out.append(provider_func(**faker_kwargs))
    return out
```

The ~100ms Faker construction cost per column is the tradeoff; amortized over 100K rows it is negligible (1µs/row). Measure with `timeit` before committing.

---

### F3 -- MEDIUM | Correctness

**File:** `src/decoy_engine/generation/synthesize.py:35-48` (`_get_default_faker`, double-checked locking)

**Issue:** The double-checked locking pattern used for `_DEFAULT_FAKER` has a data race under free-threaded CPython (PEP 703, Python 3.13+):

```python
def _get_default_faker() -> Faker:
    global _DEFAULT_FAKER
    if _DEFAULT_FAKER is None:          # read without lock -- data race under 3.13 free-thread
        with _DEFAULT_FAKER_LOCK:
            if _DEFAULT_FAKER is None:
                _DEFAULT_FAKER = Faker()
    return _DEFAULT_FAKER
```

Under CPython's GIL (≤ 3.12), the outer check-then-lock is safe because the GIL prevents torn reads. Under free-threaded CPython (3.13+ with `--disable-gil`), two threads can pass the outer `is None` check simultaneously, both enter the lock, and the second call overwrites the first Faker instance. The overwrite is harmless if `Faker()` is idempotent (it is), but the pattern establishes a systemic habit that is unsafe in the 3.13 free-thread context.

**Why it matters:** Low impact today (the overwrite is idempotent). Medium impact as a systemic pattern: six prior QA reviews (engine 06-02 through 06-05) have flagged identical double-checked singletons across the engine. Fixing consistently in one pass is more valuable than fixing each occurrence individually.

**Fix:** Always acquire the lock; for an already-initialized singleton the uncontested lock acquisition costs ~50ns, negligible versus Faker construction (~100ms):

```python
def _get_default_faker() -> Faker:
    global _DEFAULT_FAKER
    with _DEFAULT_FAKER_LOCK:
        if _DEFAULT_FAKER is None:
            _DEFAULT_FAKER = Faker()
        return _DEFAULT_FAKER
```

Alternatively, use a module-level `functools.lru_cache` singleton (the pattern recommended in 06-02 reviews):
```python
import functools

@functools.lru_cache(maxsize=None)
def _default_faker() -> Faker:
    return Faker()
```
`lru_cache` uses an internal RLock that is correct under both GIL and free-thread. This replaces both `_DEFAULT_FAKER` and `_DEFAULT_FAKER_LOCK` with a single, correct, idiomatic construct.

---

### F4 -- MEDIUM | Correctness / Design

**File:** `src/decoy_engine/generation/synthesize.py:168-175` (`_reference`, missing `ref_column` existence check)

**Issue:** `parent_tbl.column(ref_column)` raises a bare `KeyError` from PyArrow if `ref_column` does not exist in the parent table:

```python
parent_tbl = pools[ref_table]
raw_vals = parent_tbl.column(ref_column).to_pylist()  # KeyError if ref_column absent
```

`PipelineConfig._reference_graph_valid` validates this at config time. However, the docstring explicitly states: "generate_tables is documented to accept unvalidated dicts (V1-parity callers)." The `generate_tables` function already adds a typed error for missing `reference_table` (the F-7 fix). It should add the same for missing `reference_column`.

**Why it matters:** A raw `KeyError: 'Field nonexistent_col not found in schema'` from PyArrow does not identify which child table/column triggered the reference, making debugging a multi-table config painful.

**Fix:**

```python
try:
    raw_vals = parent_tbl.column(ref_column).to_pylist()
except KeyError:
    raise ValueError(
        f"reference column {ref_column!r} not found in generated table "
        f"{ref_table!r} (available: {parent_tbl.schema.names})"
    ) from None
```

---

### F5 -- MEDIUM | Design (Technical Debt)

**File:** `src/decoy_engine/generation/synthesize.py:240-255` (`_formula` delegation to private V1 method)

**Issue:** `_formula` calls the private method `ColumnGenerator._eval_formula_inline`:

```python
cg = ColumnGenerator(seed=seed, derive_key=derive_key)
series = cg._eval_formula_inline(n, formula, col.get("name", "unnamed_column"), col)
return series.tolist()
```

The underscore prefix signals no stability guarantee. If V1 receives a fix that changes `_eval_formula_inline`'s return type (e.g. returning a NumPy array instead of a pandas `Series`), `.tolist()` silently changes behavior. If the formula produces non-serializable types (e.g. `complex` from `cmath`), `pa.table(data)` fails with an opaque `ArrowInvalid` error that does not identify the offending column.

The same delegation pattern appears in `_reference` for `_apply_cardinality_bounds`.

**Why it matters:** Known technical debt; deferred to S9 (V1 removal). The risk is a silent regression if V1 is patched between now and S9.

**Fix:** Add a parity assertion test that runs on every CI build:

```python
# tests/parity/test_formula_delegation_contract.py
def test_eval_formula_inline_returns_series():
    """Regression guard: _eval_formula_inline must return pd.Series until S9."""
    import pandas as pd
    from decoy_engine.generators.columns import ColumnGenerator
    cg = ColumnGenerator(seed=0, derive_key=None)
    result = cg._eval_formula_inline(5, "row_index * 2", "col", {})
    assert isinstance(result, pd.Series), type(result)
    assert result.tolist() == [0, 2, 4, 6, 8]
```

This is a targeted regression guard, not a spec — it documents the assumed interface and fails loudly if V1 is patched to break it before S9.

---

### F6 -- LOW | Correctness

**File:** `src/decoy_engine/generation/synthesize.py:296-310` (`_reference`, `int()` truncates float cardinality params)

**Issue:**

```python
min_per = int(col.get("min_per_parent") or 0)
max_per = int(col.get("max_per_parent") or 0)
```

YAML floats parse as `float` in Python. `min_per_parent: 1.5` in a YAML file becomes `float` 1.5, and `int(1.5) == 1` silently truncates it. The cardinality repair is triggered with `min_per=1` when the author intended something non-integer — possibly a misconfiguration.

`PipelineConfig` should reject non-integer cardinality params at validation time. If it does, this branch is unreachable from validated configs. If it does not (V1-parity callers), the silent truncation hides the misconfiguration.

**Fix:** Guard against non-integer values from unvalidated callers:

```python
raw_min = col.get("min_per_parent") or 0
raw_max = col.get("max_per_parent") or 0
for raw, name in [(raw_min, "min_per_parent"), (raw_max, "max_per_parent")]:
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(
            f"column {col.get('name')!r}: {name}={raw!r} must be a whole number"
        )
min_per = int(raw_min)
max_per = int(raw_max)
```

---

### F7 -- LOW | Performance

**File:** `src/decoy_engine/generation/synthesize.py:195-200` (`_faker`, `get_faker_providers` called per column)

**Issue:** `providers = get_faker_providers(faker_inst)` is called once per `_faker` invocation (once per column). If `get_faker_providers` builds a new provider name → callable dict by introspecting the Faker instance (O(num_providers)), this is repeated work for every Faker column.

**Why it matters:** With 200 Faker providers per locale and 20 Faker columns in a table, this is 4000 provider registry lookups on every `generate_tables` call. If `get_faker_providers` is O(1) (e.g. already returns a cached dict), this finding does not apply.

**Verify:** Check `src/decoy_engine/internal/faker_setup.py`:
- If `get_faker_providers` is decorated with `@functools.lru_cache` or equivalent: no action needed.
- If it constructs a fresh dict each call: cache it on the faker instance or at module level per locale.

---

### F8 -- NIT | Reliability

**File:** `src/decoy_engine/generation/synthesize.py:237-249` (`_apply_null_probability`, copy allocation)

**Issue:** `out = list(values)` always allocates a full copy of `values` before applying nulls. For a 1M-row column with `null_probability=0.01` (10K nulls), this allocates 1M slots to modify ~10K of them. There is no way to avoid the copy if the input `values` is to remain immutable, so this is unavoidable with the current interface. Noting for completeness.

**No action required** unless profiling shows list copy as a bottleneck in `_apply_null_probability`. If it does, the fix is to apply nulls in-place by having generators return mutable lists, which is already the case for most generators.

---

## 3. Performance Notes

| Hotpath | Bottleneck | Complexity | Measure with |
|---------|-----------|------------|-------------|
| `_faker` (any Faker column) | CPU + lock contention (F2) | O(n * lock_cost) | `timeit`; compare 1-thread vs 2-thread at 100K rows |
| `_categorical` | CPU -- `rng.choices(cats, k=n)` | O(n) | Correct; no action |
| `_reference` (large parent pool) | CPU -- `seen: set` dedup loop | O(len(raw_vals)); typically small | No action |
| `_sequence` | CPU -- Python loop, f-string per row | O(n) | Vectorizable via `np.arange` + str ops for large n; only optimize if benchmarked |
| `pa.table(data)` | Memory -- lists of n items per column | O(columns * n) | `memory_profiler` on large configs |
| `_topo_sort` | CPU -- iterative DFS | O(V + E) where V=tables, E=references | Negligible in practice |

For `_sequence`, a vectorized replacement (no Python loop) for large-n tables:
```python
# Only worth implementing if benchmarked as a bottleneck (>1M rows typical)
import numpy as np
values = start + np.arange(n) * step
str_vals = np.char.zfill(values.astype(str), pad) if pad else values.astype(str)
result = np.char.add(np.char.add(prefix, str_vals), suffix).tolist()
```
Benchmark first — `timeit` the Python loop vs. numpy at 1M rows before committing.

---

## 4. Suggested Tests

| Test | What to verify |
|------|----------------|
| `test_generate_tables_key_order_stable_across_hashseed` | Run `generate_tables` with a two-parent config under 50 different `PYTHONHASHSEED` env values (subprocess); assert `list(result.keys())` is identical in all runs |
| `test_reference_missing_ref_column_typed_error` | `generate_tables` with a reference column pointing to a non-existent column in the parent table raises `ValueError` with `ref_column` in the message (after F4 fix) |
| `test_faker_lock_held_duration_concurrent` | Spawn two threads each calling `_faker` on a 50K-row column simultaneously; assert wall time < 1.2x serial time (this will FAIL before V2.1; document as known failing test) |
| `test_null_probability_deterministic_across_hashseed` | Same config+seed run 50 times with different `PYTHONHASHSEED`; assert null positions are identical |
| `test_null_probability_zero_no_nulls` | `null_probability: 0.0` on any column type; assert no `None` values in output |
| `test_null_probability_one_all_nulls` | `null_probability: 1.0`; assert all values are `None` |
| `test_sequence_pad_length_zero` | `pad_length: 0`; assert `start=1` → `"1"` (not `"01"` or `""`) |
| `test_sequence_step_negative` | `start: 10, step: -1, n: 5`; assert `["10", "9", "8", "7", "6"]` |
| `test_reference_empty_parent_pool_returns_nones` | Parent table has zero rows; child reference column returns `[None] * n` |
| `test_reference_insertion_order_dedup` | Parent has values `["b", "a", "b", "a"]`; `_reference` unique-list should be `["b", "a"]` (insertion order), not `["a", "b"]` (sorted) |
| `test_topo_sort_multi_parent_stable_order` | Two tables A and B both required by C; assert `_topo_sort` emits A and B in declaration order regardless of PYTHONHASHSEED (after F1 fix) |
| `test_formula_with_references_warns` | Formula column with `references: [other_col]` emits a `UserWarning` and returns `[None] * n` |
| `test_get_default_faker_thread_safe` | Spawn 20 threads all calling `_get_default_faker()` simultaneously; assert they all get the same object and no exception is raised |

---

## 5. What's Good

- **QA-7 F1 + C1 fix** (Faker lock scope): moving the pre-seed `seed_instance` call inside the `_FAKER_CALL_LOCK` block is the correct fix. The docstring explanation (thread A seeds, thread B clobbers before thread A draws) is exactly right and well-commented.
- **QA 2026-05-31 F3 closure** (`_apply_null_probability` single Random reuse): allocating `rng = random.Random()` once and calling `rng.seed(...)` per row is the correct pattern — same first draw as `random.Random(s)`, V1 byte-parity preserved, ~3-5x faster than per-row allocation. The comment explaining WHY this preserves parity is exemplary.
- **QA-7 F8 typed seed error**: `int(raw_seed)` with a clear error message for non-numeric seeds is the right contract enforcement. Consistent with the plan compiler's post-QA-3 F1 behavior.
- **F-7 fix** (reference_table validation): raising a typed `ValueError` with the table and column names in the message rather than letting a downstream `KeyError` propagate is the right pattern. Consistent with F4's recommended fix for `ref_column`.
- **Insertion-order unique for `_reference`**: the manual `seen: set` / `ref_vals: list` dedup is correct — `set()` would lose insertion order and break byte-parity with V1's `dropna().unique()`.
- **Iterative DFS in `_topo_sort`**: replacing recursive DFS with an explicit work stack correctly avoids `RecursionError` on deep reference chains. The comment citing the prior recursive implementation's 1000-frame limit is precise and actionable.
- **`_categorical` instance-local `random.Random`**: using `rng = random.Random(col_seed)` instead of `random.seed(col_seed)` is the right pattern for thread safety without breaking V1 byte-parity. Consistent across all non-Faker generators.
- **`pools` topo ordering in `generate_tables`**: building `pools[name] = tbl` and `out[name] = tbl` in topo order ensures `_reference` always finds the parent in `pools` by the time the child is generated. The validator + topo-sort combination makes the KeyError in `pools[ref_table]` unreachable from validated configs — a clean design.
