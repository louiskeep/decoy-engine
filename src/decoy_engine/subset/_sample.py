"""Sample requests (stub)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine.subset._types import FanOutPolicy

SAMPLE_ROWS_MIN = 1
SAMPLE_ROWS_MAX = 1000
SAMPLE_KEPT_CEILING = 1_000_000


@dataclass(frozen=True)
class SampleSpec:
    rows: int
    roots: tuple[str, ...]
    display_rows: int = 0


def sample_request(
    config: dict[str, Any],
    *,
    rows: int,
    root: str | None = None,
    use_config_subset: bool = True,
    display_rows: int = 0,
) -> Any:
    from decoy_engine.subset._scope import SubsetRequest

    return SubsetRequest(relationships=(), seeds=(), policy=FanOutPolicy())
