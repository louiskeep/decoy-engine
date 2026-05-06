# Pipeline Graph — `decoy-engine`

> **Status:** target — engine implementation of the cross-repo graph pipeline contract.
> **Last reviewed:** 2026-05-05
> **Canonical source:** [forge-platform/PIPELINE_GRAPH_GUIDE.md](../forge-platform/PIPELINE_GRAPH_GUIDE.md). When the contract changes, update the platform copy first; mirror here in the same PR.

This is the engine-side mirror. For the cross-repo split, YAML schema, node-kinds catalog, per-node preview API, and phased rollout plan, **read the platform copy first** (link above) — it owns the contract.

Engine-specific notes are below; everything else is duplicated from the platform doc verbatim.

---

## Engine-side responsibilities

The engine owns four public symbols in `decoy_engine.graph`:

| Symbol | Purpose |
|---|---|
| `validate_graph(yaml_text) -> None` | Raises `PipelineValidationError` on bad config |
| `run_graph(yaml_text, ctx=None) -> RunResult` | Executes the DAG end-to-end |
| `preview_graph(yaml_text, node_id, row_limit=50, ctx=None) -> PreviewResult` | Best-effort node-output sample |
| `RunResult`, `PreviewResult` | TypedDicts; see `graph/types.py` |

Internal layout under `src/decoy_engine/graph/`:

```
graph/
├── __init__.py          ← re-exports validate / run / preview / RunResult / PreviewResult
├── runner.py            ← run() + preview() drivers + topo sort
├── topo.py              ← Kahn's algorithm + cycle detection
├── types.py             ← TypedDicts: NodeSpec, EdgeSpec, GraphConfig, RunResult, PreviewResult
└── ops/
    ├── __init__.py      ← OPS registry (kind-string → op module)
    ├── _base.py         ← shared op protocol helpers
    ├── source_db.py     ← DSN OR connector_id + ctx.resolve_connector callback
    ├── source_file.py
    ├── drop_column.py
    ├── select_column.py
    ├── filter.py        ← pandas df.query(predicate, engine='python')
    ├── dedupe.py
    ├── mask_op.py       ← reuses transforms/registry + masker/processor
    ├── generate_op.py   ← reuses generators/column_generator
    ├── target_file.py
    └── target_db.py
```

`GraphConfigValidator` extends `internal/validator.py` and mirrors the existing `MaskerConfigValidator` style.

## Op protocol

Each op module exposes a small flat protocol (no classes — keeps the surface area minimal):

```python
KIND: str = 'drop_column'
INPUT_ARITY: tuple[int, int] = (1, 1)   # (min, max). (0, 0) for sources, (1, None) for transforms
OUTPUT_KIND: Literal['stream', 'sink'] = 'stream'   # 'sink' for targets

def validate_config(config: dict) -> None: ...
def apply(inputs: list[pd.DataFrame], config: dict, ctx: ExecutionContext | None) -> pd.DataFrame: ...
```

For sources, `inputs` is empty; `apply` returns a freshly-read DataFrame. For targets, `apply` performs the side effect (write file/db) and returns an empty DataFrame. For transforms, `apply` reads `inputs[0]` and returns a derived DataFrame.

## Connector resolution

`source.db` and `target.db` accept either:

- `dsn`: a direct DSN string (used by the CLI)
- `connector_id`: an integer reference resolved via `ctx.resolve_connector(id) -> str` callback (used by the platform)

When both are missing, validation fails. When both are present, `dsn` wins (platform users won't normally pass both; if they do, the inline DSN is treated as authoritative for that run).

## Validation rules

`GraphConfigValidator` enforces:

1. `mode == 'graph'`
2. `nodes` is a non-empty list, `edges` is a list (possibly empty for single-node graphs — though sources alone aren't useful)
3. Optional `schema_version` defaults to 1; > 1 is rejected
4. Each node id matches regex `^[a-zA-Z][a-zA-Z0-9_]{0,63}$`
5. Node ids are unique within the pipeline
6. Each edge `from`/`to` matches a node id
7. Each kind is in the registered ops set (unknown kinds rejected — Q1)
8. Per-kind config is delegated to `ops[kind].validate_config(node['config'])`
9. Cardinality: sources have 0 incoming, targets have 0 outgoing, transforms have ≥1 incoming
10. Acyclic (Kahn's; `ValidationError("graph has a cycle", "edges")` on failure)

Errors raise the existing `ValidationError` from `internal/validator.py`, wrapped to `PipelineValidationError` by the public `validate_graph` entry point.

## Cache (preview)

Per-call internal cache only. Composite key:

```python
sha256(node.kind + canonical_json(node.config) + sha256s_of_upstream_node_hashes + str(row_limit))
```

No cross-call persistence in this phase. Platform calls `preview_graph` fresh each time.

## Testing layout

Mirrors `tests/unit` + `tests/integration`:

- `tests/unit/test_graph_validator.py`
- `tests/unit/test_graph_topo.py`
- `tests/unit/test_graph_ops.py` (one fixture per op kind)
- `tests/integration/test_graph.py` (end-to-end small graphs)

## Out of scope (Phase 2)

- Multi-input ops: `join`, `union` — Phase 5
- Streaming / chunked execution — current ops materialize full DataFrames
- Cross-request preview cache — fresh per call
- User-defined ops — fixed registry only
- CLI changes — Phase 6 (`forge` repo)
- Platform changes — Phase 3 (`forge-platform` repo)

For everything else, defer to the platform doc.
