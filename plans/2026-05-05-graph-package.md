# Phase 2 — `decoy_engine.graph` package

> **Status:** shipped (uncommitted)
> **Branch:** `feature/graph-package` (proposed)
> **References:** [forge-platform/PIPELINE_GRAPH_GUIDE.md](../../forge-platform/PIPELINE_GRAPH_GUIDE.md)

## Goal

Add the smallest forge-engine surface that lets a node-graph pipeline run end-to-end:

- `decoy_engine.graph.validate(yaml_text) -> None`
- `decoy_engine.graph.run(yaml_text, ctx=None) -> RunResult`
- `decoy_engine.graph.preview(yaml_text, node_id, row_limit=50, ctx=None) -> PreviewResult`

MVP node kinds (per the guide §3): `source.db`, `source.file`, `drop_column`, `select_column`, `filter`, `dedupe`, `mask`, `generate`, `target.file`, `target.db`. Multi-input ops (`join`, `union`) are out of scope for this phase.

This phase **adds**, never modifies. Existing `Masker` / `DataGenerator` / connectors / transforms keep their public APIs untouched. Graph ops *reuse* those internals (transform registry, MaskingProcessor, ColumnGenerator) so we don't duplicate column-strategy logic.

## File layout (additions only)

```
forge-engine/
├── PIPELINE_GRAPH_GUIDE.md             ← mirror of forge-platform/PIPELINE_GRAPH_GUIDE.md
├── src/decoy_engine/
│   ├── graph/                          ← NEW package, sibling to masker/ and generators/
│   │   ├── __init__.py                 ← re-export validate / run / preview / RunResult / PreviewResult
│   │   ├── runner.py                   ← run() + preview() drivers + topo sort
│   │   ├── ops/
│   │   │   ├── __init__.py             ← OPS registry (kind-string → op module)
│   │   │   ├── _base.py                ← shared NodeOp dataclass + helpers
│   │   │   ├── source_db.py
│   │   │   ├── source_file.py
│   │   │   ├── drop_column.py
│   │   │   ├── select_column.py
│   │   │   ├── filter.py               ← uses pandas df.query() per Q2 decision
│   │   │   ├── dedupe.py
│   │   │   ├── mask_op.py              ← reuses transforms/registry + masker/processor
│   │   │   ├── generate_op.py          ← reuses generators/column_generator
│   │   │   ├── target_file.py
│   │   │   └── target_db.py
│   │   └── types.py                    ← TypedDicts: NodeSpec, EdgeSpec, GraphConfig, RunResult, PreviewResult
│   ├── internal/
│   │   └── validator.py                ← extend with GraphConfigValidator (mirrors MaskerConfigValidator pattern)
│   └── __init__.py                     ← add 5 new exports to __all__
└── tests/
    ├── unit/
    │   ├── test_graph_validator.py     ← good + bad configs, regex, cycle detection
    │   ├── test_graph_topo.py          ← topo sort + cycle detection
    │   └── test_graph_ops.py           ← each op in isolation on synthetic DataFrames
    └── integration/
        └── test_graph.py               ← end-to-end small graphs (run + preview)
```

## Public API surface

Append to `src/decoy_engine/__init__.py`:

```python
from decoy_engine.graph import (
    validate as validate_graph,
    run as run_graph,
    preview as preview_graph,
    RunResult,
    PreviewResult,
)

__all__ += [
    'validate_graph', 'run_graph', 'preview_graph',
    'RunResult', 'PreviewResult',
]
```

Naming: `_graph` suffix on the public exports avoids collision with `validate_config` (already exported for the legacy validation helper).

## TypedDicts in `graph/types.py`

```python
class NodeSpec(TypedDict, total=False):
    id: str
    kind: str
    config: dict[str, Any]
    position: dict[str, float]   # {x, y} — UI metadata, runner ignores

class EdgeSpec(TypedDict):
    from_: str   # field name `from_` because `from` is a Python keyword;
                  # serializer maps to `from` in YAML
    to: str

class GraphConfig(TypedDict, total=False):
    mode: Literal['graph']
    schema_version: int
    settings: dict[str, Any]
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]

class NodeRunRecord(TypedDict):
    node_id: str
    kind: str
    status: Literal['ok', 'error']
    row_count: int | None
    elapsed_ms: int
    error: str | None

class RunResult(TypedDict):
    nodes: list[NodeRunRecord]
    success: bool
    elapsed_ms: int

class PreviewResult(TypedDict):
    node_id: str
    columns: list[str]
    rows: list[list[Any]]
    applied_chain: list[str]
    row_count: int
    elapsed_ms: int
    error: str | None
```

## Validator (`internal/validator.py`)

Extend the existing module with `GraphConfigValidator` mirroring `MaskerConfigValidator`'s style. Validation steps in order:

1. **Top-level shape:** `mode == 'graph'`, `nodes` is a list, `edges` is a list. Optional `schema_version` defaults to 1; reject if > 1 (forward-compat hatch).
2. **Per-node:** `id` matches regex `^[a-zA-Z][a-zA-Z0-9_]{0,63}$`, `kind` is in registered ops set, `config` is a dict.
3. **Uniqueness:** node ids are unique (per Q3 decision — failing on collision protects both app and user).
4. **Edges reference real nodes:** every `from`/`to` matches a node id.
5. **Per-kind config:** delegate to `ops[kind].validate_config(node['config'])` — each op is responsible for its own config keys.
6. **Cardinality:** sources have 0 incoming edges, targets have 0 outgoing, transforms have ≥1 incoming.
7. **Acyclic:** Kahn's algorithm; raise `ValidationError("graph has a cycle", "edges")` if topo sort doesn't consume every node.

Errors raise the existing `ValidationError` from `internal/validator.py` so platform / CLI handle them with the same code path as legacy validation.

## Ops registry

`graph/ops/__init__.py`:

```python
from decoy_engine.graph.ops import (
    source_db, source_file,
    drop_column, select_column, filter as filter_op, dedupe,
    mask_op, generate_op,
    target_file, target_db,
)

OPS: dict[str, ModuleType] = {
    'source.db':     source_db,
    'source.file':   source_file,
    'drop_column':   drop_column,
    'select_column': select_column,
    'filter':        filter_op,
    'dedupe':        dedupe,
    'mask':          mask_op,
    'generate':      generate_op,
    'target.file':   target_file,
    'target.db':     target_db,
}
```

Each op module exposes a small flat protocol (no classes — keeps the surface area minimal):

```python
KIND: str = 'drop_column'
INPUT_ARITY: tuple[int, int] = (1, 1)   # (min, max) — (0, 0) for sources, (1, None) for transforms
OUTPUT_KIND: Literal['stream', 'sink'] = 'stream'   # 'sink' for targets

def validate_config(config: dict) -> None: ...
def apply(inputs: list[pd.DataFrame], config: dict, ctx: ExecutionContext | None) -> pd.DataFrame: ...
```

For sources, `inputs` is empty; `apply` returns a DataFrame freshly read.
For targets, `apply` performs the side effect (write file/db) and returns an empty DataFrame.
For transforms, `apply` reads `inputs[0]` and returns a derived DataFrame.

### Per-op notes

- **`source.db`** — accepts EITHER `dsn` (direct connection string, used by CLI) OR `connector_id` + an optional `ctx.resolve_connector(id) -> dsn` callback (used by platform). Reads via `pd.read_sql(...)` with a configurable `LIMIT` for preview mode.
- **`source.file`** — reads CSV/parquet/fixed-width via `connectors.factory.create_io_handler` for parity with existing engine plumbing.
- **`drop_column` / `select_column`** — `pd.DataFrame.drop(columns=[...], errors='raise')` / `df[columns]`.
- **`filter`** — `df.query(predicate, engine='python')` per Q2. Pandas raises `UndefinedVariableError` on bad column refs; we catch and re-raise as `OpError` with node context.
- **`dedupe`** — `df.drop_duplicates(subset=on, keep=keep)`.
- **`mask`** — instantiates `decoy_engine.masker.processor.MaskingProcessor` against an in-memory dataframe with the node's `columns` config. Reuses the transform registry directly.
- **`generate`** — instantiates `decoy_engine.generators.column_generator.ColumnGenerator` for each column. Standalone (no input) when used as a source-shaped op; pairs with an upstream df when chained (acts like a "synthesize replacements for these columns" step).
- **`target.file`** — `df.to_csv(path)` / `df.to_parquet(path)` based on extension or `format` config.
- **`target.db`** — same DSN/connector_id pattern as `source.db`. `df.to_sql(table, engine, if_exists=write_mode)`.

## Runner

`graph/runner.py`:

```python
def run(yaml_text: str, ctx: ExecutionContext | None = None) -> RunResult:
    config = yaml.safe_load(yaml_text)
    GraphConfigValidator().validate(config)
    order = topo_order(config['nodes'], config['edges'])
    cache: dict[str, pd.DataFrame] = {}
    records: list[NodeRunRecord] = []
    for nid in order:
        node = next(n for n in config['nodes'] if n['id'] == nid)
        op = OPS[node['kind']]
        inputs = [cache[e['from']] for e in config['edges'] if e['to'] == nid]
        t0 = time.monotonic()
        try:
            df = op.apply(inputs, node.get('config', {}), ctx)
            cache[nid] = df
            records.append({...status='ok', row_count=len(df), elapsed_ms=...})
        except Exception as exc:
            records.append({...status='error', error=str(exc), ...})
            return {'nodes': records, 'success': False, ...}
    return {'nodes': records, 'success': True, ...}
```

`preview()` is `run()` with two changes:

1. Sources accept a `__row_limit` hint via a private kwarg pattern — pass through ctx or a temporary `config['__preview_row_limit']` key. Pandas reads / SQL queries cap to that limit.
2. The walk stops once `node_id` is computed; downstream nodes don't run.
3. Targets are valid `node_id`s — preview returns the dataframe that would have been written, but does NOT execute the side effect.

## Test strategy

Mirror the existing `tests/unit` + `tests/integration` split. Reuse `mock_logger` / `tmp_path` fixtures.

**Unit tests** (one file per concern):
- `test_graph_validator.py` — good config passes, bad configs raise specific `ValidationError` per rule (id regex, missing edge endpoint, cycle, unknown kind, etc.).
- `test_graph_topo.py` — linear chain → expected order; diamond → both topo orders accepted; cycle → raises.
- `test_graph_ops.py` — each op:
  - validate_config: good / bad
  - apply: golden DataFrame in/out
  - mask + generate ops verify they delegate to existing engine code (don't reimplement)

**Integration tests:**
- `test_graph.py`:
  - 3-node chain `source.file → drop_column → target.file` end-to-end, assert output CSV matches expected
  - Preview at each node returns expected df
  - Filter with bad predicate fails with informative error
  - Run with empty pipeline (no nodes) raises validation error
  - Smoke test for each op kind in a tiny pipeline

## Steps in execution order

| # | Step | Est. | Notes |
|---|---|---|---|
| 1 | Mirror `PIPELINE_GRAPH_GUIDE.md` to forge-engine root | 5 min | Status header + adjust internal links |
| 2 | `graph/types.py` + `graph/__init__.py` skeleton | 1h | Just types and stub functions raising NotImplementedError |
| 3 | `internal/validator.py` — `GraphConfigValidator` | 1d | Tests-first, every rule has a test |
| 4 | `graph/topo.py` (Kahn's) | 2h | With cycle detection; trivial unit tests |
| 5 | `graph/ops/_base.py` + ops registry skeleton | 2h | Empty op modules registered, all `apply` raise NotImplementedError |
| 6 | Implement `source.file` + `target.file` + `drop_column` + `select_column` | 1d | Simplest, no external deps |
| 7 | Implement `filter` + `dedupe` | 0.5d | df.query / drop_duplicates |
| 8 | Implement `source.db` + `target.db` | 1d | DSN handling, connector_id pattern, integration test against sqlite |
| 9 | Implement `mask` + `generate` | 1d | Wrap existing MaskingProcessor and ColumnGenerator; verify by integration test |
| 10 | `graph/runner.py` — `run()` + `preview()` | 1d | Once ops exist, runner is mechanical |
| 11 | Public `__init__.py` exports + final integration tests | 0.5d | Ship-ready smoke tests |
| 12 | Update CLAUDE.md to reference the mirrored guide | 5 min | |

**Total: ~7 working days** for a focused engineer, assuming familiarity with the engine codebase. First end-to-end pipeline demo runs at the end of step 10.

## Resolved coordination details

- **Validator strictness on unknown kinds (Q1).** Reject. Validator raises `ValidationError(f"unknown kind: {kind}", f"nodes[{i}].kind")`.
- **Schema versioning (Q4).** `schema_version: 1` on the YAML root, optional, defaults to 1. Validator rejects > 1 with a forward-compat error message.
- **Cache invalidation (Q5).** Engine-internal. Hash composite = `sha256(node.kind + canonical_json(node.config) + sha256s_of_upstream_node_hashes)`. Cache key includes `row_limit`. Lifetime: process-scoped (no cross-request cache in this phase — platform calls `preview()` fresh each time).

## Verification

- `pytest forge-engine/tests/unit/test_graph_*.py` — all pass.
- `pytest forge-engine/tests/integration/test_graph.py` — all pass.
- `pytest forge-engine/tests/` — full suite still green (no existing tests broken).
- A 3-node graph with `source.file → drop_column → target.file` runs end-to-end with `decoy_engine.graph.run` and produces a CSV missing the dropped columns.
- `decoy_engine.graph.preview(yaml, 'drop1', row_limit=10)` returns 10 rows with the dropped columns missing.

## Out of scope (Phase 2)

- Multi-input ops: `join`, `union` — Phase 5.
- Streaming / chunked execution — current ops materialize full DataFrames in memory. Phase 5+.
- Cache persistence across requests / processes — fresh per call for now.
- Custom user-defined ops — fixed registry only.
- CLI changes — covered in Phase 6 (forge repo).
- Platform changes — covered in Phase 3 (forge-platform).
