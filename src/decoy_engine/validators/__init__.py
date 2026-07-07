"""Job-level validator framework for Decoy engine (SP-05 / P5.INFRA.4).

Public API::

    from decoy_engine.validators import validate
    report = validate(outputs, config)   # -> ValidationReport

``validate()`` is the single entry point. It reads the ``validators:`` block
from the pipeline config, runs each configured validator against the pipeline
outputs (read-only), and returns a frozen ``ValidationReport``.

Built-in validators
-------------------
luhn
    Luhn mod-10 check digit per column. Delegates to
    ``checksums.validate('luhn', value)`` (SP-04).
npi
    US National Provider Identifier check digit per column. Delegates to
    ``checksums.validate('npi', value)`` (SP-04).
iban
    IBAN ISO 13616 mod-97 check digits per column. Delegates to
    ``checksums.validate('iban', value)`` (SP-04).
vin
    VIN ISO 3779 check character per column. Delegates to
    ``checksums.validate('vin', value)`` (SP-04).
fk_intact
    Every non-null child FK value resolves to a parent PK. Uses
    the SDV HMA1 parent-first DAG pattern.
no_orphan_children
    Every child row has a non-null FK value. Uses the SDV HMA1
    parent-first DAG pattern.
leak_check
    Compares output values against source values per column and flags
    residual source values above a ratio threshold (Sprint 2 honesty pack).
regex_match
    Every non-null value matches an operator-supplied regex (whole-cell).
column_in_set
    Every value belongs to an operator-supplied allowed set.
parent_window_respected
    Every child date falls within its parent's declared window (pairs with
    the ``windowed_date`` generate strategy).
reconciliation_holds
    A parent aggregate cell reconciles with its child rows (pairs with the
    ``derived_aggregate`` strategy).

Sprint 2 honesty pack (2026-07-04, D2): ``validate()`` gained a keyword-only
``sources`` parameter carrying the caller-loaded pre-mask source tables, so
validators that compare output against source (leak_check) can do so without
a second plumbing mechanism. Additive; existing callers are unaffected.

Design constraints
------------------
- Validation never mutates output (CLAUDE.md, best-practices section 2.1).
- ValidationReport is frozen (dataclass frozen=True).
- The assertion/deny-path test lands before the implementation
  (tests/unit/validators/test_report_frozen.py).
- Fail-closed by default: a validator failure raises ValidatorFailedError
  unless quarantine is enabled with the validation_fail trigger.
"""

from __future__ import annotations

from decoy_engine.errors import ValidatorFailedError
from decoy_engine.validators._registry import validate
from decoy_engine.validators._types import (
    QuarantineSummary,
    ValidationReport,
    ValidatorFinding,
)

__all__ = [
    "QuarantineSummary",
    "ValidationReport",
    "ValidatorFailedError",
    "ValidatorFinding",
    "validate",
]
