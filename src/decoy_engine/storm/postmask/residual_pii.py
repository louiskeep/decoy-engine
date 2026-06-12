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

Source comparison (audit C1 fix, 2026-06-12): when ``source_frames``
is supplied, every detector-flagged column is additionally compared
POSITIONALLY against its source column and the base severity can be
escalated (never lowered). The signal is ``source_identity_rate`` --
the fraction of aligned non-null rows where output == source. Value-set
membership is deliberately NOT a signal: shuffle re-emits 100% of
source values by design, categorical(from_profile) resamples the source
category set, and reference draws from the parent domain, so membership
false-positives on exactly the strategies that are working correctly.
The substitution-vs-value-reuse split mirrors
``validation/post/_checks/_leakage.py`` (``_VALUE_REUSE_STRATEGIES``):
substitution strategies escalate on majority positional identity;
value-reuse strategies escalate only on FULL positional identity (the
mask provably did not move anything); constant-source columns
(nunique <= 1) never escalate because identity is meaningless there.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.storm.detectors import run_all_detectors
from decoy_engine.storm.postmask.types import ResidualPIIFinding, Severity

# Strategies whose output is EXPECTED to look like real PII. A detector
# hit on a column masked with one of these is informational, not a
# warning.
_PRODUCES_PII_LIKE_VALUES: frozenset[str] = frozenset(
    {
        "faker",
        "formula",
        "categorical",
        "reference",
        "date_shift",
    }
)

# Strategies that DESTROY detector patterns. A surviving hit on a column
# masked with one of these is a fail (the mask did not work).
# QA-4 F1 (2026-06-01): text_redact was missing from this set.
# text_redact replaces matched PII spans with a fixed token; any
# detector still hitting after text_redact ran is a failure of the
# detector coverage and must surface as 'fail' (not 'warning').
_DESTROYS_PATTERN: frozenset[str] = frozenset(
    {
        "hash",
        "redact",
        "text_redact",
        "bucketize",
    }
)

# Dennis M12 fix (2026-06-01): strategies that explicitly leave the
# column untouched. A detector hit on these is expected -- the user
# deliberately marked the column as a passthrough -- so it's classified
# 'info' (a confirmation), not 'warning'. Without this, every
# passthrough column that matched any detector emitted a false-positive
# residual-PII warning.
_NO_OP_BY_DESIGN: frozenset[str] = frozenset(
    {
        "passthrough",
    }
)

# Strategies that REPLACE values per-cell: positional identity with the
# source at scale means the mask did not fire. Mirrors the substitution
# side of validation/post/_checks/_leakage.py.
_SUBSTITUTION_STRATEGIES: frozenset[str] = frozenset(
    {
        "faker",
        "date_shift",
        "fpe",
    }
)

# Strategies that legitimately re-emit source VALUES (in new positions /
# proportions): only FULL positional identity is a failure signal.
# Mirrors _VALUE_REUSE_STRATEGIES in _leakage.py, plus reference (parent
# domain == source FK domain by design).
_REUSES_SOURCE_VALUES: frozenset[str] = frozenset(
    {
        "shuffle",
        "categorical",
        "reference",
    }
)

# Escalation thresholds for substitution strategies and unconfigured
# columns. Faker/FPE positional self-maps are near-impossible at scale,
# so >=50% identity means the mask mostly did not fire; 5-50% surfaces
# as a non-blocking warning (partial failure, operator review). The
# min-rows floor keeps tiny frames from failing on coincidence.
_LEAK_FAIL_RATE = 0.5
_LEAK_WARN_RATE = 0.05
_LEAK_FAIL_MIN_ROWS = 3

_SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "fail": 2, "error": 3}


def check_residual_pii(
    output_frames: dict[str, pd.DataFrame],
    config: dict[str, Any],
    *,
    source_frames: dict[str, pd.DataFrame] | None = None,
) -> list[ResidualPIIFinding]:
    """Scan masked output for surviving PII patterns.

    Args:
        output_frames: ``{table_name: post-mask DataFrame}``.
        config: the validated pipeline config dict (from
            ``PipelineConfig.model_dump()``).
        source_frames: ``{table_name: pre-mask DataFrame}``. When
            supplied, detector-flagged columns are compared positionally
            against source and severity escalates on output==source
            identity (see module docstring). When ``None``, behavior
            degrades to detector-plus-strategy classification only --
            real leaks on PII-like-producer strategies are then
            invisible, so callers that have the source should pass it.

    Returns:
        List of ResidualPIIFinding. Empty list if nothing matched.
        Severity is classified based on the column's configured
        masking strategy (or absence of one), then escalated by the
        source comparison when evidence of a leak exists.
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

            identity = None
            if source_frames is not None and configured not in _NO_OP_BY_DESIGN:
                identity = _source_identity(source_frames, table_name, col_name, series)
            if identity is not None:
                rate, identical_count, src_distinct = identity
                override = _escalate(
                    configured=configured,
                    identity_rate=rate,
                    identical_count=identical_count,
                    src_distinct=src_distinct,
                    detector_confidence=top.confidence,
                    detector_id=top.detector_id,
                )
                if override is not None and (
                    _SEVERITY_ORDER[override[0]] > _SEVERITY_ORDER[severity]
                ):
                    severity, message = override

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
                    source_identity_rate=identity[0] if identity is not None else None,
                    source_compared=identity is not None,
                )
            )

    return findings


def _source_identity(
    source_frames: dict[str, pd.DataFrame],
    table_name: str,
    col_name: str,
    out_col: pd.Series,
) -> tuple[float, int, int] | None:
    """Positional out==src identity over comparable rows, or None.

    Returns ``(identity_rate, identical_count, source_distinct)``. None
    when the source lacks the table/column, row counts differ (a
    row-count mismatch is policy_validation's M13 concern, not a leak
    signal), or the comparison itself fails. Must never raise: the
    runner's per-check catch-all would otherwise collapse every
    residual finding into a single error row.
    """
    try:
        src_df = source_frames.get(table_name)
        if src_df is None or col_name not in src_df.columns:
            return None
        src_col = src_df[col_name]
        if len(src_col) != len(out_col):
            return None
        # Positional comparison regardless of index shape (QA-4 F6
        # precedent in policy_validation).
        src = src_col.reset_index(drop=True).astype(object)
        out = out_col.reset_index(drop=True).astype(object)
        both_present = src.notna() & out.notna()
        comparable = int(both_present.sum())
        if comparable == 0:
            return None
        identical = int((src[both_present] == out[both_present]).sum())
        src_distinct = int(src.nunique(dropna=True))
        return (identical / comparable, identical, src_distinct)
    except Exception:
        return None


def _escalate(
    *,
    configured: str | None,
    identity_rate: float,
    identical_count: int,
    src_distinct: int,
    detector_confidence: str,
    detector_id: str,
) -> tuple[Severity, str] | None:
    """Severity override from source-identity evidence, or None.

    Decision table (module docstring): substitution strategies fail at
    majority identity and warn at the 5% floor; value-reuse strategies
    and everything else fail only at FULL identity; unconfigured
    columns fail at majority identity but only on a high-confidence
    detector hit (loose detectors like mrn/license_num must not fail
    verbatim-preserved ID columns on a medium-confidence guess).
    Constant-source columns never escalate.
    """
    if src_distinct <= 1:
        return None

    full_identity = identity_rate == 1.0

    if configured is None:
        if (
            identity_rate >= _LEAK_FAIL_RATE
            and identical_count >= _LEAK_FAIL_MIN_ROWS
            and detector_confidence == "high"
        ):
            return (
                "fail",
                f"column matched {detector_id!r}, was not configured to be "
                f"masked, and is positionally identical to source for "
                f"{identity_rate:.0%} of rows -- real values shipped to the "
                "output unmasked.",
            )
        return None

    if configured in _SUBSTITUTION_STRATEGIES:
        if identity_rate >= _LEAK_FAIL_RATE and identical_count >= _LEAK_FAIL_MIN_ROWS:
            return (
                "fail",
                f"column matched {detector_id!r} and output is positionally "
                f"identical to source for {identity_rate:.0%} of comparable "
                f"rows; strategy {configured!r} should have replaced these "
                "values -- the mask did not fire.",
            )
        if identity_rate >= _LEAK_WARN_RATE:
            return (
                "warning",
                f"column matched {detector_id!r} and {identity_rate:.0%} of "
                f"output rows kept their source value under strategy "
                f"{configured!r} -- partial mask failure, verify the output.",
            )
        return None

    if configured == "formula":
        # Formula may legitimately incorporate / echo source fragments;
        # only full-column identity (an identity formula on a PII
        # column) is certain failure.
        if full_identity:
            return (
                "fail",
                f"column matched {detector_id!r} and the formula output is "
                "identical to source for every row -- the expression does "
                "not transform the value.",
            )
        if identity_rate >= _LEAK_FAIL_RATE:
            return (
                "warning",
                f"column matched {detector_id!r} and {identity_rate:.0%} of "
                "formula output rows equal their source value -- verify the "
                "expression actually masks.",
            )
        return None

    # Value-reuse strategies (shuffle/categorical/reference) and any
    # other configured strategy: full positional identity means the
    # mask provably moved nothing.
    if full_identity:
        return (
            "fail",
            f"column matched {detector_id!r} and output is positionally "
            f"identical to source for every row; strategy {configured!r} "
            "produced no movement -- the mask did not fire.",
        )
    return None


def _classify(detector_id: str, configured: str | None) -> tuple[Severity, str]:
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
    if configured in _REUSES_SOURCE_VALUES:
        # Shuffle re-emits the source value multiset by design, so a
        # detector hit on shuffled real values is the expected outcome
        # (categorical/reference already classify as info via
        # _PRODUCES_PII_LIKE_VALUES). The source-identity backstop in
        # _escalate fails the column if the shuffle moved nothing.
        return (
            "info",
            f"column matched {detector_id!r}; expected because the configured "
            f"strategy {configured!r} re-emits source values in new positions.",
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
