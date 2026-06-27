"""Validator registry for the SP-05 job-level validator framework (P5.INFRA.4).

The registry maps validator names to their callable implementations. Each
callable has the signature::

    fn(
        outputs: dict[str, pa.Table],
        entry: dict[str, Any],
        config: dict[str, Any],
    ) -> tuple[ValidatorFinding, ...]

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
from decoy_engine.validators._types import ValidationReport, ValidatorFinding

_ValidatorFn = Callable[
    [dict[str, pa.Table], dict[str, Any], dict[str, Any]],
    tuple[ValidatorFinding, ...],
]

_REGISTRY: dict[str, _ValidatorFn] = {
    "luhn": validate_luhn,
    "npi": validate_npi,
    "iban": validate_iban,
    "vin": validate_vin,
    "fk_intact": validate_fk_intact,
    "no_orphan_children": validate_no_orphan_children,
}


def validate(
    outputs: dict[str, pa.Table],
    config: dict[str, Any],
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
        findings = fn(outputs, entry, config)
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
