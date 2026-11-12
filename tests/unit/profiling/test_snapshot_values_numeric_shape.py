"""A redacted fit withholds the quantile grid and the exact values (GEN-NUM-SHAPE, row 24u)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.statistical import StatisticalSpecError, load_spec, sample_column
from decoy_engine.profiling import _snapshot_values as sv
from decoy_engine.profiling import distribution_snapshot_document


@pytest.fixture
def force_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sv, "value_sensitive_names", lambda fields: [f["name"] for f in fields])


def _normal() -> pd.DataFrame:
    return pd.DataFrame({"x": np.random.default_rng(504).normal(50.0, 5.0, 2_000)})


def _prices() -> pd.DataFrame:
    prices = np.random.default_rng(503).choice([4.99, 9.99, 19.99, 49.99], size=2_000)
    return pd.DataFrame({"x": prices})


def _doc(df: pd.DataFrame, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(distribution_snapshot_document(df, **kw)))
    return out


@pytest.mark.parametrize(("frame", "key"), [(_normal, "quantile_grid"), (_prices, "exact_values")])
def test_sensitive_number_column_drops_the_shape(
    force_sensitive: None, frame: Any, key: str
) -> None:
    doc = _doc(frame())
    stats = doc["columns"]["x"]["stats"]
    assert key in stats
    assert stats[key] is None
    out = sample_column(load_spec({"name": "x"}, snapshot=doc), 500, col_seed=1)
    lo, hi = stats["bin_edges"][0], stats["bin_edges"][-1]
    assert all(lo <= v <= hi for v in out)
    kept = _doc(frame(), include_values=True)["columns"]["x"]["stats"]
    assert kept[key] is not None


@pytest.mark.parametrize(("frame", "key"), [(_normal, "quantile_grid"), (_prices, "exact_values")])
def test_restored_shape_on_a_redacted_column_is_refused(
    force_sensitive: None, frame: Any, key: str
) -> None:
    raw = _doc(frame(), include_values=True)
    doc = _doc(frame())
    load_spec({"name": "x"}, snapshot=doc)
    doc["columns"]["x"]["stats"][key] = raw["columns"]["x"]["stats"][key]
    with pytest.raises(StatisticalSpecError) as caught:
        load_spec({"name": "x"}, snapshot=doc)
    assert caught.value.code == "statistical_snapshot_values_redacted"
