"""ExecutionEvent: a structured observation surfaced during a run (engine-v2 S9).

A lightweight event the runner can emit for timing/diagnostic surfaces. Quality
observations ride S5's `QualityWarning` (R14); this is for execution-layer
events that are not quality warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExecutionEvent:
    """One execution-layer event (e.g. boundary conversion, parallelism stat)."""

    kind: str
    detail: dict[str, Any] = field(default_factory=dict)
