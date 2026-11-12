"""STUB (red-first): replaced by the real scan in a later commit."""

from __future__ import annotations

from collections.abc import Sequence

from decoy_engine.verification.canary._aggregate import Totals, build_report
from decoy_engine.verification.canary._types import (
    CanaryArtifactInput,
    CanaryBindingSource,
    CanaryConsumerReason,
    CanarySpanPolicy,
    CanarySpanReport,
)

MAX_SCANNED_ARTIFACTS = 64


def scan_canary_artifacts(
    canaries: Sequence[str],
    policy: CanarySpanPolicy,
    *,
    binding_source: CanaryBindingSource,
    artifacts: Sequence[CanaryArtifactInput],
    consumer_reasons: frozenset[CanaryConsumerReason],
) -> CanarySpanReport:
    return build_report(
        binding_source=binding_source,
        input_canary_count=0,
        accepted_canary_count=0,
        patterns=None,
        requested_artifact_count=0,
        totals=Totals(),
        rejected=True,
    )
