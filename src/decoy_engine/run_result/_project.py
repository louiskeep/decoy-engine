"""The per-table projection of a run: what each declared table did and what was delivered.

Pure and I/O free. The inputs are typed facts (the progress collector's counters, the delivery
facts, the engine's ``ExecutionResult``); the output feeds the run-result builder and nothing
else decides a row count or a table outcome. Nothing here reads a cell value, an exception
or a path; table names pass through ``approve_name``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from decoy_engine.config import classify_table_kinds
from decoy_engine.reasons import approve_name
from decoy_engine.run_result._model import (
    Delivery,
    DeliveryMode,
    DeliveryOutcome,
    RowsSource,
    TableDelivery,
    TableKind,
    TableOutcome,
    TableResult,
    VaultState,
)


class _Attributed(Protocol):
    @property
    def table(self) -> str: ...


class ExecutionFacts(Protocol):
    """The part of ``execution.ExecutionResult`` the projection reads. A protocol, because
    ``run_result`` sits below ``execution`` in the layer contract and may not import it."""

    @property
    def outputs(self) -> Mapping[str, Any]: ...

    @property
    def row_errors(self) -> tuple[_Attributed, ...]: ...


_NO_DELIVERY = TableDelivery(
    state="none", format=None, output=None, reason=None, dropped_because=None
)


@dataclass(frozen=True)
class CollectedFacts:
    """Typed counters from the progress stream. A table in ``finished`` finished its phase
    (its value is the phase's ``rows_out``, or ``None`` when the event carried none)."""

    started: frozenset[str] = frozenset()
    finished: Mapping[str, int | None] = field(default_factory=dict)
    errored: frozenset[str] = frozenset()
    rate_rows: Mapping[str, int] = field(default_factory=dict)
    elapsed: Mapping[str, float] = field(default_factory=dict)
    aggregate_error: bool = False
    phases: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        # A snapshot: later events must not change what the builder already holds.
        for name in ("finished", "rate_rows", "elapsed"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(self, "started", frozenset(self.started))
        object.__setattr__(self, "errored", frozenset(self.errored))
        object.__setattr__(self, "phases", tuple(self.phases))


@dataclass(frozen=True)
class DeliveryFacts:
    """Per-table delivery entries, keyed by table name. Only target tables appear."""

    mode: DeliveryMode
    vault: VaultState
    tables: Mapping[str, TableDelivery]

    def __post_init__(self) -> None:
        # One entry type: a second delivery vocabulary would drift from the document's.
        if not isinstance(self.tables, Mapping):
            raise TypeError("delivery tables must be a mapping")
        if not all(isinstance(entry, TableDelivery) for entry in self.tables.values()):
            raise TypeError("delivery entries must be run_result.TableDelivery")
        # Each entry is rebuilt through the model's constructor from a dump, so its reason data
        # is a validated deep copy that nothing the caller still holds can reach.
        owned = {name: TableDelivery(**entry.model_dump()) for name, entry in self.tables.items()}
        object.__setattr__(self, "tables", MappingProxyType(owned))

    @classmethod
    def not_applicable(cls) -> DeliveryFacts:
        """A preview, or a run with no file target."""
        return cls("in_place", "not_used", {})


@dataclass(frozen=True)
class ProjectedTables:
    tables: tuple[TableResult, ...]
    delivery: Delivery


def _rows(
    name: str, result: ExecutionFacts | None, facts: CollectedFacts
) -> tuple[int | None, RowsSource | None]:
    if result is not None and name in result.outputs:
        return result.outputs[name].num_rows, "outputs"
    if facts.finished.get(name) is not None:
        return facts.finished[name], "phase"
    if name in facts.rate_rows:
        return facts.rate_rows[name], "rate"
    return None, None


def _outcome(
    name: str, result: ExecutionFacts | None, facts: CollectedFacts, run_failed: bool
) -> TableOutcome:
    if name in facts.errored:
        return "failed"
    if (result is not None and name in result.outputs) or name in facts.finished:
        return "succeeded"
    if facts.aggregate_error:
        return "failed"
    if name in facts.started or name in facts.rate_rows:
        # A start with no end is an early close by a consumer that stopped on purpose, unless
        # the run itself failed.
        return "failed" if run_failed else "succeeded"
    return "not_run"


def _kind(kind: str) -> TableKind:
    if kind == "mask":
        return "mask"
    if kind == "generate":
        return "generate"
    raise ValueError("unknown table kind")


def _delivery_outcome(entries: Mapping[str, TableDelivery]) -> DeliveryOutcome:
    if not entries:
        return "not_applicable"
    delivered = sum(entry.state == "delivered" for entry in entries.values())
    if delivered == len(entries):
        return "delivered"
    return "partial" if delivered else "nothing"


def project_tables(
    *,
    config: Mapping[str, Any],
    execution_result: ExecutionFacts | None,
    facts: CollectedFacts | None,
    delivery: DeliveryFacts | None,
    run_failed: bool,
) -> ProjectedTables:
    """One entry per declared table, in config order.

    ``run_failed`` is true when the run ended in failure or cancellation; it decides whether a
    table that started and never finished counts as failed. It has no default on purpose.

    ``rows_out`` takes the first of: ``execution_result.outputs``, the phase finish rows, the
    final rate sample, else null; ``rows_source`` names which. A delivery fact of ``failed``
    wins over execution success and carries its reason.
    """
    facts = facts or CollectedFacts()
    delivery = delivery or DeliveryFacts.not_applicable()
    row_errors = execution_result.row_errors if execution_result is not None else None
    kinds = classify_table_kinds(dict(config))
    if not delivery.tables.keys() <= kinds.keys():
        # A mis-keyed adapter would otherwise split the tables from the summary.
        raise ValueError("delivery names a table the config does not declare")
    rows: list[TableResult] = []
    for index, (name, kind) in enumerate(kinds.items()):
        rows_out, source = _rows(name, execution_result, facts)
        entry = delivery.tables.get(name, _NO_DELIVERY)
        failed_delivery = entry.state == "failed"
        rows.append(
            TableResult(
                index=index,
                name=approve_name(name),
                kind=_kind(kind),
                outcome="failed"
                if failed_delivery
                else _outcome(name, execution_result, facts, run_failed),
                rows_out=rows_out,
                rows_source=source,
                row_errors=None if row_errors is None else sum(r.table == name for r in row_errors),
                elapsed_s=facts.elapsed.get(name),
                reason=entry.reason if failed_delivery else None,
                delivery=entry,
            )
        )
    summary = Delivery(
        mode=delivery.mode, outcome=_delivery_outcome(delivery.tables), vault=delivery.vault
    )
    return ProjectedTables(tuple(rows), summary)
