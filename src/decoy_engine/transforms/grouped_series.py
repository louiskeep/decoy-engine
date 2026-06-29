"""grouped_series column-level strategy (SP-10c / P5.S.grouped_series.1).

Generates a per-group series that resets or accumulates within each partition
defined by a group_by column, ordered by an order_by column. Two generators
ship in SP-10c:

  cumcount     - 0-based (or start-based) counter that resets at each group
                 boundary. No RNG needed; output is position within the sorted
                 group.
  monotone_walk - values that are non-decreasing within each group. Each step
                  is a seeded non-negative integer drawn from numpy RNG, so
                  the walk is deterministic under a fixed seed.

Pattern: per-group sequential indexing mirrors pandas GroupBy.cumcount() and
SDV-style per-group sequencing.

  pandas cumcount reference: https://pandas.pydata.org/docs/reference/
  api/pandas.core.groupby.GroupBy.cumcount.html (pandas 2.x, Apache-2.0).

  SDV per-group sequencing pattern: SDV TimeSeriesSynthesizer groups data
  by an entity_columns key and generates within-group sequences independently
  (https://sdv.dev/SDV/user_guides/timeseries/index.html, MIT License).

  numpy seeded RNG: numpy.random.default_rng for non-negative step sampling
  (NumPy, BSD License; https://numpy.org/doc/stable/reference/random/
  generator.html#numpy.random.Generator.integers).

Security design:
  The generator set is a CLOSED enumeration (GROUPED_SERIES_GENERATORS).
  Only the two listed generator names can construct a valid config. No
  eval() or exec() anywhere in this module.

Determinism:
  cumcount: deterministic by group-sort position; no RNG.
  monotone_walk: deterministic under the same 8-byte seed; uses per-group
  sub-key derived from the seed bytes and group position index.

Validation timing:
  group_by + order_by + generator: config-parse time (GroupedSeriesConfig.from_dict).
  column-ref existence: plan-compile time (check_grouped_series_refs).
  Validation never mutates (per engine rule).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.plan._errors import PlanCompileError

# Closed enumeration of allowed generator names.
GROUPED_SERIES_GENERATORS: frozenset[str] = frozenset({"cumcount", "monotone_walk"})

_MIN_STEP = 0
_MAX_STEP_DEFAULT = 10  # upper bound for monotone_walk integer steps


@dataclass(frozen=True)
class GroupedSeriesConfig:
    """Configuration for a grouped_series column.

    Attributes:
        group_by:  Name of the column that partitions rows into groups.
        order_by:  Name of the column that defines sort order within each
                   group.
        generator: Which series generator to apply. One of "cumcount"
                   (default) or "monotone_walk".
        start:     Starting value for the first row of each group (default 0
                   for cumcount, 1 for monotone_walk).
        step:      Step size between positions in cumcount; minimum step for
                   monotone_walk (default 1).
        max_step:  Upper bound for the step per row in monotone_walk (default
                   10). Ignored by cumcount.
    """

    group_by: str
    order_by: str
    generator: str
    start: int
    step: int
    max_step: int

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> GroupedSeriesConfig:
        """Parse and validate a grouped_series config dict.

        group_by, order_by, and generator (when provided) are validated at
        parse time. Column-ref existence is validated at plan-compile time via
        check_grouped_series_refs.

        Args:
            cfg: Config dict with required keys ``group_by`` and ``order_by``.

        Raises:
            PlanCompileError: ``group_by`` is missing or empty.
            PlanCompileError: ``order_by`` is missing or empty.
            PlanCompileError: ``generator`` is set but not in
                GROUPED_SERIES_GENERATORS.
        """
        group_by = cfg.get("group_by")
        if not group_by:
            raise PlanCompileError(
                code="grouped_series_group_by_missing",
                path="provider_config.group_by",
                message=(
                    "'group_by' is required for the grouped_series strategy. "
                    "Provide the name of the column that partitions rows."
                ),
            )

        order_by = cfg.get("order_by")
        if not order_by:
            raise PlanCompileError(
                code="grouped_series_order_by_missing",
                path="provider_config.order_by",
                message=(
                    "'order_by' is required for the grouped_series strategy. "
                    "Provide the name of the column that sorts rows within each "
                    "group."
                ),
            )

        generator = str(cfg.get("generator", "cumcount"))
        if generator not in GROUPED_SERIES_GENERATORS:
            raise PlanCompileError(
                code="grouped_series_generator_invalid",
                path="provider_config.generator",
                message=(
                    f"'generator' must be one of "
                    f"{sorted(GROUPED_SERIES_GENERATORS)!r}; got {generator!r}."
                ),
            )

        # Defaults differ by generator: cumcount starts at 0, monotone_walk at 1.
        default_start = 0 if generator == "cumcount" else 1
        start = int(cfg.get("start", default_start))
        step = int(cfg.get("step", 1))
        max_step = int(cfg.get("max_step", _MAX_STEP_DEFAULT))

        return cls(
            group_by=str(group_by),
            order_by=str(order_by),
            generator=generator,
            start=start,
            step=step,
            max_step=max_step,
        )


def _group_seed(seed: bytes, group_label: Any, group_index: int) -> int:
    """Derive a per-group integer seed from the job seed + group identity.

    Uses SHA-256 over (seed_bytes || group_label_utf8 || group_index_bytes)
    to keep group seeds independent. Not HKDF: this is a convenience helper
    for numpy RNG seeding only, not for keyed derivation.

    Args:
        seed:        8-byte job seed.
        group_label: The group_by column value (stringified for hashing).
        group_index: Ordinal position of this group in the sorted group list.

    Returns:
        A non-negative 64-bit integer suitable for numpy.random.default_rng.
    """
    label_bytes = str(group_label).encode("utf-8", errors="replace")
    idx_bytes = group_index.to_bytes(8, "big")
    digest = hashlib.sha256(seed + label_bytes + idx_bytes).digest()
    return int.from_bytes(digest[:8], "big")


def apply_grouped_series(
    config: GroupedSeriesConfig,
    df: pd.DataFrame,
    seed: bytes,
) -> pd.Series:
    """Generate a per-group series column from the group_by + order_by columns.

    Pattern: pandas GroupBy.cumcount() for cumcount; seeded numpy integer RNG
    for monotone_walk steps (SDV per-group sequencing model).

    Args:
        config: Parsed GroupedSeriesConfig.
        df:     DataFrame containing group_by and order_by columns.
        seed:   8-byte job seed for RNG-backed generators (monotone_walk).

    Returns:
        A pandas Series aligned to ``df``'s index with the series values.
    """
    group_col = config.group_by
    order_col = config.order_by
    n = len(df)

    if n == 0:
        return pd.Series([], dtype=int)

    # Annotate with original position so we can map results back to input order.
    pos_col = "__decoy_row_pos__"
    working = df[[group_col, order_col]].copy()
    working[pos_col] = range(n)

    if config.generator == "cumcount":
        return _apply_cumcount(config, working, group_col, order_col, pos_col, n)
    # monotone_walk
    return _apply_monotone_walk(config, working, group_col, order_col, pos_col, n, seed)


def _apply_cumcount(
    config: GroupedSeriesConfig,
    working: pd.DataFrame,
    group_col: str,
    order_col: str,
    pos_col: str,
    n: int,
) -> pd.Series:
    """cumcount: position within each group after sorting by order_by.

    Pattern: equivalent to pandas GroupBy.cumcount() with a custom start/step.
    Reference: pandas.core.groupby.GroupBy.cumcount (pandas 2.x, Apache-2.0).
    """
    sorted_df = working.sort_values([group_col, order_col], kind="stable")

    result: list[int] = [0] * n
    current_group: Any = object()  # sentinel, never equal to a real value
    counter = 0

    for _, row in sorted_df.iterrows():
        g = row[group_col]
        orig_pos = int(row[pos_col])
        if g != current_group:
            current_group = g
            counter = 0
        result[orig_pos] = config.start + counter * config.step
        counter += 1

    return pd.Series(result, dtype=int)


def _apply_monotone_walk(
    config: GroupedSeriesConfig,
    working: pd.DataFrame,
    group_col: str,
    order_col: str,
    pos_col: str,
    n: int,
    seed: bytes,
) -> pd.Series:
    """monotone_walk: non-decreasing values within each group.

    Each step is a non-negative integer drawn from a seeded
    numpy.random.default_rng. Steps are in [0, config.max_step]. The
    first value in each group is config.start.

    Pattern: numpy.random.default_rng.integers for seeded step sampling
    (NumPy, BSD License; https://numpy.org/doc/stable/reference/random/
    generator.html#numpy.random.Generator.integers).
    """
    sorted_df = working.sort_values([group_col, order_col], kind="stable")

    # Collect groups in the order they first appear (sorted by group value
    # so the group-seed is stable regardless of input order).
    groups: list[Any] = sorted(working[group_col].unique(), key=str)
    group_index_map = {g: i for i, g in enumerate(groups)}

    result: list[int] = [0] * n
    current_group: Any = object()
    cumulative = config.start
    group_rng: np.random.Generator | None = None

    for _, row in sorted_df.iterrows():
        g = row[group_col]
        orig_pos = int(row[pos_col])
        if g != current_group:
            current_group = g
            cumulative = config.start
            g_seed = _group_seed(seed, g, group_index_map[g])
            group_rng = np.random.default_rng(g_seed)
        # First row of the group gets start; subsequent rows add a non-neg step.
        result[orig_pos] = cumulative
        if group_rng is not None:
            step = int(group_rng.integers(config.step, config.max_step + 1))
            cumulative += step

    return pd.Series(result, dtype=int)
