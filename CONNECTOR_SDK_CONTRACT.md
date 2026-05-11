# Connector SDK contract

> **Status:** locked 2026-05-10 (Phase 2 of the polars-duckdb hybrid plan).
> **Last reviewed:** 2026-05-10.
> **Companion doc:** [`CONNECTOR_SDK_GUIDE.md`](CONNECTOR_SDK_GUIDE.md) covers the Sprint G file-shaped SDK (`FileSource` / `FileSink` for S3 / GCS / SFTP / community connectors). This file is the legacy table-shaped contract (load/save returning `pyarrow.Table`).

The contract here is what every read / write integration in `decoy_engine.connectors/` and (later) the customer SDK from Roadmap Item 24 must satisfy. The runner depends on it — drift = silent breakage at the op boundary.

## Return shape

**Connectors return `pyarrow.Table` from read methods and accept `pyarrow.Table` in write methods.**

That's the whole contract. The runner converts to / from pandas / polars / duckdb at the op boundary; connectors stay engine-agnostic.

```python
class MyConnector(BaseConnector):
    def load(self, config: dict[str, Any]) -> pa.Table:
        ...

    def save(self, table: pa.Table, config: dict[str, Any]) -> None:
        ...
```

## Why Arrow specifically

- **Arrow is the substrate.** Returning Arrow keeps connectors composable across the three engines (DuckDB, Polars, Pandas) without per-engine adapters.
- **Zero-copy when the consumer is DuckDB or Polars.** Connectors that return pandas force the runner to pay an Arrow → pandas → Arrow round-trip when the consuming op is Polars or DuckDB.
- **One contract, not three.** External connector authors (Item 24's customer SDK) need ONE return shape, not "depends on which op consumes you."

## Capability flags (optional)

Connectors declare what they support. The runner uses these to optimize predicate / projection pushdown when an upstream op (filter, select_column) sits next to the connector.

```python
@dataclass
class ConnectorCapabilities:
    streaming: bool         # read in batches, not all-at-once
    pushdown_filter: bool   # accept a filter expression to push down
    pushdown_select: bool   # accept a column projection to push down
    preview: bool           # support a row_limit hint cheaply
```

A connector that doesn't declare a capability is assumed to lack it. The runner falls back to the unfused path (load full table → apply op).

## Backward compatibility (transitional, removed at Phase 8)

Existing pandas-returning connectors keep working through the migration window. The runner wraps them: it calls `pa.Table.from_pandas(connector.load(...))` on the way in and `connector.save(table.to_pandas(), ...)` on the way out.

The wrapper goes away in Phase 8. New connectors written after Phase 2 must follow the Arrow contract directly — there's no path where adding a pandas-returning connector is the right call.

## What the contract does NOT cover

- **Schema discovery** lives in the connector, not the contract. `decoy_engine.schema.SchemaInspector` is the orchestration point; connectors implement the per-source side however makes sense.
- **Auth / credential resolution** is the platform's job (`ctx.resolve_connector(connector_id)` → DSN). Connectors take a DSN, never resolve one.
- **Telemetry** is logged through `ctx.logger`. Connectors should log "loaded X rows from Y" but not roll their own log infrastructure.

## Verification

The connector test suite (`tests/unit/test_connectors.py` plus the new `tests/unit/test_connector_contract.py` from this phase) asserts:

- Every registered connector's `load()` returns a `pyarrow.Table`.
- Every registered connector's `save()` accepts a `pyarrow.Table` (and a `pandas.DataFrame` for the backward-compat window).
- Capability flags, when declared, are honored: a connector with `pushdown_filter = True` accepts and applies a filter expression; one without it ignores the flag and the runner pays the post-load filter cost.
