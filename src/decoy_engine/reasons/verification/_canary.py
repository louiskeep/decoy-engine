"""Catalog of the ``verification`` namespace: the residual canary check (F34).

The wire strings equal ``decoy_engine.verification.canary.CanaryReason`` values, and the
declaration order below is that enum's order, which is the canonical ``reasons`` order of a
``canary-span-report/v1``. Added at registry 1.3.0.
"""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "CANARY_ARTIFACT_UNREADABLE",
    "CANARY_ARTIFACT_UNSUPPORTED",
    "CANARY_BINDING_ABSENT",
    "CANARY_CELL_UNCOMPARED",
    "CANARY_INPUT_REJECTED",
    "CANARY_MATCH",
    "CANARY_PUBLICATION_INACTIVE",
    "CANARY_PUBLICATION_MISMATCH",
    "CANARY_RESOURCE_LIMIT",
    "CANARY_SCAN_INCOMPLETE",
    "CANARY_SPAN_REASONS",
    "CANARY_SPAN_SHORT_EXCLUDED",
    "CANARY_TARGET_UNAVAILABLE",
]

_d = _catalog.verification("CanarySpan")

CANARY_MATCH = _d("canary_match", "A supplied canary was found in a job output.", since="1.3.0")
CANARY_SCAN_INCOMPLETE = _d(
    "canary_scan_incomplete", "The canary check did not see every output cell.", since="1.3.0"
)
CANARY_INPUT_REJECTED = _d(
    "canary_input_rejected", "The canary check inputs were rejected.", since="1.3.0"
)
CANARY_BINDING_ABSENT = _d(
    "canary_binding_absent", "No publication record bound the scanned outputs.", since="1.3.0"
)
CANARY_PUBLICATION_INACTIVE = _d(
    "canary_publication_inactive", "Output publication is not active.", since="1.3.0"
)
CANARY_TARGET_UNAVAILABLE = _d(
    "canary_target_unavailable", "The job has no committed output to scan.", since="1.3.0"
)
CANARY_PUBLICATION_MISMATCH = _d(
    "canary_publication_mismatch",
    "An output does not match its publication record.",
    since="1.3.0",
)
CANARY_ARTIFACT_UNREADABLE = _d(
    "canary_artifact_unreadable", "An output could not be read.", since="1.3.0"
)
CANARY_ARTIFACT_UNSUPPORTED = _d(
    "canary_artifact_unsupported",
    "An output format or column type is not supported.",
    since="1.3.0",
)
CANARY_RESOURCE_LIMIT = _d(
    "canary_resource_limit", "A scan budget or size limit was reached.", since="1.3.0"
)
CANARY_CELL_UNCOMPARED = _d(
    "canary_cell_uncompared", "Some output cells could not be compared.", since="1.3.0"
)
CANARY_SPAN_SHORT_EXCLUDED = _d(
    "canary_span_short_excluded",
    "A canary shorter than the span minimum was checked as a whole cell only.",
    since="1.3.0",
)

CANARY_SPAN_REASONS = (
    CANARY_MATCH,
    CANARY_SCAN_INCOMPLETE,
    CANARY_INPUT_REJECTED,
    CANARY_BINDING_ABSENT,
    CANARY_PUBLICATION_INACTIVE,
    CANARY_TARGET_UNAVAILABLE,
    CANARY_PUBLICATION_MISMATCH,
    CANARY_ARTIFACT_UNREADABLE,
    CANARY_ARTIFACT_UNSUPPORTED,
    CANARY_RESOURCE_LIMIT,
    CANARY_CELL_UNCOMPARED,
    CANARY_SPAN_SHORT_EXCLUDED,
)
