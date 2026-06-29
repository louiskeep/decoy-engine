"""windowed_date column-level strategy (SP-10c / P5.S.windowed_date).

Generates a date within a bounded window relative to an anchor date column.
The output date for each row is:

    anchor_date + offset_days

where offset_days is sampled from [min_days, max_days] (inclusive) using a
seeded per-row RNG.

Three distributions are supported:

  uniform  - each day in [min_days, max_days] is equally likely.
  early    - dates near the start of the window are more likely (geometric
             decay from min_days toward max_days).
  late     - dates near the end of the window are more likely (geometric
             decay from max_days toward min_days).

Pattern: pandas.Timestamp + pandas.Timedelta for date arithmetic (pandas 2.x,
Apache-2.0; https://pandas.pydata.org/docs). Per-row seeded offset sampling
via numpy.random.default_rng (NumPy, BSD License; https://numpy.org/doc/
stable/reference/random/generator.html). The per-row seed uses
decoy_engine.generators.derivation.GenDeriveContext.row_int("np", i) to keep
output consistent with the engine-wide derivation contract.

Security design:
  The distribution set is a CLOSED enumeration (WINDOWED_DATE_DISTRIBUTIONS).
  Only the three listed names can construct a valid config. No eval() or
  exec() anywhere in this module.

Determinism:
  Same 8-byte seed + same anchor column -> byte-identical output dates on
  every run.

Validation timing:
  anchor + max_days + distribution + min <= max: config-parse time
  (WindowedDateConfig.from_dict).
  anchor column existence: plan-compile time (check_windowed_date_refs).
  Validation never mutates (per engine rule).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.plan._errors import PlanCompileError

# Closed enumeration of allowed distributions.
WINDOWED_DATE_DISTRIBUTIONS: frozenset[str] = frozenset({"uniform", "early", "late"})

# Output date format for string-typed anchor columns.
_DATE_FMT = "%Y-%m-%d"


@dataclass(frozen=True)
class WindowedDateConfig:
    """Configuration for a windowed_date column.

    Attributes:
        anchor:       Name of the anchor date column. Rows in the same table
                      must carry parseable date or datetime values in this
                      column.
        min_days:     Minimum offset from anchor in days (may be negative for
                      dates before the anchor; default 0).
        max_days:     Maximum offset from anchor in days (must be >= min_days).
        distribution: Sampling distribution over [min_days, max_days]. One of
                      "uniform", "early", or "late".
    """

    anchor: str
    min_days: int
    max_days: int
    distribution: str

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> WindowedDateConfig:
        """Parse and validate a windowed_date config dict.

        anchor, max_days, and distribution are validated at parse time.
        Anchor column existence is validated at plan-compile time via
        check_windowed_date_refs.

        Args:
            cfg: Config dict with required keys ``anchor`` and ``max_days``.

        Raises:
            PlanCompileError: ``anchor`` is missing or empty.
            PlanCompileError: ``max_days`` is missing.
            PlanCompileError: ``distribution`` is set but not in
                WINDOWED_DATE_DISTRIBUTIONS.
            PlanCompileError: ``min_days`` > ``max_days``.
        """
        anchor = cfg.get("anchor")
        if not anchor:
            raise PlanCompileError(
                code="windowed_date_anchor_missing",
                path="provider_config.anchor",
                message=(
                    "'anchor' is required for the windowed_date strategy. "
                    "Provide the name of the reference date column."
                ),
            )

        if "max_days" not in cfg or cfg.get("max_days") is None:
            raise PlanCompileError(
                code="windowed_date_max_days_missing",
                path="provider_config.max_days",
                message=(
                    "'max_days' is required for the windowed_date strategy. "
                    "Provide the maximum offset in days from the anchor date."
                ),
            )

        min_days = int(cfg.get("min_days", 0))
        max_days = int(cfg["max_days"])

        if min_days > max_days:
            raise PlanCompileError(
                code="windowed_date_min_exceeds_max",
                path="provider_config.min_days",
                message=(f"'min_days' ({min_days}) must be <= 'max_days' ({max_days})."),
            )

        distribution = str(cfg.get("distribution", "uniform"))
        if distribution not in WINDOWED_DATE_DISTRIBUTIONS:
            raise PlanCompileError(
                code="windowed_date_distribution_invalid",
                path="provider_config.distribution",
                message=(
                    f"'distribution' must be one of "
                    f"{sorted(WINDOWED_DATE_DISTRIBUTIONS)!r}; "
                    f"got {distribution!r}."
                ),
            )

        return cls(
            anchor=str(anchor),
            min_days=min_days,
            max_days=max_days,
            distribution=distribution,
        )


def _row_seed(job_seed: bytes, row_index: int) -> int:
    """Derive a per-row integer seed for windowed_date offset sampling.

    Uses SHA-256 over (job_seed || row_index_bytes) to produce a stable,
    per-row seed without requiring a full GenDeriveContext (which needs a
    derive_key that is not available in all call sites). This is consistent
    with the engine pattern of per-row HMAC derivation.

    Args:
        job_seed:  8-byte job seed.
        row_index: 0-based row index in the DataFrame.

    Returns:
        Non-negative 64-bit integer for numpy.random.default_rng seeding.
    """
    idx_bytes = row_index.to_bytes(8, "big")
    digest = hashlib.sha256(job_seed + idx_bytes).digest()
    return int.from_bytes(digest[:8], "big")


def _sample_offset(
    rng: np.random.Generator,
    min_days: int,
    max_days: int,
    distribution: str,
) -> int:
    """Sample one offset in [min_days, max_days] for the given distribution.

    uniform: numpy integers uniform in [min_days, max_days].
    early:   geometric-like decay: sample uniformly, then take the min of two
             draws (makes smaller offsets more likely, biasing toward start).
    late:    geometric-like decay: sample uniformly, then take the max of two
             draws (makes larger offsets more likely, biasing toward end).

    This is a "minimum/maximum of two uniform draws" technique, a simple
    approximation to triangular/exponential bias that requires no extra
    parameters and keeps the distribution strictly within [min_days, max_days].
    """
    span = max_days - min_days
    if span == 0:
        return min_days
    a = int(rng.integers(min_days, max_days + 1))
    if distribution == "uniform":
        return a
    b = int(rng.integers(min_days, max_days + 1))
    if distribution == "early":
        return min(a, b)
    # distribution == "late"
    return max(a, b)


def apply_windowed_date(
    config: WindowedDateConfig,
    df: pd.DataFrame,
    seed: bytes,
) -> list[str]:
    """Generate one date per row within the configured window around the anchor.

    Pattern: pandas Timestamp + Timedelta for date arithmetic (pandas 2.x,
    Apache-2.0). numpy.random.default_rng for seeded per-row offset sampling.

    Args:
        config: Parsed WindowedDateConfig.
        df:     DataFrame containing the anchor date column.
        seed:   8-byte job seed for per-row RNG seeding.

    Returns:
        List of ISO-format date strings (``YYYY-MM-DD``) aligned to df rows.
    """
    anchor_series = df[config.anchor]
    result: list[str] = []

    for i, raw_anchor in enumerate(anchor_series):
        anchor_ts = pd.Timestamp(raw_anchor)
        row_rng = np.random.default_rng(_row_seed(seed, i))
        offset = _sample_offset(row_rng, config.min_days, config.max_days, config.distribution)
        out_ts = anchor_ts + pd.Timedelta(days=offset)
        result.append(out_ts.strftime(_DATE_FMT))

    return result
