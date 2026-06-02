# QA Review — FC-1 Mixed-Mode: Engine Layer
**Date:** 2026-06-02  
**Branch reviewed:** `feat/fc-1-mixed-mode` (tip `49eab5a`)  
**Files reviewed:**
- `src/decoy_engine/execution/_pipeline.py` (new)
- `src/decoy_engine/config/_pipeline.py` (FC-1 rewrite)
- `src/decoy_engine/config/_tables.py`
- `src/decoy_engine/generation/synthesize.py`
- `src/decoy_engine/execution/_adapter.py`
- `tests/unit/execution/test_run_pipeline.py` (new)
**Prior QA branches avoided:** `qa/review-2026-06-02-determinism`, `qa/review-2026-06-02-engine`, `qa/review-2026-06-02-execution`, `qa/review-2026-06-02-generators-profile-context`, `qa/review-2026-06-02-providers-config-storm`, `qa/review-2026-06-02-transforms-connectors-relationships`

---

## 1. Summary

FC-1 (`feat/fc-1-mixed-mode`) is a well-scoped feature. The schema surgery (dropping the top-level `mode` discriminator, per-table XOR enforcement, updated `_reference_graph_valid`) is correctly done, the `run_pipeline` sequencing (generate-first → merge → mask) is sound, and the determinism envelope is preserved — `generate_tables` reads its seed directly from `config["global_settings"]["seed"]`, so no seed-path is broken by the new unified entry.

The single most important issue is a **schema/runtime mismatch on the generate→mask FK direction**: `_reference_graph_valid` allows a generate child to reference a mask parent, but `synthesize.py::generate_tables` raises a plain `ValueError` at runtime when it encounters that case. That ValueError is neither a `PipelineConfigError` nor a `KeyResolutionError`, so it falls into the platform's bare `except Exception` handler with no job-status flip and no manifest — the job hangs in `running`. The schema should reject this shape at validation time until V2.1.

A secondary issue — `_topo_sort` in `synthesize.py` uses recursive DFS — is the exact class of stack-overflow bug that the iterative rewrite in `_reference_graph_valid` was introduced to fix.

---

## 2. Findings

### Finding 1
**Severity:** High  
**Category:** Correctness  
**Location:** `src/decoy_engine/generation/synthesize.py::_topo_sort`

**Issue:** `_topo_sort` uses recursive DFS (`dfs(parent)` inside `dfs`). A chain of >1000 generate tables with reference dependencies will hit Python's default recursion limit and raise `RecursionError` at config-execution time. This is the exact same bug class that the iterative DFS in `config/_pipeline.py::_reference_graph_valid` was written to fix — the comment there reads: _"Pre-fix a chain of >1000 tables (or any cycle of depth >1000) raised Python's default RecursionError at config-load time. Iterative DFS produces identical cycle-detection semantics."_ The fix was applied to the validator but not to the topological sort in the synthesizer.

**Impact:** Any generate config with a reference chain longer than the recursion limit (~1000 on CPython default) crashes the engine at runtime with a bare `RecursionError`, not a typed `PipelineConfigError`. The worker's bare `except Exception` logs only the type name; the job hangs in `running` (manifest never written).

**Recommended fix:** Replace the recursive `dfs` with an iterative Kahn's algorithm (simpler for a DAG that the validator already certified acyclic):

```python
def _topo_sort(deps: dict[str, set[str]]) -> list[str]:
    """Iterative Kahn's sort over the reference dep graph."""
    in_degree: dict[str, int] = {n: 0 for n in deps}
    for n, parents in deps.items():
        for p in parents:
            if p in in_degree:
                in_degree[p] += 0  # ensure parent is keyed
    # Re-compute: in_degree[n] = number of parents n depends on
    in_degree = {n: 0 for n in deps}
    for n, parents in deps.items():
        for p in parents:
            if p in deps:
                in_degree[n] = in_degree.get(n, 0) + 1
    queue = [n for n, d in in_degree.items() if d == 0]
    result: list[str] = []
    while queue:
        n = queue.pop(0)
        result.append(n)
        for candidate, parents in deps.items():
            if n in parents:
                in_degree[candidate] -= 1
                if in_degree[candidate] == 0:
                    queue.append(candidate)
    return result
```

Verification: `python -c 'from decoy_engine.generation.synthesize import _topo_sort; d = {str(i): {str(i+1)} for i in range(1500)}; d["1500"] = set(); print(len(_topo_sort(d)))'` — should print `1501` without RecursionError.

---

### Finding 2
**Severity:** High  
**Category:** Correctness  
**Location:** `src/decoy_engine/config/_pipeline.py::_reference_graph_valid` (lines ~122–130) vs `src/decoy_engine/generation/synthesize.py::generate_tables` (line ~61)

**Issue:** Schema/runtime mismatch on the generate→mask FK direction.

`_reference_graph_valid` explicitly permits a generate-kind child to reference a mask-kind parent (`elif parent.columns: parent_cols = {c.name for c in parent.columns}` — no error raised). The engine execution docstring (`execution/_pipeline.py`) acknowledges this direction is deferred: _"Generate child to mask parent FK direction... the resolution path is V2.1."_

However, `synthesize.py::generate_tables` raises a **plain `ValueError`** (not `PipelineConfigError`) at runtime when it detects this:
```python
if ref not in generate_by_name:
    raise ValueError(
        f"table {name!r} column {col.get('name')!r}: "
        f"reference_table {ref!r} is not a generate table"
    )
```

Consequence on the platform side:
1. Schema validation **passes** — the operator receives no error at submit time.
2. `run_pipeline` is called; `generate_tables` raises `ValueError`.
3. `ValueError` is not caught by `except PipelineConfigError` or `except (KeyResolutionError, ImportError)` in `_run_v2_pipeline_in_worker`.
4. It falls into `except Exception` which **only logs `type(exc).__name__`** — no `job.status = JobStatus.failed`, no `finished_at`, no `update_finished_manifest`.
5. If `run_v2_pipeline_from_config` / `run_v2_pipeline_job` don't internally catch `ValueError` and flip status, **the job hangs in `running` forever**.

**Impact:** A valid-by-schema config that uses a deferred feature silently hangs the job rather than surfacing a clear, recoverable error to the operator.

**Recommended fix:** Add a guard to `_reference_graph_valid` that rejects generate→mask FK until V2.1 ships:

```python
# In _reference_graph_valid, after checking parent exists:
parent = by_name[ref_table]
if parent.columns and not parent.generate_columns:
    raise ValueError(
        f"table {table.name!r}: reference column {col.name!r} "
        f"references mask-kind table {ref_table!r}; "
        "generate-to-mask FK is deferred to V2.1. "
        "Use a generate parent or wait for V2.1."
    )
```

Alternatively, convert the ValueError in `synthesize.py` to `PipelineConfigError` so it is caught by the platform's first `except` clause, which does flip status and write the manifest. Either approach is acceptable; rejecting at schema time is cleaner for the operator UX.

---

### Finding 3
**Severity:** Medium  
**Category:** Reliability  
**Location:** `src/decoy_engine/config/_pipeline.py::_reference_graph_valid` — local type annotation

**Issue:** The `stack` local variable carries a string annotation `"Iterator[str]"` but `Iterator` is not imported in `config/_pipeline.py`:

```python
stack: list[tuple[str, "Iterator[str]"]] = []
```

`from __future__ import annotations` is NOT at the top of `config/_pipeline.py` (it is present in `execution/_pipeline.py`). At runtime, local variable annotations are not evaluated in Python 3.10+, so there is no `NameError`. However, mypy and pyright will flag this as `Name "Iterator" is not defined` in `config/_pipeline.py`, blocking a clean static-analysis run.

**Recommended fix:**
```python
from __future__ import annotations  # at module top, OR

# Alternatively, import just for the annotation:
from collections.abc import Iterator
```

The annotation is purely cosmetic here (local variable hints carry no runtime meaning in Python 3.10+), so the simplest fix is `from __future__ import annotations` at the top of `config/_pipeline.py` (consistent with `execution/_pipeline.py`).

---

### Finding 4
**Severity:** Low  
**Category:** Reliability  
**Location:** `src/decoy_engine/config/_pipeline.py::_per_table_kind_consistency`

**Issue:** The validator checks `if mask_table_names and not self.sources` to catch "at least one mask table but no sources block." This is correct as a global gate. However, it does not check that **each individual mask table** has a corresponding entry in `self.sources` by name. A config with `sources: {"customers": {...}}` and two mask tables `customers` + `orders` passes the validator but fails at runtime when the runner tries to resolve the source path for `orders`.

**Impact:** Medium — runtime failure, not silent wrong output. The operator sees a source-not-found error rather than a validation error, which is recoverable but less informative.

**Recommended fix:** Add per-table check in the validator:
```python
for tname in mask_table_names:
    if tname not in self.sources:
        raise ValueError(
            f"mask table {tname!r} has no corresponding entry in `sources:`; "
            f"declare `sources.{tname}` or remove the mask strategy."
        )
```
Note: this is a stricter contract than V1. Confirm with Dennis whether the platform runner is expected to resolve source paths by convention (not by name match) before adding this — the platform's S17 transform path may already handle name→path mapping differently.

---

## 3. Performance Notes

**`_topo_sort` (synthesize.py):** The current recursive DFS is O(V + E) in time and O(V) in stack space. For V=N tables it uses N stack frames. The iterative Kahn's replacement keeps the same O(V + E) time complexity with O(V) heap space and no stack depth risk. For typical pipeline configs (< 50 tables) the current implementation is fast; the risk is purely at the stack depth limit.

**`run_pipeline` (execution/_pipeline.py):** The function calls `profile_source`, `compile_plan`, `generate_tables`, and `PandasExecutionAdapter.run` in sequence. The bottleneck for mixed configs will be the mask side (pandas adapter), consistent with the pre-FC-1 pure-mask path. No new N+1 patterns or redundant work introduced. The `dict(sources) if sources else {}` copy at entry is O(N tables) — cheap; it prevents the caller's source dict from being mutated by the merge step.

**Profile measurement:** For mixed configs, measure with:
```
python -m cProfile -s cumulative -c 'from decoy_engine.execution._pipeline import run_pipeline; run_pipeline(cfg, sources=srcs, engine_version="bench")'
```
Focus on the `generate_tables` vs `adapter.run` split to understand where time is spent as generate table counts grow.

---

## 4. Suggested Tests

1. **Generate→mask FK rejected at validation** (once Finding 2 is fixed):
```python
def test_generate_child_referencing_mask_parent_rejected():
    with pytest.raises(ValidationError, match="generate-to-mask FK is deferred"):
        PipelineConfig.model_validate({
            "version": 1,
            "global_settings": {"seed": 0},
            "sources": {"customers": {"type": "file", "format": "csv", "path": "c.csv"}},
            "tables": [
                {"name": "customers", "columns": [{"name": "id", "strategy": "faker", "provider": "person_email", "namespace": "ns"}]},
                {"name": "synth_orders", "row_count": 10, "generate_columns": [
                    {"name": "cid", "type": "reference", "reference_table": "customers", "reference_column": "id"}
                ]},
            ],
            "targets": {"customers": {"type": "file", "format": "csv", "path": "out.csv"}, "synth_orders": {"type": "file", "format": "csv", "path": "out2.csv"}},
        })
```

2. **`_topo_sort` deep chain (1500 tables) does not RecursionError** (once Finding 1 is fixed):
```python
def test_topo_sort_deep_chain_no_recursion_error():
    deps = {str(i): {str(i + 1)} for i in range(1500)}
    deps["1500"] = set()
    result = _topo_sort(deps)
    assert len(result) == 1501
    assert result[-1] == "0"  # root (no parents) is last in topo post-order
```

3. **`run_pipeline` with no-source pure-generate config** (already covered in `test_run_pipeline.py`; extend with seed reproducibility across 3 independent runs, not just 2, to guard against first-run initialization effects).

4. **`_per_table_kind_consistency` missing per-table source entry** (once Finding 4 is accepted):
```python
def test_mask_table_without_source_entry_rejected():
    with pytest.raises(ValidationError, match="no corresponding entry in `sources:`"):
        PipelineConfig.model_validate({...  # customers in sources, orders mask table but no sources.orders ...})
```

---

## 5. What's Good

- **Determinism is intact.** `generate_tables` reads `global_settings.seed` directly from the config dict; `run_pipeline` passes the same validated dump to both `generate_tables` and `compile_plan`. No seed split, no hidden nondeterminism introduced.
- **Generate-first sequencing is correct.** Running generate tables before mask tables and merging their outputs into `merged_sources` ensures mask tables can use generate output as FK pools — the ordering guarantee is structurally enforced, not convention-dependent.
- **`TableConfig._mask_xor_generate` is tight.** The XOR validator prevents ambiguous configurations at the earliest possible point; `classify_table_kinds` then has a clean invariant to rely on.
- **`ExecutionResult.table_kinds` is a clean extension.** Adding it as a `field(default_factory=dict)` keeps backward compatibility with all pre-FC-1 call sites that don't populate it.
- **Test coverage for FC-1 in `test_run_pipeline.py` is good** — classify, pure-generate, pure-mask, mixed, and determinism are all covered. The two determinism cells (`test_pure_generate_two_runs_byte_equal`, `test_mixed_two_runs_byte_equal`) use `to_pydict()` comparison which catches value drift but not Arrow schema drift; consider adding `result1.outputs[name].schema == result2.outputs[name].schema` to the assertion for completeness.
