"""Residual canary detection across cells and text spans (F34): public types and verdict policy.

The report carries closed enums, booleans, ordinals and counts only. Status comes from one
precedence table and reasons from one union rule (``CanaryVerdictPolicy``).
"""

from decoy_engine.verification.canary._policy import (
    POLICY_TABLE,
    CanaryPolicyRow,
    CanaryVerdictPolicy,
    completion_class,
    compose_reasons,
    derive_status,
)
from decoy_engine.verification.canary._scan import MAX_SCANNED_ARTIFACTS, scan_canary_artifacts
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
    CanarySpanPolicy,
    CanarySpanReport,
    CanarySpanReportInvalid,
    CanarySpanReportSchema,
    CanaryStatus,
)

__all__ = [
    "MAX_COUNT",
    "MAX_FINDINGS",
    "MAX_SCANNED_ARTIFACTS",
    "POLICY_TABLE",
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
    "CanaryPolicyRow",
    "CanaryReason",
    "CanarySpanPolicy",
    "CanarySpanReport",
    "CanarySpanReportInvalid",
    "CanarySpanReportSchema",
    "CanaryStatus",
    "CanaryVerdictPolicy",
    "completion_class",
    "compose_reasons",
    "derive_status",
    "scan_canary_artifacts",
]
