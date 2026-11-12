"""STUB."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from decoy_engine.verification.postmask._stream_input import TableBatches


@dataclass(frozen=True)
class DetectorRate:
    detector_id: str
    match_rate: float
    confidence: str


@dataclass(frozen=True)
class ColumnFacts:
    table: str
    column: str


def column_facts(
    source: TableBatches | None,
    output: TableBatches,
    columns: Sequence[str],
    *,
    batch_rows: int = 100_000,
    scratch: Path,
    memory_limit: str,
) -> dict[str, ColumnFacts]:
    return {}
