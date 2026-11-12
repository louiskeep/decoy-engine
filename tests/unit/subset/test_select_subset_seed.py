"""`select_subset` takes its sampling seed from the config, never from a caller-held value."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.plan import PlanCompileError, job_seed_for_config
from decoy_engine.subset import (
    FanOutPolicy,
    SeedSpec,
    SubsetRequest,
    SubsetSource,
    run_subset,
    select_subset,
)

N = 200


def _table() -> pa.Table:
    return pa.table({"id": list(range(N))})


def _request() -> SubsetRequest:
    return SubsetRequest(
        relationships=(),
        seeds=(SeedSpec(table="t", mode="sample", key_columns=("id",), count=20),),
        policy=FanOutPolicy(),
    )


def _cfg(seed: Any) -> dict[str, Any]:
    return {"global_settings": {"seed": seed}}


def _selected(config: dict[str, Any]) -> frozenset[int]:
    sel = select_subset(_request(), {"t": _table()}, config=config)
    return frozenset(_table()["id"][i].as_py() for i in sel.survivors["t"])


def _planned(tmp_path: Path, config: dict[str, Any]) -> frozenset[int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "t.parquet"
    pl.from_arrow(_table()).write_parquet(path)  # type: ignore[union-attr]
    req = _request()
    run_subset(
        sources={"t": SubsetSource(path=str(path), format="parquet")},
        relationships=req.relationships,
        seeds=req.seeds,
        policy=req.policy,
        job_seed=job_seed_for_config(config),
        engine_version="t",
        output_dir=tmp_path / "out",
    )
    return frozenset(pl.read_parquet(tmp_path / "out" / "t.parquet")["id"].to_list())


def test_selection_follows_the_seed_the_config_resolves_to(tmp_path: Path) -> None:
    first, second = _cfg(1), _cfg(2)
    picked_first, picked_second = _selected(first), _selected(second)
    assert len(picked_first) == len(picked_second) == 20
    assert picked_first != picked_second
    assert picked_first == _planned(tmp_path / "a", first)
    assert picked_second == _planned(tmp_path / "b", second)


def test_an_invalid_seed_in_the_config_is_refused() -> None:
    with pytest.raises(PlanCompileError) as exc:
        select_subset(_request(), {"t": _table()}, config=_cfg(True))
    assert exc.value.code == reasons.plan.SEED_NOT_NUMERIC
