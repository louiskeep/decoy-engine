# Polars + DuckDB hybrid engine — detailed implementation plan

> **⚠ Pre-customer.** Decoy is in development. No production users are affected.
> **Status:** planning — companion to [2026-05-10-polars-duckdb-hybrid-engine.md](2026-05-10-polars-duckdb-hybrid-engine.md). Read the architecture plan first for the *why*; this doc is the *how*.
> **Branch:** `feature/polars-duckdb-hybrid-plan`
> **Audience:** the implementer of this work — code-level walk-through with file paths, function signatures, and test patterns.

---

## Table of contents

1. [Phase 1 — Arrow runner cache + eviction + STORM benchmark](#phase-1)
2. [Phase 2 — Op-type registry + connector SDK contract](#phase-2)
3. [Phase 3 — Polars relational ops](#phase-3)
4. [Phase 4 — DuckDB source/sink + `engine: hybrid` opt-in](#phase-4)
5. [Phase 5 — Preview path + error translation](#phase-5)
6. [Phase 6 — Parity test suite + dogfood review](#phase-6)
7. [Phase 7 — Docs + Polars cheat sheet](#phase-7)
8. [Phase 8 — Default flip + cleanup](#phase-8)

---

<a name="phase-1"></a>

## Phase 1 — Arrow runner cache + eviction + STORM benchmark (~2 weeks)

### Files touched

- `src/decoy_engine/graph/runner.py` — cache becomes `dict[str, pyarrow.Table]`; add eager eviction
- `src/decoy_engine/graph/conversion.py` — **NEW**: Arrow ↔ pandas conversion shims
- `tests/unit/test_graph_runner_cache.py` — **NEW**: cache eviction tests
- `tests/benchmark/test_storm_arrow_boundary.py` — **NEW**: STORM benchmark

### Conversion shim contract

```python
# src/decoy_engine/graph/conversion.py
from typing import Literal
import pyarrow as pa
import pandas as pd

EngineType = Literal["pandas", "polars", "duckdb", "arrow"]

def arrow_to_engine(table: pa.Table, engine: EngineType) -> object:
    """Convert pyarrow.Table to the op's native engine type."""
    if engine == "arrow":
        return table
    if engine == "pandas":
        return table.to_pandas(types_mapper=pd.ArrowDtype)
    if engine == "polars":
        import polars as pl
        return pl.from_arrow(table)
    if engine == "duckdb":
        # DuckDB consumes pyarrow.Table directly via from_arrow()
        return table
    raise ValueError(f"unknown engine: {engine}")

def engine_to_arrow(result: object, engine: EngineType) -> pa.Table:
    """Convert op output back to pyarrow.Table for caching."""
    if engine == "arrow":
        return result  # type: ignore
    if engine == "pandas":
        return pa.Table.from_pandas(result, preserve_index=False)  # type: ignore
    if engine == "polars":
        return result.to_arrow()  # type: ignore
    if engine == "duckdb":
        return result  # type: ignore  # DuckDB ops return pyarrow.Table by convention
    raise ValueError(f"unknown engine: {engine}")
```

**Note on `types_mapper=pd.ArrowDtype`.** pandas 2.x supports Arrow-backed dtypes; using them avoids re-allocation on `to_pandas()`. Verify the engine's pandas pin supports this; fall back to default `to_pandas()` (slower, copies) if not.

### Runner cache eviction algorithm

```python
# src/decoy_engine/graph/runner.py
class GraphRunner:
    def __init__(self, graph: GraphConfig, ctx: ExecutionContext):
        self.graph = graph
        self.ctx = ctx
        self.cache: dict[str, pa.Table] = {}
        # NEW: per-node remaining-consumer count
        self._remaining_consumers: dict[str, int] = self._count_consumers()

    def _count_consumers(self) -> dict[str, int]:
        """For each node, count how many downstream edges consume its output."""
        counts: dict[str, int] = {n.id: 0 for n in self.graph.nodes}
        for edge in self.graph.edges:
            counts[edge.from_id] += 1
        return counts

    def _consume(self, node_id: str) -> pa.Table:
        """Read upstream output and decrement consumer count.
        Evict cache entry when count hits zero."""
        table = self.cache[node_id]
        self._remaining_consumers[node_id] -= 1
        if self._remaining_consumers[node_id] == 0:
            # Last consumer; release the cached output for GC.
            del self.cache[node_id]
        return table

    def run(self) -> dict[str, pa.Table]:
        for node in self._topological_order():
            inputs = [self._consume(e.from_id) for e in self._edges_into(node.id)]
            engine = self._native_engine_for(node.kind)
            engine_inputs = [arrow_to_engine(inp, engine) for inp in inputs]
            result = self._dispatch(node, engine_inputs)
            self.cache[node.id] = engine_to_arrow(result, engine)
        return self.cache  # remaining entries are sink outputs
```

**Eviction edge cases to test:**

| Graph shape | Expected behavior |
|---|---|
| Linear `A→B→C→D` | A evicted after B reads; B after C; C after D |
| Branching `A→B; A→C; B→D; C→D` | A evicted after both B and C read; B and C evicted after D reads both |
| Sink with no downstream | Output stays in `self.cache` at run-end |
| Preview mode (truncated execution) | Preview node's output stays cached; downstream consumer count not decremented |
| Self-loop (forbidden by validator) | Defensive `assert node.id != edge.to_id` in `_count_consumers` |

### STORM benchmark harness

```python
# tests/benchmark/test_storm_arrow_boundary.py
"""Phase 1 deliverable: measure Arrow → pandas conversion cost in STORM scans.
If overhead > 10%, declare STORM as NATIVE_ENGINE = 'arrow' in Phase 2."""

import time
import pyarrow as pa
import pandas as pd
from decoy_engine.storm import scan
from decoy_engine.graph.conversion import arrow_to_engine

def make_fixture(rows: int = 1_000_000) -> pa.Table:
    """1M-row HIPAA-shaped fixture: SSN-like / phone / email / DOB / 25 numerics."""
    # ... fixture generation
    return table

def bench_pandas_baseline(table: pa.Table) -> float:
    df = table.to_pandas()
    start = time.perf_counter()
    profile = scan(df, source_label="bench")
    return time.perf_counter() - start

def bench_arrow_boundary(table: pa.Table) -> float:
    start = time.perf_counter()
    df = arrow_to_engine(table, "pandas")  # the conversion we're measuring
    profile = scan(df, source_label="bench")
    return time.perf_counter() - start

def test_storm_arrow_overhead_under_10_percent():
    table = make_fixture(1_000_000)
    baseline = bench_pandas_baseline(table)
    with_boundary = bench_arrow_boundary(table)
    overhead_pct = (with_boundary - baseline) / baseline * 100
    print(f"baseline={baseline:.2f}s; with_arrow_boundary={with_boundary:.2f}s; overhead={overhead_pct:.1f}%")
    # NOT a hard assert — this is a benchmark, not a regression test.
    # The number drives the Phase 2 STORM engine declaration decision.
    if overhead_pct > 10:
        print(f"WARN: overhead exceeds 10%; declare STORM NATIVE_ENGINE='arrow' in Phase 2")
```

**Decision tree.** Run the benchmark on a clean dev box. If overhead < 10%: STORM stays `NATIVE_ENGINE = "pandas"`. If ≥ 10%: STORM declares `NATIVE_ENGINE = "arrow"`, and STORM internals switch to `pyarrow.compute` for column stats where possible (next subsection).

### STORM Arrow consumption (only if benchmark says yes)

If STORM moves to `NATIVE_ENGINE = "arrow"`, the per-column stats loop changes:

```python
# src/decoy_engine/storm/profiler.py — Arrow path (Phase 1+ if benchmark requires)
import pyarrow.compute as pc

def _column_stats_arrow(col: pa.ChunkedArray, name: str) -> FieldStats:
    # Cheap stats native to pyarrow.compute — no pandas conversion
    null_count = pc.count_substring(col, pa.scalar(None)).as_py() if pa.types.is_string(col.type) else col.null_count
    distinct = pc.count_distinct(col).as_py()
    # ... etc

    # For ops pyarrow.compute doesn't support (regex on a value sample,
    # person-name detection heuristic), convert THIS column to pandas:
    if needs_regex_match:
        col_pandas = col.to_pandas()
        # ... existing pandas regex match logic
```

**Net effect.** Per-column conversion is cheaper than per-table conversion because most stats (count, distinct, null, min/max for numerics, length stats for strings) skip the conversion entirely.

### Phase 1 verification

- [ ] `pytest tests/unit/test_graph_runner_cache.py` — eviction works for all 5 graph shapes above
- [ ] `pytest tests/benchmark/test_storm_arrow_boundary.py` — prints overhead %; benchmark recorded in plan as decision input
- [ ] Existing 397 engine tests still pass (no regression in current ops)
- [ ] `decoy-engine` package imports `pyarrow` at top level (it should already; pandas depends on it transitively, but make explicit)

---

<a name="phase-2"></a>

## Phase 2 — Op-type registry + connector SDK contract (~1 week)

### Files touched

- `src/decoy_engine/graph/ops/_base.py` — add `NATIVE_ENGINE` to op contract
- `src/decoy_engine/graph/ops/{filter,sort,dedupe,derive,...}.py` — declare current engine
- `src/decoy_engine/graph/registry.py` — runtime lookup `kind → native_engine`
- `decoy-engine/CONNECTOR_SDK_CONTRACT.md` — **NEW**: lock the contract here, before any Polars/DuckDB ops ship

### Op contract change

```python
# src/decoy_engine/graph/ops/_base.py
# Each op module declares:
KIND: str = "filter"
NATIVE_ENGINE: Literal["pandas", "polars", "duckdb", "arrow"] = "pandas"  # NEW
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND: str = "stream"
```

All existing ops declare `NATIVE_ENGINE = "pandas"` in this phase — no behavioral change. Phases 3 and 4 flip individual ops to `"polars"` / `"duckdb"` as they get ported.

### Runtime registry

```python
# src/decoy_engine/graph/registry.py
def native_engine_for(kind: str) -> EngineType:
    """Look up the native engine declaration for an op kind."""
    module = _OP_MODULES[kind]
    return getattr(module, "NATIVE_ENGINE", "pandas")  # default for ops that haven't declared
```

Default-to-pandas keeps backward compatibility during the migration window.

### Connector SDK contract — the actual document

```markdown
# Connector SDK contract

> Locked 2026-05-10 (Phase 2 of polars-duckdb-hybrid-engine plan).

## Return shape

Connectors return `pyarrow.Table` from read methods and accept `pyarrow.Table`
in write methods. Period.

The runner converts to/from pandas/polars at op boundaries. Connectors stay
engine-agnostic.

## Why

- Arrow is the substrate. Returning Arrow keeps connectors composable across
  the three engines (DuckDB, Polars, Pandas) without per-engine adapters.
- Connectors that return pandas force the runner to pay an Arrow → pandas →
  Arrow round-trip when the consumer is Polars or DuckDB. Returning Arrow
  is zero-copy when the consumer is DuckDB or Polars.
- External connectors (Item 24 SDK) need ONE contract, not three.

## Capability flags (optional)

Connectors declare what they support. The runner uses these to optimize:

  class ConnectorCapabilities:
      streaming: bool         # read in batches, not all-at-once
      pushdown_filter: bool   # accept a filter expression to push down
      pushdown_select: bool   # accept a column projection to push down
      preview: bool           # support a row_limit hint cheaply

A connector that doesn't declare a capability is assumed to lack it.

## Backward compatibility

Existing pandas-returning connectors are wrapped at the runner boundary:
the wrapper calls `.to_pandas()` or `pa.Table.from_pandas()` as needed.
This is a transitional shim; new connectors must follow the Arrow contract.
```

### Phase 2 verification

- [ ] `from decoy_engine.graph.registry import native_engine_for` returns `"pandas"` for every op (no behavior change)
- [ ] `CONNECTOR_SDK_CONTRACT.md` committed at engine repo root
- [ ] Existing connectors continue to work via the backward-compat wrapper (no code change required for current consumers)

---

<a name="phase-3"></a>

## Phase 3 — Polars relational ops (~3 weeks)

### Ops ported

`filter`, `sort`, `dedupe`, `derive`, `drop_column`, `select_column`, `limit`. Plus `join` and `group_by` if Item 19 lands during this window.

### Pattern: porting `filter`

**Before (pandas):**

```python
# src/decoy_engine/graph/ops/filter_op.py — pandas
import pandas as pd
from decoy_engine.graph.ops._base import OpError

KIND = "filter"
NATIVE_ENGINE = "pandas"  # → flips to "polars" in this phase

def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    expr = config["where"]
    try:
        return df.query(expr)
    except Exception as e:
        raise OpError(f"filter: {e}")
```

**After (Polars):**

```python
# src/decoy_engine/graph/ops/filter_op.py — Polars
import polars as pl
from decoy_engine.graph.ops._base import OpError

KIND = "filter"
NATIVE_ENGINE = "polars"

def apply(inputs, config, ctx) -> pl.DataFrame:
    df = inputs[0]  # already pl.DataFrame thanks to runner conversion
    expr_str = config["where"]
    try:
        # Translate pandas-query syntax to Polars expression
        polars_expr = _translate_query_to_polars(expr_str, df.columns)
        return df.filter(polars_expr)
    except Exception as e:
        raise OpError(f"filter: {e}")
```

**Translation layer.** `_translate_query_to_polars()` parses the pandas-query string and emits a Polars expression. Most cases are mechanical (`col == 'x'` → `pl.col('col') == pl.lit('x')`). Edge cases (nested boolean ops, `.str.contains()`) need explicit support. Document all unsupported syntax in a per-op `TRANSLATION_NOTES.md`.

### Pattern: porting `sort`

```python
# src/decoy_engine/graph/ops/sort.py — Polars
KIND = "sort"
NATIVE_ENGINE = "polars"

def apply(inputs, config, ctx) -> pl.DataFrame:
    df = inputs[0]
    by = config["by"]                   # list[str] or str
    descending = config.get("desc", False)  # bool or list[bool]
    return df.sort(by=by, descending=descending, maintain_order=True)
    # maintain_order=True approximates pandas' kind="mergesort" (stable)
```

**Sort tie-breaking note.** pandas `kind="mergesort"` is stable. Polars `maintain_order=True` is stable. Both promise the same ordering for tied keys → same output. **Verify in parity test** with a deliberately-tied fixture.

### Forbidden footgun: `.map_elements()`

```python
# DO NOT DO THIS:
return df.with_columns(
    pl.col("name").map_elements(custom_python_func, return_dtype=pl.Utf8)
)
```

`.map_elements()` accepts a Python callback and runs it per row. Looks like Polars but isn't — falls out of the lazy planner, slows to pandas-speed, breaks parallelization. Two approved alternatives:

1. **Rewrite as a Polars expression.** Most "I need a callback" cases can be expressed as `pl.col(...).str.replace(...)` / `pl.when(...).then(...).otherwise(...)`.
2. **Move the op back to pandas.** If the logic genuinely needs Python (e.g. calls a custom Faker function), declare `NATIVE_ENGINE = "pandas"` and accept the boundary. The runner pays the conversion; we keep the value.

**Code review checkpoint.** Every PR in Phase 3 that introduces a Polars op must justify any `.map_elements()` call in the PR description, or get reviewer pushback.

### Parity test pattern

```python
# tests/parity/test_filter_parity.py
"""Filter op: pandas reference vs Polars implementation."""

import pandas as pd
import polars as pl
import pytest
from decoy_engine.graph.ops import filter_op  # Polars version
from decoy_engine.graph.ops._legacy import filter_op as filter_op_legacy  # frozen pandas

@pytest.fixture
def fixture():
    """A 100K-row mixed-type fixture covering NaN / null / string / numeric / date."""
    return _make_fixture()

@pytest.mark.parametrize("expr", [
    "age > 30",
    "name == 'Alice'",
    "(age > 30) & (status == 'active')",
    # ... known semantic differences documented in SEMANTIC_DIFFERENCES.md
])
def test_filter_parity(fixture, expr):
    pandas_result = filter_op_legacy.apply([fixture], {"where": expr}, ctx=None)
    polars_result = filter_op.apply([pl.from_pandas(fixture)], {"where": expr}, ctx=None)
    pd.testing.assert_frame_equal(
        pandas_result.reset_index(drop=True),
        polars_result.to_pandas().reset_index(drop=True),
        check_dtype=False,  # Arrow-backed pandas has slightly different dtype names
    )
```

**Frozen legacy reference.** Phase 3 keeps a frozen copy of each pandas op at `_legacy/` for parity testing. Deleted in Phase 8.

### Documented semantic differences

```markdown
# tests/parity/SEMANTIC_DIFFERENCES.md
| Behavior | Pandas | Polars | Decision |
|---|---|---|---|
| Empty string in CSV column | Loaded as `""` | Loaded as `null` | Document; recommend explicit null check |
| NaN in numeric column | `float64` `NaN` | `null` (no NaN concept) | Normalize at conversion boundary |
| Sort tie-break for floats | Mergesort, stable | maintain_order=True, stable | Equivalent |
| `.query()` operator precedence | Python `and`/`or` allowed | Polars expressions only | Translation layer rejects ambiguous cases |
| Column name with `.` | Allowed in `.query()` | Requires bracket syntax | Translator rewrites |
| Datetime parsing | `pd.to_datetime` | `pl.col(...).str.to_datetime` | Different format strings |
```

### Phase 3 verification

- [ ] `pytest tests/parity/` — every ported op has a parity test that passes
- [ ] `SEMANTIC_DIFFERENCES.md` lists every documented divergence
- [ ] No `.map_elements()` calls in committed code (grep gate in CI)
- [ ] Performance: each ported op is ≥ 1× as fast as pandas (Polars is usually 5–30× faster; if it's not at least equal, something's wrong with the port)

---

<a name="phase-4"></a>

## Phase 4 — DuckDB source/sink + `engine: hybrid` opt-in (~2.5 weeks)

### Ops ported

`source.file` (CSV / parquet / JSON), `target.file` (CSV / parquet), `source.db` (Postgres / MySQL / SQLite via DuckDB extensions), `target.db` (same).

### Pattern: porting `source.db`

**Before (pandas):**

```python
# src/decoy_engine/graph/ops/source_db.py — pandas
import pandas as pd
from sqlalchemy import create_engine

KIND = "source.db"
NATIVE_ENGINE = "pandas"

def apply(inputs, config, ctx) -> pd.DataFrame:
    conn_url = config["connector_url"]
    table = config["table"]
    engine = create_engine(conn_url)
    return pd.read_sql(f"SELECT * FROM {table}", engine)  # full-table load
```

**After (DuckDB):**

```python
# src/decoy_engine/graph/ops/source_db.py — DuckDB
import duckdb
import pyarrow as pa

KIND = "source.db"
NATIVE_ENGINE = "duckdb"  # returns pyarrow.Table

def apply(inputs, config, ctx) -> pa.Table:
    conn_url = config["connector_url"]
    table = config["table"]
    schema = config.get("schema", "public")

    db = duckdb.connect(":memory:")
    db.execute("INSTALL postgres_scanner; LOAD postgres_scanner")  # idempotent
    db.execute(f"ATTACH '{conn_url}' AS src (TYPE POSTGRES, READ_ONLY)")
    # Pushdown filters/select happen automatically via DuckDB's planner
    return db.execute(f"SELECT * FROM src.{schema}.{table}").arrow()
```

**Pushdown.** DuckDB's planner automatically pushes filters and column selects down through `postgres_scanner`. If a downstream `filter` op is present, the runner can pass its predicate to the source op as a `pushdown_filter` capability — but that's a Phase 4.5 optimization. Phase 4 ships full-table reads; pushdown is a separate enhancement plan.

### `engine: hybrid` opt-in flag

```yaml
# Pipeline YAML
mode: graph
engine: hybrid  # NEW — opt-in to the new path; default is "pandas" until Phase 8

nodes:
  - id: src1
    kind: source.db
    config:
      connector_url: postgresql://...
      table: customers
  # ...
```

**Runner dispatch.**

```python
# src/decoy_engine/graph/runner.py
def __init__(self, graph: GraphConfig, ctx: ExecutionContext):
    # ...
    self.engine_mode = graph.engine or "pandas"  # default backward-compat

def _native_engine_for(self, kind: str) -> EngineType:
    if self.engine_mode == "pandas":
        return "pandas"  # force everything to pandas, ignoring NATIVE_ENGINE
    # hybrid mode: respect each op's declaration
    return native_engine_for(kind)
```

**`engine: pandas`** keeps the old behavior — every op runs on pandas, the new Polars/DuckDB ops fall back to their pandas predecessors via `_legacy/`. **`engine: hybrid`** runs each op on its declared engine.

This is the dogfood mechanism. Customers (when we have them) opt in per-pipeline. Until then, internal testing flips between modes to validate the hybrid path on real-shaped data.

### DuckDB extension cold-start on Windows

```python
# src/decoy_engine/graph/ops/source_db.py — defensive extension load
def _ensure_postgres_scanner(db: duckdb.DuckDBPyConnection) -> None:
    """Load postgres_scanner; on first use it fetches from duckdb.org.
    Pre-bundle in the install package OR catch the network error gracefully."""
    try:
        db.execute("LOAD postgres_scanner")
    except duckdb.Error as e:
        if "extension" in str(e).lower():
            # Likely the extension hasn't been installed yet
            db.execute("INSTALL postgres_scanner")
            db.execute("LOAD postgres_scanner")
        else:
            raise
```

**Test on a clean Windows VM** before Phase 4 ships. The extension fetch is a "professional tool" failure mode if a customer's first run hits a 30-second download with no progress indicator. Mitigation: pre-bundle extensions in the install package, OR surface a clear "fetching DuckDB postgres extension…" status message on first run.

### Phase 4 verification

- [ ] Each ported source/sink op has a parity test against the pandas legacy
- [ ] `engine: hybrid` flag works on a sample pipeline; pandas default still works
- [ ] Clean Windows VM: `decoy run` on a `source.db` pipeline succeeds on first call
- [ ] Memory profile: `source.db` on a 50M-row Postgres table runs without OOM (validates streaming claim)

---

<a name="phase-5"></a>

## Phase 5 — Preview path + error translation (~1 week)

### Files touched

- `src/decoy_engine/graph/runner.py` — preview-path serialization always returns pandas-compatible JSON
- `forge-platform/api/jobs/runner.py` — same; convert at the boundary
- `src/decoy_engine/graph/errors.py` — **NEW**: error-translation layer

### Preview boundary

The preview UI consumes `{ columns: [...], rows: [[...], [...]] }`. The runner must produce that shape regardless of internal engine:

```python
# src/decoy_engine/graph/runner.py
def preview_output(self, node_id: str, row_limit: int) -> dict:
    table = self.cache[node_id]  # pyarrow.Table
    truncated = table.slice(0, row_limit)
    df = truncated.to_pandas()  # always pandas at the boundary
    return {
        "columns": list(df.columns),
        "rows": df.values.tolist(),  # JSON-serializable
        "row_count": len(df),
    }
```

**Identical UI output regardless of engine.** This is the gate for "no UX regression."

### Error translation layer

```python
# src/decoy_engine/graph/errors.py
"""Map engine-specific exceptions to user-friendly messages.

Polars and DuckDB raise different exception shapes than pandas.
Without translation, the canvas user sees 'SchemaError: column foo not found'
which is fine for engineers but useless for users."""

import polars as pl
import duckdb
import pandas as pd
from decoy_engine.graph.ops._base import OpError

def translate(exc: Exception, op_kind: str, node_id: str) -> OpError:
    """Map an engine exception to a user-friendly OpError."""
    if isinstance(exc, pl.exceptions.ColumnNotFoundError):
        return OpError(
            f"Node '{node_id}' ({op_kind}): column '{_extract_column(exc)}' "
            f"not found in input. Did upstream drop it?"
        )
    if isinstance(exc, pl.exceptions.SchemaError):
        return OpError(
            f"Node '{node_id}' ({op_kind}): schema mismatch — {exc}. "
            f"Check column types match between inputs."
        )
    if isinstance(exc, duckdb.CatalogException):
        return OpError(
            f"Node '{node_id}' ({op_kind}): table or column missing — {exc}. "
            f"Check the connector schema."
        )
    if isinstance(exc, duckdb.IOException):
        return OpError(
            f"Node '{node_id}' ({op_kind}): I/O error reading source — {exc}."
        )
    # Default: wrap with node context
    return OpError(f"Node '{node_id}' ({op_kind}): {exc}")
```

The runner wraps every op call:

```python
def _dispatch(self, node, inputs):
    try:
        return self._invoke_op(node, inputs)
    except Exception as e:
        raise translate(e, node.kind, node.id) from e
```

### Phase 5 verification

- [ ] Preview UI on `localhost:5173` shows identical output for the 4 representative pipelines (mask-only / transform-only / generate / hybrid)
- [ ] Deliberately-broken pipeline produces a user-friendly error message in the canvas, not a Python traceback

---

<a name="phase-6"></a>

## Phase 6 — Parity test suite + dogfood review (~2 weeks)

### Test matrix

For each Polars-ported op (Phase 3) + DuckDB-ported op (Phase 4):

- **Standard fixture** — 100K-row mixed-type table covering NaN / null / string / numeric / date
- **Edge fixtures** — empty input, single-row input, all-null column, mixed-type column (object dtype in pandas), unicode strings, datetime tz-aware vs tz-naive
- **Operation-specific fixtures** — sort with ties, dedupe with all-duplicates / no-duplicates, filter with always-true / always-false predicate

For each fixture × each op, assert `pandas_result == polars_result` (with `check_dtype=False` because of Arrow-backed dtype name differences).

**Documented exceptions** stay in `SEMANTIC_DIFFERENCES.md`. Tests assert exceptions explicitly:

```python
def test_csv_empty_string_loaded_as_null_in_polars():
    """SEMANTIC_DIFFERENCE: CSV empty strings load as null in Polars,
    empty string in pandas. Documented; downstream code must handle."""
    csv_data = "name,age\nAlice,30\n,25\n"
    pandas_df = pd.read_csv(io.StringIO(csv_data))
    polars_df = pl.read_csv(io.StringIO(csv_data))
    assert pandas_df.iloc[1]["name"] == ""
    assert polars_df[1]["name"] is None
```

### Dogfood review

By Phase 6 start, the `engine: hybrid` flag has been live since Phase 4. Internal pipelines are running on it. Phase 6 reviews:

- Every internal pipeline's run history under `engine: hybrid`
- Any error / regression / unexpected behavior captured in logs
- Performance comparison (latency, memory) vs `engine: pandas` runs
- Customer dogfood pipelines (when customers exist; pre-customer this is internal-only)

**Default-flip gate.** No Phase 8 until:

1. Parity test suite is green.
2. Every documented semantic difference has a justification + downstream-handling note.
3. Dogfood review surfaced no unfixed regressions.
4. Performance is ≥ pandas baseline on representative pipelines.

### Phase 6 verification

- [ ] `pytest tests/parity/` green (full op × fixture matrix)
- [ ] `SEMANTIC_DIFFERENCES.md` complete and reviewed
- [ ] Dogfood review document committed at `plans/2026-XX-XX-hybrid-engine-dogfood-review.md`
- [ ] Sign-off from implementer + reviewer: "default flip is safe"

---

<a name="phase-7"></a>

## Phase 7 — Docs + Polars cheat sheet (~1 week)

### Deliverables

1. Update `forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md` with the hybrid engine reference. Replace "server-side pandas; no dialect compiler" with "server-side hybrid (DuckDB / Polars / Pandas) over Arrow; no dialect compiler."
2. Update `forge-engine/SHARED_ENGINE_ARCHITECTURE.md` with the three-engine boundary diagram (ASCII art is fine).
3. Promote `decoy-engine/CONNECTOR_SDK_CONTRACT.md` (Phase 2) to `decoy-engine/CONNECTOR_SDK_GUIDE.md` with full external-author tutorial: auth flow, capability flags, contract test suite, example connector.
4. Write `decoy-engine/POLARS_FOR_PANDAS_USERS.md` cheat sheet (next subsection).

### `POLARS_FOR_PANDAS_USERS.md` outline

```markdown
# Polars for pandas users — quick reference

## Top 20 idioms side-by-side

| pandas | Polars |
|---|---|
| `df[df.col == 'x']` | `df.filter(pl.col('col') == 'x')` |
| `df[['a', 'b']]` | `df.select(['a', 'b'])` |
| `df.groupby('col').agg({'val': 'sum'})` | `df.group_by('col').agg(pl.col('val').sum())` |
| `df.merge(other, on='key')` | `df.join(other, on='key')` |
| `df.sort_values('col', ascending=False)` | `df.sort('col', descending=True)` |
| `df.drop_duplicates(['a', 'b'])` | `df.unique(subset=['a', 'b'])` |
| `df.assign(c=df.a + df.b)` | `df.with_columns((pl.col('a') + pl.col('b')).alias('c'))` |
| `df.rename(columns={'a': 'A'})` | `df.rename({'a': 'A'})` |
| `df.dropna(subset=['col'])` | `df.drop_nulls(subset=['col'])` |
| `df.fillna({'col': 0})` | `df.with_columns(pl.col('col').fill_null(0))` |
| `df.col.str.contains('x')` | `pl.col('col').str.contains('x')` |
| `df.col.str.replace('a', 'b')` | `pl.col('col').str.replace('a', 'b')` |
| `pd.to_datetime(df.col)` | `pl.col('col').str.to_datetime()` |
| `df.shape` | `df.shape` (same!) |
| `df.head(n)` | `df.head(n)` (same!) |
| `df.col.value_counts()` | `df.group_by('col').len()` |
| `df.col.isin(['a', 'b'])` | `pl.col('col').is_in(['a', 'b'])` |
| `df.col.fillna(method='ffill')` | `df.with_columns(pl.col('col').forward_fill())` |
| `df.col.cumsum()` | `pl.col('col').cum_sum()` |
| `df.pivot(...)` | `df.pivot(...)` (similar API) |

## The `.map_elements()` footgun

When you find yourself reaching for it, stop. Three real examples:

### Example 1: rewrite as Polars expression (preferred)

```python
# Footgun:
df.with_columns(
    pl.col("name").map_elements(lambda x: x.upper().strip(), return_dtype=pl.Utf8)
)

# Correct:
df.with_columns(pl.col("name").str.to_uppercase().str.strip_chars())
```

### Example 2: when there's no Polars equivalent → declare pandas

```python
# Footgun:
df.with_columns(
    pl.col("encrypted").map_elements(decrypt_via_kms, return_dtype=pl.Utf8)
)

# Correct: this op is per-row Python with a non-Polars dependency.
# In its module declare NATIVE_ENGINE = "pandas" and the runner converts at boundary.
```

### Example 3: `.map_elements()` is OK but rare

```python
# Acceptable when the callback is fast pure-Python and there's no
# Polars expression that fits AND the column is small. Document why.
df.with_columns(
    pl.col("status_code").map_elements(_translate_legacy_code, return_dtype=pl.Utf8)
    # NOTE: legacy translation table is dict lookup, ~50 rows; .map_elements is fine.
)
```

## Lazy vs eager mental model

### Eager (default)

```python
df = pl.read_csv('file.csv')   # loads entire file into RAM
df = df.filter(...)            # filter applied immediately
```

### Lazy (preferred for big data)

```python
df = (
    pl.scan_csv('file.csv')        # builds a query plan; doesn't load
    .filter(pl.col('country') == 'US')  # plan adds filter
    .select(['id', 'name'])             # plan adds projection
    .collect()                          # NOW the file is read,
                                        # filter and projection pushed down
                                        # to the read step
)
```

**Use `scan_*` instead of `read_*`** for any input that's "big enough to care." The lazy planner is most of the win.

## Cheat sheet for our workload

- All relational ops (filter / sort / dedupe / derive / join / group_by) are Polars.
- All mask transforms (hash / faker / fpe / etc.) are pandas.
- All sources / sinks (CSV / parquet / Postgres / MySQL) are DuckDB.
- The runner converts between these via `arrow_to_engine()` / `engine_to_arrow()`.
- See `CONNECTOR_SDK_GUIDE.md` if writing a new connector.
- See parity tests in `tests/parity/` for known semantic differences.
```

### Phase 7 verification

- [ ] `etl-direction-and-connector-sdk.md` updated and merged
- [ ] `SHARED_ENGINE_ARCHITECTURE.md` has the three-engine diagram
- [ ] `CONNECTOR_SDK_GUIDE.md` is the authoritative external-author tutorial
- [ ] `POLARS_FOR_PANDAS_USERS.md` exists and covers the top-20 idioms + footgun + lazy/eager

---

<a name="phase-8"></a>

## Phase 8 — Default flip + cleanup (~1 week)

### The flip

```python
# src/decoy_engine/graph/config.py
DEFAULT_ENGINE: EngineType = "hybrid"  # was "pandas"
```

Pipelines without an explicit `engine:` key get hybrid. `engine: pandas` still works as opt-out for one release cycle.

### Cleanup tasks

- Delete `_legacy/` op modules (frozen pandas references kept through Phase 6 for parity tests)
- Delete the `chunked_iterator()` dead code in `csv_connector.py:111-126` (obsoleted by DuckDB)
- Remove the backward-compat connector wrapper that auto-converted pandas-returning connectors (all connectors now return Arrow per the contract)
- Update README.md / CLAUDE.md to reflect the new architecture

### Sales / marketing follow-ups (non-blocking)

- Update the pandas-ETL-ceiling memo with new tier numbers
- Release notes: "Memory ceiling messages disappear from the UI for source/transform ops"
- Train sales on the new conversation: "tens of millions, not millions; hundreds of millions on a good box"

### Phase 8 verification

- [ ] `pytest tests/` green
- [ ] `pytest tests/parity/` (still green; the suite stays through one release cycle as a guard)
- [ ] Pipelines without `engine:` key default to hybrid
- [ ] `engine: pandas` still works (one-cycle fallback)
- [ ] Calibration benchmark (50M-row mask-only pipeline) runs cleanly on a 32 GB box
- [ ] All cleanup tasks complete; codebase has no `_legacy/` directories

---

## Phase-by-phase deliverables checklist

| Phase | Files new / touched | Tests | Docs |
|---|---|---|---|
| 1 | runner.py, conversion.py | runner cache eviction, STORM benchmark | — |
| 2 | _base.py, registry.py | engine declaration smoke test | CONNECTOR_SDK_CONTRACT.md |
| 3 | filter/sort/dedupe/derive/etc. ops | parity tests per op | TRANSLATION_NOTES.md |
| 4 | source.db/source.file/target.db/target.file ops, runner.py | parity + memory profile | engine: hybrid flag docs |
| 5 | runner.py preview path, errors.py | preview output identity, error translation | — |
| 6 | tests/parity/ full suite | full op×fixture matrix | dogfood-review plan doc |
| 7 | — | — | ETL plan update, SHARED_ENGINE_ARCHITECTURE update, CONNECTOR_SDK_GUIDE.md, POLARS_FOR_PANDAS_USERS.md |
| 8 | config.py default flip | full test suite, calibration benchmark | release notes, ceiling-memo update |
