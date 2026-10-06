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

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import (
    ARROW_PYTHON,
    OPERATORS,
    PANDAS_ORACLE,
    RUST_COMPANION,
    RUST_POOL_SELECT,
)
from decoy_engine.execution.native._chunked_group_key_gate import sibling_resident_sources
from decoy_engine.execution.native._faker_positional_admission import chunked_positional_column
from decoy_engine.execution.native._plan import compile_native_plan
from decoy_engine.execution.native._requirements import CHUNKED_ROUTE_VETOED_STRATEGIES

__all__ = [
    "ARROW_PYTHON",
    "PANDAS_ORACLE",
    "RUST_COMPANION",
    "RUST_POOL_SELECT",
    "ColumnPlan",
    "aggregate_chunked_route_evidence",
    "chunk_route_evidence",
    "executed_backend",
    "merge_executed_backend",
    "plan_column_backends",
]

# The backend vocabulary lives in the leaf operator registry (this module imports the planner,
# so the registry cannot import it back); re-exported above so existing imports keep working.

# Derived from the operator registry; edit the registry. hash, categorical, bucket_perturb,
# date_shift and group_key run on a compiled kernel and report the companion as their backend.
_COMPANION_STRATEGIES = frozenset(
    s.strategy for s in OPERATORS.values() if s.planned_backend == RUST_COMPANION
)


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
    spec = OPERATORS.get(strategy)
    return PANDAS_ORACLE if spec is None else spec.planned_backend


def plan_column_backends(
    config: dict[str, Any],
    profile: Any,
    *,
    table: str,
    engine_version: str,
    registry: Any,
    first_schema: pa.Schema | None = None,
) -> tuple[ColumnPlan, ...]:
    """The configured columns of `table` with their planned backends.

    Unconfigured columns kept under the passthrough policy are not listed: they
    veto the table to the oracle, which enforces the policy.

    `first_schema` is the first chunk's schema when the caller has it: a group_key node's
    sibling type is then the real one and not the profile's coarse label, so the planned
    backend agrees with the route decision.
    """
    plan = compile_native_plan(
        config,
        profile,
        engine_version=engine_version,
        registry=registry,
        resident_sources=sibling_resident_sources(config, table, first_schema),
    )
    out: list[ColumnPlan] = []
    for node in plan.nodes:
        if node.table != table:
            continue
        positional = node.kind == "scalar" and chunked_positional_column(
            config, table, node.strategy, node.columns[0]
        )
        backend = RUST_COMPANION if positional else _planned_backend(node)
        out.extend(ColumnPlan(col, node.strategy, backend) for col in node.columns)
    return tuple(out)


def executed_backend(planned_backend: str, *, native_admitted: bool, kernel_idle: bool) -> str:
    """The one executed-backend rule, shared by the chunked route and the unified slice.

    A column the native route did not take ran on pandas. An admitted column that made no
    compiled call ran Arrow work in Python. Anything else ran its planned backend."""
    if not native_admitted:
        return PANDAS_ORACLE
    return ARROW_PYTHON if kernel_idle else planned_backend


def merge_executed_backend(acc: dict[str, Any], col: dict[str, Any]) -> None:
    """Fold one chunk's column evidence into the running per-column total.

    A column reports its planned backend if any chunk really ran it there, so a degenerate
    first or last chunk (which reports `arrow_python`) never hides a later or earlier
    compiled run. A column whose every chunk was idle keeps `arrow_python`. Both the resident
    and the streamed aggregation call this one rule so they cannot disagree on chunk order."""
    if col["executed_backend"] == col["planned_backend"]:
        acc["executed_backend"] = col["executed_backend"]


def chunk_route_evidence(
    *,
    table: str,
    native_admitted: bool,
    reroute_reason: str | None,
    columns: Iterable[ColumnPlan],
    elapsed_ms: Mapping[str, float],
    pandas_read_passthrough: Iterable[str] = (),
    kernel_idle_columns: Iterable[str] = (),
) -> dict[str, Any]:
    """One chunk's evidence: every column called once, with its own elapsed time.

    `pandas_read_passthrough` lists the passthrough columns that still go through
    pandas because a `when:` predicate or a sibling-reading strategy reads them (every
    passthrough column under a custom adapter); all other passthrough columns are carried
    and never converted.

    `kernel_idle_columns` lists the admitted columns that ran no compiled kernel on this chunk
    (a bucket_perturb or date_shift chunk with no parseable row, an empty group_key chunk). They ran Arrow passthrough work in Python,
    so they report `arrow_python` rather than the planned companion backend."""
    idle = frozenset(kernel_idle_columns)
    return {
        "table": table,
        "native_admitted": native_admitted,
        "reroute_reason": reroute_reason,
        "pandas_read_passthrough": sorted(pandas_read_passthrough),
        "columns": [
            {
                "column": c.column,
                "strategy": c.strategy,
                "planned_backend": c.planned_backend,
                "executed_backend": executed_backend(
                    c.planned_backend, native_admitted=native_admitted, kernel_idle=c.column in idle
                ),
                "calls": 1,
                "elapsed_ms": float(elapsed_ms.get(c.column, 0.0)),
            }
            for c in columns
        ],
    }


def aggregate_chunked_route_evidence(results: Iterable[Any]) -> dict[str, Any]:
    """Sum calls and elapsed time per column across the chunk results' evidence."""
    head: dict[str, Any] | None = None
    read_head: list[str] | None = None
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
        listed = evidence.get("pandas_read_passthrough")
        if head is evidence:
            read_head = listed
        elif listed != read_head:
            raise ExecutionError(
                code="chunked_route_evidence_inconsistent",
                message=(
                    "aggregate_chunked_route_evidence takes the results of one call; chunk "
                    f"evidence disagrees on pandas_read_passthrough ({read_head!r} then "
                    f"{listed!r})."
                ),
            )
        for col in evidence["columns"]:
            acc = sums.get(col["column"])
            if acc is None:
                sums[col["column"]] = dict(col)
            else:
                acc["calls"] += col["calls"]
                acc["elapsed_ms"] += col["elapsed_ms"]
                merge_executed_backend(acc, col)
    if head is None:
        return {
            "table": None,
            "native_admitted": False,
            "reroute_reason": None,
            "pandas_read_passthrough": [],
            "columns": [],
        }
    return {
        "table": head["table"],
        "native_admitted": head["native_admitted"],
        "reroute_reason": head["reroute_reason"],
        "pandas_read_passthrough": [] if read_head is None else list(read_head),
        "columns": list(sums.values()),
    }
