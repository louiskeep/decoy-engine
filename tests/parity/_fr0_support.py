"""Shared helpers for the Phase F FR0 route-independence and budget tests (test-side only)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.generation import _plan_entry
from decoy_engine.generation._streamed import GenerationResult, run_generation
from decoy_engine.plan import compile_plan
from tests.parity import _f0_fixtures as fx
from tests.parity._f0_canonical import table_fingerprint


def plan_of(cfg: dict[str, Any]) -> Any:
    return compile_plan(cfg, fx._profile(), decoy_engine_version="fr0")


def prepared_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """The configuration the core sees: the plan's embedded, validated JSON."""
    config = json.loads(plan_of(cfg).generation.config_json)
    assert isinstance(config, dict)
    return config


def partition_from_cuts(cuts: dict[str, Sequence[int]]) -> Callable[[str, int], list[int]]:
    """A per-table partition from cut points (clamped, sorted); empty cuts is one batch.

    Duplicate or boundary cuts yield zero-row batches on purpose.
    """

    def partition(table: str, rows: int) -> list[int]:
        points = sorted(min(max(c, 0), rows) for c in cuts.get(table, ()))
        edges = [0, *points, rows]
        return [b - a for a, b in pairwise(edges)]

    return partition


def run_resident(
    cfg: dict[str, Any],
    partition: Callable[[str, int], Sequence[int]] | None = None,
    **kwargs: Any,
) -> dict[str, pa.Table]:
    result = run_generation(prepared_config(cfg), partition=partition, **kwargs)
    assert result.tables is not None
    return result.tables


def run_streamed(
    cfg: dict[str, Any],
    target: Path,
    partition: Callable[[str, int], Sequence[int]] | None = None,
    **kwargs: Any,
) -> tuple[GenerationResult, dict[str, pa.Table]]:
    """Run into a bounded Parquet sink; return the result and the committed tables read back."""
    from decoy_engine.execution import ParquetTransactionalSink

    result = run_generation(
        prepared_config(cfg),
        sink=ParquetTransactionalSink(target),
        partition=partition,
        **kwargs,
    )
    tables = {p.stem: pq.read_table(p) for p in sorted(target.glob("*.parquet"))}
    return result, tables


def fingerprints(tables: dict[str, pa.Table]) -> dict[str, dict[str, Any]]:
    """Order-independent canonical fingerprints with Parquet-only schema metadata dropped."""
    return {
        name: table_fingerprint(t.replace_schema_metadata(None))
        for name, t in sorted(tables.items())
    }


def generate_tables_via_plan(cfg: dict[str, Any], **kwargs: Any) -> dict[str, pa.Table]:
    return _plan_entry.generate_tables(plan_of(cfg), **kwargs)
