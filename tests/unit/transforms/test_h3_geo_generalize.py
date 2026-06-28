"""SP-08b geo_generalize-2 tests: lat/lng -> H3 cell generalization (TDD: tests first).

Tests cover:
  H3.1 - Known coordinates produce the correct H3 cell at each resolution.
  H3.2 - Cascade: sparse-area coordinates cascade to a larger (lower-res) cell.
  H3.3 - H3 cell index round-trip stability (decode + re-encode is stable).
  H3.4 - H3 missing -> fail closed with a clear error naming the geo extra.
  H3.5 - Config validation: type=lat_lng + h3 cascade levels accepted.
  H3.6 - Invalid cascade levels raise PlanCompileError.
  H3.7 - Integration through the real plan/run path (STRATEGY-WIRING GUARD).
  H3.8 - non_top_label derived from configured cascade, not hardcoded h3_resolution_9.

Methodology: H3 geospatial indexing system (Uber, Apache-2.0).
  H3 hierarchical grid: resolution 9 ~150m, 7 ~1km, 5 ~9km.
  See: https://h3geo.org/docs/core-library/restable/
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.geo_generalize import (
    GeoGeneralizeConfig,
    validate_geo_generalize_config,
)

# Seattle Space Needle coordinates.
_SEA_LAT = 47.6205
_SEA_LNG = -122.3493


# ── H3.1: Per-resolution generalization ──────────────────────────────────────


class TestH3PerResolution:
    """H3 cells at each resolution level for known coordinates."""

    def test_resolution_9_cell_from_known_coords(self):
        """Resolution 9 cell for Seattle Space Needle must be a valid H3 string."""
        h3 = pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        df = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        config = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "h3_resolution_7", "h3_resolution_5", "suppress"],
            k_threshold=1,
        )
        result_df, evidence = cascade_latlng_column(df, "coords", config)
        cell = result_df["coords"].iloc[0]
        # Verify it is a valid H3 cell index.
        assert h3.is_valid_cell(cell), f"Expected valid H3 cell, got {cell!r}"
        assert evidence.decisions[0] == "h3_resolution_9"

    def test_resolution_7_cell_is_coarser_than_resolution_9(self):
        """Resolution 7 cell is the parent of the resolution 9 cell."""
        h3 = pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        df_hi = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        df_lo = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        cfg_hi = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "suppress"],
            k_threshold=1,
        )
        cfg_lo = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_7", "suppress"],
            k_threshold=1,
        )
        res9_df, _ = cascade_latlng_column(df_hi, "coords", cfg_hi)
        res7_df, _ = cascade_latlng_column(df_lo, "coords", cfg_lo)
        cell9 = res9_df["coords"].iloc[0]
        cell7 = res7_df["coords"].iloc[0]
        # The resolution-7 parent of the resolution-9 cell must equal the direct res7 result.
        parent = h3.cell_to_parent(cell9, 7)
        assert cell7 == parent, (
            f"res7 cell {cell7!r} does not match parent of res9 cell {cell9!r} -> {parent!r}"
        )

    def test_resolution_5_cell_is_coarser_than_resolution_7(self):
        """Resolution 5 cell is the ancestor of the resolution 7 cell."""
        h3 = pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        df = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        cfg7 = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_7", "suppress"],
            k_threshold=1,
        )
        cfg5 = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_5", "suppress"],
            k_threshold=1,
        )
        res7_df, _ = cascade_latlng_column(df.copy(), "coords", cfg7)
        res5_df, _ = cascade_latlng_column(df.copy(), "coords", cfg5)
        cell7 = res7_df["coords"].iloc[0]
        cell5 = res5_df["coords"].iloc[0]
        parent = h3.cell_to_parent(cell7, 5)
        assert cell5 == parent


# ── H3.2: Cascade for sparse areas ───────────────────────────────────────────


class TestH3CascadeSparse:
    """When only one record lands in the high-resolution cell, cascade to lower res."""

    def test_single_record_cascades_down(self):
        """k_threshold=2, single record -> cascades below h3_resolution_9."""
        pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        df = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        config = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "h3_resolution_7", "h3_resolution_5", "suppress"],
            k_threshold=2,  # requires at least 2 records in the same H3 cell
        )
        result_df, evidence = cascade_latlng_column(df, "coords", config)
        decision = evidence.decisions[0]
        # Must cascade past resolution_9 (only 1 record).
        assert decision != "h3_resolution_9", (
            "Single record must cascade below h3_resolution_9 when k=2."
        )

    def test_many_records_in_same_cell_stay_at_highest_res(self):
        """Multiple records in the same H3 cell satisfy k >= 2 at resolution 9."""
        pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        # Three nearby coordinates that resolve to the same res-9 cell.
        coords = [
            f"{_SEA_LAT},{_SEA_LNG}",
            f"{_SEA_LAT + 0.00001},{_SEA_LNG + 0.00001}",  # within ~1m
            f"{_SEA_LAT + 0.00002},{_SEA_LNG + 0.00002}",
        ]
        df = pd.DataFrame({"coords": coords})
        config = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "h3_resolution_7", "h3_resolution_5", "suppress"],
            k_threshold=2,
        )
        result_df, evidence = cascade_latlng_column(df, "coords", config)
        # All three must land at or above resolution_9 IF they share the same cell.
        # At least some should stay at h3_resolution_9 (count >= 2).
        assert any(d == "h3_resolution_9" for d in evidence.decisions), (
            "Records in the same H3 cell should satisfy k=2 at h3_resolution_9."
        )

    def test_suppress_when_all_levels_below_threshold(self):
        """With impossibly high k, all levels cascade to suppress."""
        pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        df = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        config = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "h3_resolution_7", "h3_resolution_5", "suppress"],
            k_threshold=999_999,
        )
        result_df, evidence = cascade_latlng_column(df, "coords", config)
        assert result_df["coords"].iloc[0] == "", "Suppress must produce empty string."
        assert evidence.decisions[0] == "suppressed"


# ── H3.3: Round-trip stability ────────────────────────────────────────────────


class TestH3RoundTrip:
    """H3 cell index -> cell center -> re-encode at same resolution must be stable."""

    def test_h3_cell_roundtrip_stable(self):
        """h3.latlng_to_cell(h3.cell_to_latlng(cell), res) == cell for each resolution."""
        h3 = pytest.importorskip("h3")

        for resolution in (9, 7, 5):
            cell = h3.latlng_to_cell(_SEA_LAT, _SEA_LNG, resolution)
            center_lat, center_lng = h3.cell_to_latlng(cell)
            re_encoded = h3.latlng_to_cell(center_lat, center_lng, resolution)
            assert re_encoded == cell, (
                f"H3 round-trip failed at resolution {resolution}: "
                f"{cell!r} -> center ({center_lat}, {center_lng}) -> {re_encoded!r}"
            )


# ── H3.4: Fail-closed when h3 is not installed ────────────────────────────────


class TestH3MissingFailClosed:
    """Without h3 installed, geo_generalize with type=lat_lng must fail closed."""

    def test_import_error_names_geo_extra(self, monkeypatch):
        """Calling cascade_latlng_column without h3 raises ImportError naming the extra."""
        import sys

        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        # Simulate h3 not being importable.
        monkeypatch.setitem(sys.modules, "h3", None)  # type: ignore[arg-type]
        df = pd.DataFrame({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        config = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "suppress"],
            k_threshold=1,
        )
        with pytest.raises(ImportError, match="geo"):
            cascade_latlng_column(df, "coords", config)


# ── H3.5: Config validation ───────────────────────────────────────────────────


class TestH3ConfigValidation:
    def test_lat_lng_type_accepted(self):
        """validate_geo_generalize_config must accept type: lat_lng."""
        validate_geo_generalize_config(
            {
                "type": "lat_lng",
                "cascade": ["h3_resolution_9", "h3_resolution_7", "suppress"],
                "k_threshold": 5,
            }
        )

    def test_lat_lng_requires_suppress_terminator(self):
        """lat_lng cascade without suppress must raise."""
        with pytest.raises(PlanCompileError, match="suppress"):
            validate_geo_generalize_config(
                {
                    "type": "lat_lng",
                    "cascade": ["h3_resolution_9", "h3_resolution_7"],
                }
            )

    def test_invalid_type_still_raises(self):
        """Unknown type still raises PlanCompileError."""
        with pytest.raises(PlanCompileError):
            validate_geo_generalize_config(
                {"type": "country", "cascade": ["h3_resolution_9", "suppress"]}
            )


# ── H3.6: Invalid cascade levels ─────────────────────────────────────────────


class TestH3InvalidCascadeLevel:
    def test_invalid_h3_level_raises(self):
        """h3_resolution_99 is not a valid level; must raise PlanCompileError."""
        with pytest.raises(PlanCompileError, match="h3_resolution_99"):
            validate_geo_generalize_config(
                {
                    "type": "lat_lng",
                    "cascade": ["h3_resolution_99", "suppress"],
                }
            )


# ── H3.7: Integration through the real plan/run path ─────────────────────────


class TestH3Integration:
    """Verifies the STRATEGY-WIRING GUARD: H3 geo_generalize through plan/run."""

    def test_h3_geo_generalize_through_pandas_adapter(self):
        """geo_generalize with type=lat_lng wired end-to-end through PandasExecutionAdapter."""
        pytest.importorskip("h3")

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        col_seed = ColumnSeed(
            namespace=None,
            strategy="geo_generalize",
            provider="geo_generalize",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(
                ("type", "lat_lng"),
                ("cascade", ["h3_resolution_9", "h3_resolution_7", "h3_resolution_5", "suppress"]),
                ("k_threshold", 1),
            ),
            coherent_with=(),
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\xab" * 8,
                per_table=(("t", TableSeed(per_column=(("coords", col_seed),), per_group=())),),
            )
        )
        src = pa.table(
            {
                "coords": [
                    f"{_SEA_LAT},{_SEA_LNG}",
                    "34.0522,-118.2437",  # LA
                    "41.8781,-87.6298",  # Chicago
                ]
            }
        )
        result = PandasExecutionAdapter().run_single(
            plan,
            src,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
        import h3 as _h3

        out = result.output.column("coords").to_pylist()
        assert len(out) == 3
        for cell in out:
            assert _h3.is_valid_cell(cell), f"Expected H3 cell, got {cell!r}"

    def test_h3_does_not_mutate_source_df(self):
        """cascade_latlng_column must not mutate the input DataFrame."""
        pytest.importorskip("h3")
        from decoy_engine.transforms.geo_generalize import cascade_latlng_column

        original_val = f"{_SEA_LAT},{_SEA_LNG}"
        df = pd.DataFrame({"coords": [original_val]})
        config = GeoGeneralizeConfig(
            type="lat_lng",
            cascade=["h3_resolution_9", "suppress"],
            k_threshold=1,
        )
        cascade_latlng_column(df, "coords", config)
        assert df["coords"].iloc[0] == original_val, "Source DataFrame must not be mutated."


# ── H3.8: non_top_label derivation from cascade config ──────────────────────


class TestH3NonTopLabelCascadeWarning:
    """H3.8 - geo_generalize_cascade warning uses the configured top level, not hardcoded res-9.

    When a recipe's cascade starts at h3_resolution_7 (coarser than the default),
    rows retained at h3_resolution_7 must NOT be flagged in the QualityWarning.
    The hardcoded 'h3_resolution_9' label caused spurious warning entries for any
    cascade that did not start at resolution 9.
    """

    def test_non_res9_cascade_no_spurious_warning_for_top_level(self):
        """H3.8a - cascade starting at res-7: rows at res-7 must NOT appear in warning.

        With the bug: non_top_label = 'h3_resolution_9' (hardcoded) -> res-7 decisions
        compare != -> spurious warning. After fix: non_top_label derived from the first
        cascade level -> h3_resolution_7 decisions compare == -> no spurious flag.
        """
        pytest.importorskip("h3")

        import pyarrow as pa

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        col_seed = ColumnSeed(
            namespace=None,
            strategy="geo_generalize",
            provider="geo_generalize",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(
                ("type", "lat_lng"),
                # Cascade starts at res-7, NOT res-9.
                ("cascade", ["h3_resolution_7", "h3_resolution_5", "suppress"]),
                ("k_threshold", 1),  # low threshold: one record is enough to stay at res-7
            ),
            coherent_with=(),
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\xab" * 8,
                per_table=(("t", TableSeed(per_column=(("coords", col_seed),), per_group=())),),
            )
        )
        # Single coord: at k_threshold=1, one record satisfies the threshold -> stays at res-7.
        src = pa.table({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        result = PandasExecutionAdapter().run_single(
            plan,
            src,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
        # At k=1 with one record, the row satisfies threshold at h3_resolution_7.
        # No warning should be emitted (the row is at the top configured level).
        cascade_warnings = [w for w in result.warnings if w.code == "geo_generalize_cascade"]
        assert len(cascade_warnings) == 0, (
            f"No cascade warning expected when all rows stay at the configured top level "
            f"(h3_resolution_7). Got: {cascade_warnings}"
        )

    def test_non_res9_cascade_warning_emitted_when_coarser(self):
        """H3.8b - cascade starting at res-7, high k: cascades to res-5, warning emitted."""
        pytest.importorskip("h3")

        import pyarrow as pa

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        col_seed = ColumnSeed(
            namespace=None,
            strategy="geo_generalize",
            provider="geo_generalize",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(
                ("type", "lat_lng"),
                ("cascade", ["h3_resolution_7", "h3_resolution_5", "suppress"]),
                ("k_threshold", 999_999),  # impossibly high: forces cascade to suppress
            ),
            coherent_with=(),
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\xab" * 8,
                per_table=(("t", TableSeed(per_column=(("coords", col_seed),), per_group=())),),
            )
        )
        src = pa.table({"coords": [f"{_SEA_LAT},{_SEA_LNG}"]})
        result = PandasExecutionAdapter().run_single(
            plan,
            src,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
        # With k=999_999 the row cascades past all levels to suppress.
        # A warning MUST be emitted.
        cascade_warnings = [w for w in result.warnings if w.code == "geo_generalize_cascade"]
        assert len(cascade_warnings) == 1, (
            f"Expected exactly one cascade warning when all rows cascade below top level. "
            f"Got: {cascade_warnings}"
        )
        decisions = cascade_warnings[0].detail["cascade_decisions"]
        # The decision must be 'suppressed' (not h3_resolution_7, which is the top level).
        assert all(v == "suppressed" for v in decisions.values()), (
            f"Expected all decisions to be 'suppressed', got: {decisions}"
        )
