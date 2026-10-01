"""Per-column route evidence for `run_mask_chunked`.

A column's planned backend is the capability decision for its strategy and
config alone, taken before any whole-table veto (a `when:` predicate, a
non-pandas adapter, a missing companion, a schema mismatch, a mixed table). The
executed backend is what actually ran. Keeping the two apart is what makes a
column planned for Rust that ran on pandas visible.

The payload is plain primitives (dicts, lists, strings, finite floats, ints) so
it survives `json.dumps(..., allow_nan=False)`. `NativeRouteEvidence` stays the
legacy, separately-owned record of the route that ran.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.native._plan import compile_native_plan
from decoy_engine.execution.native._requirements import (
    CHUNKED_ROUTE_VETOED_STRATEGIES,
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)

RUST_COMPANION = "rust_companion"
RUST_POOL_SELECT = "rust_pool_select"
ARROW_PYTHON = "arrow_python"
PANDAS_ORACLE = "pandas_oracle"


@dataclass(frozen=True)
class ColumnPlan:
    column: str
    strategy: str
    planned_backend: str


def _planned_backend(node: Any) -> str:
    if node.kind != "scalar" or node.fallback_policy != "native":
        return PANDAS_ORACLE
    strategy = node.strategy
    if strategy in CHUNKED_ROUTE_VETOED_STRATEGIES:
        return PANDAS_ORACLE
    if strategy in NATIVE_POOL_STRATEGIES:
        return RUST_POOL_SELECT
    if strategy == "hash":
        return RUST_COMPANION
    if strategy in NATIVE_KERNEL_STRATEGIES:
        return ARROW_PYTHON
    return PANDAS_ORACLE


def plan_column_backends(
    config: dict[str, Any], profile: Any, *, table: str, engine_version: str
) -> tuple[ColumnPlan, ...]:
    """The configured columns of `table` with their planned backends.

    Unconfigured columns kept under the passthrough policy are not listed: they
    veto the table to the oracle, which enforces the policy.
    """
    plan = compile_native_plan(config, profile, engine_version=engine_version)
    out: list[ColumnPlan] = []
    for node in plan.nodes:
        if node.table != table:
            continue
        backend = _planned_backend(node)
        out.extend(ColumnPlan(col, node.strategy, backend) for col in node.columns)
    return tuple(out)


def chunk_route_evidence(
    *,
    table: str,
    native_admitted: bool,
    reroute_reason: str | None,
    columns: Iterable[ColumnPlan],
    elapsed_ms: Mapping[str, float],
) -> dict[str, Any]:
    """One chunk's evidence: every column called once, with its own elapsed time."""
    return {
        "table": table,
        "native_admitted": native_admitted,
        "reroute_reason": reroute_reason,
        "columns": [
            {
                "column": c.column,
                "strategy": c.strategy,
                "planned_backend": c.planned_backend,
                "executed_backend": c.planned_backend if native_admitted else PANDAS_ORACLE,
                "calls": 1,
                "elapsed_ms": float(elapsed_ms.get(c.column, 0.0)),
            }
            for c in columns
        ],
    }


def aggregate_chunked_route_evidence(results: Iterable[Any]) -> dict[str, Any]:
    """Sum calls and elapsed time per column across the chunk results' evidence."""
    head: dict[str, Any] | None = None
    sums: dict[str, dict[str, Any]] = {}
    for result in results:
        evidence = result.quality_metrics.get("chunked_route")
        if evidence is None:
            continue
        if head is None:
            head = evidence
        elif evidence["table"] != head["table"]:
            raise ExecutionError(
                code="chunked_route_evidence_mixed_tables",
                message=(
                    "aggregate_chunked_route_evidence takes the results of one table; got "
                    f"{head['table']!r} and {evidence['table']!r}."
                ),
            )
        for col in evidence["columns"]:
            acc = sums.get(col["column"])
            if acc is None:
                sums[col["column"]] = dict(col)
            else:
                acc["calls"] += col["calls"]
                acc["elapsed_ms"] += col["elapsed_ms"]
    if head is None:
        return {"table": None, "native_admitted": False, "reroute_reason": None, "columns": []}
    return {
        "table": head["table"],
        "native_admitted": head["native_admitted"],
        "reroute_reason": head["reroute_reason"],
        "columns": list(sums.values()),
    }
