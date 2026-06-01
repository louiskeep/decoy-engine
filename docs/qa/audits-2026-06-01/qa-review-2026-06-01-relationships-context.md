# QA Review — `relationships/` + `context.py` — 2026-06-01

**Reviewer:** Claude (QA/Performance)
**Scope:** `src/decoy_engine/relationships/_graph.py`, `src/decoy_engine/relationships/_namespace.py`, `src/decoy_engine/context.py`
**Ref:** `main` @ `36d3f4157defaf73957e15675ac7d0e02c3fe626`
**Prior coverage excluded:** `determinism/` (QA-7 F11 already shipped), `generation/synthesize.py` (QA-7 session), `connectors/` (2026-05-31 session2 + QA-7 session).

---

## 1. Summary

The relationship graph builder and namespace registry are functionally correct: the DAG is cycle-detected, topological ordering is deterministic (sorted-queue Kahn's), and the namespace ambiguity rules are consistently enforced. Two compile-time performance issues will become visible at real pipeline scales (500+ tables), and a silent duplicate-key win in the orphan policy check is a low-probability correctness risk. `context.py` is solid after the S21 Q11 canonical HKDF fix; one minor observability gap remains in `emit_step`.

**Most important finding:** `for_column` in `NamespaceRegistry` is an O(B×K) linear scan called once per deterministic column per plan compile — at 200 namespaces × 50 columns each this is 10,000 iterations per column lookup.

---

## 2. Findings

### F1 — Medium — Performance — `_graph.py: build_relationship_graph` — O(n²) topological sort

**Location:** `build_relationship_graph`, lines ~168–196 (Kahn's algorithm block)

**Issue:** The `queue` is a Python `list`. Each iteration does `queue.pop(0)` (O(n)) then `queue.sort()` after appending new zero-indegree nodes. For a 1,000-node FK graph the worst-case complexity is O(n² + n·k·log k) where k is the average out-degree. At 500 tables this is ~250,000 list-copy operations at compile time; at 1,000 it is ~1M — measurable (~50–200 ms in CPython depending on machine).

**Impact:** Plan compile latency only (not hot path). Still, at enterprise pipeline sizes (hundreds of tables with composite FKs) the compiler becomes perceptibly slow on every config edit.

**Fix:** Replace the list queue with a `heapq` min-heap. Nodes are `(table_name, parent_columns)` tuples which are already sortable, so `heapq.heappush`/`heappop` gives O(n log n) overall without the full re-sort on each step.

```python
import heapq

queue_heap: list[tuple[str, tuple[str, ...]]] = []
for n, d in indegree.items():
    if d == 0:
        heapq.heappush(queue_heap, n)
ordered: list[tuple[str, tuple[str, ...]]] = []
while queue_heap:
    node = heapq.heappop(queue_heap)
    ordered.append(node)
    for nxt in sorted(out_edges[node]):
        indegree[nxt] -= 1
        if indegree[nxt] == 0:
            heapq.heappush(queue_heap, nxt)
```

Output is byte-identical to the current sorted-queue output because both produce lexicographically smallest topological order for the same DAG. Verify with: `assert new_ordering == old_ordering` on the existing topological-sort test fixtures.

**Benchmark:** `timeit` the current vs heap version with a synthetic 500-node chain graph. Expect 5–20× improvement.

---

### F2 — Medium — Performance — `_namespace.py: NamespaceRegistry.for_column` — O(B×K) linear scan

**Location:** `NamespaceRegistry.for_column`, `NamespaceRegistry.for_relationship` (which calls `for_column` twice)

**Issue:** `for_column` iterates `self.bindings` (B namespaces) and for each binding iterates `binding.declared_by` (K `(table, cols)` tuples). The lookup is O(B×K) per call. It is called at least once per deterministic column during `build_namespace_registry` (step 3) and at least once per FK relationship during `build_relationship_graph`. For a pipeline with 200 deterministic columns, 50 namespaces each holding 40 columns, each `for_column` call scans up to 2,000 entries.

**Impact:** Compile-time only, but repeated 200+ times during namespace build + graph build → up to 400,000 dict-entry comparisons per compile. At larger pipeline scales (400 columns, 100 namespaces) this approaches 80,000 iterations per column lookup × 400 = 32M comparisons per compile.

**Fix:** Pre-compute a reverse dict in `build_namespace_registry` and store it alongside (or inside) the registry. Since `NamespaceRegistry` is `frozen=True`, add the index as a field constructed before the dataclass is frozen:

```python
@dataclass(frozen=True)
class NamespaceRegistry:
    bindings: tuple[NamespaceBinding, ...]
    _index: dict[tuple[str, tuple[str, ...]], str]  # (table, cols) -> namespace

    def for_column(self, table: str, columns: tuple[str, ...]) -> str | None:
        return self._index.get((table, columns))
```

In `build_namespace_registry`, before constructing the registry:
```python
index = {key: ns for ns, keys in namespace_to_columns.items() for key in keys}
return NamespaceRegistry(bindings=bindings, _index=index)
```

This makes every `for_column` call O(1). `for_relationship` drops from O(3B×K) to O(3) for the three fallback chain lookups.

**Verify:** Existing namespace round-trip tests should pass unchanged. Add a large-registry benchmark (200 namespaces × 50 columns each; measure `for_column` call time before and after).

---

### F3 — Low — Correctness — `_graph.py: check_orphan_fk_policy_completeness` — silent duplicate-key win

**Location:** `check_orphan_fk_policy_completeness`, config-loop block (~line 220)

**Issue:** If a config's `relationships` list contains two entries with the same `(parent_table, parent_columns)` key but different `orphan_policy` values, the second entry silently overwrites the first in `config_lookup`. No error or warning. The winning policy is whichever appears last in the YAML list.

**Impact:** An operator who accidentally duplicates a relationship entry with conflicting policies (e.g., `preserve` for a PoC config + `fail` for a prod config merged by mistake) sees no error. The plan silently uses the last-declared policy, which may be the wrong one.

**Probability:** Low (requires a duplicate entry with different values in the same file). But a merge conflict resolution tool could easily produce this.

**Fix:**
```python
if (parent_table, tuple(parent_cols)) in config_lookup:
    existing = config_lookup[(parent_table, tuple(parent_cols))]
    if existing != policy:
        raise PlanCompileError(
            code="orphan_fk_policy_duplicate",
            path=f"relationships[{idx}]",
            message=(
                f"Relationship for parent {parent_table}.{parent_cols} "
                f"declares orphan_policy={policy!r} but a previous entry "
                f"declared {existing!r}. Remove the duplicate entry."
            ),
        )
    # same policy on a duplicate entry: skip silently
    continue
config_lookup[(parent_table, tuple(parent_cols))] = policy
```

---

### F4 — Nit — Observability — `context.py: emit_step` — silent TypeError swallow

**Location:** `emit_step`, `except TypeError` block (~line 99)

**Issue:** When a `step()` implementation raises `TypeError` (indicating a signature mismatch between the engine's `emit_step` call and the platform's `JobLogger.step`), the first fallback catches it and tries the 3-kwarg form. If that also raises, it's swallowed silently. This is intentional for forward-compatibility, but makes it invisible when a newer `node_id` kwarg is never propagated (e.g., if the platform deploys a JobLogger version that doesn't accept `node_id`).

**Impact:** Steps lose `node_id` attribution silently — the canvas deep-link stops working in the reporting UI without any log signal to diagnose.

**Fix:** Add a DEBUG-level log in the first `except TypeError` branch:
```python
except TypeError:
    import logging
    logging.getLogger(__name__).debug(
        "emit_step: step() rejected new kwargs (node_id, error_class); "
        "falling back to 3-kwarg form. Check engine/platform version alignment."
    )
    try:
        fn(name, status=status, rows_in=rows_in, rows_out=rows_out)
    except Exception:
        pass
```

---

### F5 — Nit — Design — `_namespace.py: build_namespace_registry` — `None`-sentinel pattern in `composite_group_ns`

**Location:** `build_namespace_registry`, step 2.5 block (~lines 268–295)

**Issue:** `composite_group_ns` maps `(table, group)` → `str | None` where `None` means "no explicit namespace; use derived." The code checks `if group_key not in composite_group_ns: composite_group_ns[group_key] = None` to set the sentinel, but then checks `if explicit_ns: prev = composite_group_ns.get(group_key)` before overwriting. The interplay between the `None` sentinel and the dict-presence check is subtle: if a group is first seen with `explicit_ns=None` (no namespace declared), `composite_group_ns[group_key] = None` is set; then if the same group is seen again with an explicit namespace, `prev = composite_group_ns.get(group_key)` returns `None`, which `if prev is not None and prev != ns` treats as "no conflict" — correct, but the reader must hold both invariants in their head simultaneously.

**Not a bug** — the output is correct. But the pattern is fragile enough to invite a future regression if someone reorganizes the two branches.

**Suggestion:** Use a `dict[..., str]` (never `None`) and only insert when an explicit namespace is known. Derive the fallback name at the loop-over-composite_group_ns step rather than storing `None`.

---

## 3. Performance Notes

| Module | Bottleneck class | Complexity | Measure with |
|--------|-----------------|------------|-------------|
| `_graph.py: build_relationship_graph` (Kahn's) | CPU — list operations | O(n²) worst case | `timeit` with synthetic 500-node chain graph |
| `_namespace.py: for_column` | CPU — linear scan | O(B×K) per call | `timeit` with 200-namespace × 50-col registry |
| `_namespace.py: build_namespace_registry` step 3 | CPU — nested loops | O(tables × columns) | `cProfile` on a large pipeline config |

Both bottlenecks are **compile-time only** — they run once per plan compile, not per row processed. For pipelines that compile on every API call, they're more visible; for pipelines with a compile-then-execute split (CLI `decoy plan` pre-computed), they only matter at plan-generation time.

**Profile command:**
```bash
python -m cProfile -s cumulative -c \
  "from decoy_engine.plan._compile import compile_plan; compile_plan(big_config, big_profile)"
```

---

## 4. Suggested Tests

### F1 — heap vs list topological ordering equivalence
```python
# Generate a random 500-node FK DAG, run both implementations, assert equal ordering.
def test_build_relationship_graph_topo_order_equivalence():
    # Build relationships for a 50-table chain: t1→t2→...→t50
    # also include 10 sibling branches for breadth
    ...
    graph = build_relationship_graph(rels, ...)
    assert graph.ordering == expected_lexicographic_order
```

### F2 — `for_column` returns consistent result vs index
```python
def test_namespace_registry_for_column_index_matches_scan():
    registry = build_namespace_registry(config_with_50_namespaces, profile)
    for binding in registry.bindings:
        for (table, cols) in binding.declared_by:
            assert registry.for_column(table, cols) == binding.namespace
```

### F3 — duplicate orphan_policy raises
```python
def test_check_orphan_fk_policy_completeness_rejects_duplicate_conflict():
    config = {"relationships": [
        {"parent": {"table": "orders", "columns": ["id"]}, "orphan_policy": "preserve"},
        {"parent": {"table": "orders", "columns": ["id"]}, "orphan_policy": "fail"},
    ]}
    with pytest.raises(PlanCompileError, match="orphan_fk_policy_duplicate"):
        check_orphan_fk_policy_completeness(config, relationships)

def test_check_orphan_fk_policy_completeness_allows_identical_duplicate():
    # same policy declared twice: no error
    ...
```

### F4 — emit_step TypeError fallback is logged
```python
def test_emit_step_logs_debug_on_signature_mismatch(caplog):
    class OldJobLogger:
        def step(self, name, *, status, rows_in, rows_out): ...
    with caplog.at_level(logging.DEBUG, logger="decoy_engine.context"):
        emit_step(OldJobLogger(), "mask", status="finish", node_id="n1")
    assert "falling back to 3-kwarg form" in caplog.text
```

### General — cycle detection
```python
def test_build_relationship_graph_raises_on_cycle():
    # a→b, b→c, c→a
    with pytest.raises(PlanCompileError, match="fk_cycle"):
        build_relationship_graph(cyclic_rels, ...)
```

---

## 5. What's Good

- **Deterministic topological ordering**: using a sorted queue (and sorting neighbor lists before insertion) ensures byte-stable ordering across Python runtimes. Most implementations skip this and produce ordering that varies with dict/set ordering. This is the right call for a system that must be reproducible.
- **Injective length-prefixing in `derive()`**: `len(namespace).to_bytes(4, 'big') + namespace_bytes + len(source).to_bytes(4, 'big') + source` correctly prevents `("abc", "def")` colliding with `("ab", "cdef")`. Many HMAC-based schemes skip this and are silently collision-prone.
- **Multi-parent FK rejection**: raising `multi_parent_fk_unsupported` instead of silently using first-parent-wins is the right call. First-parent-wins is a common silent bug in FK-aware maskers.
- **`NamespaceConfigError` as `PlanCompileError` subclass**: callers writing `except PlanCompileError` catch everything, while `isinstance(e, NamespaceConfigError)` + `e.code` still discriminate. Clean error hierarchy.
- **`context.py` emit helpers**: the `getattr(logger, "method", None)` pattern with try/except swallow around structured calls is the correct way to layer an optional protocol on top of a required one. `stdlib logging.Logger` satisfies `Logger` without any special casing.
