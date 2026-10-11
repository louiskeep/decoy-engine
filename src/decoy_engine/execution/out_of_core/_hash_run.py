"""Per-run, per-column hash disposition for the out-of-core route.

The route masks `hash` columns in `mask_batch` (payload and parent-key columns
alike). This module owns two things that must not live in the drivers: the
decision of which backend computes each hash column, and the caller-visible
record of that decision (`ExecutionResult.quality_metrics[HASH_DISPOSITION_KEY]`).

A column is decided ONCE per table, before its first batch, and the decision is
immutable for the run: a whole column takes one backend, never half of each.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._operator_registry import PYTHON_SCALAR
from decoy_engine.execution.out_of_core._mask import table_seed
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge

HASH_DISPOSITION_KEY = "out_of_core_hash"
_NO_KERNEL_RAN = "none"
_REASON_NO_ROWS = "no_rows_hashed"
_REASON_ROUTING_PENDING = "native_hash_routing_pending"
REMAP_DEFERRED = "fk_key_hash_remap_native_deferred"


@dataclass
class HashColumnDecision:
    """The backend one hash column takes for the whole run, plus how many rows it hashed."""

    table: str
    column: str
    backend: str
    reason: str | None
    rows: int = 0

    def entry(self) -> dict[str, Any]:
        ran = self.rows > 0
        reason = self.reason or (None if ran else _REASON_NO_ROWS)
        entry: dict[str, Any] = {
            "table": self.table,
            "column": self.column,
            "path": "mask_batch",
            "executed_backend": self.backend if ran else _NO_KERNEL_RAN,
        }
        if reason is not None:
            entry["reason"] = reason
        return entry


class TableHashPlan:
    """The immutable per-column decisions for one table."""

    def __init__(self, decisions: Mapping[str, HashColumnDecision]) -> None:
        self._decisions = dict(decisions)

    @property
    def decisions(self) -> Mapping[str, HashColumnDecision]:
        return self._decisions

    def note_batch(self, column: str, batch: pa.RecordBatch) -> None:
        """Record that `batch` is about to be hashed under this column's decision."""
        decision = self._decisions.get(column)
        if decision is not None and batch.schema.get_field_index(column) >= 0:
            decision.rows += batch.num_rows


class OocHashRun:
    """Run-scoped hash dispositions, collected across every table of one run."""

    def __init__(self) -> None:
        self._plans: list[TableHashPlan] = []
        self._remap: list[dict[str, Any]] = []

    def prepare_table(
        self,
        plan: Plan,
        table_name: str,
        source_schema: pa.Schema,
        skip_columns: frozenset[str],
        incoming_edges: Iterable[RelationshipEdge],
        *,
        site: str,
    ) -> TableHashPlan:
        decisions: dict[str, HashColumnDecision] = {}
        seed = table_seed(plan, table_name)
        for column, column_seed in seed.per_column if seed is not None else ():
            if column_seed.strategy != "hash" or column in skip_columns:
                continue
            if source_schema.get_field_index(column) < 0:
                continue
            decisions[column] = HashColumnDecision(
                table_name, column, PYTHON_SCALAR, _REASON_ROUTING_PENDING
            )
        table_plan = TableHashPlan(decisions)
        self._plans.append(table_plan)
        self._remap.extend(_remap_entries(plan, table_name, incoming_edges, site))
        return table_plan

    def metrics(self) -> dict[str, Any]:
        """The `quality_metrics` fragment: empty when the run hashed nothing."""
        entries = [d.entry() for p in self._plans for d in p.decisions.values()]
        entries.extend(self._remap)
        return {HASH_DISPOSITION_KEY: entries} if entries else {}


def _remap_entries(
    plan: Plan, table_name: str, incoming_edges: Iterable[RelationshipEdge], site: str
) -> list[dict[str, Any]]:
    """REMAP mints orphan keys with the Python hash at the join; recorded as deferred."""
    entries: list[dict[str, Any]] = []
    for edge in incoming_edges:
        if edge.orphan_policy is not OrphanPolicy.REMAP:
            continue
        parent_seed = table_seed(plan, edge.parent_table)
        parent_strategy = dict(parent_seed.per_column if parent_seed is not None else ())
        for parent_col, child_col in zip(edge.parent_columns, edge.child_columns, strict=True):
            seed = parent_strategy.get(parent_col)
            if seed is None or seed.strategy != "hash":
                continue
            entries.append(
                {
                    "table": table_name,
                    "column": child_col,
                    "path": "fk_remap",
                    "executed_backend": PYTHON_SCALAR,
                    "reason": f"{REMAP_DEFERRED}:{site}",
                }
            )
    return entries


__all__ = [
    "HASH_DISPOSITION_KEY",
    "REMAP_DEFERRED",
    "HashColumnDecision",
    "OocHashRun",
    "TableHashPlan",
]
