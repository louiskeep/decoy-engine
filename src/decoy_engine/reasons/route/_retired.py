"""Catalog of the ``route`` namespace: tombstones of identities that shipped and were renamed.

A reason that shipped in a tagged release stays resolvable by its historic name so persisted
evidence can still be read: it is deprecated, never deleted, and points at its replacement. The
retired spelling appears nowhere else in the engine (the residual sentry excepts only this file
and the pinned manifest).
"""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "RUN_STORM",
    "RUN_STORM_REQUESTED",
]

# v0.16.0 route reason, F29 identity; renamed by the Pre-scan / Transform / Post-scan reframe.
RUN_STORM_REQUESTED = _catalog.route("decoy_engine.execution._pipeline_routing")(
    "run_storm_requested",
    "The STORM post-mask checks need the full frame resident.",
    deprecated_in="1.0.0",
    replaced_by=("route", "post_scan_integrity_requested"),
)
# v0.16.0 resident-output reason of the chunked output sink, renamed by the same reframe.
RUN_STORM = _catalog.route("decoy_engine.execution._chunked_output_sink")(
    "run_storm",
    "Resident output required by the requested STORM post-mask checks.",
    deprecated_in="1.0.0",
    replaced_by=("route", "post_scan_integrity"),
)
