"""Validator registry for the SP-05 job-level validator framework (P5.INFRA.4).

The registry maps validator names to their callable implementations. Each
callable has the signature::

    fn(
        outputs: dict[str, pa.Table],
        entry: dict[str, Any],
        config: dict[str, Any],
        *,
        sources: dict[str, pa.Table] | None = None,
    ) -> tuple[ValidatorFinding, ...]

Sprint 2 honesty-pack S1 (2026-07-04, D2): ``sources`` is an additive,
keyword-only parameter carrying the caller-loaded pre-mask source tables
(``run_pipeline``'s ``caller_sources``, always in scope even for pure-generate
configs, where it is ``{}``). It exists so a validator that compares output
values against source values (leak_check, S2) can do so without a second
plumbing mechanism. The five pre-existing validators accept and ignore it.

Calling ``validate(outputs, config)`` runs every entry in the ``validators:``
config block, collects findings from each, and returns a frozen
``ValidationReport``.

Adding a validator:
  1. Implement the function (see _catalog.py or _fk_validators.py).
  2. Register it in ``_REGISTRY`` below.
  3. Write a happy + deny-path test in tests/unit/validators/test_catalog.py.

The registry raises ``ValueError`` for unknown validator names at the point of
the ``validate()`` call so the operator gets a clear error at runtime rather
than a silent no-op.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pyarrow as pa

from decoy_engine.validators._catalog import (
    validate_iban,
    validate_luhn,
    validate_npi,
    validate_vin,
)
from decoy_engine.validators._fk_validators import (
    validate_fk_intact,
    validate_no_orphan_children,
)
from decoy_engine.validators._generic import validate_column_in_set, validate_regex_match
from decoy_engine.validators._leak_check import validate_leak_check
from decoy_engine.validators._relationship_checks import (
    validate_parent_window_respected,
    validate_reconciliation_holds,
)
from decoy_engine.validators._types import ValidationReport, ValidatorFinding

_ValidatorFn = Callable[..., tuple[ValidatorFinding, ...]]

_REGISTRY: dict[str, _ValidatorFn] = {
    "luhn": validate_luhn,
    "npi": validate_npi,
    "iban": validate_iban,
    "vin": validate_vin,
    "fk_intact": validate_fk_intact,
    "no_orphan_children": validate_no_orphan_children,
    "leak_check": validate_leak_check,
    "regex_match": validate_regex_match,
    "column_in_set": validate_column_in_set,
    "parent_window_respected": validate_parent_window_respected,
    "reconciliation_holds": validate_reconciliation_holds,
}


def validate(
    outputs: dict[str, pa.Table],
    config: dict[str, Any],
    *,
    sources: dict[str, pa.Table] | None = None,
) -> ValidationReport:
    """Run all configured validators and return a frozen ValidationReport.

    Reads the ``validators:`` list from ``config``, dispatches each entry to
    its registered implementation, collects all findings, and returns a
    ``ValidationReport`` whose ``passed`` attribute is ``True`` iff no findings
    were produced.

    This function is read-only with respect to ``outputs``: it does not mutate
    any ``pa.Table`` it receives (per CLAUDE.md: "Validation never mutates").

    Args:
        outputs: Read-only pipeline output tables, keyed by table name.
        config: Pipeline config dict (the validated dump from
            ``PipelineConfig.model_validate``). Validators are read from the
            ``validators:`` list.
        sources: Read-only caller-loaded pre-mask source tables, keyed by
            table name (Sprint 2 honesty pack, D2). ``None`` when the caller
            has no sources to offer (identical to passing ``{}``). Used by
            ``leak_check`` to compare output values against their source.

    Returns:
        Frozen ValidationReport.

    Raises:
        ValueError: If a validator entry names an unknown validator.
    """
    validator_entries: list[dict[str, Any]] = config.get("validators") or []
    all_findings: list[ValidatorFinding] = []
    validators_run: list[str] = []

    t0 = time.perf_counter()

    for entry in validator_entries:
        if not isinstance(entry, dict):
            raise ValueError(
                f"each validators: entry must be a dict with a 'name' key, "
                f"got {type(entry).__name__!r}: {entry!r}"
            )
        name: str = entry.get("name") or ""
        fn = _REGISTRY.get(name)
        if fn is None:
            raise ValueError(f"unknown validator {name!r}. Known validators: {sorted(_REGISTRY)}")
        findings = fn(outputs, entry, config, sources=sources)
        all_findings.extend(findings)
        if name not in validators_run:
            validators_run.append(name)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    passed = len(all_findings) == 0
    return ValidationReport(
        passed=passed,
        validators_run=tuple(validators_run),
        findings=tuple(all_findings),
        elapsed_ms=elapsed_ms,
    )
