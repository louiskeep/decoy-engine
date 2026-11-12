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

from decoy_engine.generation import generation_order
from decoy_engine.reasons import ReasonCode

DeliveryState = Literal["delivered", "failed", "withheld", "dropped"]


@dataclass(frozen=True)
class TableDelivery:
    state: DeliveryState
    # The smallest failed table name the dropped table is reachable from; None unless dropped.
    dropped_because: str | None = None


@dataclass(frozen=True)
class DeliveryDecision:
    # Only tables with a file target appear; a table without one has no delivery state.
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


def _dropped_because(config: Mapping[str, Any], failed: list[str]) -> dict[str, str]:
    """Each table reachable from a failed one, keyed to the smallest failed name reaching it."""
    edges = _children(config)
    reached: dict[str, str] = {}
    visited: set[str] = set()
    # Sorted starts and a shared visited set: a table first reached from the smallest failed
    # name is also reachable from every larger one, so the first claim is the answer.
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
    failed = [name for name, cause in outcomes.items() if cause is not None]
    strict = (config.get("delivery") or {}).get("mode", "strict") == "strict"
    reached = {} if strict or not failed else _dropped_because(config, failed)
    tables: dict[str, TableDelivery] = {}
    for name, cause in outcomes.items():
        if cause is not None:
            tables[name] = TableDelivery("failed")
        elif failed and strict:
            tables[name] = TableDelivery("withheld")
        elif name in reached:
            tables[name] = TableDelivery("dropped", reached[name])
        else:
            tables[name] = TableDelivery("delivered")
    deliver = any(t.state == "delivered" for t in tables.values())
    return DeliveryDecision(tables, deliver)
