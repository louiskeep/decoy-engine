"""The one emitter for value-free generation warnings: log the summary, then hand the record on."""

from __future__ import annotations

import logging
from collections.abc import Callable

from decoy_engine.generation.pool import QualityWarning


def emit_generation_warning(
    record: QualityWarning,
    log: logging.Logger,
    summary: str,
    on_warning: Callable[[QualityWarning], None] | None,
) -> None:
    """`summary` carries names and counts only; the callback sees the structured record."""
    log.warning("%s", summary)
    if on_warning is not None:
        on_warning(record)
