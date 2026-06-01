"""Residual PII check (Reframe-A).

Re-runs the existing Storm detectors over the MASKED OUTPUT to find
columns that still match a PII pattern. Distinguishes three cases:

1. Detector hit on a column that was NOT configured to be masked.
   The operator likely forgot a column. Severity: warning.
2. Detector hit on a column that WAS configured to be masked to a
   strategy that legitimately produces detectable values (faker name
   on a person_name column produces person_name detector hits; this
   is the expected outcome). Severity: info.
3. Detector hit on a column that WAS configured to be masked to a
   strategy that should DESTROY the pattern (hash, redact, bucketize),
   but the pattern survived. Indicates the mask did not fire or
   failed silently. Severity: fail.

Strategies that ARE expected to produce detector hits (i.e. legitimate
realistic-fake): faker, formula, categorical (when categories are
realistic-looking), reference (when parent values are realistic), date_shift.

Strategies that DESTROY the detector pattern: hash, redact, bucketize,
truncate (when truncation depth is small enough that the pattern can no
longer match). FPE is a special case -- it preserves character class
distribution but scrambles values, so it might or might not produce
detector hits depending on the source's natural format.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.storm.detectors import run_all_detectors
from decoy_engine.storm.postmask.types import ResidualPIIFinding


# Strategies whose output is EXPECTED to look like real PII. A detector
# hit on a column masked with one of these is informational, not a
# warning.
_PRODUCES_PII_LIKE_VALUES: frozenset[str] = frozenset({
    "faker",
    "formula",
    "categorical",
    "reference",
    "date_shift",
})

# Strategies that DESTROY detector patterns. A surviving hit on a column
# masked with one of these is a fail (the mask did not work).
# QA-4 F1 (2026-06-01): text_redact was missing from this set.
# text_redact replaces matched PII spans with a fixed token; any
# detector still hitting after text_redact ran is a failure of the
# detector coverage and must surface as 'fail' (not 'warning').
_DESTROYS_PATTERN: frozenset[str] = frozenset({
    "hash",
    "redact",
    "text_redact",
    "bucketize",
})

# Dennis M12 fix (2026-06-01): strategies that explicitly leave the
# column untouched. A detector hit on these is expected -- the user
# deliberately marked the column as a passthrough -- so it's classified
# 'info' (a confirmation), not 'warning'. Without this, every
# passthrough column that matched any detector emitted a false-positive
# residual-PII warning.
_NO_OP_BY_DESIGN: frozenset[str] = frozenset({
    "passthrough",
})


def check_residual_pii(
    output_frames: dict[str, pd.DataFrame],
    config: dict[str, Any],
) -> list[ResidualPIIFinding]:
    """Scan masked output for surviving PII patterns.

    Args:
        output_frames: ``{table_name: post-mask DataFrame}``.
        config: the validated pipeline config dict (from
            ``PipelineConfig.model_dump()``).

    Returns:
        List of ResidualPIIFinding. Empty list if nothing matched.
        Severity is classified based on the column's configured
        masking strategy (or absence of one).
    """
    findings: list[ResidualPIIFinding] = []

    # Build a lookup of configured masking strategy per (table, column).
    # Per-table strategy = column-level "strategy" key in tables[].columns[].
    strategy_by_col: dict[tuple[str, str], str] = {}
    for table_cfg in config.get("tables") or []:
        table_name = table_cfg.get("name")
        if not isinstance(table_name, str):
            continue
        for col_cfg in table_cfg.get("columns") or []:
            col_name = col_cfg.get("name")
            strategy = col_cfg.get("strategy")
            if isinstance(col_name, str) and isinstance(strategy, str):
                strategy_by_col[(table_name, col_name)] = strategy

    for table_name, df in output_frames.items():
        for col_name in df.columns:
            series = df[col_name]
            matches = run_all_detectors(series, col_name)
            if not matches:
                continue
            # The dominant detector match (highest match_rate; first in list
            # per run_all_detectors's sorted-descending order).
            top = matches[0]
            configured = strategy_by_col.get((table_name, col_name))
            severity, message = _classify(top.detector_id, configured)
            findings.append(
                ResidualPIIFinding(
                    table=table_name,
                    column=col_name,
                    detector_id=top.detector_id,
                    match_rate=top.match_rate,
                    severity=severity,
                    configured_strategy=configured,
                    sample_match_count=int(top.match_rate * len(series)),
                    message=message,
                )
            )

    return findings


def _classify(detector_id: str, configured: str | None) -> tuple[str, str]:
    """Classify a detector hit + return (severity, human-readable message).

    Three buckets per the module docstring:
      - no strategy configured: warning (operator may have forgotten)
      - PII-like-producer strategy: info (expected)
      - pattern-destroying strategy: fail (mask didn't work)
      - other strategy (passthrough, fpe, shuffle, etc.): warning
        (might be a leak depending on context; let the operator decide)
    """
    if configured is None:
        return (
            "warning",
            f"column matched {detector_id!r} but was not configured to be "
            "masked. Verify whether this column should be sensitive.",
        )
    if configured in _NO_OP_BY_DESIGN:
        # Dennis M12 fix: passthrough is an explicit operator decision
        # to leave the column unchanged. A detector hit is expected.
        return (
            "info",
            f"column matched {detector_id!r}; the configured strategy "
            f"{configured!r} is a no-op by design, so the value survives "
            "unchanged.",
        )
    if configured in _PRODUCES_PII_LIKE_VALUES:
        return (
            "info",
            f"column matched {detector_id!r}; expected because the configured "
            f"strategy {configured!r} produces realistic-looking values.",
        )
    if configured in _DESTROYS_PATTERN:
        return (
            "fail",
            f"column matched {detector_id!r} but was configured for strategy "
            f"{configured!r}, which should destroy the pattern. The mask "
            "may not have fired -- inspect the output.",
        )
    return (
        "warning",
        f"column matched {detector_id!r}; configured strategy {configured!r} "
        "does not have a documented residual-PII expectation. Verify the "
        "output is acceptable.",
    )
