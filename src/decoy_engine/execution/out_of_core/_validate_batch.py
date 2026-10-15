"""Per-batch job validators for the out-of-core FK route (D3a).

Established methodology: the chunked oracle (`_chunked_oracle.py`) validates per chunk and
this module follows the same shape, with the D3b vault collector (`_vault_collect.py`) as the
structural twin: a cheap admission allowlist at routing time, a per-batch hook at each
driver's mask step, one assembly step at run end. The per-cell verdicts are NOT
re-implemented: each batch is checked by calling the registered validator function itself
(`validators._registry._REGISTRY`) on one column occurrence, so a verdict cannot drift from
the full-frame oracle's. Only the report bookkeeping lives here.

Report parity. `_registry.validate` emits one finding per failing configured
(validator entry, table, column occurrence) carrying WHOLE-TABLE row positions and a
total-count detail string, in validator-entry order then configured table/column order.
Repeated entries stay separate; only `validators_run` dedups names. This module keys its
accumulator by that same (entry ordinal, table, occurrence) triple, shifts each batch's
table-relative indices by the rows already seen for that table, and re-emits in
configured order. The aggregate detail reuses the validator's own wording with the leading
row count replaced by the whole-table total.

Why admission is two allowlists. Validators stringify and compare cell values, so any
OOC-vs-full-frame output difference (int64-vs-float64 "7" vs "7.0", NaN-vs-null, native vs
Python NFC category selection; see `tests/parity/SEMANTIC_DIFFERENCES.md`) could flip a
verdict. A job is admitted only when every validator is a per-cell check (verdict
independent of other rows and tables) and every validated column is a route-stable string
output. `hash` and `categorical` are excluded because their native kernels normalise
post-runtime Unicode differently from the Python oracle, which can change WHICH value is
emitted, not just its spelling. Incoming-FK child-key columns are RAW at the mask step and
rewritten at the join, so a validator reading one would see the wrong value.

Memory. Per-batch work is bounded by `batch_rows`. Retained state is the failing row
positions, O(failing rows) and unbounded in the worst case; the routing fit-gate is a coarse
envelope restriction, not a bound on findings residency.

Log hygiene: findings carry validator names, columns and row positions only, never cell text.
"""

from __future__ import annotations

import dataclasses
import re
import time
from array import array
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.validators._registry import _REGISTRY
from decoy_engine.validators._types import ValidationReport, ValidatorFinding

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "ADMITTED_VALIDATORS",
    "VALIDATOR_OOC_STRATEGIES",
    "ValidatorCollector",
    "admitted_validators",
    "build_validator_collector",
    "collect_batch_validation",
    "finish_validation",
    "validators_ooc_safe",
]

# Per-cell, row-local checks: a cell's verdict never reads another row or table.
# `no_orphan_children` is row-local too but needs post-FK-rewrite evaluation, so it declines.
ADMITTED_VALIDATORS = frozenset({"luhn", "npi", "iban", "vin", "regex_match", "column_in_set"})

# Strategies whose OOC output equals the Python oracle's byte for byte.
VALIDATOR_OOC_STRATEGIES = frozenset({"truncate", "redact", "passthrough"})

_STRING_SOURCE = (pa.string(), pa.large_string(), pa.binary(), pa.large_binary())
_STRING_OUTPUT = (pa.string(), pa.large_string())

_LEADING_COUNT = re.compile(r"^\d+")

_Key = tuple[int, str, int]


@dataclass(frozen=True)
class _Occurrence:
    """One configured (validator entry, table, column) slot; duplicates stay separate."""

    key: _Key
    table: str
    column: str
    name: str
    fn: Callable[..., tuple[ValidatorFinding, ...]]
    entry: dict[str, Any]


def _incoming_child_keys(graph: RelationshipGraph) -> set[tuple[str, str]]:
    return {(e.child_table, c) for e in graph.edges for c in e.child_columns}


def _column_map(entry: dict[str, Any]) -> dict[str, list[str]] | None:
    """The entry's `columns` mapping, or None when it is not the shape the oracle iterates."""
    columns = entry.get("columns") or {}
    if not isinstance(columns, dict):
        return None
    for table, names in columns.items():
        if not isinstance(table, str) or not isinstance(names, list):
            return None
        if not all(isinstance(n, str) for n in names):
            return None
    return columns


def _column_admitted(
    plan: Plan,
    table: str,
    column: str,
    schemas: Mapping[str, pa.Schema],
    child_keys: set[tuple[str, str]],
) -> bool:
    from decoy_engine.execution.out_of_core._compat import _column_seed
    from decoy_engine.execution.out_of_core._mask import masked_output_type

    if (table, column) in child_keys:
        return False
    schema = schemas.get(table)
    if schema is None or column not in schema.names:
        # No such table or column: the oracle skips it, so no value can ever be checked.
        return True
    seed = _column_seed(plan, table, column)
    if seed is None or seed.strategy not in VALIDATOR_OOC_STRATEGIES:
        return False
    source_type = schema.field(column).type
    if source_type not in _STRING_SOURCE:
        return False
    try:
        # The RESOLVED output type: a redact replacement can be numeric on a string source.
        return masked_output_type(seed, source_type) in _STRING_OUTPUT
    except (ExecutionError, pa.ArrowException, TypeError, ValueError):
        return False


def admitted_validators(
    config: Mapping[str, Any] | None,
    relationships: RelationshipGraph,
    compiled_plan: Plan,
    source_schemas: Mapping[str, pa.Schema],
) -> list[dict[str, Any]] | None:
    """The validator entries when the whole job may validate out-of-core, else None.

    All-or-nothing: one declined validator or column keeps the job on full-frame. The plan
    resolves each column's strategy and output type; the schemas give the source dtype.
    """
    entries = (config or {}).get("validators") or []
    if not entries:
        return None
    quarantine = (config or {}).get("quarantine") or {}
    if quarantine.get("enabled") and "validation_fail" in (quarantine.get("triggers") or []):
        # The OOC route has no row-removal channel; never retro-filter a streamed sink.
        return None
    child_keys = _incoming_child_keys(relationships)
    admitted: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("name") not in ADMITTED_VALIDATORS:
            return None
        columns = _column_map(entry)
        if columns is None:
            return None
        for table, names in columns.items():
            if not all(
                _column_admitted(compiled_plan, table, name, source_schemas, child_keys)
                for name in names
            ):
                return None
        admitted.append(entry)
    return admitted


def validators_ooc_safe(
    config: Mapping[str, Any],
    plan: Plan,
    graph: RelationshipGraph,
    caller_sources: Mapping[str, pa.Table | LazySource],
) -> bool:
    """Routing signal: True when `admitted_validators` admits the job."""
    if not config.get("validators"):
        return False  # the common case: nothing to read from the sources
    schemas = {table: source.schema for table, source in caller_sources.items()}
    return admitted_validators(config, graph, plan, schemas) is not None


class ValidatorCollector:
    """Accumulates per-batch findings and assembles the oracle-identical report."""

    def __init__(self, entries: list[dict[str, Any]], config: Mapping[str, Any]) -> None:
        self._config = dict(config)
        self._names = [entry["name"] for entry in entries]
        self._ordered: list[_Occurrence] = []
        self._by_table: dict[str, list[_Occurrence]] = {}
        for ordinal, entry in enumerate(entries):
            fn = _REGISTRY[entry["name"]]
            # Parameter validation (bad regex, missing allowed_values) raises before any
            # table is read, exactly as the oracle does even for empty output.
            fn({}, entry, self._config)
            for table, names in (_column_map(entry) or {}).items():
                for occurrence, column in enumerate(names):
                    slot = _Occurrence(
                        key=(ordinal, table, occurrence),
                        table=table,
                        column=column,
                        name=entry["name"],
                        fn=fn,
                        entry={**entry, "columns": {table: [column]}},
                    )
                    self._ordered.append(slot)
                    self._by_table.setdefault(table, []).append(slot)
        self._rows_seen: dict[str, int] = {}
        self._failing: dict[_Key, array[int]] = {}
        self._first: dict[_Key, str] = {}
        self._elapsed = 0.0

    def add_batch(self, table: str, batch: pa.RecordBatch) -> None:
        """Check one masked batch; table-relative offsets become whole-table positions."""
        started = time.perf_counter()
        offset = self._rows_seen.get(table, 0)
        self._rows_seen[table] = offset + batch.num_rows
        slots = self._by_table.get(table)
        if slots:
            names = set(batch.schema.names)
            as_table = pa.Table.from_batches([batch])
            for slot in slots:
                if slot.column not in names:
                    continue
                for finding in slot.fn({table: as_table}, slot.entry, self._config):
                    failing = self._failing.setdefault(slot.key, array("q"))
                    self._first.setdefault(slot.key, finding.detail)
                    failing.extend(offset + i for i in finding.failing_row_indices)
        self._elapsed += time.perf_counter() - started

    def assemble(self) -> ValidationReport:
        """The `_registry.validate` report: findings in configured order, merged indices."""
        findings: list[ValidatorFinding] = []
        for slot in self._ordered:
            failing = self._failing.get(slot.key)
            if not failing:
                continue
            # The validator's own wording, with its per-batch count swapped for the total.
            detail = self._first[slot.key]
            findings.append(
                ValidatorFinding(
                    validator=slot.name,
                    table=slot.table,
                    column=slot.column,
                    failing_row_indices=tuple(failing),
                    detail=_LEADING_COUNT.sub(str(len(failing)), detail, count=1),
                )
            )
        return ValidationReport(
            passed=not findings,
            validators_run=tuple(dict.fromkeys(self._names)),
            findings=tuple(findings),
            elapsed_ms=self._elapsed * 1000.0,
        )


def build_validator_collector(
    config: Mapping[str, Any] | None,
    plan: Plan,
    graph: RelationshipGraph,
    sources: Mapping[str, pa.Table | LazySource],
) -> ValidatorCollector | None:
    """The collector for a validator-bearing job, None when the job declares none.

    Fails closed on a job the route cannot validate: skipping would report success for
    checks that never ran. Routing only sends admitted jobs here; a direct caller of
    `run_fk_out_of_core` is not routed.
    """
    if not config or not config.get("validators"):
        return None
    schemas = {table: source.schema for table, source in sources.items()}
    entries = admitted_validators(config, graph, plan, schemas)
    if entries is None:
        raise ExecutionError(
            code="out_of_core_validators_not_admitted",
            message=(
                "the out-of-core route can only validate per-cell checks on route-stable string "
                "columns without quarantine; use execution_mode='full_frame' for this job."
            ),
        )
    return ValidatorCollector(entries, config)


def collect_batch_validation(
    collector: ValidatorCollector | None, table: str, masked_batch: pa.RecordBatch
) -> None:
    """Check one masked batch; a no-op without validators, so both drivers call it unconditionally."""
    if collector is not None:
        collector.add_batch(table, masked_batch)


def finish_validation(
    collector: ValidatorCollector | None, quality_metrics: dict[str, Any]
) -> None:
    """Stamp `quality_metrics["validation"]`, then fail closed on any finding.

    Runs before the sink publish so a failed validation commits nothing.
    """
    if collector is None:
        return
    report = collector.assemble()
    quality_metrics["validation"] = {"validators": dataclasses.asdict(report)}
    if not report.passed:
        from decoy_engine.errors import ValidatorFailedError

        raise ValidatorFailedError(report)
