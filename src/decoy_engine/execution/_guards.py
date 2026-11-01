"""Execution-time ingest guards (engine-v2 S13).

`reject_null_bearing_int` is the B1 backstop for the `no_profile` path. The
plan-compile check `null_bearing_int_unsupported` rejects integer + null-bearing
columns under truncate/categorical when a profile is present; under
`no_profile=True` that check lands in `checks_skipped` because dtype/null_count
are unavailable at compile time. This guard re-checks at ingest, on the
`pa.Table` sources BEFORE the `to_pandas` conversion (so the integer type is
still intact, not yet widened to float), and raises the typed error before any
adapter runs. The rejection is loud and identical regardless of route.

PO-settled 2026-05-28 (S13 spec section 1.5); consistent with the S5
float-canonicalization hard error. This rejects loudly + identically; it is not a
silent downgrade.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list
from decoy_engine.plan._checks import _INT_NULL_REJECTED_STRATEGIES

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph


def null_bearing_int_candidates(
    work: Iterable[WorkNode], relationship_graph: RelationshipGraph
) -> Iterator[WorkNode]:
    """Scalar nodes whose strategy is in the int+null reject set and that are masked.

    FK-child columns are EXEMPT: they are resolved through the relationship edge
    (not masked by the strategy), and an FK job runs via the pandas oracle on both
    substrates, so the int+null divergence cannot arise for them. Shared by every
    route's guard so the predicate cannot drift between resident and lazy sources.
    """
    for node in work:
        if node.kind != "scalar" or node.strategy not in _INT_NULL_REJECTED_STRATEGIES:
            continue
        if relationship_graph.parents_of(node.table, node.columns):
            continue  # FK child: resolved via the edge, not masked; no divergence
        yield node


def null_bearing_int_error(node: WorkNode) -> ExecutionError:
    """The one typed rejection every route raises for an integer column with nulls."""
    return ExecutionError(
        code=reasons.runtime.NULL_BEARING_INT_UNSUPPORTED,
        message=(
            f"Column {node.table}.{node.columns[0]} is an integer column with nulls "
            f"masked under {node.strategy!r}. Integer-with-null is not supported "
            "under truncate/categorical (the masked value is ambiguous "
            "across execution substrates); stringify or bin the column upstream."
        ),
    )


def reject_null_bearing_int(
    plan: Plan,
    sources: Mapping[str, pa.Table],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Raise ExecutionError if a truncate/categorical node masks an integer
    source column that contains a null. Substrate-agnostic: operates on the Arrow
    sources before conversion, so both adapters reject the same input identically.
    """
    work = build_work_list(plan, registry)
    for node in null_bearing_int_candidates(work, relationship_graph):
        table = sources.get(node.table)
        if table is None:
            continue
        column = node.columns[0]
        if column not in table.column_names:
            continue
        if pa.types.is_integer(table.schema.field(column).type) and (
            table.column(column).null_count > 0
        ):
            raise null_bearing_int_error(node)


def reject_gated_hash_on_integer(
    plan: Plan,
    schemas: Mapping[str, pa.Schema],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Raise if a `when:`-gated hash node masks an integer source column.

    Keyed on the Arrow schema, so it fires with or without nulls and independent of the
    profile or chunk partition. A gated hash would write string tokens into an integer
    frame column and crash late on the first chunk that happens to carry a null. A table
    absent from `schemas` is skipped; the map is by table so a same-named column of
    another type is not confused. FK children are exempt (resolved through the edge).
    """
    for node in build_work_list(plan, registry):
        gate = getattr(node.plan_slice, "when", None)
        if node.kind != "scalar" or node.strategy != "hash" or gate is None:
            continue
        schema = schemas.get(node.table)
        column = node.columns[0]
        if schema is None or column not in schema.names:
            continue
        if not pa.types.is_integer(schema.field(column).type):
            continue
        if relationship_graph.parents_of(node.table, node.columns):
            continue
        raise ExecutionError(
            code=reasons.runtime.GATED_HASH_ON_INTEGER_UNSUPPORTED,
            message=(
                f"Column {node.table}.{column} is an integer column hashed under a "
                "`when:` condition. Conditional hashing of an integer column is not "
                "supported yet; hash the whole column, or stringify it upstream."
            ),
        )


def run_chunk_ingest_guards(
    plan: Plan,
    sources: Mapping[str, pa.Table],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Every per-chunk ingest guard that runs on the Arrow sources before masking.

    The one place both chunked routes call: the pandas adapter at the top of
    `run`, and the native chunk loop before it masks, so an input one route
    refuses is refused by the other with the same error. A new ingest guard is
    added here and reaches both.
    """
    reject_null_bearing_int(plan, sources, registry, relationship_graph)
    reject_gated_hash_on_integer(
        plan, {name: tbl.schema for name, tbl in sources.items()}, registry, relationship_graph
    )


__all__ = [
    "null_bearing_int_candidates",
    "null_bearing_int_error",
    "reject_gated_hash_on_integer",
    "reject_null_bearing_int",
    "run_chunk_ingest_guards",
]
