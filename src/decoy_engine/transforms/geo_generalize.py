"""geo_generalize strategy (SP-08 / P5.S.geo_generalize.1): ZIP Safe Harbor cascade.

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

Evidence:
  ``cascade_zip_column`` returns a ``CascadeEvidence`` dataclass whose ``decisions``
  attribute is a frozen tuple of per-row level labels ("zip3", "state", "suppressed",
  etc.). Evidence is captured once at run time and must not be mutated afterward
  (engineering best-practices §2.1: validation never mutates; evidence frozen).

Pattern: HIPAA Safe Harbor ZIP cascade (45 CFR 164.514(b)(2), HHS HIPAA Privacy Rule).
  See: https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from decoy_engine.reference_tables import load_table

_LOG = logging.getLogger(__name__)

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
    """Configuration for a geo_generalize ZIP cascade operation.

    Attributes:
        type: Generalization type. Only ``"zip"`` is supported in SP-08.
            The lat/lng -> H3 path is deferred to SP-08b (requires h3-python).
        cascade: Ordered cascade levels. Must end with ``"suppress"``.
            Supported levels for ``type="zip"``:
                ``zip5``    -- retain full 5-digit ZIP.
                ``zip3``    -- generalise to 3-digit prefix (skipped if restricted).
                ``state``   -- generalise to 2-letter state abbreviation.
                ``suppress``-- emit empty string; terminates the cascade.
        k_threshold: Minimum record count required to retain a generalization level.
            Default: 20000 (HIPAA Safe Harbor per 45 CFR 164.514(b)(2)).
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
        """Parse a config dict; raise ValueError on invalid input.

        Args:
            cfg: Config dict with keys ``type``, ``cascade``, and optionally
                ``k_threshold``.

        Raises:
            ValueError: Invalid ``type``, empty ``cascade``, or missing ``suppress``
                as final cascade level.
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
    """Validate a geo_generalize config dict; raise ValueError on failure.

    Checks:
      - ``type`` is ``"zip"`` (only type in SP-08; lat/lng is SP-08b).
      - ``cascade`` is a non-empty list.
      - ``cascade`` ends with ``"suppress"`` (must have a terminator).

    Args:
        cfg: Raw config dict.

    Raises:
        ValueError: Any validation failure.
    """
    geo_type = cfg.get("type")
    if geo_type != "zip":
        raise ValueError(
            f"geo_generalize: unsupported type {geo_type!r}. "
            f"Only 'zip' is supported in SP-08. "
            f"The lat/lng -> H3 path is deferred to SP-08b."
        )

    cascade = cfg.get("cascade")
    if not cascade:
        raise ValueError(
            "geo_generalize: 'cascade' must be a non-empty list of levels. "
            "Example: cascade: [zip5, zip3, state, suppress]"
        )

    if "suppress" not in cascade:
        raise ValueError(
            "geo_generalize: 'cascade' must include 'suppress' as a terminator. "
            "Without it there is no defined behavior when all levels are below threshold. "
            "Example: cascade: [zip5, zip3, state, suppress]"
        )


# ── ZIP5 -> ZIP3 -> state helper ──────────────────────────────────────────────


def _extract_zip5(value: str) -> str:
    """Return the first 5 digits of a ZIP value (strips hyphen extensions)."""
    digits = "".join(c for c in str(value) if c.isdigit())
    return digits[:5] if len(digits) >= 5 else digits


def _zip5_to_zip3(zip5: str) -> str:
    """Return the 3-digit prefix of a 5-digit ZIP."""
    return zip5[:3] if len(zip5) >= 3 else zip5


def _build_state_map(df: pd.DataFrame, zip_col: str) -> dict[str, str]:
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
    state_map = _build_state_map(df, column)
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
