"""Catalog of the ``route`` namespace: reasons added after the baseline (F29 remediation)."""

from __future__ import annotations

from typing import Any

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import UNRELEASED, Kind, Outcome, Phase, RetryClass, Severity

__all__ = [
    "MIXED_GENERATE_MASK_CHUNKED",
    "MIXED_RUNTIME_DECLINED",
    "OUT_OF_CORE_INELIGIBLE",
    "POST_SCAN_MODELS_RESIDENT",
    "POST_SCAN_MODELS_UNSUPPORTED",
    "POST_VALIDATION_REQUESTED",
    "SEQUENTIAL_INELIGIBLE",
]

_d = _catalog.route("decoy_engine.execution._pipeline_routing")

POST_VALIDATION_REQUESTED = _d(
    "post_validation_requested", "The post-validation scans need the full frame resident."
)
POST_SCAN_MODELS_RESIDENT = _d(
    "post_scan_models_resident",
    "The model Post-scan checks need the full frame resident.",
    since=UNRELEASED,
)
MIXED_GENERATE_MASK_CHUNKED = _d(
    "mixed_generate_mask_chunked",
    "A mixed generate and mask job was admitted to the chunked route.",
)

MIXED_RUNTIME_DECLINED = _d(
    "mixed_runtime_declined",
    "The mixed route declined at run time and continued on the full-frame route.",
)

# The typed identity of an explicit `execution_mode` the job is not eligible for. The governor
# reroutes on exactly these two, never on message text.
_FAIL: dict[str, Any] = {
    "kind": Kind.FAILURE,
    "severity": Severity.ERROR,
    "outcome": Outcome.CONFIGURATION,
    "retry": RetryClass.CONFIGURATION_REPAIR,
    "phases": (Phase.RESOLVE,),
}
SEQUENTIAL_INELIGIBLE = _d(
    "sequential_ineligible",
    "An explicit sequential route was requested for a job that is not eligible for it.",
    **_FAIL,
)
OUT_OF_CORE_INELIGIBLE = _d(
    "out_of_core_ineligible",
    "An explicit out-of-core route was requested for a job that is not eligible for it.",
    **_FAIL,
)
# SCAN-ML: a route that runs no post-scan refuses a config asking for a model post-scan check.
POST_SCAN_MODELS_UNSUPPORTED = _catalog.route("PostScanRouteError")(
    "post_scan_models_unsupported",
    "This route runs no post-scan, so it refuses model post-scan detectors and ner_findings: fail.",
    kind=Kind.FAILURE,
    severity=Severity.ERROR,
    outcome=Outcome.CONFIGURATION,
    retry=RetryClass.CONFIGURATION_REPAIR,
    phases=(Phase.RESOLVE, Phase.COMPILE),
    since="1.25.0",
)
