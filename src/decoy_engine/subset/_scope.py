"""Typed subset requests and results: what to select, which tables it narrows, what it chose.

A request carries no sources and no paths, so the same request applies to resident tables, lazy
sources or Parquet files. `scope_for` is the one place that decides what happens to a table no
seed reaches: it passes through whole.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from decoy_engine.plan._types import PlanRelationship
from decoy_engine.subset._types import FanOutPolicy, SeedSpec, SubsetPlan


@dataclass(frozen=True)
class SubsetRequest:
    relationships: tuple[PlanRelationship, ...]
    seeds: tuple[SeedSpec, ...]
    policy: FanOutPolicy


@dataclass(frozen=True)
class SubsetScope:
    """`tables` are reachable from a seed; `whole` were supplied and no seed reaches them."""

    tables: frozenset[str]
    whole: frozenset[str]


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
    """Split the supplied tables into those a seed reaches over the declared edges (either
    direction, as the closure walks them) and those it does not."""
    supplied = frozenset(table_names)
    neighbours: dict[str, set[str]] = {}
    for rel in request.relationships:
        for child in rel.children:
            neighbours.setdefault(rel.parent.table, set()).add(child.table)
            neighbours.setdefault(child.table, set()).add(rel.parent.table)
    reached = {seed.table for seed in request.seeds}
    frontier = list(reached)
    while frontier:
        for nxt in neighbours.get(frontier.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    return SubsetScope(tables=supplied & reached, whole=supplied - reached)
