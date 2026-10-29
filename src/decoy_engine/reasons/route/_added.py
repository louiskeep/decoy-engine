"""Catalog of the ``route`` namespace: reasons added after the baseline (F29 remediation)."""

from __future__ import annotations

from typing import Any

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Kind, Outcome, Phase, RetryClass, Severity

__all__ = [
    "MIXED_GENERATE_MASK_CHUNKED",
    "MIXED_RUNTIME_DECLINED",
    "OUT_OF_CORE_INELIGIBLE",
    "POST_VALIDATION_REQUESTED",
    "RUN_STORM_REQUESTED",
    "SEQUENTIAL_INELIGIBLE",
]

_d = _catalog.route("decoy_engine.execution._pipeline_routing")

POST_VALIDATION_REQUESTED = _d(
    "post_validation_requested", "The post-validation scans need the full frame resident."
)
RUN_STORM_REQUESTED = _d(
    "run_storm_requested", "The STORM post-mask checks need the full frame resident."
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
