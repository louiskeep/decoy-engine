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

Pattern: derive()-per-column namespace + bounded date offset. Per-row seeded
offset sampling via numpy.random.default_rng (NumPy, BSD License;
https://numpy.org/doc/stable/reference/random/generator.html). The per-row
seed comes from decoy_engine.determinism._derive.derive(seed, namespace,
row_index.to_bytes(8, "big")) (HKDF-SHA256 + HMAC-SHA256, RFC 5869 + RFC
2104), keyed by the column namespace so two windowed_date columns produce
independent per-row offset streams.

Security design:
  The distribution set is a CLOSED enumeration (WINDOWED_DATE_DISTRIBUTIONS).
  Only the three listed names can construct a valid config. No eval() or
  exec() anywhere in this module.

Determinism:
  Same 8-byte seed + same namespace + same anchor column -> byte-identical
  output dates on every run.

Validation timing:
  anchor + max_days + distribution + min <= max: config-parse time
  (WindowedDateConfig.from_dict).
  anchor column existence: plan-compile time (check_windowed_date_refs).
  Validation never mutates (per engine rule).
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.determinism._derive import derive
from decoy_engine.errors import DecoyError
from decoy_engine.plan._errors import PlanCompileError

# Closed enumeration of allowed distributions.
WINDOWED_DATE_DISTRIBUTIONS: frozenset[str] = frozenset({"uniform", "early", "late"})

# Output date format for string-typed anchor columns.
_DATE_FMT = "%Y-%m-%d"

# Supported date range, for the anchor and for the produced date. It is declared (not inherited
# from pandas' nanosecond bounds) so the accepted domain does not move with a pandas release, and
# it is narrower than those bounds so window arithmetic cannot silently wrap near the edge.
SUPPORTED_MIN = pd.Timestamp("1700-01-01")
SUPPORTED_MAX = pd.Timestamp("2200-12-31 23:59:59.999999999")

# The only string anchor grammar: a numeric ISO date or ISO datetime with NO timezone. Anything
# else is rejected BEFORE parsing, because `pd.Timestamp` also accepts "now"/"today" (wall clock),
# zone-bearing strings, and locale or partial forms, none of which are reproducible or
# route-independent.
_ISO_ANCHOR = re.compile(
    r"\A\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?)?\Z",
    re.ASCII,
)


class WindowedDateError(DecoyError, ValueError):
    """A non-null anchor cannot be windowed (grammar, NaT, out of range, or overflow).

    The message names the row position and the reason only, never the anchor value, so a
    failure cannot leak cell content into logs.
    """


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


def _is_null_cell(raw: Any) -> bool:
    """True only for an actual null cell. A string such as "" or "NaT" is NOT null."""
    if raw is None or raw is pd.NaT or raw is pd.NA:
        return True
    return isinstance(raw, float) and raw != raw  # NaN is the only self-unequal float


def _parse_anchor(raw: Any, position: int) -> pd.Timestamp:
    """Validate one non-null anchor against the supported grammar and range, then parse it."""
    ts: pd.Timestamp
    if isinstance(raw, str):
        if _ISO_ANCHOR.match(raw) is None:
            raise WindowedDateError(
                f"windowed_date anchor at row {position} is not an ISO date or datetime "
                "without a timezone"
            )
        try:
            ts = pd.Timestamp(raw)
        except (ValueError, OverflowError) as exc:
            raise WindowedDateError(
                f"windowed_date anchor at row {position} is not a valid calendar date"
            ) from exc
    elif isinstance(raw, (pd.Timestamp, _dt.datetime, _dt.date)):
        # Typed date32 / timestamp anchors (and datetime64 mask-side columns) arrive as these.
        if getattr(raw, "tzinfo", None) is not None:
            raise WindowedDateError(f"windowed_date anchor at row {position} carries a timezone")
        try:
            ts = pd.Timestamp(raw)
        except (ValueError, OverflowError) as exc:
            raise WindowedDateError(
                f"windowed_date anchor at row {position} is outside the supported range"
            ) from exc
    else:
        raise WindowedDateError(
            f"windowed_date anchor at row {position} has unsupported type {type(raw).__name__}"
        )
    if ts is pd.NaT or pd.isna(ts):
        raise WindowedDateError(f"windowed_date anchor at row {position} parses to NaT")
    if not (SUPPORTED_MIN <= ts <= SUPPORTED_MAX):
        raise WindowedDateError(
            f"windowed_date anchor at row {position} is outside the supported range"
        )
    return ts


def _shift_and_format(anchor: pd.Timestamp, offset: int, position: int) -> str:
    try:
        out = anchor + pd.Timedelta(days=offset)
        if not (SUPPORTED_MIN <= out <= SUPPORTED_MAX):
            raise OverflowError
        return str(out.strftime(_DATE_FMT))
    except (OverflowError, ValueError) as exc:
        raise WindowedDateError(
            f"windowed_date result at row {position} is outside the supported range"
        ) from exc


def apply_windowed_date(
    config: WindowedDateConfig,
    df: pd.DataFrame,
    seed: bytes,
    namespace: str,
    row_offset: int = 0,
    positions: Sequence[int] | None = None,
) -> list[str | None]:
    """Generate one date per row within the configured window around the anchor.

    Pattern: derive()-per-column namespace (HKDF-SHA256 + HMAC-SHA256) for
    per-row per-column seed isolation; numpy.random.default_rng for seeded
    offset sampling; pandas Timestamp + Timedelta for date arithmetic.

    Args:
        config:     Parsed WindowedDateConfig.
        df:         DataFrame containing the anchor date column.
        seed:       8-byte job seed for per-row RNG seeding.
        namespace:  Per-column namespace string (e.g. "windowed_date/<col>")
                    passed to derive() so two windowed_date columns in the
                    same job produce independent offset streams.
        row_offset: Durable global row number of `df`'s row 0 (Phase 4 slice
                    1 / DGRN). Default 0 keeps every full-frame / non-chunked
                    call byte-unchanged: `row_index` for local row `j` is
                    `row_offset + j`, which equals the plain `j` full-frame
                    enumeration produces when `row_offset == 0`. A chunked
                    caller passes the chunk's own durable offset so `i`
                    tracks the row's true position in the whole, unchunked
                    source instead of restarting at 0 each chunk.

    Returns:
        List of ISO-format date strings (``YYYY-MM-DD``) aligned to df rows. A genuine null
        anchor cell yields ``None`` and consumes no draw.

    Raises:
        WindowedDateError: a non-null anchor fails the accepted grammar, parses to NaT (including
            ``""`` and ``"NaT"``), lies outside ``SUPPORTED_MIN``..``SUPPORTED_MAX``, or the
            window arithmetic or formatting overflows. Fail-closed: no row is clamped.
    """
    anchor_series = df[config.anchor]
    result: list[str | None] = []

    # C8-iii-d: `positions` gives each row its full-table row number under a `when:` gate, so a
    # selected row seeds the same as in an ungated run. Without it, rows are contiguous from
    # `row_offset`, byte-identical to the previous `enumerate(..., start=row_offset)`.
    row_numbers: Sequence[int] = (
        positions if positions is not None else range(row_offset, row_offset + len(anchor_series))
    )
    for i, raw_anchor in zip(row_numbers, anchor_series, strict=True):
        if _is_null_cell(raw_anchor):
            result.append(None)
            continue
        anchor_ts = _parse_anchor(raw_anchor, int(i))
        row_seed_int = int.from_bytes(derive(seed, namespace, int(i).to_bytes(8, "big"))[:8], "big")
        row_rng = np.random.default_rng(row_seed_int)
        offset = _sample_offset(row_rng, config.min_days, config.max_days, config.distribution)
        result.append(_shift_and_format(anchor_ts, offset, int(i)))

    return result
