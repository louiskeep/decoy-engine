"""Catalog of the ``safe_output`` namespace. Added at registry 1.5.0."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Severity

__all__ = ["INVALID", "LEGACY_WITHHELD"]

_d = _catalog.safe_output("SafeOutput")

INVALID = _d(
    "invalid",
    "This log line could not be recorded safely, so a placeholder was stored.",
    severity=Severity.ERROR,
    since="1.5.0",
)
LEGACY_WITHHELD = _d(
    "legacy_withheld",
    "Older log lines were withheld because they may contain unchecked text.",
    since="1.5.0",
)
