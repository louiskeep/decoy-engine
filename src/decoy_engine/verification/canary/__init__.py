"""Residual canary detection across cells and text spans (F34): public types and verdict policy.

The report carries closed enums, booleans, ordinals and counts only. Status comes from one
precedence table and reasons from one union rule (``CanaryVerdictPolicy``).
"""

from decoy_engine.verification.canary._types import (
    MAX_COUNT,
    MAX_FINDINGS,
    CanaryArtifactInput,
    CanaryArtifactOutcome,
    CanaryArtifactReadFailure,
    CanaryBindingSource,
    CanaryColumnFinding,
    CanaryCompletionClass,
    CanaryConsumerReason,
    CanaryMatchMode,
    CanaryNormalizationSchema,
    CanaryObservedReason,
    CanaryPolicyInputError,
    CanaryReason,
    CanaryReportInvalid,
    CanaryReportSchema,
    CanarySpanPolicy,
    CanarySpanReport,
    CanaryStatus,
)

__all__ = [
    "MAX_COUNT",
    "MAX_FINDINGS",
    "CanaryArtifactInput",
    "CanaryArtifactOutcome",
    "CanaryArtifactReadFailure",
    "CanaryBindingSource",
    "CanaryColumnFinding",
    "CanaryCompletionClass",
    "CanaryConsumerReason",
    "CanaryMatchMode",
    "CanaryNormalizationSchema",
    "CanaryObservedReason",
    "CanaryPolicyInputError",
    "CanaryReason",
    "CanaryReportInvalid",
    "CanaryReportSchema",
    "CanarySpanPolicy",
    "CanarySpanReport",
    "CanaryStatus",
]
