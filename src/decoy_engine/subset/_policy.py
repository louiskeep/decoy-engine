"""SS4: edge-direction resolution, the fan-out budget gate, and the dry-run estimate.

GATE-1 #3: upward parent-completeness is ON by default so a subset job never
silently dangles a child FK. `resolve_edge_directions` enforces that
disabling upward traversal on any edge is an explicit, acknowledged choice
(`allow_dangling=True`); `make_budget_check` builds the closure-injected
checker that hard-fails BEFORE any materialization the first round a cap is
exceeded (never truncate-and-flag: truncation re-introduces orphans).
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from math import ceil
from typing import Any

import polars as pl

from decoy_engine.subset._closure import BudgetCheck, ClosureResult
from decoy_engine.subset._errors import SubsetBudgetExceededError, SubsetConfigError
from decoy_engine.subset._types import (
    EdgeDirection,
    EdgeStats,
    FanOutBudget,
    FanOutPolicy,
    FkPreflightReport,
    SubsetEdge,
    SubsetPlan,
    TableEstimate,
)


def resolve_edge_directions(
    edges: tuple[SubsetEdge, ...], policy: FanOutPolicy
) -> dict[str, EdgeDirection]:
    """Resolve every edge's traversal direction, defaulting to "both"."""
    valid_ids = {e.edge_id for e in edges}
    directions: dict[str, EdgeDirection] = {edge_id: "both" for edge_id in valid_ids}
    for edge_id, direction in policy.edge_directions:
        if edge_id not in valid_ids:
            raise SubsetConfigError(
                code="subset_unknown_edge",
                message=f"edge_directions names unknown edge_id {edge_id!r}; valid edge_ids: "
                f"{sorted(valid_ids)!r}",
            )
        directions[edge_id] = direction

    if not policy.allow_dangling:
        dangling = sorted(edge_id for edge_id, d in directions.items() if d in ("downward", "none"))
        if dangling:
            raise SubsetConfigError(
                code="subset_dangling_not_acknowledged",
                message="disabling upward traversal on an edge can orphan child FKs "
                f"(edges: {dangling!r}); set allow_dangling=True on the FanOutPolicy to "
                "acknowledge this explicitly, or leave the edge direction as 'both'/'upward'",
            )
    return directions


def make_budget_check(budget: FanOutBudget, total_seed_rows: int) -> BudgetCheck:
    """Build the closure-injected budget checker.

    `max_table_seed_multiple`'s per-table cap uses the GLOBAL seed row total
    across every seeded table (implementation guide section 1.2 / open risk
    6): GATE-1 #3 says "per-table multiple-of-seed-size cap" without defining
    seed size for an unseeded table, so this locks it to the global total so
    every table -- seeded or not -- has a well-defined cap.
    """

    def check(survivors: Mapping[str, set[int]], edge_stats: tuple[EdgeStats, ...]) -> None:
        if budget.max_total_rows is not None:
            total = sum(len(s) for s in survivors.values())
            if total > budget.max_total_rows:
                top = max(
                    edge_stats,
                    key=lambda s: (s.rows_added_downward + s.rows_added_upward, s.edge_id),
                )
                raise SubsetBudgetExceededError(
                    scope="total",
                    table=None,
                    cap=budget.max_total_rows,
                    actual=total,
                    seed_total=total_seed_rows,
                    edge_id=top.edge_id,
                )
        if budget.max_table_seed_multiple is not None:
            cap = ceil(budget.max_table_seed_multiple * total_seed_rows)
            for table in sorted(survivors):
                actual = len(survivors[table])
                if actual > cap:
                    # Offending edge: the top contributor of rows INTO this table.
                    top = max(
                        edge_stats,
                        key=partial(_rows_added_into_sort_key, table=table),
                    )
                    raise SubsetBudgetExceededError(
                        scope="table",
                        table=table,
                        cap=cap,
                        actual=actual,
                        seed_total=total_seed_rows,
                        edge_id=top.edge_id,
                    )

    return check


def build_estimate(
    *,
    engine_version: str,
    seed_specs_public: tuple[Mapping[str, Any], ...],
    key_frames: Mapping[str, pl.DataFrame],
    seed_counts: Mapping[str, int],
    seed_null_excluded: Mapping[str, int],
    closure: ClosureResult,
    budget: FanOutBudget,
    preflight: FkPreflightReport,
) -> SubsetPlan:
    """Assemble the dry-run `SubsetPlan`.

    Computed from the SAME survivor sets `_materialize.py` will write, which
    is what makes acceptance test 5 (dry-run == materialized) hold with exact
    equality, not tolerance: a `SubsetPlan` only exists for a passing budget
    (a failed budget check raises before this function is reached).
    """
    tables: list[TableEstimate] = []
    warnings: list[str] = []
    for table in sorted(key_frames):
        surviving = len(closure.survivors.get(table, frozenset()))
        tables.append(
            TableEstimate(
                table=table,
                input_rows=key_frames[table].height,
                seed_rows=seed_counts.get(table, 0),
                surviving_rows=surviving,
                seed_null_excluded=seed_null_excluded.get(table, 0),
            )
        )
        if surviving == 0:
            warnings.append(
                f"table {table} has no surviving rows; it is disconnected from every seed "
                "under the configured traversal directions"
            )

    return SubsetPlan(
        engine_version=engine_version,
        seed_specs_public=seed_specs_public,
        tables=tuple(tables),
        edges=closure.edge_stats,
        closure_rounds=closure.rounds,
        budget=budget,
        budget_outcome="pass",
        total_surviving_rows=sum(t.surviving_rows for t in tables),
        preflight=preflight,
        warnings=tuple(warnings),
    )


def _rows_added_into(stat: EdgeStats, table: str) -> int:
    """Rows `stat`'s edge contributed INTO `table` (either direction)."""
    parent_table, child_table = _edge_endpoints(stat.edge_id)
    contribution = 0
    if child_table == table:
        contribution += stat.rows_added_downward
    if parent_table == table:
        contribution += stat.rows_added_upward
    return contribution


def _rows_added_into_sort_key(stat: EdgeStats, *, table: str) -> tuple[int, str]:
    """`max(..., key=...)` sort key: highest contribution into `table`, tie-break by edge_id."""
    return (_rows_added_into(stat, table), stat.edge_id)


def _edge_endpoints(edge_id: str) -> tuple[str, str]:
    """Recover (parent_table, child_table) from an edge_id string.

    edge_id format (from `_edges.py`): "{ptable}.{pcols} -> {ctable}.{ccols}".
    """
    left, right = edge_id.split(" -> ")
    parent_table = left.split(".", 1)[0]
    child_table = right.split(".", 1)[0]
    return parent_table, child_table
