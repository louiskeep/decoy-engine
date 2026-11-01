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

from decoy_engine import reasons
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
from decoy_engine.execution.native._route_decline_codes import RouteDeclineRecord, decline_payload

__all__ = [
    "ARROW_PYTHON",
    "PANDAS_ORACLE",
    "RUST_COMPANION",
    "RUST_POOL_SELECT",
    "ColumnPlan",
    "aggregate_chunked_route_evidence",
    "check_decline_records_consistent",
    "chunk_route_evidence",
    "copy_decline_records",
    "copy_strategy_branch_calls",
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
    decline_records: Iterable[RouteDeclineRecord] = (),
    strategy_branch_calls: Mapping[str, int] | None = None,
    columns: Iterable[ColumnPlan],
    elapsed_ms: Mapping[str, float],
    pandas_read_passthrough: Iterable[str] = (),
    kernel_idle_columns: Iterable[str] = (),
    preserved_rows: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """One chunk's evidence: every column called once, with its own elapsed time.

    `pandas_read_passthrough` lists the passthrough columns that still go through
    pandas because a `when:` predicate or a sibling-reading strategy reads them (every
    passthrough column under a custom adapter); all other passthrough columns are carried
    and never converted.

    `kernel_idle_columns` lists the admitted columns that ran no compiled kernel on this chunk
    (a bucket_perturb or date_shift chunk with no parseable row, an empty group_key chunk). They ran Arrow passthrough work in Python,
    so they report `arrow_python` rather than the planned companion backend.

    `preserved_rows` maps each column that opted into D1c `preserve_values` to the rows it passed
    through this chunk. Only those columns carry the key, so an unset config adds nothing.

    `decline_records` is the table's structured decline causes (OBS-2b), fixed at preflight so
    every chunk carries the same list; the payload form is `decline_payload`, exactly
    code/table/column/table_wide per record, with no provider, expression or exception text.

    `strategy_branch_calls` is a COPY of the table's cumulative per-strategy branch-activation
    count at the end of this chunk (`NativeRouteEvidence.kernel_calls`), so each chunk's
    snapshot is a superset of the previous one and the folders keep the last, never the sum.
    A branch activation is one (column, chunk) run of that strategy's branch. It is NOT a
    compiled-kernel invocation count: a successful faker pool selection counts, as do idle
    bucket_perturb, date_shift and group_key chunks and the Python-only text_redact and
    text_mask branches, while an idle categorical or faker branch and a branch whose `when:`
    mask left it idle do not. A strategy that never activated is absent, never zero. The
    per-column `calls` field is a different, per-column chunk count and is unchanged."""
    idle = frozenset(kernel_idle_columns)
    kept = preserved_rows or {}
    return {
        "table": table,
        "native_admitted": native_admitted,
        "reroute_reason": reroute_reason,
        "decline_records": decline_payload(tuple(decline_records)),
        "strategy_branch_calls": dict(strategy_branch_calls or {}),
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
                **({"preserved_rows": kept[c.column]} if c.column in kept else {}),
            }
            for c in columns
        ],
    }


def copy_decline_records(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A detached copy of a chunk's decline records (absent on a hand-built payload: none)."""
    return [dict(r) for r in evidence.get("decline_records") or ()]


def copy_strategy_branch_calls(evidence: Mapping[str, Any]) -> dict[str, int]:
    """A detached copy of a chunk's cumulative branch-call snapshot."""
    return dict(evidence.get("strategy_branch_calls") or {})


def check_decline_records_consistent(
    head: Mapping[str, Any], evidence: Mapping[str, Any], *, who: str
) -> None:
    """Decline records are preflight-constant, so every chunk must carry the head's list."""
    if copy_decline_records(evidence) != copy_decline_records(head):
        raise ExecutionError(
            code=reasons.runtime.CHUNKED_ROUTE_EVIDENCE_INCONSISTENT,
            message=f"{who} takes the results of one call; chunk evidence disagrees on decline_records.",
        )


def aggregate_chunked_route_evidence(results: Iterable[Any]) -> dict[str, Any]:
    """Sum calls and elapsed time per column across the chunk results' evidence."""
    head: dict[str, Any] | None = None
    read_head: list[str] | None = None
    branch_calls: dict[str, int] = {}
    sums: dict[str, dict[str, Any]] = {}
    for result in results:
        evidence = result.quality_metrics.get("chunked_route")
        if evidence is None:
            continue
        if head is None:
            head = evidence
        elif evidence["table"] != head["table"]:
            raise ExecutionError(
                code=reasons.runtime.CHUNKED_ROUTE_EVIDENCE_MIXED_TABLES,
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
                code=reasons.runtime.CHUNKED_ROUTE_EVIDENCE_INCONSISTENT,
                message=(
                    "aggregate_chunked_route_evidence takes the results of one call; chunk "
                    f"evidence disagrees on pandas_read_passthrough ({read_head!r} then "
                    f"{listed!r})."
                ),
            )
        if head is not evidence:
            check_decline_records_consistent(head, evidence, who="aggregate_chunked_route_evidence")
        # Cumulative snapshot: the last folded chunk's is the final total (never summed).
        branch_calls = copy_strategy_branch_calls(evidence)
        for col in evidence["columns"]:
            acc = sums.get(col["column"])
            if acc is None:
                sums[col["column"]] = dict(col)
            else:
                acc["calls"] += col["calls"]
                acc["elapsed_ms"] += col["elapsed_ms"]
                if "preserved_rows" in col:
                    acc["preserved_rows"] = acc.get("preserved_rows", 0) + col["preserved_rows"]
                merge_executed_backend(acc, col)
    if head is None:
        return {
            "table": None,
            "native_admitted": False,
            "reroute_reason": None,
            "decline_records": [],
            "strategy_branch_calls": {},
            "pandas_read_passthrough": [],
            "columns": [],
        }
    return {
        "table": head["table"],
        "native_admitted": head["native_admitted"],
        "reroute_reason": head["reroute_reason"],
        "decline_records": copy_decline_records(head),
        "strategy_branch_calls": branch_calls,
        "pandas_read_passthrough": [] if read_head is None else list(read_head),
        "columns": list(sums.values()),
    }
