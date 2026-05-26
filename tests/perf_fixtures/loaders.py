"""Loader helpers for the PERF.BASE.2 fixture suite.

Public test surface. Tests under the ``perf`` pytest marker call
``load_tier("small"|"medium"|"large")`` and get a pandas DataFrame
back. The loader fails loudly when a tier's Parquet is missing rather
than silently falling through; for the ``large`` tier the message
points at the generation command.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .schema import TIERS, TierSpec, get_tier

_FIXTURES_DIR = Path(__file__).resolve().parent


def fixture_path(tier_name: str) -> Path:
    """Return the Parquet path for ``tier_name`` without reading it."""
    return _FIXTURES_DIR / tier_name / "data.parquet"


def load_tier(tier_name: str) -> pd.DataFrame:
    """Load a tier's Parquet fixture as a pandas DataFrame.

    Raises FileNotFoundError with a regen-command hint if the file is
    missing. Tests should skip-or-error on the FileNotFoundError so the
    benchmark suite degrades gracefully when only a subset of tiers is
    present locally.
    """
    tier: TierSpec = get_tier(tier_name)
    path = fixture_path(tier.name)
    if not path.exists():
        committed_note = (
            "committed; check out the file or git lfs pull"
            if tier.committed
            else "NOT committed; regenerate with: "
            f"python scripts/gen_perf_fixtures.py {tier.name}"
        )
        raise FileNotFoundError(
            f"perf fixture missing: {path} ({committed_note})"
        )
    return pd.read_parquet(path, engine="pyarrow")


def available_tiers() -> list[str]:
    """Return the names of tiers whose Parquet exists on disk now.

    Useful for skipping unavailable tiers in parameterized tests without
    making the test class import-time-aware of disk state.
    """
    return [name for name in TIERS if fixture_path(name).exists()]
