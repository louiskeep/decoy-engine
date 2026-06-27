"""Check-digit validators: luhn, npi, iban, vin (SP-05 / P5.INFRA.4).

Each validator is a callable with signature::

    def validate_<name>(
        outputs: dict[str, pa.Table],
        entry: dict[str, Any],
        config: dict[str, Any],
    ) -> tuple[ValidatorFinding, ...]

``outputs`` is the immutable read-only output from the pipeline. ``entry`` is
the per-validator config block (``{"name": "luhn", "columns": {"t": ["cc"]}}``).
``config`` is the full pipeline config dict (for FK validators that read the
``relationships:`` block).

All four check-digit validators delegate entirely to
``decoy_engine.checksums.validate(scheme, value)`` (SP-04 / P5.INFRA.1).
No check-digit logic is re-implemented here.

Null values in any column are skipped: a null is not a Luhn/NPI/IBAN/VIN
violation. This matches the engine's treatment of nullable FKs.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

import decoy_engine.checksums as _checksums
from decoy_engine.validators._types import ValidatorFinding

# ---------------------------------------------------------------------------
# Check-digit validators (luhn / npi / iban / vin)
# ---------------------------------------------------------------------------


def _check_digit_validator(
    scheme: str,
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """Generic column-level check-digit validator.

    Iterates over every (table, column) pair in ``entry["columns"]``, reads
    each non-null value from the output table, and calls
    ``checksums.validate(scheme, value)``. Returns one ``ValidatorFinding``
    per column that contains at least one failing row.

    Null values are skipped (they are not treated as check-digit failures).

    Args:
        scheme: Checksum scheme name passed to ``checksums.validate()``.
        outputs: Read-only pipeline output tables; must not be mutated.
        entry: Per-validator config block with ``columns`` sub-key.

    Returns:
        Tuple of ValidatorFinding (empty tuple when all values pass).
    """
    columns_map: dict[str, list[str]] = entry.get("columns") or {}
    findings: list[ValidatorFinding] = []

    for table_name, col_names in columns_map.items():
        table = outputs.get(table_name)
        if table is None:
            continue
        for col_name in col_names:
            if col_name not in table.schema.names:
                continue
            col: pa.ChunkedArray = table.column(col_name)
            values: list[Any] = col.to_pylist()
            failing: list[int] = []
            for i, val in enumerate(values):
                if val is None:
                    continue  # nulls are not a check-digit violation
                try:
                    valid = _checksums.validate(scheme, str(val))
                except ValueError:
                    valid = False
                if not valid:
                    failing.append(i)
            if failing:
                findings.append(
                    ValidatorFinding(
                        validator=scheme,
                        table=table_name,
                        column=col_name,
                        failing_row_indices=tuple(failing),
                        detail=(
                            f"{len(failing)} row(s) in {table_name}.{col_name} "
                            f"failed {scheme} check-digit validation"
                        ),
                    )
                )

    return tuple(findings)


def validate_luhn(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """luhn: validates Luhn-checksum integrity per column.

    Delegates to ``checksums.validate('luhn', value)`` (SP-04 / P5.INFRA.1),
    which uses python-stdnum 2.2 ``stdnum.luhn`` (Luhn 1954, US Patent
    2,950,048). No check-digit logic is implemented here.

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block for this validator instance.
        config: Full pipeline config dict (unused by this validator).

    Returns:
        Tuple of ValidatorFinding for each column with Luhn failures.
    """
    return _check_digit_validator("luhn", outputs, entry)


def validate_npi(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """npi: validates US National Provider Identifier check digit per column.

    Delegates to ``checksums.validate('npi', value)`` (SP-04 / P5.INFRA.1),
    which implements the CMS NPPES NPI Check Digit Procedure (2008). No
    check-digit logic is implemented here.

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block for this validator instance.
        config: Full pipeline config dict (unused by this validator).

    Returns:
        Tuple of ValidatorFinding for each column with NPI failures.
    """
    return _check_digit_validator("npi", outputs, entry)


def validate_iban(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """iban: validates IBAN mod-97 check digits per column.

    Delegates to ``checksums.validate('iban', value)`` (SP-04 / P5.INFRA.1),
    which uses python-stdnum 2.2 ``stdnum.iban`` (ISO 13616 / ISO 7064
    mod-97). No check-digit logic is implemented here.

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block for this validator instance.
        config: Full pipeline config dict (unused by this validator).

    Returns:
        Tuple of ValidatorFinding for each column with IBAN failures.
    """
    return _check_digit_validator("iban", outputs, entry)


def validate_vin(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
) -> tuple[ValidatorFinding, ...]:
    """vin: validates VIN ISO 3779 check character per column.

    Delegates to ``checksums.validate('vin', value)`` (SP-04 / P5.INFRA.1),
    which implements NHTSA 49 CFR Part 565 Appendix B. No check-digit logic
    is implemented here.

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block for this validator instance.
        config: Full pipeline config dict (unused by this validator).

    Returns:
        Tuple of ValidatorFinding for each column with VIN failures.
    """
    return _check_digit_validator("vin", outputs, entry)
