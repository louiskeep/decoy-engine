"""Stub."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decoy_engine.run_result._model import Delivery, TableDelivery, TableResult


@dataclass(frozen=True)
class CollectedFacts:
    started: Any = frozenset()
    finished: Any = field(default_factory=dict)
    errored: Any = frozenset()
    rate_rows: Any = field(default_factory=dict)
    aggregate_error: bool = False


@dataclass(frozen=True)
class DeliveryFacts:
    mode: str = "in_place"
    vault: str = "not_used"
    tables: Any = field(default_factory=dict)

    @classmethod
    def not_applicable(cls) -> DeliveryFacts:
        return cls()


@dataclass(frozen=True)
class ProjectedTables:
    tables: tuple[TableResult, ...]
    delivery: Delivery


def project_tables(*, config: Any, execution_result: Any = None, facts: Any = None,
                   delivery: Any = None, run_failed: bool = False) -> ProjectedTables:
    return ProjectedTables((), Delivery(mode="in_place", outcome="nothing", vault="not_used"))


__all__ = ["TableDelivery"]
