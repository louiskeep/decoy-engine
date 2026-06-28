"""SP-08 geo_generalize strategy tests (TDD: tests land first).

Tests cover:
  B.1 - ZIP5 -> ZIP3 happy path: large ZIP3 (above threshold), no cascade.
  B.2 - Cascade to state on a restricted/small ZIP3.
  B.3 - Cascade to suppress when state too thin.
  B.4 - Per-row cascade-level evidence recorded and frozen.
  B.5 - Config validation: bad type / bad cascade list raises.
  B.6 - Default HIPAA k_threshold = 20000.

Methodology: HIPAA Safe Harbor cascade per 45 CFR 164.514(b)(2)(i)(B).
Restricted 3-digit ZIP prefix list is the HHS-published set; loaded from
the shipped us_zip3_population.parquet reference table.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.transforms.geo_generalize import (
    GeoGeneralizeConfig,
    cascade_zip_column,
    validate_geo_generalize_config,
)

# ── B.1: ZIP5 -> ZIP3 happy path ─────────────────────────────────────────────

# 98101 = Seattle, WA - zip3 prefix "981" is NOT in the restricted list.
# The us_zip5_city_state table has rows with zip 98101..98105 (population > 20k each).
_LARGE_ZIP_SERIES = pd.Series(["98101", "98102", "98103"])
_SMALL_ZIP_SERIES = pd.Series(["03601", "03602"])  # prefix 036 - restricted


class TestZip5ToZip3HappyPath:
    def test_large_zip3_stays_at_zip3_level(self):
        """A ZIP5 whose 3-digit prefix is NOT restricted outputs the ZIP3 prefix."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        df = pd.DataFrame({"zipcode": _LARGE_ZIP_SERIES.tolist()})
        result_df, evidence = cascade_zip_column(df, "zipcode", config)

        for val in result_df["zipcode"]:
            # 981xx prefix - should generalize to "981" (zip3)
            assert val == "981", (
                f"Large-population ZIP3 '981' should stay at zip3 level, got {val!r}."
            )

    def test_zip5_input_format_preserved(self):
        """Input ZIPs can be 5 digits with or without hyphen extension; parse cleanly."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        df = pd.DataFrame({"zipcode": ["98101-1234", "98102"]})
        # Should not raise; hyphen-extended ZIPs extract the first 5 digits.
        result_df, _ = cascade_zip_column(df, "zipcode", config)
        assert len(result_df) == 2


# ── B.2: Cascade to state on small/restricted ZIP3 ───────────────────────────


class TestCascadeToState:
    def test_restricted_zip3_cascades_to_state(self):
        """ZIP5 with restricted zip3 prefix must cascade past zip3 to state.

        Prefix '036' is in the HHS restricted list (< 20,000 population).
        The cascade is: zip5 -> zip3 (restricted) -> state.
        """
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        # Use a ZIP5 with restricted prefix '036'. The zip5 itself is not in
        # the shipped reference table so the zip5 in-dataset count will be 1
        # (below threshold). zip3 '036' is restricted. So it cascades to state.
        df = pd.DataFrame({"z": ["03601", "03602"]})
        result_df, evidence = cascade_zip_column(df, "z", config)

        # Both should end up at state level (or suppressed if state count is tiny).
        # With only 2 rows and k=20000 all should go to state or suppress.
        for val in result_df["z"]:
            # Must NOT be the zip5 or zip3 level (both below threshold).
            assert val not in ("03601", "03602"), "Should not retain zip5 for restricted prefix."
            assert val != "036", "Should not retain zip3 for restricted prefix."

    def test_cascade_level_recorded_in_evidence(self):
        """The cascade level for each row must be recorded in the evidence."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        df = pd.DataFrame({"zipcode": ["98101", "03601"]})
        _, evidence = cascade_zip_column(df, "zipcode", config)

        assert len(evidence.decisions) == 2, "One evidence entry per row."
        # Row 0: large prefix -> zip3
        assert evidence.decisions[0] == "zip3"
        # Row 1: restricted prefix -> state or suppress
        assert evidence.decisions[1] in ("state", "suppressed")


# ── B.3: Cascade to suppress ─────────────────────────────────────────────────


class TestCascadeToSuppress:
    def test_suppressed_when_all_levels_below_threshold(self):
        """When no cascade level satisfies the threshold, the row is suppressed."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=9_999_999,  # impossibly high; all levels will cascade
        )
        df = pd.DataFrame({"zipcode": ["98101", "98102"]})
        result_df, evidence = cascade_zip_column(df, "zipcode", config)

        for i, val in enumerate(result_df["zipcode"]):
            assert val == "", (
                f"Row {i}: should be suppressed (empty string) when threshold unreachable, "
                f"got {val!r}."
            )
        for decision in evidence.decisions:
            assert decision == "suppressed", f"Evidence must record 'suppressed', got {decision!r}."

    def test_suppress_is_empty_string(self):
        """Suppressed rows are written as empty string, not None or 'None'."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=9_999_999,
        )
        df = pd.DataFrame({"zipcode": ["10001"]})
        result_df, _ = cascade_zip_column(df, "zipcode", config)
        assert result_df["zipcode"].iloc[0] == "", "Suppress must produce empty string."


# ── B.4: Evidence is a frozen snapshot ───────────────────────────────────────


class TestEvidenceFrozen:
    def test_cascade_evidence_is_frozen(self):
        """CascadeEvidence.decisions must be an immutable sequence."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        df = pd.DataFrame({"zipcode": ["98101", "98102", "98103"]})
        _, evidence = cascade_zip_column(df, "zipcode", config)

        # CascadeEvidence uses a tuple so mutations are impossible.
        assert isinstance(evidence.decisions, tuple), (
            "CascadeEvidence.decisions must be a tuple (frozen, immutable)."
        )
        with pytest.raises((TypeError, AttributeError)):
            evidence.decisions[0] = "zip5"  # type: ignore[index]

    def test_evidence_per_row_count(self):
        """Evidence must have exactly one entry per input row."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        n = 7
        df = pd.DataFrame({"zipcode": ["98101"] * n})
        _, evidence = cascade_zip_column(df, "zipcode", config)
        assert len(evidence.decisions) == n

    def test_evidence_labels_are_strings(self):
        """Each evidence entry must be a string label, not an enum or None."""
        config = GeoGeneralizeConfig(
            type="zip",
            cascade=["zip5", "zip3", "state", "suppress"],
            k_threshold=20000,
        )
        df = pd.DataFrame({"zipcode": ["98101", "03601"]})
        _, evidence = cascade_zip_column(df, "zipcode", config)
        for d in evidence.decisions:
            assert isinstance(d, str) and d, "Each decision must be a non-empty string."


# ── B.5: Config validation ────────────────────────────────────────────────────


class TestGeoGeneralizeConfigValidation:
    def test_invalid_type_raises(self):
        """Only 'zip' is supported as type in this sprint."""
        with pytest.raises(ValueError, match="type"):
            validate_geo_generalize_config(
                {"type": "latlng", "cascade": ["zip3"], "k_threshold": 20000}
            )

    def test_empty_cascade_raises(self):
        """An empty cascade list is invalid."""
        with pytest.raises(ValueError, match="cascade"):
            validate_geo_generalize_config({"type": "zip", "cascade": [], "k_threshold": 20000})

    def test_missing_suppress_in_cascade_raises(self):
        """Cascade without 'suppress' as final level is invalid (no terminator)."""
        with pytest.raises(ValueError, match="suppress"):
            validate_geo_generalize_config(
                {"type": "zip", "cascade": ["zip5", "zip3"], "k_threshold": 20000}
            )

    def test_valid_config_does_not_raise(self):
        """Standard HIPAA config must pass validation."""
        validate_geo_generalize_config(
            {"type": "zip", "cascade": ["zip5", "zip3", "state", "suppress"], "k_threshold": 20000}
        )


# ── B.6: Default k_threshold ─────────────────────────────────────────────────


class TestDefaultThreshold:
    def test_default_k_threshold_is_20000(self):
        """HIPAA Safe Harbor k_threshold default must be 20000."""
        cfg = GeoGeneralizeConfig(type="zip", cascade=["zip5", "zip3", "state", "suppress"])
        assert cfg.k_threshold == 20000, (
            f"Default k_threshold must be 20000 per HIPAA Safe Harbor, got {cfg.k_threshold}."
        )

    def test_geo_generalize_config_from_dict_defaults(self):
        """from_dict without k_threshold falls back to 20000."""
        cfg = GeoGeneralizeConfig.from_dict(
            {"type": "zip", "cascade": ["zip5", "zip3", "state", "suppress"]}
        )
        assert cfg.k_threshold == 20000
