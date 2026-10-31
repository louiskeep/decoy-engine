"""Catalog of the ``pre_scan`` namespace, part 1 (phase-envelope failures)."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "PROFILE_FAILED",
]

_d = _catalog.pre_scan("decoy_engine.phase")

PROFILE_FAILED = _d("profile_failed", "The Pre-scan profile did not complete.")
