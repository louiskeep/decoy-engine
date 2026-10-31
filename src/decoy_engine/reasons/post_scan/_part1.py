"""Catalog of the ``post_scan`` namespace, part 1 (phase-envelope failures)."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "CHECK_ERROR",
    "PASS_FAILED",
    "SCAN_CRASHED",
]

_d = _catalog.post_scan("decoy_engine.phase")

CHECK_ERROR = _d("check_error", "A Post-scan check raised before producing a result.")
SCAN_CRASHED = _d("scan_crashed", "A post-validation scan crashed before producing a result.")
PASS_FAILED = _d("pass_failed", "The Post-scan pass could not start.")
