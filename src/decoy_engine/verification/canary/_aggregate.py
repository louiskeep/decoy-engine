"""STUB (red-first): replaced by the real aggregation in the next commit."""

from __future__ import annotations

from dataclasses import fields

from decoy_engine.verification.canary._match import CellTally, PatternCounts
from decoy_engine.verification.canary._types import (
    CanaryArtifactOutcome,
    CanaryBindingSource,
    CanaryNormalizationSchema,
    CanaryReason,
    CanarySpanReport,
    CanarySpanReportSchema,
    CanaryStatus,
)


class Totals:
    capped = False

    def outcome(self, outcome: CanaryArtifactOutcome) -> None:
        return None

    def columns(self, n: int) -> None:
        return None

    def rows(self, n: int) -> None:
        return None

    def text(self, n: int) -> None:
        return None

    def cells(
        self, artifact: int, column: int, tally: CellTally, *, uncompared: int, text_bytes: int
    ) -> None:
        return None


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
    kw: dict[str, object] = {f.name: 0 for f in fields(CanarySpanReport)}
    kw.update(
        schema_version=CanarySpanReportSchema.V1,
        normalization_policy_version=CanaryNormalizationSchema.V1,
        status=CanaryStatus.REJECTED,
        complete=False,
        reasons=(CanaryReason.INPUT_REJECTED,),
        binding_source=binding_source,
        count_capped=False,
        findings=(),
    )
    return CanarySpanReport(**kw)  # type: ignore[arg-type]


def check_report(report: CanarySpanReport) -> None:
    return None
