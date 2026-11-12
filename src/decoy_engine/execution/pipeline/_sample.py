"""Which masked columns of a sample may differ from the full run."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SAMPLE_EXACT_WHEN_DETERMINISTIC: frozenset[str] = frozenset()


def is_sample_exact(strategy: str, column: Mapping[str, Any]) -> bool:
    return True


def approximate_columns(config: dict[str, Any]) -> list[tuple[str, str]]:
    return []
