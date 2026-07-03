"""Shared fixtures for `tests/unit/subset` (Sprint G, implementation guide section 8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pytest

from decoy_engine.plan._types import PlanRelationship, PlanRelationshipEnd

JOB_SEED = b"\x01\x02\x03\x04\x05\x06\x07\x08"


def make_parquet(
    tmp_path: Path, name: str, data: dict[str, Any], schema: dict | None = None
) -> str:
    """Write `data` to `tmp_path/<name>.parquet` and return the path as a string."""
    df = pl.DataFrame(data, schema=schema)
    path = tmp_path / f"{name}.parquet"
    df.write_parquet(path)
    return str(path)


def rel(
    pt: str, pc: tuple[str, ...], ct: str, cc: tuple[str, ...], policy: str = "preserve"
) -> PlanRelationship:
    """One-child PlanRelationship helper."""
    return PlanRelationship(
        parent=PlanRelationshipEnd(table=pt, columns=pc),
        children=(PlanRelationshipEnd(table=ct, columns=cc),),
        orphan_policy=policy,  # type: ignore[arg-type]
        namespace=None,
    )


@pytest.fixture
def job_seed() -> bytes:
    return JOB_SEED
