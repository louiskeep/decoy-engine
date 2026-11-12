"""Typed subset requests and results: what to select, which tables it narrows, what it chose.

A request carries no sources and no paths, so the same request applies to resident tables, lazy
sources or Parquet files. `scope_for` is the one place that decides what happens to a table no
seed reaches: it passes through whole.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from decoy_engine.plan._types import PlanRelationship
from decoy_engine.subset._edges import linked_neighbours
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetPlan

if TYPE_CHECKING:
    from decoy_engine.subset._sample import SampleSpec


@dataclass(frozen=True)
class SubsetRequest:
    relationships: tuple[PlanRelationship, ...]
    seeds: tuple[SeedSpec, ...]
    policy: FanOutPolicy
    # Edges dropped before the request was built (a generate-kind end has no source to read).
    excluded_edges: tuple[str, ...] = ()
    sample: SampleSpec | None = None


@dataclass(frozen=True)
class SubsetScope:
    """`tables` are reachable from a seed; `whole` were supplied and no seed reaches them. A sample
    has no `whole`: `head` holds its edgeless tables, read as their first rows."""

    tables: frozenset[str]
    whole: frozenset[str]
    head: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SubsetSelection:
    """`survivors` hold row indices for in-scope tables only; `counts` are (source, kept) rows
    for every supplied table."""

    survivors: Mapping[str, frozenset[int]]
    counts: Mapping[str, tuple[int, int]]
    scope: SubsetScope
    excluded_edges: tuple[str, ...]
    estimate: SubsetPlan


def scope_for(request: SubsetRequest, table_names: Iterable[str]) -> SubsetScope:
    """Split the supplied tables into those a seed reaches over the declared edges and those
    it does not. Reach ignores `edge_directions`: a table whose only edge is `upward` is still
    narrowed (to no rows), as standalone `plan_subset` reports it, never passed whole."""
    supplied = frozenset(table_names)
    neighbours = linked_neighbours(request.relationships)
    reached = {seed.table for seed in request.seeds}
    frontier = list(reached)
    while frontier:
        for nxt in neighbours.get(frontier.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    if request.sample is None:
        return SubsetScope(tables=supplied & reached, whole=supplied - reached)
    # A sample never passes a table whole: what no seed closes over is read head-first, and a
    # head table with an edge would skip the closure that keeps its links whole.
    head_seeded = {s.table for s in request.seeds if s.mode == "head" and s.table not in neighbours}
    tables = supplied & (reached - head_seeded)
    head = supplied - tables
    if head & neighbours.keys():
        raise ValueError("a sample head table has a declared edge")
    return SubsetScope(tables=tables, whole=frozenset(), head=head)
