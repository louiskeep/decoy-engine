"""Catalog of the ``post_scan`` namespace, part 1 (phase-envelope failures)."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import RetryClass

__all__ = [
    "CHECK_ERROR",
    "NER_BLOCKED",
    "PASS_FAILED",
    "SCAN_CRASHED",
]

_d = _catalog.post_scan("decoy_engine.phase")

CHECK_ERROR = _d("check_error", "A Post-scan check raised before producing a result.")
SCAN_CRASHED = _d("scan_crashed", "A post-validation scan crashed before producing a result.")
PASS_FAILED = _d("pass_failed", "The Post-scan pass could not start.")
# SCAN-ML: under `ner_findings: fail` an NER finding withholds the run's outputs.
NER_BLOCKED = _catalog.post_scan("PostScanBlockedError")(
    "ner_blocked",
    "An NER post-scan finding blocked this run under ner_findings: fail, so no output was published.",
    retry=RetryClass.CONFIGURATION_REPAIR,
    since="1.26.0",
)
