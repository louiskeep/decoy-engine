"""Per-run, per-column hash disposition for the out-of-core route.

The route masks `hash` columns in `mask_batch` (payload and parent-key columns
alike). This module owns two things that must not live in the drivers: the
decision of which backend computes each hash column, and the caller-visible
record of that decision (`ExecutionResult.quality_metrics[HASH_DISPOSITION_KEY]`).

A column is decided ONCE per table, before its first batch, from the companion
load result and the column's up-front INPUT Arrow type (the hash output is always
a string, so the output type says nothing about the input). The decision is
immutable for the run: a whole column takes one backend, never half of each. A
native column validates every batch against the pinned type and aborts the run on a
mismatch or a missing column instead of switching path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.out_of_core._hash_native import (
    REASON_UNAVAILABLE,
    BoundHashKernel,
    decline_reason,
)
from decoy_engine.execution.out_of_core._mask import table_seed
from decoy_engine.relationships._graph import OrphanPolicy

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.relationships._graph import RelationshipEdge

HASH_DISPOSITION_KEY = "out_of_core_hash"
REMAP_DEFERRED = "fk_key_hash_remap_native_deferred"
_NO_KERNEL_RAN = "none"
_REASON_NO_ROWS = "no_rows_hashed"


@dataclass
class HashColumnDecision:
    """The backend one hash column takes for the whole run, and how many rows it hashed.

    `pinned_type` is the input Arrow type a native column is held to."""

    table: str
    column: str
    backend: str
    reason: str | None
    pinned_type: pa.DataType
    rows: int = 0

    @property
    def native(self) -> bool:
        return self.backend == RUST_COMPANION

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
    """The immutable per-column decisions for one table, and the per-batch gate."""

    def __init__(
        self, decisions: Mapping[str, HashColumnDecision], bound: BoundHashKernel | None
    ) -> None:
        self._decisions = dict(decisions)
        self._bound = bound

    @property
    def decisions(self) -> Mapping[str, HashColumnDecision]:
        return self._decisions

    def kernel_for(self, column: str, batch: pa.RecordBatch) -> BoundHashKernel | None:
        """The native kernel for `column` in `batch`, or None when it stays on Python.

        A native column is held to its pinned input type: a batch that lacks it or
        carries another type aborts the run, so a column never changes path mid-stream.
        """
        decision = self._decisions.get(column)
        if decision is None:
            return None
        idx = batch.schema.get_field_index(column)
        if decision.native:
            if idx < 0:
                raise ExecutionError(
                    code="out_of_core_hash_column_missing",
                    message=(
                        f"hash column {column!r} of table {decision.table!r} is missing from a "
                        "streamed batch; the run is aborted rather than skipping it."
                    ),
                )
            if batch.schema.field(idx).type != decision.pinned_type:
                raise ExecutionError(
                    code="out_of_core_hash_input_type_drift",
                    message=(
                        f"hash column {column!r} of table {decision.table!r} was pinned as "
                        f"{decision.pinned_type} but a batch carries "
                        f"{batch.schema.field(idx).type}; the run is aborted rather than "
                        "changing path mid-column."
                    ),
                )
        if idx >= 0:
            decision.rows += batch.num_rows
        return self._bound if decision.native else None


class OocHashRun:
    """Run-scoped hash decisions, collected across every table of one run.

    The companion is loaded at most once per run, on the first hash column that
    needs a decision; a plan with no hash column never touches it.
    """

    def __init__(self, *, native_threads: int | None = None) -> None:
        self._native_threads = native_threads
        self._loaded = False
        self._bound: BoundHashKernel | None = None
        self._plans: list[TableHashPlan] = []
        self._remap: list[dict[str, Any]] = []

    def _kernel(self) -> BoundHashKernel | None:
        if not self._loaded:
            self._loaded = True
            try:
                self._bound = BoundHashKernel(load_compiled_crypto_kernel(), self._native_threads)
            except CryptoExtensionUnavailableError:
                self._bound = None
        return self._bound

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
            input_type = source_schema.field(column).type
            bound = self._kernel()
            reason = decline_reason(column, input_type) if bound is not None else REASON_UNAVAILABLE
            native = bound is not None and reason is None
            decisions[column] = HashColumnDecision(
                table_name,
                column,
                RUST_COMPANION if native else PYTHON_SCALAR,
                reason,
                input_type,
            )
        table_plan = TableHashPlan(decisions, self._bound)
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
        parent_columns = dict(parent_seed.per_column if parent_seed is not None else ())
        for parent_col, child_col in zip(edge.parent_columns, edge.child_columns, strict=True):
            seed = parent_columns.get(parent_col)
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
