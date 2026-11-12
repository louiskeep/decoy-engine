"""Plan and route builders for the fitted cell format route tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.generation._plan_entry import generate_tables
from decoy_engine.generation.pinned import compile_pinned_plan, execute_pinned
from decoy_engine.generation.window import generate_rows
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile
from tests.unit.generation.statistical._cf_support import fit, mixed_frame, null_at

SEED = 4242
ROWS = 240
REF = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)


def route_frame(n: int = 1_000) -> pd.DataFrame:
    """The mixed frame plus a whole-number float column with nulls, a day column and an all-null one."""
    frame = mixed_frame(n)
    rng = np.random.default_rng(5)
    frame["qty"] = rng.integers(1, 80, n).astype("float64")
    frame.loc[null_at(n, 0.2, 9), "qty"] = np.nan
    days = pd.Timestamp("2021-01-01") + pd.to_timedelta(rng.integers(0, 700, n), unit="D")
    frame["day"] = pd.Series(days)
    frame.loc[null_at(n, 0.3, 10), "day"] = pd.NaT
    hours = pd.Timestamp("2021-05-05 03:00") + pd.to_timedelta(rng.integers(0, 5_000, n), unit="h")
    frame["seen"] = pd.Series(hours)
    frame.loc[null_at(n, 0.4, 11), "seen"] = pd.NaT
    frame["void"] = None
    return frame


def write_snapshot(path: Path, snap: dict[str, Any]) -> str:
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def stat(snap_file: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": "statistical",
        "snapshot_file": snap_file,
        "allow_real_categories": True,
        **extra,
    }


def config(columns: list[dict[str, Any]], rows: int = ROWS) -> dict[str, Any]:
    return {
        "global_settings": {"seed": SEED},
        "tables": [{"name": "t", "row_count": rows, "generate_columns": columns}],
    }


def all_columns(snap_file: str, *, with_void: bool = True, **extra: Any) -> list[dict[str, Any]]:
    names = ["price", "tier", "seen", "note", "qty", "day"] + (["void"] if with_void else [])
    return [stat(snap_file, n, **extra) for n in names]


def plan_of(cfg: dict[str, Any]) -> Any:
    return compile_plan(cfg, empty_profile(), decoy_engine_version="test")


def whole(cfg: dict[str, Any]) -> pa.Table:
    return generate_tables(plan_of(cfg))["t"]


def pinned(cfg: dict[str, Any]) -> pa.Table:
    return execute_pinned(compile_pinned_plan(cfg, REF), SEED)["t"]


def windows(cfg: dict[str, Any], cuts: tuple[int, ...] = (0, 70, 150, ROWS)) -> pa.Table:
    plan = plan_of(cfg)
    parts = [
        generate_rows(plan, table="t", offset=a, count=b - a, seed=SEED).table
        for a, b in pairwise(cuts)
    ]
    return pa.concat_tables(parts)


def digest(table: pa.Table) -> str:
    text = repr((str(table.schema), table.to_pylist()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fitted(tmp_path: Path, *, cell_format: bool = True) -> str:
    return write_snapshot(
        tmp_path / ("fit.json" if cell_format else "old.json"),
        fit(route_frame(), cell_format=cell_format),
    )
