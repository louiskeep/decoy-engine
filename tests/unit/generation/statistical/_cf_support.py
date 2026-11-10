"""Builders shared by the fitted cell format sampling tests."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.generation.statistical import load_spec
from decoy_engine.quality.snapshot import compute_distribution_snapshot


def null_at(n: int, rate: float, seed: int) -> np.ndarray:
    """Boolean mask with exactly `round(n * rate)` True cells, so the source rate is exact."""
    mask = np.zeros(n, dtype=bool)
    mask[np.random.default_rng(seed).permutation(n)[: round(n * rate)]] = True
    return mask


def mixed_frame(n: int = 2_000) -> pd.DataFrame:
    """Numeric, categorical, datetime and freetext columns at null rates 0.10/0.25/0.40/0.50."""
    rng = np.random.default_rng(11)
    frame = pd.DataFrame(
        {
            "price": rng.normal(100.0, 20.0, size=n).round(2),
            "tier": rng.choice(["gold", "silver", "bronze"], size=n),
            "seen": pd.Timestamp("2020-01-01") + pd.to_timedelta(rng.integers(0, 900, n), unit="D"),
            "note": [f"note {i} " + "x" * int(k) for i, k in enumerate(rng.integers(3, 40, n))],
        }
    )
    for seed, (name, rate) in enumerate(
        (("price", 0.10), ("tier", 0.25), ("seen", 0.40), ("note", 0.50))
    ):
        frame.loc[null_at(n, rate, seed), name] = None
    frame["seen"] = pd.to_datetime(frame["seen"])
    return frame


def fit(df: pd.DataFrame, *, cell_format: bool = True, **kw: Any) -> dict[str, Any]:
    """A snapshot as the file reader hands it over: JSON round-tripped."""
    snap = compute_distribution_snapshot(df, cell_format=cell_format, **kw)
    return json.loads(json.dumps(snap))


def stat_cfg(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "statistical", "allow_real_categories": True, **extra}


def spec_of(snap: dict[str, Any], name: str, **extra: Any) -> Any:
    return load_spec(stat_cfg(name, **extra), snapshot=snap)


def none_rate(values: list[Any]) -> float:
    return sum(v is None for v in values) / len(values)


def band(rate: float, n: int, sigmas: float = 4.0) -> tuple[float, float]:
    half = sigmas * (rate * (1 - rate) / n) ** 0.5
    return rate - half, rate + half


def similarity(snap: dict[str, Any], out: list[Any], name: str = "x") -> float:
    """Value similarity (the IQR-scaled marginal score) of generated `out` against `snap`."""
    from decoy_engine.quality import compute_fidelity

    fitted = compute_distribution_snapshot(pd.DataFrame({name: out}))
    columns = compute_fidelity(snap, fitted)["marginal"]["columns"]
    return float(columns[0]["value_similarity"])


def draw(snap: dict[str, Any], n: int = 20_000, *, name: str = "x", **extra: Any) -> list[Any]:
    """`n` values from the statistical sampler for column `name` of `snap`, seed 7."""
    from decoy_engine.generation.statistical import sample_column

    return sample_column(spec_of(snap, name, **extra), n, col_seed=7)
