"""Typed subset requests and results: what to select, which tables it narrows, what it chose.

A request carries no sources and no paths, so the same request applies to resident tables, lazy
sources or Parquet files. `scope_for` is the one place that decides what happens to a table no
seed reaches: it passes through whole.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import polars as pl
import pyarrow as pa

from decoy_engine._execution_error import lazy_source_changed
from decoy_engine.plan._types import PlanRelationship
from decoy_engine.subset._edges import linked_neighbours, reach
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

    def fingerprint(self) -> str:
        """SHA-256 over the canonical dump: the subset half of a run's identity. The run config
        drops the `subset` block, so this fingerprint is what covers the seed literals."""
        canonical = json.dumps(dataclasses.asdict(self), sort_keys=True, default=repr)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class SubsetScope:
    """`tables` are reachable from a seed; `whole` were supplied and no seed reaches them. A sample
    has no `whole`: `head` holds its edgeless tables, read as their first rows."""

    tables: frozenset[str]
    whole: frozenset[str]
    head: frozenset[str] = frozenset()


class MaterializationCheck:
    """The first pass's kept key columns, held to compare with what materialization reads.

    Opaque on purpose: the cells are never returned, repr'd, serialized or logged, so a
    selection can be shown without exposing a key."""

    __slots__ = ("_names", "_rows")

    def __init__(self, rows: Mapping[str, pl.DataFrame]) -> None:
        self._rows = dict(rows)
        self._names = {name: tuple(frame.columns) for name, frame in self._rows.items()}

    def __repr__(self) -> str:
        return "MaterializationCheck()"

    def columns(self, table: str) -> tuple[str, ...]:
        """The key column names to collect for `table`, in comparison order (names only)."""
        return self._names[table]

    def verify(self, table: str, actual: pa.Table) -> None:
        """Raise `lazy_source_changed` unless `actual` equals the first pass exactly: Arrow
        types, null placement and row order. Raised outside any handler so the refusal carries
        no cause or context."""
        expected = self._rows[table]
        different = False
        try:
            found = cast("pl.DataFrame", pl.from_arrow(actual))
            different = found.schema != expected.schema or not found.equals(expected)
        except Exception:
            different = True
        if different:
            raise lazy_source_changed(table, "the kept key columns differ between the two passes")


@dataclass(frozen=True)
class SubsetSelection:
    """`survivors` hold row indices for in-scope tables only; `counts` are (source, kept) rows
    for every supplied table."""

    survivors: Mapping[str, frozenset[int]]
    counts: Mapping[str, tuple[int, int]]
    scope: SubsetScope
    excluded_edges: tuple[str, ...]
    estimate: SubsetPlan
    # Stage-transfer state, never an output: no value is exposed (see `MaterializationCheck`).
    materialization_check: MaterializationCheck | None = field(
        default=None, repr=False, compare=False
    )


def scope_for(request: SubsetRequest, table_names: Iterable[str]) -> SubsetScope:
    """Split the supplied tables into those a seed reaches over the declared edges and those
    it does not. Reach ignores `edge_directions`: a table whose only edge is `upward` is still
    narrowed (to no rows), as standalone `plan_subset` reports it, never passed whole."""
    supplied = frozenset(table_names)
    neighbours = linked_neighbours(request.relationships)
    reached = reach((seed.table for seed in request.seeds), neighbours)
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
