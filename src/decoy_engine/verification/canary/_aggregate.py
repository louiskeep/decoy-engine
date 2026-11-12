"""Aggregation of one canary scan into ``CanarySpanReport`` (F34 E3, invariants I2, I5, I6).

``Totals`` accumulates what the scan loop sees: artifact buckets, row, column and cell counts,
and one finding per ``(artifact, column, mode)``. Every count saturates at ``MAX_COUNT`` and
sets ``capped``. ``build_report`` is the only place in this package that constructs a
``CanarySpanReport``, rejected reports included. It derives the observed reasons from the
counts, then takes status and reasons from ``CanaryVerdictPolicy`` alone. ``check_report``
re-derives both from a finished report, because the E0 constructor checks the fields of a report
but not that they agree with each other (a ``COMPLETE`` report with an unreadable artifact
constructs). ``build_report`` runs it on every report before returning.
"""

from __future__ import annotations

from collections.abc import Callable

from decoy_engine.verification.canary._match import CellTally, PatternCounts
from decoy_engine.verification.canary._policy import CanaryVerdictPolicy
from decoy_engine.verification.canary._types import (
    MAX_COUNT,
    MAX_FINDINGS,
    CanaryArtifactOutcome,
    CanaryBindingSource,
    CanaryColumnFinding,
    CanaryMatchMode,
    CanaryNormalizationSchema,
    CanaryObservedReason,
    CanaryReason,
    CanarySpanReport,
    CanarySpanReportInvalid,
    CanarySpanReportSchema,
    CanaryStatus,
)

_BUCKET = {o: f"{o.value}_artifact_count" for o in CanaryArtifactOutcome}
_COUNTS = [
    "input_canary_count",
    "accepted_canary_count",
    "exact_pattern_count",
    "normalized_pattern_count",
    "normalization_collision_count",
    "excluded_short_canary_count",
    "requested_artifact_count",
    "scanned_column_count",
    "scanned_row_count",
    "scanned_text_byte_count",
    "oversized_cell_count",
    "uncompared_cell_count",
    "affected_cell_count",
    "finding_count",
    *_BUCKET.values(),
]
_MODES = (
    ("exact_cell", CanaryMatchMode.EXACT_CELL),
    ("exact_span", CanaryMatchMode.EXACT_SPAN),
    ("nfc_whitespace_span", CanaryMatchMode.NFC_WHITESPACE_SPAN),
)
Key = tuple[int, int, CanaryMatchMode]


class Totals:
    """Mutable accumulator for one scan call; never shared between calls."""

    def __init__(self) -> None:
        self.capped = False
        self._n = dict.fromkeys(_COUNTS, 0)
        self._cells: dict[Key, int] = {}
        self._cells_capped: set[Key] = set()

    def count(self, name: str) -> int:
        return self._n[name]

    def _add(self, name: str, n: int) -> None:
        total = self._n[name] + n
        if total > MAX_COUNT:
            total, self.capped = MAX_COUNT, True
        self._n[name] = total

    def outcome(self, outcome: CanaryArtifactOutcome) -> None:
        self._add(_BUCKET[outcome], 1)

    def columns(self, n: int) -> None:
        self._add("scanned_column_count", n)

    def rows(self, n: int) -> None:
        self._add("scanned_row_count", n)

    def cells(
        self, artifact: int, column: int, tally: CellTally, *, uncompared: int, text_bytes: int
    ) -> None:
        """One column of one batch: ``uncompared`` counts cells the canonicalizer refused."""
        self._add("oversized_cell_count", tally.oversized)
        self._add("uncompared_cell_count", tally.uncompared + uncompared)
        self._add("scanned_text_byte_count", text_bytes)
        for field_name, mode in _MODES:
            n = getattr(tally, field_name)
            if not n:
                continue
            key = (artifact, column, mode)
            total = self._cells.get(key, 0) + n
            if total > MAX_COUNT:
                total = MAX_COUNT
                self._cells_capped.add(key)
                self.capped = True
            self._cells[key] = total
            self._add("affected_cell_count", n)

    def findings(self) -> list[CanaryColumnFinding]:
        found = [
            CanaryColumnFinding(a, c, m, n, (a, c, m) in self._cells_capped)
            for (a, c, m), n in self._cells.items()
        ]
        return sorted(found, key=CanaryColumnFinding.sort_key)


def _observed(count: Callable[[str], int], inactive: bool) -> frozenset[CanaryObservedReason]:
    o = CanaryObservedReason
    limited = (
        count("resource_limited_artifact_count")
        + count("unreported_artifact_count")
        + count("oversized_cell_count")
    )
    hits = (
        (inactive, o.PUBLICATION_INACTIVE),
        (count("requested_artifact_count") == 0, o.TARGET_UNAVAILABLE),
        (count("mismatched_artifact_count") > 0, o.PUBLICATION_MISMATCH),
        (count("unreadable_artifact_count") > 0, o.ARTIFACT_UNREADABLE),
        (count("unsupported_artifact_count") > 0, o.ARTIFACT_UNSUPPORTED),
        (limited > 0, o.RESOURCE_LIMIT),
        (count("uncompared_cell_count") > 0, o.CELL_UNCOMPARED),
    )
    return frozenset(reason for hit, reason in hits if hit)


def _verdict(
    count: Callable[[str], int], binding: CanaryBindingSource, *, inactive: bool, rejected: bool
) -> tuple[CanaryStatus, tuple[CanaryReason, ...]]:
    observed = _observed(count, inactive)
    status = CanaryVerdictPolicy.status(
        validation_failed=rejected, binding_source=binding, observed=observed
    )
    reasons = CanaryVerdictPolicy.reasons(
        validation_failed=rejected,
        binding_source=binding,
        observed=observed,
        has_findings=count("finding_count") > 0,
        has_short_excluded=count("excluded_short_canary_count") > 0,
    )
    return status, reasons


def build_report(
    *,
    binding_source: CanaryBindingSource,
    input_canary_count: int,
    accepted_canary_count: int,
    patterns: PatternCounts | None,
    requested_artifact_count: int,
    totals: Totals,
    publication_inactive: bool = False,
    rejected: bool = False,
) -> CanarySpanReport:
    if rejected:  # a rejected scan reports nothing but what it was given
        totals, patterns = Totals(), None
        accepted_canary_count = requested_artifact_count = 0
    found = totals.findings()
    capped = totals.capped or len(found) > MAX_FINDINGS
    counts = {name: totals.count(name) for name in _COUNTS}
    counts |= {
        "input_canary_count": min(input_canary_count, MAX_COUNT),
        "accepted_canary_count": accepted_canary_count,
        "requested_artifact_count": requested_artifact_count,
        "finding_count": min(len(found), MAX_COUNT),
    }
    if patterns is not None:
        counts |= {
            "exact_pattern_count": patterns.exact_pattern_count,
            "normalized_pattern_count": patterns.normalized_pattern_count,
            "normalization_collision_count": patterns.normalization_collision_count,
            "excluded_short_canary_count": patterns.excluded_short_canary_count,
        }
    status, reasons = _verdict(
        counts.__getitem__, binding_source, inactive=publication_inactive, rejected=rejected
    )
    report = CanarySpanReport(
        schema_version=CanarySpanReportSchema.V1,
        normalization_policy_version=CanaryNormalizationSchema.V1,
        status=status,
        complete=status is CanaryStatus.COMPLETE,
        reasons=reasons,
        binding_source=binding_source,
        count_capped=capped,
        findings=tuple(found[:MAX_FINDINGS]),
        **counts,
    )
    check_report(report)
    return report


def check_report(report: CanarySpanReport) -> None:
    """Raise ``CanarySpanReportInvalid`` unless the report agrees with its own counts."""
    if type(report) is not CanarySpanReport:
        raise CanarySpanReportInvalid
    rejected = report.status is CanaryStatus.REJECTED
    status, reasons = _verdict(
        lambda name: getattr(report, name),
        report.binding_source,
        inactive=CanaryReason.PUBLICATION_INACTIVE in report.reasons,
        rejected=rejected,
    )
    buckets = sum(getattr(report, name) for name in _BUCKET.values())
    ok = report.status is status and report.reasons == reasons
    ok = ok and buckets == report.requested_artifact_count
    ok = ok and not (rejected and (report.finding_count > 0 or report.findings))
    if not ok:
        raise CanarySpanReportInvalid
