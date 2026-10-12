"""Phase D / D1c: shared fixtures and runners for the sentinel-preserve suites.

Every route is driven through its real public entry, not a unit stub:

- oracle: `PandasExecutionAdapter.run_single`
- native chunked and oracle chunked: `run_mask_chunked` (native vs a forced-oracle twin)
- native full-frame: the shadow coordinator against the legacy pandas pipeline
- out-of-core: `run_fk_out_of_core` through the D1a chain harness

Expected strings are hardcoded literals or are derived from an unconfigured run of the SAME
operator on the same rows, never from the output under test.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

ISO = "%Y-%m-%d"
DMY = "%d/%m/%Y"
NS = "ns_p"
JOB_SEED = b"\xb0\x0c\xfe\x00\x12\x34\x56\x78"

# In pandas' range, and one the shift/bucket arithmetic would move.
IN_RANGE = "2099-01-01"
# Outside pandas' ns range: only the Python strptime retry can match it.
OUT_OF_RANGE = "9999-12-31"
NORMAL = ["2024-03-05", "2023-11-14", "2022-07-30", "2021-01-09", "2024-02-29"]

OPERATORS = {
    "bucket_perturb": {"bucket": "month"},
    "date_shift": {"min_days": -30, "max_days": 30},
}


def operator_config(strategy: str, **extra: Any) -> dict[str, Any]:
    """The operator's base config with an explicit ISO format, plus `extra`."""
    return {**OPERATORS[strategy], "date_format": ISO, **extra}


def seed_for(strategy: str, config: dict[str, Any], *, namespace: str | None = NS) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=tuple(config.items()),
        coherent_with=(),
    )


_GRAPH = RelationshipGraph(edges=(), ordering=())
_NAMESPACES = NamespaceRegistry(bindings=())


class OracleRun(SimpleNamespace):
    values: list[Any]
    type: pa.DataType
    metrics: dict[str, Any]
    errors: tuple[Any, ...]


def run_oracle(strategy: str, config: dict[str, Any], column: pa.Array) -> OracleRun:
    """One column through the pandas oracle route, with its metrics and row errors."""
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=JOB_SEED,
            per_table=(
                ("t", TableSeed(per_column=(("d", seed_for(strategy, config)),), per_group=())),
            ),
        )
    )
    result = PandasExecutionAdapter().run_single(
        plan,
        pa.table({"d": column}),
        registry=get_default_registry(),
        relationship_graph=_GRAPH,
        namespace_registry=_NAMESPACES,
    )
    out = result.output.column("d")
    return OracleRun(
        values=out.to_pylist(),
        type=out.type,
        metrics=dict(result.quality_metrics),
        errors=tuple(result.row_errors),
    )


def table_digest(table: pa.Table) -> str:
    """Whole-output digest: schema (with metadata) and every value byte."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


def preserved_total(metrics: dict[str, Any], column: str = "d") -> int:
    """The aggregate preserved-row count the oracle metrics report for `column`."""
    return sum(
        e["preserved_rows"] for e in metrics.get("date_preserve", []) if e["column"] == column
    )


def strings(*values: str | None) -> pa.Array:
    return pa.array(list(values), pa.string())
