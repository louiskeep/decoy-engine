"""The sample request: head seeds over child-free tables and upward closure, as a `SubsetRequest`.

A sample is the subset closure, never a cut of one table: the seeds are the first N rows of every
table no retained edge makes a parent, and a retained edge only walks up to the parents those rows
need. An explicit root walks both ways in its own component.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

from decoy_engine.config import classify_table_kinds
from decoy_engine.subset._api import subset_request_from_config
from decoy_engine.subset._edges import (
    build_subset_edges,
    linked_neighbours,
    mask_relationships,
    sample_refusal_edge,
    unknown_table_error,
)
from decoy_engine.subset._errors import invalid_knob
from decoy_engine.subset._scope import SubsetRequest
from decoy_engine.subset._types import (
    EdgeDirection,
    FanOutBudget,
    FanOutPolicy,
    SeedSpec,
)

SAMPLE_ROWS_MIN = 1
SAMPLE_ROWS_MAX = 1000
SAMPLE_KEPT_CEILING = 1_000_000
_DISPLAY_MAX = 20


def _require_int(name: str, value: object, low: int, high: int) -> None:
    # `type(...) is int` keeps True/False out: a bool would otherwise read as 1 or 0 rows.
    if type(value) is not int or not low <= value <= high:
        raise invalid_knob(f"{name} must be an integer from {low} to {high}.")


@dataclass(frozen=True)
class SampleSpec:
    """What a sample run asked for: head rows per seed table, the seed tables, source rows to show."""

    rows: int
    roots: tuple[str, ...]
    display_rows: int = 0

    def __post_init__(self) -> None:
        _require_int("rows", self.rows, SAMPLE_ROWS_MIN, SAMPLE_ROWS_MAX)
        _require_int("display_rows", self.display_rows, 0, _DISPLAY_MAX)


def _components(tables: list[str], relationships: tuple[Any, ...]) -> list[list[str]]:
    """Linked components of `tables` over the retained edges (undirected, in config order)."""
    neighbours = linked_neighbours(relationships)
    seen: set[str] = set()
    components: list[list[str]] = []
    for table in tables:
        if table in seen:
            continue
        stack, members = [table], {table}
        while stack:
            for nxt in neighbours.get(stack.pop(), ()):
                if nxt not in members:
                    members.add(nxt)
                    stack.append(nxt)
        seen |= members
        components.append([t for t in tables if t in members])
    return components


def sample_request(
    config: dict[str, Any],
    *,
    rows: int,
    root: str | None = None,
    use_config_subset: bool = True,
    display_rows: int = 0,
) -> SubsetRequest:
    """The request that makes `decoy run --sample` pick `rows` head rows and close over them.

    Raises `ExecutionError` (`invalid_execution_knob`) for a bad knob, a root with a `subset:`
    block and a generated parent of a masked child, and `SubsetPreflightError` for a root or a
    relationship table that is not a mask table.

    A config-set "downward" direction with `allow_dangling` in an unseeded component stays as the
    config states it, so that component's head seeds pull no parents.
    """
    _require_int("rows", rows, SAMPLE_ROWS_MIN, SAMPLE_ROWS_MAX)
    _require_int("display_rows", display_rows, 0, _DISPLAY_MAX)
    use_block = use_config_subset and config.get("subset") is not None
    if root is not None and use_block:
        raise invalid_knob("a sample root cannot be combined with a subset block.")
    refused = sample_refusal_edge(config)
    if refused is not None:
        parent, parent_columns, child, child_columns = refused
        raise invalid_knob(
            f"a sampled generated parent {parent!r} {list(parent_columns)} would orphan the "
            f"masked child {child!r} {list(child_columns)}."
        )
    mask_tables = [t for t, kind in classify_table_kinds(config).items() if kind == "mask"]
    if root is not None and root not in mask_tables:
        raise unknown_table_error(root)

    relationships, excluded = mask_relationships(config)
    edges = build_subset_edges(relationships)
    for e in edges:
        for table in (e.parent_table, e.child_table):
            if table not in mask_tables:
                raise unknown_table_error(table)
    # A self-reference is not a child of anything else: it must not make its table a parent, or
    # the table is never child-free and the sample closes it to nothing.
    parents = {e.parent_table for e in edges if e.parent_table != e.child_table}
    components = _components(mask_tables, relationships)
    edge_ids: dict[str, list[str]] = {t: [] for t in mask_tables}
    for e in edges:
        edge_ids[e.parent_table].append(e.edge_id)
        edge_ids[e.child_table].append(e.edge_id)

    seeds: list[SeedSpec] = []
    directions: dict[str, EdgeDirection] = {}
    budget_cap: int | None = None
    policy = FanOutPolicy()
    if use_block:
        from_config = cast(SubsetRequest, subset_request_from_config(config))
        seeds = [replace(s, cap=rows) for s in from_config.seeds]
        policy = from_config.policy
        directions = dict(policy.edge_directions)
        budget_cap = policy.budget.max_total_rows
    elif root is not None:
        seeds = [SeedSpec(table=root, mode="head", count=rows)]
    seeded = {s.table for s in seeds}

    for component in components:
        if root is not None and root in component:
            for table in component:
                for edge_id in edge_ids[table]:
                    directions[edge_id] = "both"
            continue
        if seeded & set(component):
            continue
        heads = [t for t in component if t not in parents] or component[:1]
        seeds.extend(SeedSpec(table=t, mode="head", count=rows) for t in heads)
        for table in component:
            for edge_id in edge_ids[table]:
                directions.setdefault(edge_id, "upward")

    ceiling = SAMPLE_KEPT_CEILING if budget_cap is None else min(budget_cap, SAMPLE_KEPT_CEILING)
    policy = replace(
        policy,
        budget=FanOutBudget(
            max_total_rows=ceiling, max_table_seed_multiple=policy.budget.max_table_seed_multiple
        ),
        edge_directions=tuple(sorted(directions.items())),
    )
    return SubsetRequest(
        relationships=relationships,
        seeds=tuple(seeds),
        policy=policy,
        excluded_edges=excluded,
        sample=SampleSpec(
            rows=rows, roots=tuple(sorted({s.table for s in seeds})), display_rows=display_rows
        ),
    )
