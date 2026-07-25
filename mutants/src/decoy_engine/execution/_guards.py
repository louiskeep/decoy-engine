"""Execution-time ingest guards (engine-v2 S13).

`reject_null_bearing_int` is the B1 backstop for the `no_profile` path. The
plan-compile check `null_bearing_int_unsupported` rejects integer + null-bearing
columns under truncate/hash/categorical when a profile is present; under
`no_profile=True` that check lands in `checks_skipped` because dtype/null_count
are unavailable at compile time. This guard re-checks at ingest, on the
`pa.Table` sources BEFORE any `to_pandas`/`to_polars` conversion (so the integer
type is still intact, not yet widened to float), and raises the SAME typed error
on BOTH adapters. Neither substrate silently produces output the other rejects.

PO-settled 2026-05-28 (S13 spec section 1.5); consistent with the S5
float-canonicalization hard error. This rejects loudly + identically; it is not a
silent downgrade.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list
from decoy_engine.plan._checks import _INT_NULL_REJECTED_STRATEGIES

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph


def reject_null_bearing_int(
    plan: Plan,
    sources: Mapping[str, pa.Table],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Raise ExecutionError if a truncate/hash/categorical node masks an integer
    source column that contains a null. Substrate-agnostic: operates on the Arrow
    sources before conversion, so both adapters reject the same input identically.

    FK-child columns are EXEMPT: they are resolved through the relationship edge
    (not masked by the strategy), and an FK job runs via the pandas oracle on both
    substrates, so the int+null divergence cannot arise for them.
    """
    for node in build_work_list(plan, registry):
        if node.kind != "scalar" or node.strategy not in _INT_NULL_REJECTED_STRATEGIES:
            continue
        if relationship_graph.parents_of(node.table, node.columns):
            continue  # FK child: resolved via the edge, not masked; no divergence
        table = sources.get(node.table)
        if table is None:
            continue
        column = node.columns[0]
        if column not in table.column_names:
            continue
        if pa.types.is_integer(table.schema.field(column).type) and (
            table.column(column).null_count > 0
        ):
            raise ExecutionError(
                code="null_bearing_int_unsupported",
                message=(
                    f"Column {node.table}.{column} is an integer column with nulls "
                    f"masked under {node.strategy!r}. Integer-with-null is not supported "
                    "under truncate/hash/categorical (the masked value is ambiguous "
                    "across execution substrates); stringify or bin the column upstream."
                ),
            )


__all__ = ["reject_null_bearing_int"]
