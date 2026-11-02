"""Catalog of the ``route`` namespace: the chunked output sink's resident / streamed decisions."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "FIDELITY_REPORT",
    "LEGACY_LANE",
    "NO_SINK",
    "POST_SCAN_INTEGRITY",
    "POST_VALIDATION",
    "QUARANTINE_ENABLED",
    "SINK_NOT_STREAMING",
    "SPLIT_EXTRA_SOURCES_PRESENT",
    "SPLIT_FULL_FRAME_PRESENT",
    "STREAMING_DISABLED",
]

_d = _catalog.route("decoy_engine.execution._chunked_output_sink")

STREAMING_DISABLED = _d("streaming_disabled", "Streamed chunked output is disabled for this run.")
NO_SINK = _d("no_sink", "No output sink was supplied, so the output stays resident.")
SINK_NOT_STREAMING = _d("sink_not_streaming", "The output sink cannot accept streamed batches.")
LEGACY_LANE = _d("legacy_lane", "The legacy chunked lane keeps its output resident.")
SPLIT_FULL_FRAME_PRESENT = _d(
    "split_full_frame_present",
    "A multi-table split keeps a full-frame table, so output stays resident.",
)
SPLIT_EXTRA_SOURCES_PRESENT = _d(
    "split_extra_sources_present",
    "A resident source is not part of the dispatched split, so output stays resident.",
)
QUARANTINE_ENABLED = _d(
    "quarantine_enabled", "Quarantine reads the whole output after the mask step."
)
FIDELITY_REPORT = _d(
    "fidelity_report", "The fidelity report reads the whole output after the mask step."
)
POST_VALIDATION = _d(
    "post_validation", "Post-validation reads the whole output after the mask step."
)
POST_SCAN_INTEGRITY = _d(
    "post_scan_integrity", "Resident output required by the requested Post-scan integrity checks."
)
