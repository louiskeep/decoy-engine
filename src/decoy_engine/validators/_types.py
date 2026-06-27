"""Frozen report types for the SP-05 job-level validator framework (P5.INFRA.4).

ValidationReport is the single frozen artifact produced by
``decoy_engine.validators.validate()``. It carries the aggregate pass/fail
verdict, the names of every validator that ran, every per-column or
per-relationship finding, and the wall-clock elapsed time.

Design constraints (per CLAUDE.md and engineering best-practices section 2.1):

  - Validation never mutates output. validate() accepts ``pa.Table`` objects
    read-only and returns a frozen report.
  - ValidationReport is a frozen dataclass; no attribute may be set after
    construction. The test at
    tests/unit/validators/test_report_frozen.py::test_frozen_instance_error_on_setattr
    guards this invariant mechanically.
  - Reports are frozen snapshots, not live views. Callers serialise via
    ``dataclasses.asdict(report)`` for the evidence manifest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValidatorFinding:
    """One failure record produced by a single validator run.

    A finding is ALWAYS a failure: validators that produce no findings have
    passed. The ``failing_row_indices`` tuple carries 0-based row positions
    into the output ``pa.Table`` for the named ``(table, column)`` pair.
    FK-level validators (fk_intact, no_orphan_children) set ``column`` to
    None and populate ``failing_row_indices`` with the child table's rows.

    Args:
        validator: Validator name (e.g. ``"luhn"``, ``"fk_intact"``).
        table: Output table name the finding refers to.
        column: Column name within ``table``, or None for table-level validators.
        failing_row_indices: 0-based row indices into the output table that
            failed validation.
        detail: Human-readable description of the failure.
    """

    validator: str
    table: str
    column: str | None
    failing_row_indices: tuple[int, ...]
    detail: str = ""


@dataclass(frozen=True)
class ValidationReport:
    """Frozen aggregate produced by ``validate(output, config)``.

    Never mutated after construction. Callers must not attempt to set
    attributes; doing so raises ``dataclasses.FrozenInstanceError`` because
    the dataclass is declared with ``frozen=True``.

    Serialise to a plain dict for the evidence manifest via
    ``dataclasses.asdict(report)``; the manifest writer stores the result
    under ``quality_metrics["validation"]["validators"]``.

    Args:
        passed: True iff every configured validator found zero failures.
        validators_run: Ordered tuple of validator names that were executed.
        findings: Tuple of ValidatorFinding for every failure found (empty
            when ``passed`` is True).
        elapsed_ms: Wall-clock time in milliseconds for the full validation
            phase.
    """

    passed: bool
    validators_run: tuple[str, ...]
    findings: tuple[ValidatorFinding, ...]
    elapsed_ms: float


@dataclass(frozen=True)
class QuarantineSummary:
    """Frozen evidence record for the quarantine phase of a run.

    Written to ``quality_metrics["quarantine"]`` in the evidence manifest
    when at least one row was quarantined. When no rows triggered quarantine,
    this record is not written.

    Args:
        enabled: Whether the quarantine block was enabled for this run.
        output_path: Path where the quarantine JSONL file was written.
        counts_by_trigger: Per-finding row counts keyed by trigger name.
            A row failing two validators contributes 1 to each matching
            trigger count, so the sum of counts_by_trigger values may
            exceed total_quarantined. This is intentional: per-trigger
            counts reveal which validators fired most, while
            total_quarantined reflects the distinct rows actually removed
            from the main output.
        total_quarantined: Count of DISTINCT rows removed from the main
            output (deduplicated by (table, row_index)). Equals the
            number of lines written to the quarantine JSONL file.
    """

    enabled: bool
    output_path: str
    counts_by_trigger: dict[str, int]
    total_quarantined: int
