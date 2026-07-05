"""geo_generalize strategy (SP-08 / P5.S.geo_generalize.1+2): geographic generalization.

Two generalization types:

ZIP (type="zip"):
  Implements k-threshold geographic generalization for ZIP-format columns per the
  HIPAA Safe Harbor de-identification standard (45 CFR 164.514(b)(2)). Each row
  is generalized to the most specific level whose in-dataset count satisfies the
  k-threshold; if no level satisfies the threshold, the value is suppressed.

  Cascade levels (ZIP type):
    zip5      -- retain the full 5-digit ZIP (satisfies threshold when >= k records
                 in the dataset share this ZIP5).
    zip3      -- generalise to the 3-digit prefix, UNLESS that prefix is in the
                 HHS-published restricted-prefix list (population < 20,000 per
                 45 CFR 164.514(b)(2)(i)(B)), in which case this level is skipped.
    state     -- generalise to the 2-letter state abbreviation derived from the
                 ZIP5. Satisfies threshold when >= k records in the dataset share
                 the same state.
    suppress  -- replace with empty string ("") when no level satisfies the threshold.

  HIPAA Safe Harbor restricted ZIP3 prefixes:
    The set of 3-digit ZIP code prefixes representing geographic units with fewer than
    20,000 persons per the Census-based determination is loaded from the shipped
    ``us_zip3_population`` reference table (SP-08). Source: 45 CFR 164.514(b)(2)(i)(B).
    See ``decoy_engine.reference_tables.data/us_zip3_population.parquet``.

Lat/Lng (type="lat_lng"):
  Implements H3 geospatial generalization (SP-08b / P5.S.geo_generalize.2). Each row
  truncates a "lat,lng" coordinate pair to an H3 cell at a configurable resolution,
  then cascades to coarser resolutions if the in-dataset count is below k_threshold.

  Cascade levels (lat_lng type):
    h3_resolution_9  -- H3 cell at resolution 9 (~174m average edge length, ~0.105 km2 area).
    h3_resolution_7  -- H3 cell at resolution 7 (~1.22km average edge length, ~5.16 km2 area).
    h3_resolution_5  -- H3 cell at resolution 5 (~8.54km average edge length, ~252 km2 area).
    suppress         -- replace with empty string ("") when no level satisfies the threshold.

  Output is the H3 cell INDEX STRING (not lat/lng coordinates; that would defeat
  generalization). Requires the optional `geo` extra: ``pip install decoy-engine[geo]``.

  H3 resolution scale (from https://h3geo.org/docs/core-library/restable/):
    resolution 9 ~150m edge, resolution 7 ~1km edge, resolution 5 ~9km edge.

Pattern: HIPAA Safe Harbor ZIP cascade (45 CFR 164.514(b)(2), HHS HIPAA Privacy Rule).
  See: https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/

Pattern: H3 geospatial indexing (Uber, Apache-2.0, h3-py library).
  H3 hierarchical hexagonal grid; each cell at resolution R is fully contained in its
  parent at resolution R-1. Same (lat, lng, resolution) -> same stable cell index.
  See: https://h3geo.org/docs/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.reference_tables import load_table

_LOG = logging.getLogger(__name__)

# Supported geo types.
_SUPPORTED_TYPES = frozenset({"zip", "lat_lng"})

# Valid cascade levels for the lat_lng type. The int value is the H3 resolution.
_H3_LEVEL_TO_RESOLUTION: dict[str, int] = {
    "h3_resolution_9": 9,
    "h3_resolution_7": 7,
    "h3_resolution_5": 5,
}

# All valid cascade level names across all types.
_ZIP_LEVELS = frozenset({"zip5", "zip3", "state", "suppress"})
_H3_LEVELS = frozenset(_H3_LEVEL_TO_RESOLUTION) | {"suppress"}

# Default HIPAA Safe Harbor population threshold: geographic units < 20,000 persons
# must be generalised to a coarser level (45 CFR 164.514(b)(2)(i)(B)).
HIPAA_K_THRESHOLD: int = 20_000

# Cascade level label for suppressed rows (no cascade level satisfied the threshold).
_SUPPRESS_LABEL = "suppressed"
# The value written into the DataFrame when a row is suppressed.
_SUPPRESS_VALUE = ""

# Cache for the restricted ZIP3 set so repeated calls do not reload from disk.
_RESTRICTED_ZIP3: frozenset[str] | None = None


def _load_restricted_zip3() -> frozenset[str]:
    """Load the HHS restricted ZIP3 prefix set from the shipped reference table.

    Cached after the first call (module-level singleton). Thread-safety: in the
    worst case the table is loaded twice by concurrent callers; both produce
    identical frozensets so the race is harmless.

    Source: 45 CFR 164.514(b)(2)(i)(B); us_zip3_population.parquet (SP-08).
    """
    global _RESTRICTED_ZIP3
    if _RESTRICTED_ZIP3 is not None:
        return _RESTRICTED_ZIP3
    tbl = load_table("us_zip3_population")
    prefixes = [str(v) for v in tbl._table.column("zip3").to_pylist()]
    _RESTRICTED_ZIP3 = frozenset(prefixes)
    return _RESTRICTED_ZIP3


@dataclass(frozen=True)
class GeoGeneralizeConfig:
    """Configuration for a geo_generalize cascade operation.

    Attributes:
        type: Generalization type. ``"zip"`` (HIPAA Safe Harbor cascade, SP-08)
            or ``"lat_lng"`` (H3 geospatial generalization, SP-08b).
        cascade: Ordered cascade levels. Must end with ``"suppress"``.
            Supported levels for ``type="zip"``:
                ``zip5``          -- retain full 5-digit ZIP.
                ``zip3``          -- generalise to 3-digit prefix (skipped if restricted).
                ``state``         -- generalise to 2-letter state abbreviation.
                ``suppress``      -- emit empty string; terminates the cascade.
            Supported levels for ``type="lat_lng"``:
                ``h3_resolution_9``  -- H3 cell at resolution 9 (~150m).
                ``h3_resolution_7``  -- H3 cell at resolution 7 (~1km).
                ``h3_resolution_5``  -- H3 cell at resolution 5 (~9km).
                ``suppress``         -- emit empty string; terminates the cascade.
        k_threshold: Minimum record count required to retain a generalization level.
            Default: 20000 (HIPAA Safe Harbor per 45 CFR 164.514(b)(2)).
            For lat_lng H3 use, a lower threshold (e.g. 5) is typical since H3
            resolution-9 cells cover only ~0.1 km2.
    """

    type: str
    cascade: tuple[str, ...]
    k_threshold: int = HIPAA_K_THRESHOLD

    def __init__(
        self,
        type: str,
        cascade: list[str] | tuple[str, ...],
        k_threshold: int = HIPAA_K_THRESHOLD,
    ) -> None:
        # frozen=True requires object.__setattr__ for __init__ overrides.
        object.__setattr__(self, "type", type)
        object.__setattr__(self, "cascade", tuple(cascade))
        object.__setattr__(self, "k_threshold", k_threshold)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> GeoGeneralizeConfig:
        """Parse a config dict; raise PlanCompileError on invalid input.

        Called at execution time, pre-mutation, before any row is processed.
        Validation is fail-closed: an invalid config raises before touching data.

        Args:
            cfg: Config dict with keys ``type``, ``cascade``, and optionally
                ``k_threshold``.

        Raises:
            PlanCompileError: Invalid ``type``, empty ``cascade``, missing ``suppress``
                as cascade terminator, or invalid cascade level for the given type.
        """
        validate_geo_generalize_config(cfg)
        return cls(
            type=cfg["type"],
            cascade=list(cfg["cascade"]),
            k_threshold=int(cfg.get("k_threshold", HIPAA_K_THRESHOLD)),
        )


@dataclass(frozen=True)
class CascadeEvidence:
    """Frozen per-run evidence of cascade decisions (one entry per input row).

    Attributes:
        decisions: Tuple of cascade-level labels, one per input row.
            Labels are string level names: ``"zip5"``, ``"zip3"``, ``"state"``,
            or ``"suppressed"``. Immutable after construction.

    Evidence is frozen: this dataclass uses ``frozen=True`` so Python raises
    ``FrozenInstanceError`` (a ``TypeError``) on any attempted mutation.
    Per engineering best-practices §2.1: evidence must not be mutated after
    the masking pass completes.
    """

    decisions: tuple[str, ...]


def validate_geo_generalize_config(cfg: dict[str, Any]) -> None:
    """Validate a geo_generalize config dict; raise PlanCompileError on failure.

    Called at execution time, pre-mutation, before any row is processed.
    Validation is fail-closed: an invalid config raises before touching data.

    Checks:
      - ``type`` is one of the supported types (``"zip"`` or ``"lat_lng"``).
      - ``cascade`` is a non-empty list.
      - ``cascade`` ends with ``"suppress"`` (must have a terminator).
      - Each cascade level is valid for the given ``type``.

    Args:
        cfg: Raw config dict.

    Raises:
        PlanCompileError: Any validation failure.
    """
    geo_type = cfg.get("type")
    if geo_type not in _SUPPORTED_TYPES:
        raise PlanCompileError(
            code="geo_generalize_unsupported_type",
            path="provider_config.type",
            message=(
                f"geo_generalize: unsupported type {geo_type!r}. "
                f"Supported types: {sorted(_SUPPORTED_TYPES)}."
            ),
        )

    cascade = cfg.get("cascade")
    if not cascade:
        raise PlanCompileError(
            code="geo_generalize_invalid_cascade",
            path="provider_config.cascade",
            message=(
                "geo_generalize: 'cascade' must be a non-empty list of levels. "
                "Example (zip): cascade: [zip5, zip3, state, suppress]. "
                "Example (lat_lng): cascade: [h3_resolution_9, h3_resolution_7, suppress]."
            ),
        )

    if "suppress" not in cascade:
        raise PlanCompileError(
            code="geo_generalize_missing_suppress",
            path="provider_config.cascade",
            message=(
                "geo_generalize: 'cascade' must include 'suppress' as a terminator. "
                "Without it there is no defined behavior when all levels are below threshold."
            ),
        )

    # Validate per-level names for the given type.
    valid_levels = _ZIP_LEVELS if geo_type == "zip" else _H3_LEVELS
    for level in cascade:
        if level not in valid_levels:
            raise PlanCompileError(
                code="geo_generalize_invalid_cascade_level",
                path="provider_config.cascade",
                message=(
                    f"geo_generalize: cascade level {level!r} is not valid for "
                    f"type={geo_type!r}. Valid levels: {sorted(valid_levels)}."
                ),
            )


# ── ZIP5 -> ZIP3 -> state helper ──────────────────────────────────────────────


def _extract_zip5(value: str) -> str:
    """Return the first 5 digits of a ZIP value (strips hyphen extensions)."""
    digits = "".join(c for c in str(value) if c.isdigit())
    return digits[:5] if len(digits) >= 5 else digits


def _zip5_to_zip3(zip5: str) -> str:
    """Return the 3-digit prefix of a 5-digit ZIP."""
    return zip5[:3] if len(zip5) >= 3 else zip5


def _build_state_map() -> dict[str, str]:
    """Build a zip5 -> state-abbreviation map from a ZIP reference table column.

    Looks up via the shipped us_zip5_city_state table. Falls back to an empty
    string if the ZIP5 is not in the reference table (unknown/synthetic ZIP).
    """
    try:
        tbl = load_table("us_zip5_city_state")
    except Exception:
        _LOG.warning("geo_generalize: could not load us_zip5_city_state; state fallback disabled.")
        return {}

    state_map: dict[str, str] = {}
    for i in range(tbl.row_count):
        row = tbl.row(i)
        state_map[str(row.get("zip", ""))] = str(row.get("state", ""))
    return state_map


# ── Core cascade function ─────────────────────────────────────────────────────


def cascade_zip_column(
    df: pd.DataFrame,
    column: str,
    config: GeoGeneralizeConfig,
) -> tuple[pd.DataFrame, CascadeEvidence]:
    """Generalise a ZIP column per the HIPAA Safe Harbor k-threshold cascade.

    For each row, attempts cascade levels in order until one satisfies the
    k-threshold (count of records sharing that generalization >= k_threshold)
    or ``suppress`` is reached.

    In-dataset counts are computed once before the cascade loop so the
    threshold check is applied consistently across all rows.

    The ZIP3 level is skipped for prefixes in the HHS restricted-prefix list
    (geographic units with < 20,000 persons per 45 CFR 164.514(b)(2)(i)(B))
    even when the in-dataset count would satisfy the threshold; the restricted
    set is the Safe Harbor upper bound.

    Args:
        df: Source DataFrame. Must contain ``column``.
        column: Column name holding the ZIP values.
        config: Validated :class:`GeoGeneralizeConfig`.

    Returns:
        ``(result_df, evidence)`` -- a copy of ``df`` with ``column``
        replaced by generalized values, and a :class:`CascadeEvidence`
        instance with one decision label per row.

    Note:
        Does NOT mutate ``df``. Returns a copy with only ``column`` modified.
    """
    restricted = _load_restricted_zip3()

    # Compute in-dataset value counts once (before any generalization).
    raw_col = df[column].astype(str)

    # Precompute zip5 and zip3 for each row.
    zip5_values = [_extract_zip5(v) for v in raw_col]
    zip3_values = [_zip5_to_zip3(z5) for z5 in zip5_values]

    # Build in-dataset count maps.
    zip5_counts: dict[str, int] = {}
    for z5 in zip5_values:
        zip5_counts[z5] = zip5_counts.get(z5, 0) + 1

    zip3_counts: dict[str, int] = {}
    for z3 in zip3_values:
        zip3_counts[z3] = zip3_counts.get(z3, 0) + 1

    # State-level map (zip5 -> state abbreviation).
    state_map = _build_state_map()
    state_values = [state_map.get(z5, "") for z5 in zip5_values]
    state_counts: dict[str, int] = {}
    for s in state_values:
        if s:
            state_counts[s] = state_counts.get(s, 0) + 1

    k = config.k_threshold
    result_values: list[str] = []
    decisions: list[str] = []

    for _i, (z5, z3, state) in enumerate(zip(zip5_values, zip3_values, state_values, strict=True)):
        level_out, decision = _cascade_one_row(
            z5=z5,
            z3=z3,
            state=state,
            cascade=config.cascade,
            k=k,
            zip5_counts=zip5_counts,
            zip3_counts=zip3_counts,
            state_counts=state_counts,
            restricted=restricted,
        )
        result_values.append(level_out)
        decisions.append(decision)

    result_df = df.copy()
    result_df[column] = result_values

    evidence = CascadeEvidence(decisions=tuple(decisions))
    return result_df, evidence


def _cascade_one_row(
    z5: str,
    z3: str,
    state: str,
    cascade: tuple[str, ...],
    k: int,
    zip5_counts: dict[str, int],
    zip3_counts: dict[str, int],
    state_counts: dict[str, int],
    restricted: frozenset[str],
) -> tuple[str, str]:
    """Cascade one row through the configured levels; return (output_value, label)."""
    for level in cascade:
        if level == "zip5":
            count = zip5_counts.get(z5, 0)
            if count >= k:
                return z5, "zip5"

        elif level == "zip3":
            # Skip this level for HHS-restricted prefixes regardless of count.
            if z3 in restricted:
                _LOG.debug(
                    "geo_generalize: zip3 prefix %r is HHS-restricted "
                    "(45 CFR 164.514(b)(2)(i)(B)); skipping zip3 level.",
                    z3,
                )
                continue
            # For non-restricted prefixes, population is >= 20000 per the HHS
            # Safe Harbor definition (the restricted list captures ALL prefixes
            # with population < 20000). We use the conservative HHS lower bound
            # of 20000 as the effective population count for the threshold check.
            # This means: if k_threshold <= 20000, any non-restricted prefix
            # passes; if k_threshold > 20000, fall back to the in-dataset count.
            effective_count = max(20000, zip3_counts.get(z3, 0))
            if effective_count >= k:
                return z3, "zip3"

        elif level == "state":
            if state:
                count = state_counts.get(state, 0)
                if count >= k:
                    return state, "state"

        elif level == "suppress":
            return _SUPPRESS_VALUE, _SUPPRESS_LABEL

    # Fallback: suppress if cascade list has no 'suppress' (should not happen
    # after validate_geo_generalize_config, but be fail-safe).
    return _SUPPRESS_VALUE, _SUPPRESS_LABEL


# ── H3 lat/lng generalization (SP-08b) ────────────────────────────────────────


def _require_h3() -> Any:
    """Import h3 or raise a clear ImportError naming the geo extra.

    Fail-closed guard: called at function entry by any code that needs h3.
    The error message names the optional extra so the operator knows how to
    fix it without reading source code.

    Raises:
        ImportError: h3 is not installed; names the [geo] extra.
    """
    try:
        import h3 as _h3

        return _h3
    except (ImportError, TypeError):
        raise ImportError(
            "geo_generalize with type='lat_lng' requires the h3 library. "
            "Install it with: pip install 'decoy-engine[geo]'  "
            "or: uv add 'decoy-engine[geo]'"
        )


def _parse_latlng(value: str) -> tuple[float, float] | None:
    """Parse a 'lat,lng' string into (lat, lng) floats; return None on failure."""
    try:
        parts = value.strip().split(",", 1)
        if len(parts) != 2:
            return None
        return float(parts[0].strip()), float(parts[1].strip())
    except (ValueError, AttributeError):
        return None


def cascade_latlng_column(
    df: pd.DataFrame,
    column: str,
    config: GeoGeneralizeConfig,
) -> tuple[pd.DataFrame, CascadeEvidence]:
    """Generalise a lat/lng column to H3 cell indexes via k-threshold cascade.

    Each row is parsed as ``"lat,lng"`` and encoded as an H3 cell at the highest
    configured resolution. If the in-dataset count of records sharing that cell
    is below ``k_threshold``, the cell is generalised to the next coarser resolution
    in the cascade until a resolution satisfies the threshold or ``suppress`` is
    reached.

    Output is the H3 cell index string (e.g. ``"8928308280fffff"``). This is
    deliberately NOT a lat/lng coordinate pair -- the cell index is the
    generalised value; converting back to lat/lng would restore partial precision
    and defeat the generalization.

    H3 resolution scale (from https://h3geo.org/docs/core-library/restable/):
      resolution 9 ~150m average edge, ~0.105 km2 area.
      resolution 7 ~1.22km average edge, ~5.16 km2 area.
      resolution 5 ~8.54km average edge, ~252 km2 area.

    Requires: h3 (``pip install decoy-engine[geo]``).

    Args:
        df: Source DataFrame. Must contain ``column`` with "lat,lng" string values.
        column: Column name holding the "lat,lng" coordinates.
        config: Validated :class:`GeoGeneralizeConfig` with ``type="lat_lng"``.

    Returns:
        ``(result_df, evidence)`` -- a copy of ``df`` with ``column``
        replaced by H3 cell index strings (or ``""`` for suppressed rows),
        and a :class:`CascadeEvidence` instance with one decision label per row.

    Raises:
        ImportError: h3 is not installed; names the [geo] extra (fail-closed).
    """
    h3 = _require_h3()

    raw_col = df[column].astype(str)
    # Parse (lat, lng) pairs once; None for unparseable values.
    parsed_coords: list[tuple[float, float] | None] = [_parse_latlng(v) for v in raw_col]

    # For each cascade H3 resolution, compute the cell index per row and
    # count how many rows share that cell (in-dataset aggregator).
    resolutions: list[int] = []
    for level in config.cascade:
        if level in _H3_LEVEL_TO_RESOLUTION:
            resolutions.append(_H3_LEVEL_TO_RESOLUTION[level])

    # cell_at_res[resolution][row_index] = H3 cell string (or "" for parse failure)
    cell_at_res: dict[int, list[str]] = {}
    for res in resolutions:
        cells: list[str] = []
        for coords in parsed_coords:
            if coords is None:
                cells.append("")
            else:
                cells.append(h3.latlng_to_cell(coords[0], coords[1], res))
        cell_at_res[res] = cells

    # Count in-dataset occurrences of each cell at each resolution.
    cell_counts: dict[int, dict[str, int]] = {}
    for res in resolutions:
        counts: dict[str, int] = {}
        for cell in cell_at_res[res]:
            if cell:
                counts[cell] = counts.get(cell, 0) + 1
        cell_counts[res] = counts

    k = config.k_threshold
    result_values: list[str] = []
    decisions: list[str] = []

    for row_idx, coords in enumerate(parsed_coords):
        if coords is None:
            # Unparseable coordinate: suppress immediately (cannot generalize).
            result_values.append(_SUPPRESS_VALUE)
            decisions.append(_SUPPRESS_LABEL)
            continue

        out_val, decision = _cascade_one_latlng_row(
            row_idx=row_idx,
            cascade=config.cascade,
            k=k,
            cell_at_res=cell_at_res,
            cell_counts=cell_counts,
        )
        result_values.append(out_val)
        decisions.append(decision)

    result_df = df.copy()
    result_df[column] = result_values
    return result_df, CascadeEvidence(decisions=tuple(decisions))


def _cascade_one_latlng_row(
    row_idx: int,
    cascade: tuple[str, ...],
    k: int,
    cell_at_res: dict[int, list[str]],
    cell_counts: dict[int, dict[str, int]],
) -> tuple[str, str]:
    """Cascade one lat/lng row through H3 resolutions; return (output_cell, label)."""
    for level in cascade:
        if level == "suppress":
            return _SUPPRESS_VALUE, _SUPPRESS_LABEL
        res = _H3_LEVEL_TO_RESOLUTION.get(level)
        if res is None:
            continue  # unknown level; skip (validation should have caught it)
        cell = cell_at_res[res][row_idx]
        if not cell:
            continue  # parse failure at this resolution; skip
        count = cell_counts[res].get(cell, 0)
        if count >= k:
            return cell, level
    return _SUPPRESS_VALUE, _SUPPRESS_LABEL
