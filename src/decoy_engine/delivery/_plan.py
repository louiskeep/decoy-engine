"""`plan_delivery`: the delivery state of each table from the table-level dependency graph.

Edges run parent to child, from `relationships` and from the `reference` columns of generate
tables. They come from the validated config dict, not `RelationshipGraph`, whose nodes are
`(table, columns)` and which needs a namespace registry. Table-level cycles are accepted
upstream, so the walk keeps a visited set and a strongly connected component drops whole.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from decoy_engine.config import PipelineConfigError, parse_delivery_config
from decoy_engine.generation import generation_order
from decoy_engine.reasons import Kind, ReasonCode
from decoy_engine.reasons.decoy import DELIVERY_DEPENDENT_DROPPED, DELIVERY_WITHHELD

DeliveryState = Literal["delivered", "failed", "withheld", "dropped"]

# Target types whose tables a run delivers, so every one of them needs an outcome.
DELIVERABLE_TARGET_TYPES = frozenset({"file", "s3", "gcs"})


@dataclass(frozen=True)
class TableDelivery:
    state: DeliveryState
    # The smallest failed table name the dropped table is reachable from; None unless dropped.
    dropped_because: str | None = None
    # The failure cause for failed, the delivery decision code for withheld and dropped.
    code: ReasonCode | None = None


@dataclass(frozen=True)
class DeliveryDecision:
    # Only tables with a deliverable target appear; any other table has no delivery state.
    tables: Mapping[str, TableDelivery]
    deliver: bool


def _children(config: Mapping[str, Any]) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for rel in config.get("relationships") or []:
        parent = rel["parent"]["table"]
        for child in rel["children"]:
            edges.setdefault(parent, set()).add(child["table"])
    for table, parents in generation_order(dict(config)):
        for parent in parents:
            edges.setdefault(parent, set()).add(table)
    for table, kids in edges.items():
        kids.discard(table)
    return edges


_BAD_GRAPH = "delivery dependency graph names a table not in the config"
_BAD_OUTCOME = "delivery outcome is not a failure reason code"


def _check_universe(config: Mapping[str, Any], outcomes: Mapping[str, Any]) -> None:
    """Refuse outcomes or edges that name tables the config does not hold; messages are fixed."""
    # A decision or warning code, or a non-code, would be recorded as a failed table's cause.
    for cause in outcomes.values():
        if cause is not None and not (
            isinstance(cause, ReasonCode) and cause.spec.kind is Kind.FAILURE
        ):
            raise PipelineConfigError(_BAD_OUTCOME)
    names = {t["name"] for t in config.get("tables") or []}
    targets = config.get("targets") or {}
    # Validation does not tie target keys to tables; a target no table owns has no run outcome.
    deliverable = {
        n for n, t in targets.items() if n in names and t.get("type") in DELIVERABLE_TARGET_TYPES
    }
    if set(outcomes) != deliverable:
        raise PipelineConfigError("delivery outcomes do not match the deliverable tables")
    try:
        edges = _children(config)
    except ValueError:
        # generation_order refuses a reference to a non-generate table and its message names it.
        edges = None
    if edges is None:
        raise PipelineConfigError(_BAD_GRAPH)
    if not (edges.keys() | {c for kids in edges.values() for c in kids}) <= names:
        raise PipelineConfigError(_BAD_GRAPH)


def _dropped_because(config: Mapping[str, Any], failed: list[str]) -> dict[str, str]:
    """Each table reachable from a failed one, keyed to the smallest failed name reaching it."""
    edges = _children(config)
    reached: dict[str, str] = {}
    visited: set[str] = set()
    # Sorted starts: after each start the visited set is closed under reachability, so a later
    # start never revisits it and the first claim comes from the smallest failed name.
    for start in sorted(failed):
        queue = deque([start])
        while queue:
            for nxt in sorted(edges.get(queue.popleft(), ())):
                if nxt not in visited:
                    visited.add(nxt)
                    reached[nxt] = start
                    queue.append(nxt)
    return reached


def plan_delivery(
    config: Mapping[str, Any], outcomes: Mapping[str, ReasonCode | None]
) -> DeliveryDecision:
    """Decide each table's delivery state; `outcomes` holds None for ok, a code for a failure."""
    _check_universe(config, outcomes)
    failed = [name for name, cause in outcomes.items() if cause is not None]
    strict = parse_delivery_config(config.get("delivery", {})).mode == "strict"
    reached = {} if strict or not failed else _dropped_because(config, failed)
    tables: dict[str, TableDelivery] = {}
    for name, cause in outcomes.items():
        if cause is not None:
            tables[name] = TableDelivery("failed", code=cause)
        elif failed and strict:
            tables[name] = TableDelivery("withheld", code=DELIVERY_WITHHELD)
        elif name in reached:
            tables[name] = TableDelivery("dropped", reached[name], DELIVERY_DEPENDENT_DROPPED)
        else:
            tables[name] = TableDelivery("delivered")
    # No deliverable table means nothing failed or was withheld, so the run still goes on.
    deliver = not outcomes or any(t.state == "delivered" for t in tables.values())
    return DeliveryDecision(tables, deliver)
