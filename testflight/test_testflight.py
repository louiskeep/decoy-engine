"""Test-flight main test module.

Phase 0 contains:
  - TestManifestLoader: unit tests for the manifest loader/validator.
    Loads the skeleton a_healthcare_claims manifest, round-trips the data
    model, and proves the fail-closed unknown-key rejection.

Phase 1+: parametrized job execution tests (one test per discovered job).
The parametrization will be added in Phase 1 alongside the real runner body.

All tests are marked `testflight` so the default regression loop (which
uses addopts="-m not benchmark and not testflight") never collects them.
Invoke explicitly via:

  pytest testflight -m testflight
  python scripts/test_flight.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from testflight._spec import (
    FlightManifest,
    InvariantSpec,
    TableSpec,
    TopologyLiteral,
    load_manifest,
)

pytestmark = pytest.mark.testflight

JOBS_DIR = Path(__file__).parent / "jobs"


class TestManifestLoader:
    """Unit tests for the manifest loader/validator (plan section 3 data model)."""

    def test_loads_skeleton_manifest(self) -> None:
        """The a_healthcare_claims skeleton manifest loads without error.

        Verifies that the Phase 0 skeleton manifest.yaml parses against the
        FlightManifest Pydantic model and that key JobSpec fields are
        present with expected values.
        """
        manifest_path = JOBS_DIR / "a_healthcare_claims" / "manifest.yaml"
        assert manifest_path.exists(), (
            f"Skeleton manifest not found: {manifest_path}. "
            "This file is required for Phase 0 and must not be deleted."
        )
        manifest = load_manifest(manifest_path)
        assert isinstance(manifest, FlightManifest)
        assert manifest.job_name == "a_healthcare_claims"
        assert manifest.topology == "one_to_many_multilevel"
        assert manifest.seed == 42
        assert manifest.master_key_label == "testflight"
        assert len(manifest.tables) >= 1

    def test_manifest_tables_have_required_fields(self) -> None:
        """Every TableSpec in the skeleton manifest has name, kind, row_count, source_builder."""
        manifest = load_manifest(JOBS_DIR / "a_healthcare_claims" / "manifest.yaml")
        for tbl in manifest.tables:
            assert isinstance(tbl, TableSpec)
            assert tbl.name
            assert tbl.kind in ("mask", "generate")
            assert tbl.row_count >= 1
            assert tbl.source_builder

    def test_manifest_invariants_are_present(self) -> None:
        """The skeleton manifest invariants block loads as a valid InvariantSpec."""
        manifest = load_manifest(JOBS_DIR / "a_healthcare_claims" / "manifest.yaml")
        assert isinstance(manifest.invariants, InvariantSpec)
        assert manifest.invariants.determinism is True

    def test_rejects_unknown_top_level_key(self, tmp_path: Path) -> None:
        """A manifest with an unknown top-level key raises ValueError (fail-closed).

        The extra="forbid" config on FlightManifest converts Pydantic's
        ValidationError into a ValueError from load_manifest so callers get a
        clear failure with the offending key named in the trace.
        """
        bad = tmp_path / "manifest.yaml"
        bad.write_text(
            "job_name: test\n"
            "topology: one_to_many_multilevel\n"
            "seed: 1\n"
            "master_key_label: test\n"
            "tables:\n"
            "  - name: t\n"
            "    kind: mask\n"
            "    row_count: 100\n"
            "    source_builder: fixture.build_t\n"
            "invariants:\n"
            "  determinism: true\n"
            "unknown_field: should_fail\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Manifest validation failed"):
            load_manifest(bad)

    def test_rejects_unknown_invariant_key(self, tmp_path: Path) -> None:
        """An unknown key inside the invariants block also raises ValueError.

        Verifies that extra="forbid" cascades into the nested InvariantSpec
        model, not just the top-level FlightManifest.
        """
        bad = tmp_path / "manifest.yaml"
        bad.write_text(
            "job_name: test\n"
            "topology: one_to_many_multilevel\n"
            "seed: 1\n"
            "master_key_label: test\n"
            "tables:\n"
            "  - name: t\n"
            "    kind: mask\n"
            "    row_count: 100\n"
            "    source_builder: fixture.build_t\n"
            "invariants:\n"
            "  determinism: true\n"
            "  no_such_invariant: oops\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Manifest validation failed"):
            load_manifest(bad)

    def test_rejects_invalid_topology(self, tmp_path: Path) -> None:
        """An unrecognised topology value raises ValueError."""
        bad = tmp_path / "manifest.yaml"
        bad.write_text(
            "job_name: test\n"
            "topology: not_a_real_topology\n"
            "seed: 1\n"
            "master_key_label: test\n"
            "tables:\n"
            "  - name: t\n"
            "    kind: mask\n"
            "    row_count: 100\n"
            "    source_builder: fixture.build_t\n"
            "invariants:\n"
            "  determinism: true\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Manifest validation failed"):
            load_manifest(bad)

    def test_rejects_invalid_checksum_scheme(self, tmp_path: Path) -> None:
        """An unrecognised checksum scheme raises ValueError."""
        bad = tmp_path / "manifest.yaml"
        bad.write_text(
            "job_name: test\n"
            "topology: one_to_many_multilevel\n"
            "seed: 1\n"
            "master_key_label: test\n"
            "tables:\n"
            "  - name: t\n"
            "    kind: mask\n"
            "    row_count: 100\n"
            "    source_builder: fixture.build_t\n"
            "invariants:\n"
            "  determinism: true\n"
            "  checksums:\n"
            "    - table: t\n"
            "      column: ssn\n"
            "      scheme: not_a_real_scheme\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Manifest validation failed"):
            load_manifest(bad)

    def test_topology_literal_values(self) -> None:
        """Verify that all topology literal strings round-trip via Pydantic."""
        valid_topologies: list[TopologyLiteral] = [
            "one_to_many_multilevel",
            "many_to_many_junction",
            "self_referential",
            "composite_key",
            "mixed",
        ]
        for topo in valid_topologies:
            raw = {
                "job_name": f"test_{topo}",
                "topology": topo,
                "seed": 0,
                "master_key_label": "test",
                "tables": [
                    {
                        "name": "t",
                        "kind": "mask",
                        "row_count": 1,
                        "source_builder": "fixture.build_t",
                    }
                ],
                "invariants": {"determinism": True},
            }
            manifest = FlightManifest.model_validate(raw)
            assert manifest.topology == topo


# ---------------------------------------------------------------------------
# Phase 1+: Parametrized job execution tests
# ---------------------------------------------------------------------------
# The following will be filled in during Phase 1 once the runner is
# implemented. Placeholder to show the intended structure.

# @pytest.mark.parametrize(
#     "manifest_path",
#     sorted((Path(__file__).parent / "jobs").glob("*/manifest.yaml")),
#     ids=lambda p: p.parent.name,
# )
# def test_job_passes_all_invariants(manifest_path: Path) -> None:
#     """Run one test-flight job and assert all invariants pass."""
#     from testflight._runner import run_job
#     result = run_job(manifest_path)
#     assert result.passed, (
#         f"Job {result.job_name} failed. "
#         + "\n".join(
#             f"  {r.family}: {r.detail}"
#             for r in result.invariant_results
#             if not r.passed
#         )
#     )
