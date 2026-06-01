# QA Review — `walks/` + `generators/` — 2026-06-01

**Scope:** First review of the `walks/` package (cross_file, hazards, inference, graph, diff, types) and `generators/` (columns, derivation), with targeted findings in `plan/_compile.py` and `config/_tables.py`. All prior QA branches (connectors/generation/profile, execution/quality/determinism, mg2-mg3-mg4, relationships/context, storm-hardening, persistence/scheduler, platform-api, CLI) are avoided.

**Reviewer:** Claude Sonnet 4.6 (QA role)

---

## 1. Summary

The `walks/` package is structurally sound — pure functions, frozen dataclasses, excellent testability — but ships **two Critical hidden-nondeterminism bugs** that violate the engine's core determinism contract. `generators/columns.py` has a High correctness + performance defect in its null-injection loop that silently corrupts integer column dtypes. Combined, these three findings can produce non-reproducible output and masked data that silently drifts from the expected schema type on every run.

---

## 2. Findings

### F1 — Critical | Correctness (Determinism) | `generators/columns.py` — `_generate_reference_column` uses unstable pool order

**File:** `src/decoy_engine/generators/columns.py`

**Code:**
```python
ref_values = ref_df[reference_column].dropna().unique().tolist()
```

**Issue:** `pd.Series.unique()` returns values in *first-occurrence* order — the order they appear in `ref_df`. `ref_df` is a DataFrame that arrives from a prior generation pass or, for masking, from a database read. Database reads without an explicit `ORDER BY` return rows in undefined order that varies across server restarts, page cache state, and query plan changes. The `ref_rng` is seeded deterministically from `_column_seed`, so the same RNG seed + a different `ref_values` list produces different FK assignments. Two runs of the same job with the same seed but different DB row ordering produce non-identical output — **a violation of the "same seed → byte-identical output" contract**.

**Impact:** Every table with a `reference`-type generation column is affected. FK relationship columns in generated datasets will differ across runs, breaking reproducibility assertions, audit trails, and referential-integrity downstream tests.

**Fix:**
```python
# Sort the pool so FK assignment is independent of DataFrame row order.
ref_values = sorted(
    ref_df[reference_column].dropna().unique().tolist(),
    key=lambda x: str(x),  # str key handles mixed int/str pools safely
)
```
The `str` key is important: pools that contain integers (e.g., ID columns) sort numerically by `key=str` only if all values happen to be the same digit length. Better: detect the common dtype and sort accordingly.
```python
raw = ref_df[reference_column].dropna().unique().tolist()
try:
    ref_values = sorted(raw)  # works for int or str uniform pools
except TypeError:
    ref_values = sorted(raw, key=str)  # mixed-type fallback
```

**Verify:** `pytest -k test_reference_column_determinism_cross_run` — run the same job twice, pickle both outputs, `assert out_a == out_b`. Property-based with Hypothesis: shuffle `ref_df` row order, assert generated column identical.

---

### F2 — Critical | Correctness (Determinism) | `walks/cross_file.py` — `_pk_table_for_id_column` iterates a `set`, PYTHONHASHSEED-sensitive

**File:** `src/decoy_engine/walks/cross_file.py`

**Code (in `infer_cross_file_edges`):**
```python
table_names = {t.name for t in snapshot.tables}  # set
...
match = _pk_table_for_id_column(col_name, table_names)
```

**And inside `_pk_table_for_id_column`:**
```python
for t in table_names:  # iterates a set — PYTHONHASHSEED-dependent order
    if t.lower() in candidates:
        return t
...
for t in table_names:  # same problem
    lower = t.lower()
    if any(lower.endswith(c) or lower.endswith("_" + c) for c in candidates):
        return t
```

**Issue:** `table_names` is a `set[str]`. Python set iteration order is determined by `PYTHONHASHSEED`, which randomizes on every process start (unless pinned with `PYTHONHASHSEED=0`). When two tables both match the stem or suffix heuristic, which table wins as the canonical PK owner changes between processes. The `run_cross_file_walk` result — edges, hazard counts, the FK graph surfaced in the UI — is **non-deterministic across process restarts** without PYTHONHASHSEED pinning. This is explicitly called out in the application context as a Critical finding class.

**Impact:** The edges returned by `run_cross_file_walk` can differ between the job-submission process and the worker process, between two re-runs of the same walk job, and between any two platform restarts. Walk results stored in the database (JSONB) and re-derived on demand will disagree, corrupting hazard UI state.

**Fix — two changes:**

1. In `infer_cross_file_edges`, pass a `frozenset` or list but iterate in sorted order:
```python
# Change:
canonical_pk_table[col_name] = _pk_table_for_id_column(col_name, table_names)
# Ensure the passed collection is sorted for determinism inside:
canonical_pk_table[col_name] = _pk_table_for_id_column(col_name, sorted(table_names))
```

2. Change `_pk_table_for_id_column` signature to accept `Iterable[str]` (already implicitly supports it) and document the determinism contract:
```python
def _pk_table_for_id_column(column_name: str, table_names: Iterable[str]) -> str | None:
    ...
    # Both loops must iterate in a stable order; callers must pass sorted input.
    for t in table_names:
        ...
```

Alternatively, collect into a `sorted(...)` inside the function itself:
```python
sorted_tables = sorted(table_names)
for t in sorted_tables:
    if t.lower() in candidates:
        return t
for t in sorted_tables:
    lower = t.lower()
    if any(lower.endswith(c) or lower.endswith("_" + c) for c in candidates):
        return t
```

**Same fix needed** for the exact-match loop in `storm_profiles_to_snapshot` — the `tables` list is built from `profiles` which is a `list[StormProfile]`, so ordering is caller-controlled and stable. No set iteration there. The only set in this file is `table_names` in `infer_cross_file_edges`. ✓

**Verify:** Run `PYTHONHASHSEED=1 python -c "..."` vs `PYTHONHASHSEED=2 python -c "..."` on a snapshot with two tables sharing a stem (`customers` + `customer_archive`) and assert edges are identical. Add a regression test that explicitly checks stability under different hash seeds.

---

### F3 — High | Correctness + Performance | `generators/columns.py` — null injection loop corrupts int dtype and has O(n) scalar write overhead

**File:** `src/decoy_engine/generators/columns.py`, `generate_column()`

**Code:**
```python
if null_probability > 0:
    column_seed = self._column_seed(column_name, column_config)
    null_rng = random.Random()
    for i in range(num_rows):
        null_rng.seed(column_seed + i)    # 100K reseed calls for 100K rows
        if null_rng.random() < null_probability:
            result.iloc[i] = None         # pandas scalar write; upcasts int64 to float64
```

**Issues (two, both real):**

**A. Correctness — dtype mutation.** If `result` has dtype `int64` (e.g., a sequence column or Faker integer column), the first `result.iloc[i] = None` triggers an in-place dtype promotion to `float64` (since `int64` cannot hold `NaN`). All subsequent values in the column become floats. A downstream masking strategy or schema validator that expects `int64` will receive `float64`, causing type mismatches, silent truncation in downstream casts, or `check_null_bearing_int_unsupported` false-negatives (the plan check reads the *source* dtype, not the generated dtype).

**B. Performance.** For `num_rows = 100_000` and `null_probability = 0.1`: ~100K calls to `null_rng.seed()` (each is a full Mersenne Twister state reset, ~microseconds each = ~100 ms pure seeding overhead) + ~10K calls to `result.iloc[i] = None` (each pandas scalar setter checks dtypes and bounds). This path runs inside the innermost generation loop; it's a CPU bottleneck, not I/O.

**Fix:**
```python
if null_probability > 0:
    column_seed = self._column_seed(column_name, column_config)
    # Vectorized null mask: one RNG construction, one draw, one boolean
    # assignment. Preserves the per-row seed (column_seed + i) contract
    # by feeding the row indices as an offset — same null decisions as the
    # row-by-row path when numpy and Python random agree on the threshold,
    # modulo RNG family differences (see note below).
    #
    # BREAKING NOTE: numpy default_rng produces different floats than
    # Python random.Random for the same seed. The null *pattern* (which
    # rows become null) will change. This is a controlled determinism bump;
    # document in release notes and bump SEED_PROTOCOL_VERSION if null
    # pattern is part of the byte-identical contract.
    null_rng = np.random.default_rng(column_seed)
    null_mask = null_rng.random(num_rows) < null_probability
    # Use pd.array / mask-based assignment to preserve dtype.
    if null_mask.any():
        # Convert to nullable dtype before applying NaN so int columns
        # stay integer (pd.NA instead of float NaN).
        if pd.api.types.is_integer_dtype(result):
            result = result.astype("Int64")  # pandas nullable integer
        result = result.where(~null_mask, other=pd.NA)
```

If byte-identical null patterns with existing runs must be preserved (hard requirement), use the slower but shape-equivalent form:
```python
null_positions = [
    i for i in range(num_rows)
    if random.Random(column_seed + i).random() < null_probability
]
if null_positions:
    if pd.api.types.is_integer_dtype(result):
        result = result.astype("Int64")
    result.iloc[null_positions] = pd.NA
```
This still avoids the one-at-a-time reseed loop AND the progressive dtype mutation.

**Profile:** `python -m scalene generators/columns.py` with `num_rows=100_000`, `null_probability=0.1` will show the null loop as the top CPU line.

---

### F4 — High | Reliability | `walks/hazards.py` — recursive DFS in `_detect_cycles` hits Python recursion limit on deep schemas

**File:** `src/decoy_engine/walks/hazards.py`

**Code:**
```python
def visit(node: str, path: list[str]) -> None:
    color[node] = GRAY
    path.append(node)
    for neighbor in graph.adjacency.get(node, ()):
        ...
        elif color.get(neighbor, WHITE) == WHITE:
            visit(neighbor, path)   # unbounded recursion depth
    path.pop()
    color[node] = BLACK
```

**Issue:** Python's default recursion limit is 1000 (`sys.getrecursionlimit()`). A schema with a chain of 1001 tables (A→B→C→...→Z) or a cycle of depth >1000 raises `RecursionError`. The walk job would fail with an unhandled exception, producing no report. Even at depth 500 tables, the stack is deep enough to interact badly with tracebacks, GC pressure, and profilers.

Schemas of this depth are uncommon in OLTP, but data-warehouse schemas (star schemas with many bridge tables) or schema imports of large open-source datasets (e.g., OpenStreetMap, MediaWiki) can reach this depth.

**Fix — iterative DFS with explicit stack:**
```python
def _detect_cycles(graph: ERGraph) -> list[Hazard]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {t: WHITE for t in graph.adjacency}
    cycles: set[tuple[str, ...]] = set()

    for start in graph.adjacency:
        if color[start] != WHITE:
            continue
        # Explicit stack: each frame is (node, iterator-over-neighbors, path)
        stack: list[tuple[str, Iterator[str], list[str]]] = []
        path: list[str] = []

        def _push(node: str) -> None:
            color[node] = GRAY
            path.append(node)
            stack.append((node, iter(graph.adjacency.get(node, ()))))

        _push(start)
        while stack:
            node, neighbors_iter, *_ = stack[-1]
            try:
                neighbor = next(neighbors_iter)
                nc = color.get(neighbor, WHITE)
                if nc == GRAY:
                    # Back-edge found — cycle
                    idx = path.index(neighbor)
                    cycles.add(_canonical_cycle(tuple(path[idx:])))
                elif nc == WHITE:
                    _push(neighbor)
            except StopIteration:
                stack.pop()
                path.pop()
                color[node] = BLACK

    hazards: list[Hazard] = []
    for cycle in sorted(cycles):
        hazards.append(Hazard(
            kind="CIR",
            table=None,
            description=f"Cycle: {' -> '.join(cycle)} -> {cycle[0]}",
            details={"cycle": list(cycle)},
        ))
    return hazards
```

This eliminates the recursion limit while preserving identical cycle detection semantics.

**Note:** The same iterator-based refactor should be applied to `PipelineConfig._reference_graph_valid` in `config/_pipeline.py`, which has the same recursive DFS pattern and the same depth risk at config-validation time.

---

### F5 — Medium | Design | `plan/_compile.py` — `_build_relationships` re-parses orphan policy independently of `check_orphan_fk_policy_completeness`

**File:** `src/decoy_engine/plan/_compile.py`

**Code (in `compile_plan`):**
```python
orphan_policy_lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
...
relationships = _build_relationships(config, profile)  # builds its own lookup internally
```

**And inside `_build_relationships`:**
```python
orphan_policy_lookup: dict[tuple[str, tuple[str, ...]], str] = {}
config_relationships = config.get("relationships", [])
for entry in config_relationships:
    ...  # same parse logic duplicated
```

**Issue:** Two independent implementations parse the same `config.relationships` block. If the parsing logic in `_build_relationships` drifts from `check_orphan_fk_policy_completeness`, the validated lookup and the stamped Plan disagree silently. A pipeline could pass the check with policy `"fail"` but stamp `"preserve"` in the Plan if `_build_relationships` has a subtly different parse path — it would never raise at runtime but would mask FK orphan data instead of failing.

**Fix:** Pass `orphan_policy_lookup` from `check_orphan_fk_policy_completeness` into `_build_relationships`:
```python
# In compile_plan:
orphan_policy_lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
relationships = _build_relationships(config, profile, orphan_policy_lookup=orphan_policy_lookup)

# In _build_relationships signature:
def _build_relationships(
    config: dict[str, Any],
    profile: Profile,
    orphan_policy_lookup: dict[tuple[str, tuple[str, ...]], str] | None = None,
) -> tuple[PlanRelationship, ...]:
    # Remove the independent parse block; use the passed lookup directly.
    if orphan_policy_lookup is None:
        orphan_policy_lookup = {}
    ...
```

---

### F6 — Medium | Design/Reliability | `config/_tables.py` — `GenerateColumnConfig` accepts unknown extra params silently

**File:** `src/decoy_engine/config/_tables.py`

**Code:**
```python
class GenerateColumnConfig(BaseModel):
    model_config = ConfigDict(extra="allow")   # intentional, per Dennis S6-ENG-1 gate
    name: str
    type: Literal["faker", "sequence", "categorical", "formula", "reference"]
```

**Issue:** The per-type generation params (`faker_type`, `start`, `step`, `categories`, `weights`, `formula`, `reference_table`, `reference_column`) are read from `model_extra` / raw `column_config` at generation time without any upfront validation. A YAML typo like `fker_type: email` passes Pydantic validation silently and falls back to the `word` generator at generation time. The operator sees correctly-shaped output (words instead of emails) with no error. This is a silent data quality failure.

**Recommended approach:** Add per-type `model_validator`s that check required params for each `type` value. The reference type already has this pattern via `_reference_params_required`. Extend to the others:
```python
@model_validator(mode="after")
def _type_params_present(self) -> "GenerateColumnConfig":
    extras = self.model_extra or {}
    if self.type == "faker" and not extras.get("faker_type"):
        raise ValueError(f"faker column {self.name!r} requires `faker_type`")
    if self.type == "sequence" and extras.get("start") is None:
        raise ValueError(f"sequence column {self.name!r} requires `start`")
    if self.type == "categorical" and not extras.get("categories"):
        raise ValueError(f"categorical column {self.name!r} requires `categories`")
    if self.type == "formula" and not extras.get("formula"):
        raise ValueError(f"formula column {self.name!r} requires `formula`")
    return self
```
This is additive and does not remove `extra="allow"` (which has other callers that depend on pass-through). Dennis's S6-ENG-1 gate comment acknowledged this as a flag; this closes it.

---

### F7 — Medium | Correctness | `generators/columns.py` — `_generate_distribution_datetime` crashes on year-9999 source data

**File:** `src/decoy_engine/generators/columns.py`

**Code:**
```python
year_ends = pd.to_datetime([f"{y + 1}-01-01" for y in years_arr])
```

**Issue:** When a source datetime column contains a row from year 9999, `years_arr` contains 9999. `y + 1 = 10000`, and `pd.Timestamp("10000-01-01")` raises `OutOfBoundsDatetime`. This is a crash path — the distribution generator returns an exception rather than the fallback null series. The `pd.Timestamp` ceiling is 2262-04-11 for `datetime64[ns]` and 9999-12-31 for `datetime64[s]`.

**Fix:**
```python
year_ends_list = []
for y in years_arr:
    end_year = min(y + 1, 9999)
    if end_year == y:  # year == 9999, cap the end at year-end
        year_ends_list.append("9999-12-31")
    else:
        year_ends_list.append(f"{end_year}-01-01")
year_ends = pd.to_datetime(year_ends_list)
```
Alternatively, use a nanosecond-level cap: if `lo_ns >= ts_max.value`, the row is already at or past the max and should receive `ts_max` directly.

---

### F8 — Low | Performance | `generators/columns.py` — per-call locale Faker instantiation not cached

**File:** `src/decoy_engine/generators/columns.py`, `_generate_faker_column`

**Code:**
```python
if locale:
    faker_inst = make_faker(locale)          # new Faker() + provider scan every call
    providers = get_faker_providers(faker_inst)
```

**Issue:** `make_faker(locale)` creates a new `Faker` instance on every call to `_generate_faker_column`. `get_faker_providers(faker_inst)` then scans all available providers on that instance. For a table with 30 columns all using `locale: en_GB`, 30 separate Faker objects are created and 30 provider scans run. Faker instantiation is not free (~1–5ms per instance depending on locale, per `timeit`).

**Fix:** Cache locale Faker instances in a `dict` on `self`:
```python
# In __init__:
self._locale_fakers: dict[str, tuple[Faker, dict]] = {}

# In _generate_faker_column:
if locale:
    if locale not in self._locale_fakers:
        faker_inst = make_faker(locale)
        self._locale_fakers[locale] = (faker_inst, get_faker_providers(faker_inst))
    faker_inst, providers = self._locale_fakers[locale]
```
Thread safety: `ColumnGenerator` is already per-column/per-job; cache lifetime matches generator lifetime. No lock needed for the single-threaded generation path.

---

### F9 — Low | Correctness | `plan/_compile.py` — `_hash_config` silently coerces non-serializable config values

**File:** `src/decoy_engine/plan/_compile.py`

**Code:**
```python
canonical = json.dumps(
    semantic_config,
    sort_keys=True,
    ensure_ascii=True,
    separators=(",", ":"),
    default=str,   # ← silently calls str() on any non-serializable type
).encode("utf-8")
```

**Issue:** `default=str` means any non-JSON-serializable value (a `datetime`, a `UUID`, a dataclass instance) is silently converted to its `str()` representation. Two semantically different values that happen to `str()` identically (e.g., `datetime(2026, 1, 1, 0, 0)` and the string `"2026-01-01 00:00:00"`) produce the same hash. Pipeline config hash collisions are silent and cannot be detected after the fact.

In practice the config arrives via `yaml.safe_load` which only produces JSON-native types, so this is low probability. But it silently swallows the symptom of a future code path that feeds non-native types into the planner.

**Fix:** Remove `default=str` and let `json.dumps` raise `TypeError` on non-serializable input. This surfaces the bug loudly at plan-compile time rather than silently producing a wrong hash:
```python
canonical = json.dumps(
    semantic_config,
    sort_keys=True,
    ensure_ascii=True,
    separators=(",", ":"),
    # No `default=` — raise TypeError on non-JSON-native values.
).encode("utf-8")
```

---

### F10 — Nit | Performance | `generators/columns.py` — `time.time()` vs `time.perf_counter()` for latency measurement

**File:** `src/decoy_engine/generators/columns.py`, `generate_column()`

**Code:**
```python
start_time = time.time()
...
generation_time = time.time() - start_time
```

`time.time()` uses the wall clock and is subject to NTP adjustments (can go backwards). `time.perf_counter()` uses a monotonic high-resolution counter and is the correct choice for elapsed time measurement.

**Fix:** `start_time = time.perf_counter()` / `generation_time = time.perf_counter() - start_time`.

---

## 3. Performance Notes

| Module | Bottleneck class | Where to profile |
|---|---|---|
| `generators/columns.py` null loop | CPU — Python `random.seed()` calls + pandas scalar writes | `python -m scalene` on a 100K-row table with `null_probability=0.1` |
| `generators/columns.py` locale Faker | CPU — Faker construction | `timeit make_faker('en_GB')` × 30 vs cached |
| `walks/hazards.py` DFS | CPU — Python function call overhead for deep graphs | `cProfile` on a 500-table schema |
| `generators/columns.py` distribution numeric | CPU (vectorized) — no hot path issue | `np.random.default_rng` is fast; verify with `timeit` |

The **null injection loop** (F3) is the most likely performance regression under production-scale row counts. Expect ~100–300 ms overhead per 100K-row column with `null_probability > 0`. For a 10M-row table this is 10–30 seconds of pure Python overhead per null-bearing column.

**Algorithmic complexity notes:**
- `_detect_cycles` DFS: O(V + E) — correct.
- `_apply_cardinality_bounds`: O(n × |ref_pool|) for the slot-search loops. For large ref pools this is O(n²) in the worst case. Acceptable for typical pool sizes (<10K), but worth noting for large reference tables.
- `_generate_reference_column` cardinality repair (step 3): the `already_free = set(free_slots)` membership check inside a doubly-nested loop is O(|ref_pool| × |free_slots|). Could be O(|ref_pool|) with a pre-built index. Low priority.

---

## 4. Suggested Tests

| # | Location | Test case |
|---|---|---|
| T1 | `test_reference_column` | Shuffle `ref_df` row order between two `_generate_reference_column` calls with the same seed; assert output Series are identical (closes F1). |
| T2 | `test_cross_file_determinism` | Build a `SchemaSnapshot` where two tables both match a stem; run `run_cross_file_walk` twice in subprocess with different `PYTHONHASHSEED`; assert edge tuples are identical (closes F2). |
| T3 | `test_null_injection_preserves_int_dtype` | Generate a sequence column with `null_probability=0.1`; assert `result.dtype` is nullable integer (`Int64` or equivalent), not `float64` (closes F3A). |
| T4 | `test_null_injection_determinism` | Same seed, same column, two calls; assert null positions are identical (regression for F3 fix). |
| T5 | `test_detect_cycles_deep_chain` | Build an `ERGraph` with a 1500-table linear chain (A0→A1→...→A1499); assert `_detect_cycles` returns empty without RecursionError (closes F4). |
| T6 | `test_distribution_datetime_year_9999` | Snapshot with a `year_bins` entry of `{year: 9999, count: 1}`; assert `_generate_distribution_datetime` returns a valid Series, not an exception (closes F7). |
| T7 | `test_generate_column_config_faker_typo` | Construct a `GenerateColumnConfig` with `type="faker"` but `fker_type="email"` (note the typo); assert `ValidationError` is raised (closes F6 after fix). |
| T8 | `test_orphan_policy_lookup_single_source_of_truth` | Mock `check_orphan_fk_policy_completeness` to return a lookup with `"fail"` for a relationship; assert the compiled Plan also has `"fail"`, not `"preserve"` (regression for F5). |
| T9 | `test_hash_config_non_json_raises` | Feed a config dict with a `datetime` value to `_hash_config`; assert `TypeError` is raised (after F9 fix). |
| T10 | `test_locale_faker_cache_hit` | Create a `ColumnGenerator`, call `_generate_faker_column` twice with the same locale; assert only one `make_faker` call is made (via `unittest.mock.patch`). |

---

## 5. What's Good

- **`walks/` package architecture** is excellent: frozen dataclasses, pure functions, zero DB access past the snapshotter. Every function is trivially unit-testable with inline fixtures. The security-blind boundary (never touches raw row data) is well-enforced and documented.

- **`_detect_cycles` with `_canonical_cycle` rotation** is correct — same cycle traversed from two different DFS starting points produces the same normalized tuple. The deduplication via a `set[tuple[str,...]]` is clean.

- **`infer_edges` conservatism** is the right call: false positives propagate to hazard detection and the chase path; better to under-infer. The four-level match priority (exact singular → exact plural → component singular → component plural) is well-documented and easy to reason about.

- **`derivation.py` fingerprint approach** (R3.10) is solid: rename-stable seeds are critical for referential integrity across pipeline edits, and the `_EXCLUDED_FROM_FINGERPRINT` frozenset makes the contract explicit.

- **`_checks.py` null_bearing_int_unsupported** (F10, S13, row #10) is the right safety net for the int+null dtype divergence. The FK exemption is correct and well-motivated.

- **`PipelineConfig._reference_graph_valid`** catches reference cycles and FK integrity at config-submission time — exactly the right place. The three-color DFS logic is correct.

- **`_apply_cardinality_bounds` repair algorithm** handles impossible constraints gracefully (partial satisfaction with logged warning) rather than raising, which is the right UX for a generation engine where constraints are best-effort soft constraints.

---

## 6. Files Reviewed

- `src/decoy_engine/walks/cross_file.py`
- `src/decoy_engine/walks/hazards.py`
- `src/decoy_engine/walks/inference.py`
- `src/decoy_engine/walks/graph.py`
- `src/decoy_engine/walks/diff.py`
- `src/decoy_engine/walks/types.py`
- `src/decoy_engine/generators/columns.py`
- `src/decoy_engine/generators/derivation.py`
- `src/decoy_engine/plan/_compile.py` (targeted — F5, F9 only)
- `src/decoy_engine/config/_pipeline.py`
- `src/decoy_engine/config/_tables.py`

## 7. Deferred / Out of Scope

- `src/decoy_engine/instrumentation/` — not read; flagged for next session.
- `src/decoy_engine/internal/` — not read; flagged for next session.
- `src/decoy_engine/license/` — not read.
- `src/decoy_engine/schema/` — single-file (`inspector.py`, 481 bytes); trivial, deferred.
- `src/decoy_engine/sdk.py` — public API surface; deferred to a session that can also cover `__init__.py`.
- `src/decoy_engine/validation/post/` — not read; flagged for next session.
