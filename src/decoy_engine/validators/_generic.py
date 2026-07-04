"""Generic utility validators: regex_match, column_in_set (Sprint 2 honesty
pack, S3 / p5-j-validators-extended).

Pattern: Great Expectations `expect_column_values_to_match_regex` and
`expect_column_values_to_be_in_set` (great-expectations/great_expectations,
Apache-2.0). Both validators follow the `_catalog.py` per-column shape: read
outputs from `entry["columns"]`, skip nulls, one `ValidatorFinding` per
column with at least one failing row.

Trust boundary (trap T8): `pattern` and `allowed_values` are operator-supplied
pipeline config, the same trust surface as the rest of the YAML. No untrusted-
input regex hardening (e.g. ReDoS sandboxing) is in scope; a malicious config
author already controls the whole pipeline.
"""

from __future__ import annotations

import re
from typing import Any

import pyarrow as pa

from decoy_engine.validators._types import ValidatorFinding


def _columns_map(entry: dict[str, Any]) -> dict[str, list[str]]:
    return entry.get("columns") or {}


def validate_regex_match(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
    *,
    sources: dict[str, pa.Table] | None = None,
) -> tuple[ValidatorFinding, ...]:
    """regex_match: every non-null value in the named columns matches `pattern`.

    Match rule is `re.fullmatch` on `str(value)` (whole-cell assertion); an
    operator wanting substring semantics writes `.*pattern.*`. Nulls are
    skipped (a null is not a pattern violation).

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block; requires `params.pattern` (str).
        config: Full pipeline config dict (unused).
        sources: Unused by this validator (registry contract, D2).

    Returns:
        Tuple of ValidatorFinding for each column with non-matching rows.

    Raises:
        ValueError: `params.pattern` missing, or fails to compile.
    """
    params: dict[str, Any] = entry.get("params") or {}
    pattern_raw = params.get("pattern")
    if not pattern_raw or not isinstance(pattern_raw, str):
        raise ValueError(
            "regex_match validator requires params.pattern (a non-empty regex string)."
        )
    try:
        compiled = re.compile(pattern_raw)
    except re.error as exc:
        raise ValueError(
            f"regex_match validator: params.pattern is not a valid regex: {exc}"
        ) from exc

    findings: list[ValidatorFinding] = []
    for table_name, col_names in _columns_map(entry).items():
        table = outputs.get(table_name)
        if table is None:
            continue
        for col_name in col_names:
            if col_name not in table.schema.names:
                continue
            values = table.column(col_name).to_pylist()
            failing = [
                i
                for i, val in enumerate(values)
                if val is not None and compiled.fullmatch(str(val)) is None
            ]
            if failing:
                findings.append(
                    ValidatorFinding(
                        validator="regex_match",
                        table=table_name,
                        column=col_name,
                        failing_row_indices=tuple(failing),
                        detail=(
                            f"{len(failing)} row(s) in {table_name}.{col_name} "
                            f"do not match pattern {pattern_raw!r}"
                        ),
                    )
                )
    return tuple(findings)


def validate_column_in_set(
    outputs: dict[str, pa.Table],
    entry: dict[str, Any],
    config: dict[str, Any],
    *,
    sources: dict[str, pa.Table] | None = None,
) -> tuple[ValidatorFinding, ...]:
    """column_in_set: every value in the named columns belongs to `allowed_values`.

    Comparison canonicalizes both sides via `str()`, consistent with the
    check-digit validators (`_catalog.py`). Nulls are skipped by default;
    `params.allow_null: false` flips null cells to findings (GE symmetry).

    Args:
        outputs: Read-only pipeline outputs.
        entry: Config block; requires `params.allowed_values` (non-empty list).
        config: Full pipeline config dict (unused).
        sources: Unused by this validator (registry contract, D2).

    Returns:
        Tuple of ValidatorFinding for each column with out-of-set rows.

    Raises:
        ValueError: `params.allowed_values` missing or empty.
    """
    params: dict[str, Any] = entry.get("params") or {}
    allowed_raw = params.get("allowed_values")
    if not allowed_raw or not isinstance(allowed_raw, list):
        raise ValueError(
            "column_in_set validator requires params.allowed_values "
            "(a non-empty list of allowed values)."
        )
    allowed: set[str] = {str(v) for v in allowed_raw}
    allow_null = bool(params.get("allow_null", True))

    findings: list[ValidatorFinding] = []
    for table_name, col_names in _columns_map(entry).items():
        table = outputs.get(table_name)
        if table is None:
            continue
        for col_name in col_names:
            if col_name not in table.schema.names:
                continue
            values = table.column(col_name).to_pylist()
            failing: list[int] = []
            for i, val in enumerate(values):
                if val is None:
                    if not allow_null:
                        failing.append(i)
                    continue
                if str(val) not in allowed:
                    failing.append(i)
            if failing:
                findings.append(
                    ValidatorFinding(
                        validator="column_in_set",
                        table=table_name,
                        column=col_name,
                        failing_row_indices=tuple(failing),
                        detail=(
                            f"{len(failing)} row(s) in {table_name}.{col_name} "
                            f"are not in the allowed set ({len(allowed)} value(s))"
                        ),
                    )
                )
    return tuple(findings)
